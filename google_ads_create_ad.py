#!/usr/bin/env python3
"""
Create a PAUSED Google Responsive Display Ad that contains the user's photo
as part of the ad creative.

This intentionally creates a NEW Display campaign instead of trying to attach
AD_IMAGE to the existing Search ad group. A Responsive Display Ad requires the
images inside the ad itself, so the photo is part of the created ad.

Uses the existing project files:
  - .env
  - google_ads_ad_data.json
  - the image configured at image_asset.file_path
  - the service-account JSON configured in .env

Google Ads API version: v25 by default.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

from dotenv import load_dotenv
from PIL import Image, ImageFilter, ImageOps
from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException


BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"
GENERATED_DIR = BASE_DIR / "generated_display_assets"


def clean_customer_id(value: str | None, field_name: str) -> str:
    value = (value or "").replace("-", "").strip()
    if not (value.isdigit() and len(value) == 10):
        raise ValueError(
            f"{field_name} must be a 10-digit Google Ads customer ID without hyphens."
        )
    return value


def unique_name(base_name: str) -> str:
    return f"{base_name} #{uuid4().hex[:8]}"


def enum_value(enum_wrapper: Any, value: str, field_name: str) -> Any:
    normalized = str(value).strip().upper()
    try:
        return getattr(enum_wrapper, normalized)
    except AttributeError as exc:
        raise ValueError(f"Unsupported {field_name} value: {value!r}") from exc


def service_account_key_path() -> Path:
    load_dotenv(ENV_FILE, override=True)

    raw = (
        os.getenv("GOOGLE_ADS_JSON_KEY_FILE_PATH")
        or os.getenv("GOOGLE_ADS_SERVICE_ACCOUNT_JSON")
        or ""
    ).strip()

    if not raw:
        raise RuntimeError(
            "Missing GOOGLE_ADS_JSON_KEY_FILE_PATH in .env."
        )

    path = Path(raw)
    if not path.is_absolute():
        path = BASE_DIR / path

    if not path.exists():
        raise FileNotFoundError(f"Service-account JSON not found: {path}")

    return path


def service_account_metadata() -> dict[str, str]:
    key_path = service_account_key_path()
    data = json.loads(key_path.read_text(encoding="utf-8"))

    if data.get("type") != "service_account":
        raise RuntimeError(
            f"{key_path.name} is not a service-account JSON key."
        )

    project_id = str(data.get("project_id", "")).strip()
    client_email = str(data.get("client_email", "")).strip()

    if not project_id or not client_email:
        raise RuntimeError(
            f"{key_path.name} is missing project_id or client_email."
        )

    expected_project = (
        os.getenv("GOOGLE_ADS_EXPECTED_CLOUD_PROJECT_ID") or ""
    ).strip()

    if expected_project and project_id != expected_project:
        raise RuntimeError(
            "WRONG GOOGLE CLOUD PROJECT KEY. "
            f"Credential project is '{project_id}', but .env expects "
            f"'{expected_project}'."
        )

    return {
        "project_id": project_id,
        "client_email": client_email,
        "key_path": str(key_path),
    }


def load_google_ads_client(*, include_login_customer: bool) -> GoogleAdsClient:
    load_dotenv(ENV_FILE, override=True)

    developer_token = (os.getenv("GOOGLE_ADS_DEVELOPER_TOKEN") or "").strip()
    if not developer_token:
        raise RuntimeError("Missing GOOGLE_ADS_DEVELOPER_TOKEN in .env")

    metadata = service_account_metadata()

    config: dict[str, Any] = {
        "developer_token": developer_token,
        "json_key_file_path": metadata["key_path"],
        "use_proto_plus": True,
    }

    if include_login_customer:
        config["login_customer_id"] = clean_customer_id(
            os.getenv("GOOGLE_ADS_LOGIN_CUSTOMER_ID"),
            "GOOGLE_ADS_LOGIN_CUSTOMER_ID",
        )

    api_version = (
        os.getenv("GOOGLE_ADS_API_VERSION", "v25").strip() or "v25"
    )

    return GoogleAdsClient.load_from_dict(config, version=api_version)


def list_direct_accessible_customer_ids(client: GoogleAdsClient) -> set[str]:
    service = client.get_service("CustomerService")
    response = service.list_accessible_customers()
    return {
        resource_name.rsplit("/", 1)[-1]
        for resource_name in response.resource_names
    }


def manager_customer_ids(
    client: GoogleAdsClient, manager_id: str
) -> set[str]:
    service = client.get_service("GoogleAdsService")
    query = """
        SELECT
          customer_client.id,
          customer_client.level
        FROM customer_client
    """
    ids: set[str] = set()
    for row in service.search(customer_id=manager_id, query=query):
        ids.add(str(row.customer_client.id))
    return ids


def resolve_google_ads_client(target_customer_id: str) -> GoogleAdsClient:
    load_dotenv(ENV_FILE, override=True)

    manager_id = clean_customer_id(
        os.getenv("GOOGLE_ADS_LOGIN_CUSTOMER_ID"),
        "GOOGLE_ADS_LOGIN_CUSTOMER_ID",
    )

    metadata = service_account_metadata()
    print(f"Credential Google Cloud project: {metadata['project_id']}")
    print(f"Service account: {metadata['client_email']}")

    direct_client = load_google_ads_client(include_login_customer=False)
    direct_ids = list_direct_accessible_customer_ids(direct_client)

    print(
        "Service account directly accessible Google Ads customers: "
        + (", ".join(sorted(direct_ids)) if direct_ids else "<none>")
    )
    print(f"Configured manager/login customer: {manager_id}")
    print(f"Configured target customer: {target_customer_id}")

    if manager_id not in direct_ids:
        raise RuntimeError(
            f"Service account does not have access to manager {manager_id}."
        )

    manager_client = load_google_ads_client(include_login_customer=True)
    hierarchy_ids = manager_customer_ids(manager_client, manager_id)

    if target_customer_id not in hierarchy_ids:
        raise RuntimeError(
            f"Target {target_customer_id} is not under manager {manager_id}."
        )

    print(
        "Authorization mode: SERVICE ACCOUNT -> MANAGER "
        f"{manager_id} -> TARGET {target_customer_id}"
    )
    return manager_client


def load_data() -> tuple[Path, dict[str, Any]]:
    load_dotenv(ENV_FILE, override=True)

    configured = (
        os.getenv("GOOGLE_ADS_DATA_FILE", "google_ads_ad_data.json").strip()
        or "google_ads_ad_data.json"
    )

    path = Path(configured)
    if not path.is_absolute():
        path = BASE_DIR / path

    if not path.exists():
        raise FileNotFoundError(f"JSON data file not found: {path}")

    data = json.loads(path.read_text(encoding="utf-8"))
    data["customer_id"] = clean_customer_id(
        str(data.get("customer_id", "")), "customer_id"
    )
    data.setdefault("created_resources", {})
    return path, data


def save_data(path: Path, data: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def source_image_path(data: dict[str, Any]) -> Path:
    image_cfg = data.get("image_asset")
    if not image_cfg:
        raise ValueError(
            "google_ads_ad_data.json is missing the image_asset object."
        )

    raw = str(image_cfg.get("file_path", "")).strip()
    if not raw:
        raise ValueError("image_asset.file_path is empty.")

    path = Path(raw)
    if not path.is_absolute():
        path = BASE_DIR / path

    if not path.exists():
        raise FileNotFoundError(f"Your ad photo was not found: {path}")

    if path.suffix.lower() not in {".jpg", ".jpeg", ".png"}:
        raise ValueError("Your photo must be JPG/JPEG or PNG.")

    return path


def validate_text(data: dict[str, Any]) -> None:
    if "budget" not in data or "ad_group" not in data or "ad" not in data:
        raise ValueError("JSON must contain budget, ad_group, and ad objects.")

    ad = data["ad"]

    headlines = list(ad.get("headlines", []))
    descriptions = list(ad.get("descriptions", []))
    final_urls = list(ad.get("final_urls", []))

    if not headlines:
        raise ValueError("At least 1 headline is required.")
    if not descriptions:
        raise ValueError("At least 1 description is required.")
    if not final_urls:
        raise ValueError("At least 1 final URL is required.")

    for value in headlines[:5]:
        if len(value) > 30:
            raise ValueError(
                f"Responsive Display headline exceeds 30 characters: {value!r}"
            )

    for value in descriptions[:5]:
        if len(value) > 90:
            raise ValueError(
                f"Responsive Display description exceeds 90 characters: {value!r}"
            )

    long_headline = str(
        ad.get("long_headline") or headlines[0]
    ).strip()
    if len(long_headline) > 90:
        raise ValueError("long_headline must be 90 characters or fewer.")

    business_name = str(
        ad.get("business_name") or "Last Billboard"
    ).strip()
    if not business_name:
        raise ValueError("business_name cannot be empty.")
    if len(business_name) > 25:
        raise ValueError("business_name must be 25 characters or fewer.")


def _flatten_to_rgb(image: Image.Image) -> Image.Image:
    if image.mode == "RGB":
        return image

    if image.mode in ("RGBA", "LA") or (
        image.mode == "P" and "transparency" in image.info
    ):
        rgba = image.convert("RGBA")
        background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        background.alpha_composite(rgba)
        return background.convert("RGB")

    return image.convert("RGB")


def make_preserved_photo_variant(
    source_path: Path,
    destination: Path,
    width: int,
    height: int,
) -> Path:
    """
    Create a Google-compliant image while preserving the whole user's photo.
    A blurred copy fills unused space instead of cropping the original photo.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)

    with Image.open(source_path) as original:
        image = _flatten_to_rgb(original)

        background = ImageOps.fit(
            image,
            (width, height),
            method=Image.Resampling.LANCZOS,
            centering=(0.5, 0.5),
        ).filter(ImageFilter.GaussianBlur(radius=28))

        foreground = ImageOps.contain(
            image,
            (width, height),
            method=Image.Resampling.LANCZOS,
        )

        x = (width - foreground.width) // 2
        y = (height - foreground.height) // 2
        background.paste(foreground, (x, y))

        background.save(
            destination,
            format="JPEG",
            quality=90,
            optimize=True,
        )

    if destination.stat().st_size > 5 * 1024 * 1024:
        raise ValueError(
            f"Generated image exceeds 5 MB: {destination}"
        )

    return destination


