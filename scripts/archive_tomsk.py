import json
import os
from io import BytesIO
from typing import Any

import boto3
import requests
from PIL import Image

from tomsk_archive_utils import (
    SOURCE,
    SOURCE_DISPLAY,
    build_processed_snapshot,
    image_to_webp_bytes,
    item_from_processed_snapshot,
    manifest_base,
    public_url,
    rebuild_stitched_timeline,
    sha256_bytes,
    utc_now_iso,
)


R2_ACCOUNT_ID = os.environ["R2_ACCOUNT_ID"]
R2_BUCKET_NAME = os.environ["R2_BUCKET_NAME"]
R2_ACCESS_KEY_ID = os.environ["R2_ACCESS_KEY_ID"]
R2_SECRET_ACCESS_KEY = os.environ["R2_SECRET_ACCESS_KEY"]
R2_PUBLIC_BASE_URL = os.environ["R2_PUBLIC_BASE_URL"].rstrip("/")
TOMSK_IMAGE_URL = os.environ["TOMSK_IMAGE_URL"]

ENDPOINT_URL = f"https://{R2_ACCOUNT_ID}.r2.cloudflarestorage.com"

s3 = boto3.client(
    "s3",
    endpoint_url=ENDPOINT_URL,
    aws_access_key_id=R2_ACCESS_KEY_ID,
    aws_secret_access_key=R2_SECRET_ACCESS_KEY,
    region_name="auto",
)


def upload_bytes(key: str, data: bytes, content_type: str) -> None:
    s3.put_object(
        Bucket=R2_BUCKET_NAME,
        Key=key,
        Body=data,
        ContentType=content_type,
        CacheControl="public, max-age=3600",
    )


def get_existing_manifest() -> dict[str, Any]:
    key = "schumann/tomsk/manifest.json"
    try:
        obj = s3.get_object(Bucket=R2_BUCKET_NAME, Key=key)
        return json.loads(obj["Body"].read().decode("utf-8"))
    except Exception:
        return manifest_base(R2_PUBLIC_BASE_URL, utc_now_iso())


def get_image_from_r2(key: str) -> Image.Image | None:
    try:
        obj = s3.get_object(Bucket=R2_BUCKET_NAME, Key=key)
        return Image.open(BytesIO(obj["Body"].read())).convert("RGB")
    except Exception:
        return None


def build_or_extend_stitched_timeline(
    manifest: dict[str, Any],
    processed_image: Image.Image,
    date_str: str,
    captured_at: str,
) -> tuple[bytes | None, dict[str, Any]]:
    stitched_key = "schumann/tomsk/stitched/timeline.webp"
    previous_stitched = manifest.get("stitched") or {}
    previous_key = previous_stitched.get("key") or stitched_key
    existing_dates = {item.get("date") for item in manifest.get("items", [])}

    if date_str in existing_dates and previous_stitched.get("key"):
        return None, {
            **previous_stitched,
            "status": "skipped_same_day_rerun",
            "reason": "manifest already had this date before the current run",
        }

    existing_image = get_image_from_r2(previous_key)
    if existing_image is None:
        stitched_image = processed_image
        stitch_steps = [{
            "date": date_str,
            "status": "initialized",
            "overlap_px": 0,
            "append_width_px": processed_image.width,
            "match_score_mean_abs_difference": None,
        }]
    else:
        stitched_image, stitch_steps = rebuild_stitched_timeline([
            ("existing_timeline", existing_image),
            (date_str, processed_image),
        ])

    if stitched_image is None:
        raise RuntimeError("stitched timeline build produced no image")

    stitched_bytes = image_to_webp_bytes(stitched_image)
    upload_bytes(stitched_key, stitched_bytes, "image/webp")
    latest_append = stitch_steps[-1] if stitch_steps else {}

    return stitched_bytes, {
        "key": stitched_key,
        "url": public_url(R2_PUBLIC_BASE_URL, stitched_key),
        "updated_at": captured_at,
        "source": SOURCE,
        "last_source_date": date_str,
        "width": stitched_image.width,
        "height": stitched_image.height,
        "sha256": sha256_bytes(stitched_bytes),
        "mode": "overlap_stitched_from_processed_3day_snapshots",
        "latest_append": latest_append,
    }


def main() -> None:
    captured_at = utc_now_iso()
    date_str = captured_at[:10]
    year, month, _ = date_str.split("-")

    response = requests.get(TOMSK_IMAGE_URL, timeout=30)
    response.raise_for_status()
    raw_bytes = response.content
    raw_key = f"schumann/tomsk/raw/{year}/{month}/{date_str}.webp"
    raw_webp_bytes = image_to_webp_bytes(Image.open(BytesIO(raw_bytes)).convert("RGB"))

    snapshot = build_processed_snapshot(
        date_str=date_str,
        raw_key=raw_key,
        raw_bytes=raw_webp_bytes,
        public_base_url=R2_PUBLIC_BASE_URL,
    )
    processed_bytes = image_to_webp_bytes(snapshot.image)

    upload_bytes(raw_key, raw_webp_bytes, "image/webp")
    upload_bytes(snapshot.processed_key, processed_bytes, "image/webp")
    upload_bytes(snapshot.daily_key, processed_bytes, "image/webp")

    manifest = get_existing_manifest()
    stitch_warning = None
    try:
        stitched_bytes, stitched_meta = build_or_extend_stitched_timeline(
            manifest,
            snapshot.image,
            date_str,
            captured_at,
        )
    except Exception as exc:
        stitched_bytes = None
        stitch_warning = str(exc)
        stitched_meta = manifest.get("stitched")

    manifest["version"] = 2
    manifest["source"] = SOURCE
    manifest["source_display"] = SOURCE_DISPLAY
    manifest["updated_at"] = captured_at
    manifest["public_base_url"] = R2_PUBLIC_BASE_URL
    manifest["processing_mode"] = "raw_3day_snapshot_plus_overlap_stitched_timeline"
    manifest["stitched"] = stitched_meta

    manifest["items"] = [
        item for item in manifest.get("items", [])
        if item.get("date") != date_str
    ]

    item = item_from_processed_snapshot(snapshot, captured_at)
    item["stitched"] = stitched_meta
    if stitch_warning:
        item["warnings"] = [*item.get("warnings", []), "stitched_timeline_update_failed"]
        item["stitch_warning"] = stitch_warning
        item["status"] = "warning"

    manifest["items"].insert(0, item)
    manifest["items"] = manifest["items"][:400]

    manifest_bytes = json.dumps(manifest, indent=2).encode("utf-8")
    upload_bytes("schumann/tomsk/manifest.json", manifest_bytes, "application/json")

    print(f"Archived Tomsk Schumann 3-day snapshot for {date_str}")
    print(f"Raw URL: {public_url(R2_PUBLIC_BASE_URL, raw_key)}")
    print(f"Processed URL: {snapshot.processed_url}")
    print(f"Daily compatibility URL: {snapshot.daily_url}")
    if stitched_bytes:
        print(f"Stitched timeline URL: {stitched_meta['url']}")
    elif stitched_meta:
        print(f"Stitched timeline unchanged: {stitched_meta.get('url')}")
    print(f"Manifest: {public_url(R2_PUBLIC_BASE_URL, 'schumann/tomsk/manifest.json')}")


if __name__ == "__main__":
    main()
