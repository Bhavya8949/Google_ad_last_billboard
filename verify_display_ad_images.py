#!/usr/bin/env python3
"""
READ-ONLY verification for the photo Responsive Display Ad.

This file does NOT create, enable, pause, or modify anything.
It reads the Responsive Display Ad recorded in google_ads_ad_data.json
and prints the exact image Asset resources Google Ads says are inside it.

Run:
    python .\verify_display_ad_images.py
"""

from __future__ import annotations

import sys

from google.ads.googleads.errors import GoogleAdsException

from google_ads_create_ad import (
    get_image_asset_details,
    load_data,
    resolve_google_ads_client,
)


def main() -> int:
    try:
        _, data = load_data()
        customer_id = data["customer_id"]

        created = data.get("created_resources", {})
        ad_resource = str(
            created.get("responsive_display_ad", "")
        ).strip()

        if not ad_resource:
            raise RuntimeError(
                "created_resources.responsive_display_ad is missing. "
                "Run google_ads_create_ad.py successfully first."
            )

        client = resolve_google_ads_client(customer_id)
        service = client.get_service("GoogleAdsService")

        query = f"""
            SELECT
              campaign.id,
              campaign.name,
              campaign.status,
              ad_group.id,
              ad_group.name,
              ad_group.status,
              ad_group_ad.resource_name,
              ad_group_ad.status,
              ad_group_ad.ad.id,
              ad_group_ad.ad.type,
              ad_group_ad.ad.final_urls,
              ad_group_ad.policy_summary.approval_status,
              ad_group_ad.policy_summary.review_status,
              ad_group_ad.ad.responsive_display_ad.marketing_images,
              ad_group_ad.ad.responsive_display_ad.square_marketing_images,
              ad_group_ad.ad.responsive_display_ad.headlines,
              ad_group_ad.ad.responsive_display_ad.long_headline,
              ad_group_ad.ad.responsive_display_ad.descriptions,
              ad_group_ad.ad.responsive_display_ad.business_name
            FROM ad_group_ad
            WHERE ad_group_ad.resource_name = '{ad_resource}'
            LIMIT 1
        """

        rows = list(
            service.search(customer_id=customer_id, query=query)
        )
        if not rows:
            raise RuntimeError(
                f"Google Ads cannot find stored ad: {ad_resource}"
            )

        row = rows[0]
        ad = row.ad_group_ad.ad
        rda = ad.responsive_display_ad

        landscape = [
            item.asset for item in rda.marketing_images if item.asset
        ]
        square = [
            item.asset
            for item in rda.square_marketing_images
            if item.asset
        ]

        print("\n=== RESPONSIVE DISPLAY AD ===")
        print(f"Campaign: {row.campaign.name} ({row.campaign.id})")
        print(f"Campaign status: {row.campaign.status}")
        print(f"Ad group: {row.ad_group.name} ({row.ad_group.id})")
        print(f"Ad group status: {row.ad_group.status}")
        print(f"Ad ID: {ad.id}")
        print(f"Ad resource: {row.ad_group_ad.resource_name}")
        print(f"Ad status: {row.ad_group_ad.status}")
        print(f"Ad type: {ad.type_}")
        print(
            "Policy: "
            f"{row.ad_group_ad.policy_summary.approval_status} / "
            f"{row.ad_group_ad.policy_summary.review_status}"
        )
        print(f"Business name: {rda.business_name}")
        print(
            "Headlines: "
            + " | ".join(x.text for x in rda.headlines)
        )
        print(f"Long headline: {rda.long_headline.text}")
        print(
            "Descriptions: "
            + " | ".join(x.text for x in rda.descriptions)
        )
        print(f"Final URLs: {', '.join(ad.final_urls)}")

        print("\n=== IMAGE REFERENCES INSIDE THE AD ===")
        print(f"Landscape count: {len(landscape)}")
        for resource in landscape:
            print(f"  {resource}")
        print(f"Square count: {len(square)}")
        for resource in square:
            print(f"  {resource}")

        if not landscape or not square:
            print(
                "\nRESULT: FAIL - Google Ads does not report both required "
                "image types inside this ad."
            )
            return 2

        all_resources = []
        for resource in landscape + square:
            if resource not in all_resources:
                all_resources.append(resource)

        print("\n=== IMAGE ASSETS READ BACK FROM GOOGLE ===")
        for resource in all_resources:
            info = get_image_asset_details(
                client, customer_id, resource
            )
            print(f"\nName: {info['name']}")
            print(f"Asset ID: {info['id']}")
            print(f"Resource: {info['resource_name']}")
            print(
                f"Dimensions: {info['width']} x {info['height']}"
            )
            print(f"MIME: {info['mime_type']}")
            print(f"File size: {info['file_size']} bytes")
            print(f"Google-hosted URL: {info['url']}")

        print(
            "\nRESULT: PASS - Google Ads confirms the photo assets are "
            "inside the Responsive Display Ad."
        )
        return 0

    except GoogleAdsException as exc:
        print(
            f"\nGoogle Ads request failed. Request ID: {exc.request_id}; "
            f"Status: {exc.error.code().name}",
            file=sys.stderr,
        )
        for error in exc.failure.errors:
            print(f"- {error.message}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