def prepare_display_images(data: dict[str, Any]) -> tuple[Path, Path]:
    source = source_image_path(data)
    GENERATED_DIR.mkdir(parents=True, exist_ok=True)

    stem = source.stem
    landscape = GENERATED_DIR / f"{stem}_display_1200x628.jpg"
    square = GENERATED_DIR / f"{stem}_display_1200x1200.jpg"

    make_preserved_photo_variant(source, landscape, 1200, 628)
    make_preserved_photo_variant(source, square, 1200, 1200)

    print(f"Original photo: {source}")
    print(f"Display landscape photo: {landscape}")
    print(f"Display square photo: {square}")
    return landscape, square


def create_display_budget(
    client: GoogleAdsClient,
    customer_id: str,
    data: dict[str, Any],
    data_path: Path,
) -> str:
    created = data["created_resources"]
    existing = created.get("display_campaign_budget")
    if existing:
        print(f"[skip] Display campaign budget already stored: {existing}")
        return existing

    service = client.get_service("CampaignBudgetService")
    operation = client.get_type("CampaignBudgetOperation")
    budget = operation.create

    cfg = data["budget"]
    budget.name = unique_name(
        str(cfg.get("name", "Last Billboard API Budget")) + " - Display Photo"
    )
    budget.amount_micros = int(cfg["amount_micros"])
    budget.delivery_method = client.enums.BudgetDeliveryMethodEnum.STANDARD
    budget.explicitly_shared = False

    response = service.mutate_campaign_budgets(
        customer_id=customer_id,
        operations=[operation],
    )
    resource = response.results[0].resource_name
    created["display_campaign_budget"] = resource
    save_data(data_path, data)
    print(f"[created] Display campaign budget: {resource}")
    return resource


