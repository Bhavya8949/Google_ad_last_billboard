#!/usr/bin/env python3
"""
Temporarily enable the PAUSED Responsive Display Ad created by
google_ads_create_ad.py, keep it eligible to serve for about 30 minutes,
then pause it again.

IMPORTANT:
- Enabling an ad can incur real Google Ads spend.
- This script does not guarantee impressions. Google still controls policy
  review, auction eligibility, targeting, billing, and serving.
- Keep this process running until it prints that the campaign is paused.
- Ctrl+C is handled: the script immediately tries to pause the campaign,
  ad group, and ad.
- A hard power-off / process kill cannot be recovered by Python itself.

Commands:
  python publish_display_ad_30min.py --status
  python publish_display_ad_30min.py --confirm-live
  python publish_display_ad_30min.py --confirm-live --minutes 30
  python publish_display_ad_30min.py --pause-now
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any

from dotenv import load_dotenv
from google.ads.googleads.errors import GoogleAdsException

# Reuse the exact working authentication/account logic from the creator.
from google_ads_create_ad import (
    ENV_FILE,
    load_data,
    resolve_google_ads_client,
    save_data,
)


APPROVAL_STATUS_NAMES = {
    0: "UNSPECIFIED",
    1: "UNKNOWN",
    2: "DISAPPROVED",
    3: "APPROVED_LIMITED",
    4: "APPROVED",
    5: "AREA_OF_INTEREST_ONLY",
}

REVIEW_STATUS_NAMES = {
    0: "UNSPECIFIED",
    1: "UNKNOWN",
    2: "REVIEW_IN_PROGRESS",
    3: "REVIEWED",
    4: "UNDER_APPEAL",
    5: "ELIGIBLE_MAY_SERVE",
}

ENTITY_STATUS_NAMES = {
    0: "UNSPECIFIED",
    1: "UNKNOWN",
    2: "ENABLED",
    3: "PAUSED",
    4: "REMOVED",
}

AD_TYPE_NAMES = {
    19: "RESPONSIVE_DISPLAY_AD",
}

# Google Ads v25 policy approval codes that can serve, possibly with limits.
ALLOWED_APPROVAL_CODES = {3, 4, 5}


def enum_code(value: Any) -> int:
    """Convert proto-plus enum/int response values to a stable integer."""
    return int(value)


def enum_label(mapping: dict[int, str], value: Any) -> str:
    code = enum_code(value)
    return mapping.get(code, f"UNKNOWN_CODE_{code}")


def required_resources(data: dict[str, Any]) -> tuple[str, str, str]:
    created = data.get("created_resources", {})

    campaign = str(created.get("display_campaign", "")).strip()
    ad_group = str(created.get("display_ad_group", "")).strip()
    ad = str(created.get("responsive_display_ad", "")).strip()

    missing: list[str] = []
    if not campaign:
        missing.append("created_resources.display_campaign")
    if not ad_group:
        missing.append("created_resources.display_ad_group")
    if not ad:
        missing.append("created_resources.responsive_display_ad")

    if missing:
        raise RuntimeError(
            "The photo Display ad has not been fully created yet. Missing: "
            + ", ".join(missing)
            + ". Run google_ads_create_ad.py successfully first."
        )

    return campaign, ad_group, ad


def assert_resource_customer(resource_name: str, customer_id: str) -> None:
    expected = f"customers/{customer_id}/"
    if not resource_name.startswith(expected):
        raise RuntimeError(
            f"Stored resource belongs to another customer: {resource_name}"
        )


def get_ad_state(
    client,
    customer_id: str,
    ad_resource_name: str,
) -> dict[str, Any]:
    service = client.get_service("GoogleAdsService")

    query = f"""
        SELECT
          campaign.resource_name,
          campaign.status,
          ad_group.resource_name,
          ad_group.status,
          ad_group_ad.resource_name,
          ad_group_ad.status,
          ad_group_ad.ad.type,
          ad_group_ad.policy_summary.approval_status,
          ad_group_ad.policy_summary.review_status,
          ad_group_ad.primary_status,
          ad_group_ad.primary_status_reasons
        FROM ad_group_ad
        WHERE ad_group_ad.resource_name = '{ad_resource_name}'
        LIMIT 1
    """

    rows = list(service.search(customer_id=customer_id, query=query))
    if not rows:
        raise RuntimeError(
            f"Could not find stored ad in Google Ads: {ad_resource_name}"
        )

    row = rows[0]
    reasons = [
        str(reason)
        for reason in row.ad_group_ad.primary_status_reasons
    ]

    return {
        "campaign_resource": row.campaign.resource_name,
        "campaign_status": enum_label(
            ENTITY_STATUS_NAMES, row.campaign.status
        ),
        "ad_group_resource": row.ad_group.resource_name,
        "ad_group_status": enum_label(
            ENTITY_STATUS_NAMES, row.ad_group.status
        ),
        "ad_resource": row.ad_group_ad.resource_name,
        "ad_status": enum_label(
            ENTITY_STATUS_NAMES, row.ad_group_ad.status
        ),
        "ad_type": enum_label(
            AD_TYPE_NAMES, row.ad_group_ad.ad.type_
        ),
        "approval_code": enum_code(
            row.ad_group_ad.policy_summary.approval_status
        ),
        "approval_status": enum_label(
            APPROVAL_STATUS_NAMES,
            row.ad_group_ad.policy_summary.approval_status,
        ),
        "review_code": enum_code(
            row.ad_group_ad.policy_summary.review_status
        ),
        "review_status": enum_label(
            REVIEW_STATUS_NAMES,
            row.ad_group_ad.policy_summary.review_status,
        ),
        "primary_status": str(row.ad_group_ad.primary_status),
        "primary_status_reasons": reasons,
    }


def print_state(state: dict[str, Any]) -> None:
    print("\nCurrent Google Ads state")
    print("------------------------")
    print(f"Campaign status : {state['campaign_status']}")
    print(f"Ad group status : {state['ad_group_status']}")
    print(f"Ad status       : {state['ad_status']}")
    print(f"Ad type         : {state['ad_type']}")
    print(
        f"Policy approval : {state['approval_status']} "
        f"(code {state['approval_code']})"
    )
    print(
        f"Review status   : {state['review_status']} "
        f"(code {state['review_code']})"
    )
    print(f"Primary status  : {state['primary_status']}")
    if state["primary_status_reasons"]:
        print(
            "Primary reasons : "
            + ", ".join(state["primary_status_reasons"])
        )


def check_preflight(
    state: dict[str, Any],
    expected_campaign: str,
    expected_ad_group: str,
) -> None:
    if state["campaign_resource"] != expected_campaign:
        raise RuntimeError(
            "Stored display_campaign does not match the campaign containing "
            "the stored Responsive Display Ad."
        )

    if state["ad_group_resource"] != expected_ad_group:
        raise RuntimeError(
            "Stored display_ad_group does not match the ad group containing "
            "the stored Responsive Display Ad."
        )

    if state["ad_type"] != "RESPONSIVE_DISPLAY_AD":
        raise RuntimeError(
            "The stored ad is not a Responsive Display Ad. "
            f"Google reports ad type: {state['ad_type']}"
        )

    if state["approval_code"] not in ALLOWED_APPROVAL_CODES:
        raise RuntimeError(
            "The ad is not approved to serve yet. "
            f"approval_status={state['approval_status']} "
            f"(code {state['approval_code']}), "
            f"review_status={state['review_status']} "
            f"(code {state['review_code']}). "
            "Wait for Google Ads review, then run --status again."
        )

    if state["approval_code"] != 4:
        print(
            "\nWARNING: Google reports policy approval "
            f"{state['approval_status']}. Serving may be limited."
        )


def update_campaign_status(
    client,
    customer_id: str,
    resource_name: str,
    enabled: bool,
) -> None:
    service = client.get_service("CampaignService")
    operation = client.get_type("CampaignOperation")
    operation.update.resource_name = resource_name
    operation.update.status = (
        client.enums.CampaignStatusEnum.ENABLED
        if enabled
        else client.enums.CampaignStatusEnum.PAUSED
    )
    operation.update_mask.paths.append("status")
    service.mutate_campaigns(
        customer_id=customer_id,
        operations=[operation],
    )


def update_ad_group_status(
    client,
    customer_id: str,
    resource_name: str,
    enabled: bool,
) -> None:
    service = client.get_service("AdGroupService")
    operation = client.get_type("AdGroupOperation")
    operation.update.resource_name = resource_name
    operation.update.status = (
        client.enums.AdGroupStatusEnum.ENABLED
        if enabled
        else client.enums.AdGroupStatusEnum.PAUSED
    )
    operation.update_mask.paths.append("status")
    service.mutate_ad_groups(
        customer_id=customer_id,
        operations=[operation],
    )


def update_ad_status(
    client,
    customer_id: str,
    resource_name: str,
    enabled: bool,
) -> None:
    service = client.get_service("AdGroupAdService")
    operation = client.get_type("AdGroupAdOperation")
    operation.update.resource_name = resource_name
    operation.update.status = (
        client.enums.AdGroupAdStatusEnum.ENABLED
        if enabled
        else client.enums.AdGroupAdStatusEnum.PAUSED
    )
    operation.update_mask.paths.append("status")
    service.mutate_ad_group_ads(
        customer_id=customer_id,
        operations=[operation],
    )


def pause_everything(
    client,
    customer_id: str,
    campaign: str,
    ad_group: str,
    ad: str,
) -> None:
    """
    Stop serving as quickly as possible by pausing the campaign first.
    Then also pause the ad group and ad so every layer ends PAUSED.
    """
    errors: list[str] = []

    print("\nPausing campaign...")
    try:
        update_campaign_status(
            client, customer_id, campaign, enabled=False
        )
        print("[paused] Campaign")
    except Exception as exc:
        errors.append(f"campaign: {exc}")

    try:
        update_ad_group_status(
            client, customer_id, ad_group, enabled=False
        )
        print("[paused] Ad group")
    except Exception as exc:
        errors.append(f"ad group: {exc}")

    try:
        update_ad_status(
            client, customer_id, ad, enabled=False
        )
        print("[paused] Responsive Display Ad")
    except Exception as exc:
        errors.append(f"ad: {exc}")

    if errors:
        raise RuntimeError(
            "Pause completed with errors: " + " | ".join(errors)
        )


def record_live_state(
    data_path,
    data: dict[str, Any],
    *,
    state: str,
    minutes: int | None = None,
) -> None:
    live = data.setdefault("live_control", {})
    live["state"] = state
    live["updated_utc"] = datetime.now(timezone.utc).isoformat()

    if minutes is not None:
        live["requested_minutes"] = minutes

    if state == "ENABLED":
        live["enabled_utc"] = live["updated_utc"]
    elif state == "PAUSED":
        live["paused_utc"] = live["updated_utc"]

    save_data(data_path, data)


def print_google_ads_exception(exc: GoogleAdsException) -> None:
    print(
        f"\nGoogle Ads request failed. Request ID: {exc.request_id}; "
        f"Status: {exc.error.code().name}",
        file=sys.stderr,
    )
    for index, error in enumerate(exc.failure.errors, start=1):
        print(f"{index}. {error.message}", file=sys.stderr)
        if error.location:
            for element in error.location.field_path_elements:
                print(
                    f"   field: {element.field_name}",
                    file=sys.stderr,
                )


def parse_args() -> argparse.Namespace:
    load_dotenv(ENV_FILE, override=True)

    default_minutes = int(
        (os.getenv("GOOGLE_ADS_LIVE_MINUTES") or "30").strip()
    )

    parser = argparse.ArgumentParser(
        description=(
            "Temporarily enable the created Responsive Display Ad and "
            "pause it again after the requested number of minutes."
        )
    )
    parser.add_argument(
        "--minutes",
        type=int,
        default=default_minutes,
        help=(
            "How long to keep the campaign enabled. "
            f"Default: {default_minutes} minutes."
        ),
    )
    parser.add_argument(
        "--confirm-live",
        action="store_true",
        help=(
            "Required before enabling because live Google Ads can spend money."
        ),
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Show Google Ads serving/review status without changing anything.",
    )
    parser.add_argument(
        "--pause-now",
        action="store_true",
        help="Immediately pause the stored Display campaign, ad group and ad.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if args.minutes < 1 or args.minutes > 120:
        print(
            "ERROR: --minutes must be between 1 and 120.",
            file=sys.stderr,
        )
        return 2

    try:
        data_path, data = load_data()
        customer_id = data["customer_id"]

        campaign, ad_group, ad = required_resources(data)
        assert_resource_customer(campaign, customer_id)
        assert_resource_customer(ad_group, customer_id)
        assert_resource_customer(ad, customer_id)

        client = resolve_google_ads_client(customer_id)

        state = get_ad_state(client, customer_id, ad)
        print_state(state)

        if args.status:
            return 0

        if args.pause_now:
            pause_everything(
                client, customer_id, campaign, ad_group, ad
            )
            record_live_state(
                data_path, data, state="PAUSED"
            )
            print("\nEmergency pause complete.")
            return 0

        check_preflight(
            state,
            expected_campaign=campaign,
            expected_ad_group=ad_group,
        )

        if not args.confirm_live:
            print(
                "\nNO CHANGES MADE.\n"
                "This operation can create real ad spend. "
                "To start the temporary live window, run:\n\n"
                f"  python .\\publish_display_ad_30min.py "
                f"--confirm-live --minutes {args.minutes}\n"
            )
            return 2

        budget_micros = int(
            data.get("budget", {}).get("amount_micros", 0) or 0
        )
        if budget_micros > 0:
            print(
                "\nConfigured campaign daily budget from JSON: "
                f"{budget_micros / 1_000_000:g} "
                "(in the Google Ads account currency)."
            )

        print(
            f"\nStarting temporary live window: {args.minutes} minutes."
        )
        print(
            "The ad may incur real spend. Google does not guarantee "
            "impressions during this window."
        )

        activation_attempted = False
        fully_enabled = False

        try:
            activation_attempted = True

            # Enable lower levels first. The campaign is enabled LAST,
            # which is the point at which the serving window begins.
            print("\nEnabling Responsive Display Ad...")
            update_ad_status(
                client, customer_id, ad, enabled=True
            )
            print("[enabled] Responsive Display Ad")

            print("Enabling ad group...")
            update_ad_group_status(
                client, customer_id, ad_group, enabled=True
            )
            print("[enabled] Ad group")

            print("Enabling campaign...")
            update_campaign_status(
                client, customer_id, campaign, enabled=True
            )
            print("[enabled] Campaign")

            fully_enabled = True
            record_live_state(
                data_path,
                data,
                state="ENABLED",
                minutes=args.minutes,
            )

            started = time.monotonic()
            deadline = started + (args.minutes * 60)

            print(
                f"\nLIVE WINDOW STARTED. "
                f"Planned duration: {args.minutes} minutes."
            )
            print(
                "Keep this PowerShell window and computer running. "
                "Press Ctrl+C at any time to pause immediately."
            )

            next_report = started
            while True:
                now = time.monotonic()
                remaining = deadline - now
                if remaining <= 0:
                    break

                if now >= next_report:
                    print(
                        f"[timer] about "
                        f"{max(0, int((remaining + 59) // 60))} "
                        "minute(s) remaining"
                    )
                    next_report = now + 60

                time.sleep(min(10, remaining))

            print("\n30-minute-style live window completed.")

        except KeyboardInterrupt:
            print(
                "\nCtrl+C received. Pausing immediately..."
            )
        finally:
            if activation_attempted:
                try:
                    pause_everything(
                        client,
                        customer_id,
                        campaign,
                        ad_group,
                        ad,
                    )
                    record_live_state(
                        data_path, data, state="PAUSED"
                    )
                except Exception as pause_exc:
                    print(
                        "\nCRITICAL: Automatic pause had an error:",
                        pause_exc,
                        file=sys.stderr,
                    )
                    print(
                        "Run immediately:\n"
                        "  python .\\publish_display_ad_30min.py --pause-now",
                        file=sys.stderr,
                    )
                    return 1

        final_state = get_ad_state(client, customer_id, ad)
        print_state(final_state)

        if fully_enabled:
            print(
                "\nDONE: The temporary live window ended and the "
                "campaign/ad group/ad were set back to PAUSED."
            )
        return 0

    except GoogleAdsException as exc:
        print_google_ads_exception(exc)
        return 1
    except Exception as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