def create_display_campaign(
    client: GoogleAdsClient,
    customer_id: str,
    budget_resource: str,
    data: dict[str, Any],
    data_path: Path,
) -> str:
    created = data["created_resources"]
    existing = created.get("display_campaign")
    if existing:
        print(f"[skip] Display campaign already stored: {existing}")
        return existing

    service = client.get_service("CampaignService")
    operation = client.get_type("CampaignOperation")
    campaign = operation.create

    campaign_cfg = data.get("campaign", {})
    base_name = str(
        campaign_cfg.get("name", "Last Billboard API Campaign")
    )

    campaign.name = unique_name(base_name + " - Display Photo")
    campaign.advertising_channel_type = (
        client.enums.AdvertisingChannelTypeEnum.DISPLAY
    )
    campaign.status = client.enums.CampaignStatusEnum.PAUSED
    campaign.campaign_budget = budget_resource

    # Standard Display campaigns support manual CPC.
    campaign.manual_cpc = client.get_type("ManualCpc")

    eu_value = campaign_cfg.get(
        "contains_eu_political_advertising",
        "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING",
    )
    campaign.contains_eu_political_advertising = enum_value(
        client.enums.EuPoliticalAdvertisingStatusEnum,
        eu_value,
        "campaign.contains_eu_political_advertising",
    )

    response = service.mutate_campaigns(
        customer_id=customer_id,
        operations=[operation],
    )
    resource = response.results[0].resource_name
    created["display_campaign"] = resource
    save_data(data_path, data)
    print(f"[created] Display campaign: {resource}")
    return resource


def create_display_ad_group(
    client: GoogleAdsClient,
    customer_id: str,
    campaign_resource: str,
    data: dict[str, Any],
    data_path: Path,
) -> str:
    created = data["created_resources"]
    existing = created.get("display_ad_group")
    if existing:
        print(f"[skip] Display ad group already stored: {existing}")
        return existing

    service = client.get_service("AdGroupService")
    operation = client.get_type("AdGroupOperation")
    ad_group = operation.create

    cfg = data["ad_group"]
    ad_group.name = unique_name(
        str(cfg.get("name", "Last Billboard API Ad Group")) + " - Display Photo"
    )
    ad_group.campaign = campaign_resource
    ad_group.status = client.enums.AdGroupStatusEnum.PAUSED

    if cfg.get("cpc_bid_micros") is not None:
        ad_group.cpc_bid_micros = int(cfg["cpc_bid_micros"])

    # Deliberately do not set SEARCH_STANDARD here. The parent campaign is DISPLAY.
    response = service.mutate_ad_groups(
        customer_id=customer_id,
        operations=[operation],
    )
    resource = response.results[0].resource_name
    created["display_ad_group"] = resource
    save_data(data_path, data)
    print(f"[created] Display ad group: {resource}")
    return resource


def upload_image_asset(
    client: GoogleAdsClient,
    customer_id: str,
    image_path: Path,
    asset_name: str,
    created_key: str,
    data: dict[str, Any],
    data_path: Path,
) -> str:
    created = data["created_resources"]
    existing = created.get(created_key)
    if existing:
        print(f"[skip] {asset_name} already stored: {existing}")
        return existing

    service = client.get_service("AssetService")
    operation = client.get_type("AssetOperation")
    asset = operation.create

    asset.type_ = client.enums.AssetTypeEnum.IMAGE
    asset.name = unique_name(asset_name)
    asset.image_asset.data = image_path.read_bytes()

    response = service.mutate_assets(
        customer_id=customer_id,
        operations=[operation],
    )

    resource = response.results[0].resource_name
    created[created_key] = resource
    save_data(data_path, data)
    print(f"[created] {asset_name}: {resource}")
    return resource


def create_responsive_display_ad(
    client: GoogleAdsClient,
    customer_id: str,
    ad_group_resource: str,
    landscape_asset_resource: str,
    square_asset_resource: str,
    data: dict[str, Any],
    data_path: Path,
) -> str:
    created = data["created_resources"]
    existing = created.get("responsive_display_ad")
    if existing:
        print(f"[skip] Responsive Display Ad already stored: {existing}")
        return existing

    ad_cfg = data["ad"]

    service = client.get_service("AdGroupAdService")
    operation = client.get_type("AdGroupAdOperation")
    ad_group_ad = operation.create

    ad_group_ad.ad_group = ad_group_resource
    ad_group_ad.status = client.enums.AdGroupAdStatusEnum.PAUSED

    ad = ad_group_ad.ad
    ad.final_urls.extend(list(ad_cfg["final_urls"]))

    rda = ad.responsive_display_ad

    # The photo is placed INSIDE the ad as both required image formats.
    marketing_image = client.get_type("AdImageAsset")
    marketing_image.asset = landscape_asset_resource
    rda.marketing_images.append(marketing_image)

    square_marketing_image = client.get_type("AdImageAsset")
    square_marketing_image.asset = square_asset_resource
    rda.square_marketing_images.append(square_marketing_image)

    for text in list(ad_cfg["headlines"])[:5]:
        item = client.get_type("AdTextAsset")
        item.text = text
        rda.headlines.append(item)

    rda.long_headline.text = str(
        ad_cfg.get("long_headline") or ad_cfg["headlines"][0]
    ).strip()

    for text in list(ad_cfg["descriptions"])[:5]:
        item = client.get_type("AdTextAsset")
        item.text = text
        rda.descriptions.append(item)

    rda.business_name = str(
        ad_cfg.get("business_name") or "Last Billboard"
    ).strip()

    # When no fixed colors are supplied, this must remain true.
    rda.allow_flexible_color = True
    rda.format_setting = (
        client.enums.DisplayAdFormatSettingEnum.ALL_FORMATS
    )

    response = service.mutate_ad_group_ads(
        customer_id=customer_id,
        operations=[operation],
    )

    resource = response.results[0].resource_name
    created["responsive_display_ad"] = resource
    save_data(data_path, data)
    print(f"[created] Responsive Display Ad WITH PHOTO: {resource}")
    return resource


def get_image_asset_details(
    client: GoogleAdsClient,
    customer_id: str,
    asset_resource_name: str,
) -> dict[str, Any]:
    """Reads the exact image Asset back from Google Ads."""
    service = client.get_service("GoogleAdsService")

    query = f"""
        SELECT
          asset.resource_name,
          asset.id,
          asset.name,
          asset.type,
          asset.orientation,
          asset.image_asset.mime_type,
          asset.image_asset.file_size,
          asset.image_asset.full_size.width_pixels,
          asset.image_asset.full_size.height_pixels,
          asset.image_asset.full_size.url
        FROM asset
        WHERE asset.resource_name = '{asset_resource_name}'
        LIMIT 1
    """

    rows = list(service.search(customer_id=customer_id, query=query))
    if not rows:
        raise RuntimeError(
            f"Google Ads could not read back image asset: "
            f"{asset_resource_name}"
        )

    asset = rows[0].asset
    return {
        "resource_name": asset.resource_name,
        "id": str(asset.id),
        "name": asset.name,
        "type": str(asset.type_),
        "orientation": str(asset.orientation),
        "mime_type": str(asset.image_asset.mime_type),
        "file_size": int(asset.image_asset.file_size),
        "width": int(asset.image_asset.full_size.width_pixels),
        "height": int(asset.image_asset.full_size.height_pixels),
        "url": asset.image_asset.full_size.url,
    }


def verify_photo_is_in_display_ad(
    client: GoogleAdsClient,
    customer_id: str,
    ad_resource_name: str,
) -> None:
    """
    Verify the Responsive Display Ad itself contains image asset references,
    then read those exact Asset resources back from Google Ads.
    """
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
        WHERE ad_group_ad.resource_name = '{ad_resource_name}'
        LIMIT 1
    """

    rows = list(service.search(customer_id=customer_id, query=query))
    if not rows:
        raise RuntimeError(
            "The Responsive Display Ad was created but could not be read back."
        )

    row = rows[0]
    ad = row.ad_group_ad.ad
    rda = ad.responsive_display_ad

    landscape_resources = [
        item.asset for item in rda.marketing_images if item.asset
    ]
    square_resources = [
        item.asset for item in rda.square_marketing_images if item.asset
    ]

    print("\n=== GOOGLE ADS READ-BACK VERIFICATION ===")
    print(f"Campaign: {row.campaign.name} ({row.campaign.id})")
    print(f"Campaign status: {row.campaign.status}")
    print(f"Ad group: {row.ad_group.name} ({row.ad_group.id})")
    print(f"Ad group status: {row.ad_group.status}")
    print(f"Ad resource: {row.ad_group_ad.resource_name}")
    print(f"Ad ID: {ad.id}")
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
        + " | ".join(item.text for item in rda.headlines)
    )
    print(f"Long headline: {rda.long_headline.text}")
    print(
        "Descriptions: "
        + " | ".join(item.text for item in rda.descriptions)
    )
    print(f"Final URLs: {', '.join(ad.final_urls)}")

    print(
        f"Landscape image references inside ad: "
        f"{len(landscape_resources)}"
    )
    print(
        f"Square image references inside ad: "
        f"{len(square_resources)}"
    )

    if not landscape_resources or not square_resources:
        raise RuntimeError(
            "IMAGE VERIFICATION FAILED: the Google Ads ad object does not "
            "contain both landscape and square image asset references."
        )

    all_resources = []
    for resource in landscape_resources + square_resources:
        if resource not in all_resources:
            all_resources.append(resource)

    print("\nExact image Assets stored by Google Ads:")
    for resource in all_resources:
        details = get_image_asset_details(
            client, customer_id, resource
        )
        print(f"- Name: {details['name']}")
        print(f"  Resource: {details['resource_name']}")
        print(f"  Asset ID: {details['id']}")
        print(
            f"  Size: {details['width']} x {details['height']} "
            f"({details['mime_type']})"
        )
        print(f"  Bytes: {details['file_size']}")
        print(f"  Google-hosted image URL: {details['url']}")

    print("\nPHOTO INCLUDED IN RESPONSIVE DISPLAY AD: YES")
    print(
        "Note: Google Ads' Assets > Associations report can still be empty "
        "for a new PAUSED ad with no recent impressions. The ad object above "
        "is the direct source of truth for whether the photo is in the ad."
    )


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
                print(f"   field: {element.field_name}", file=sys.stderr)


def main() -> int:
    try:
        data_path, data = load_data()
        validate_text(data)
        customer_id = data["customer_id"]

        client = resolve_google_ads_client(customer_id)

        print(f"Target Google Ads customer: {customer_id}")
        print(
            "This script creates a NEW PAUSED Display campaign so the photo is "
            "part of the ad creative itself.\n"
        )

        landscape_path, square_path = prepare_display_images(data)

        budget = create_display_budget(
            client, customer_id, data, data_path
        )

        campaign = create_display_campaign(
            client, customer_id, budget, data, data_path
        )

        ad_group = create_display_ad_group(
            client, customer_id, campaign, data, data_path
        )

        landscape_asset = upload_image_asset(
            client=client,
            customer_id=customer_id,
            image_path=landscape_path,
            asset_name="Last Billboard Display Landscape Photo",
            created_key="display_landscape_image_asset",
            data=data,
            data_path=data_path,
        )

        square_asset = upload_image_asset(
            client=client,
            customer_id=customer_id,
            image_path=square_path,
            asset_name="Last Billboard Display Square Photo",
            created_key="display_square_image_asset",
            data=data,
            data_path=data_path,
        )

        ad_resource = create_responsive_display_ad(
            client=client,
            customer_id=customer_id,
            ad_group_resource=ad_group,
            landscape_asset_resource=landscape_asset,
            square_asset_resource=square_asset,
            data=data,
            data_path=data_path,
        )

        verify_photo_is_in_display_ad(
            client, customer_id, ad_resource
        )

        print("\nSUCCESS")
        print(f"Display campaign: {campaign}")
        print(f"Display ad group: {ad_group}")
        print(f"Responsive Display Ad: {ad_resource}")
        print(f"Landscape photo asset: {landscape_asset}")
        print(f"Square photo asset: {square_asset}")
        print(
            "\nEverything is PAUSED. In Google Ads, open the NEW Display "
            "campaign -> Ads to see the Responsive Display Ad and its images."
        )
        print(
            "Your previous Search campaign is left unchanged."
        )
        print(
            "\nNEXT STEP: After Google approves the ad, run "
            "publish_display_ad_30min.py --status first, then "
            "publish_display_ad_30min.py --confirm-live to make this "
            "Display ad eligible to serve for about 30 minutes."
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
