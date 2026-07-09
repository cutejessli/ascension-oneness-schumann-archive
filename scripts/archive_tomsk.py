import os
import json
import hashlib
from datetime import datetime, timezone
from io import BytesIO
from typing import Any

import boto3
import requests
from PIL import Image, ImageChops, ImageStat


SOURCE = "tomsk"
SOURCE_DISPLAY = "Tomsk Space Observing System"

# The current Tomsk shm.jpg image is about 1540x460 and shows ~3 days.
# Instead of trying to crop one fixed "daily" strip, we now keep the full raw
# snapshot, create a cleaned 3-day chart image, and stitch snapshots together by
# matching their overlapping chart regions.
#
# If Tomsk changes the image layout, tune this coarse chart box first. The edge
# trimming below will remove truly empty outer columns, but it intentionally does
# not remove dark/quiet data inside the chart.
TOMSK_CHART_CROP = {
    "left": 45,
    "top": 30,
    "right": 1500,
    "bottom": 430,
}

DARK_PIXEL_THRESHOLD = int(os.environ.get("DARK_PIXEL_THRESHOLD", "8"))
EDGE_EMPTY_NON_DARK_RATIO = float(os.environ.get("EDGE_EMPTY_NON_DARK_RATIO", "0.01"))
MAX_RIGHT_EDGE_TRIM_RATIO = float(os.environ.get("MAX_RIGHT_EDGE_TRIM_RATIO", "0.35"))
MAX_LEFT_EDGE_TRIM_RATIO = float(os.environ.get("MAX_LEFT_EDGE_TRIM_RATIO", "0.08"))

STITCH_MATCH_SCALE = int(os.environ.get("STITCH_MATCH_SCALE", "4"))
STITCH_MATCH_HEIGHT = int(os.environ.get("STITCH_MATCH_HEIGHT", "96"))
STITCH_MIN_OVERLAP_RATIO = float(os.environ.get("STITCH_MIN_OVERLAP_RATIO", "0.45"))
STITCH_MAX_OVERLAP_RATIO = float(os.environ.get("STITCH_MAX_OVERLAP_RATIO", "0.94"))

# Leave uncapped by default. If Cloudflare/image-display limits become a problem,
# set this in GitHub Actions and the script will keep the newest right-side region.
MAX_STITCH_WIDTH = int(os.environ.get("MAX_STITCH_WIDTH", "0"))

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


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def image_to_webp_bytes(image: Image.Image, quality: int = 88) -> bytes:
    out = BytesIO()
    image.save(out, format="WEBP", quality=quality, method=6)
    return out.getvalue()


def clamp_crop_box(image: Image.Image, crop: dict[str, int]) -> tuple[int, int, int, int]:
    width, height = image.size
    left = max(0, min(width, int(crop["left"])))
    top = max(0, min(height, int(crop["top"])))
    right = max(left + 1, min(width, int(crop["right"])))
    bottom = max(top + 1, min(height, int(crop["bottom"])))
    return left, top, right, bottom


def dark_pixel_ratio(image: Image.Image, threshold: int = DARK_PIXEL_THRESHOLD) -> float:
    gray = image.convert("L")
    histogram = gray.histogram()
    dark_pixels = sum(histogram[: threshold + 1])
    total_pixels = sum(histogram)
    if total_pixels == 0:
        return 1.0
    return round(dark_pixels / total_pixels, 6)


def column_non_dark_ratio(gray: Image.Image, x: int, threshold: int = DARK_PIXEL_THRESHOLD) -> float:
    height = gray.height
    sample_step = 2 if height > 150 else 1
    samples = 0
    non_dark = 0

    for y in range(0, height, sample_step):
        samples += 1
        if gray.getpixel((x, y)) > threshold:
            non_dark += 1

    if samples == 0:
        return 0.0
    return non_dark / samples


def trim_empty_edge_columns(image: Image.Image) -> tuple[Image.Image, dict[str, Any]]:
    """Trim only contiguous almost-empty columns on the outside edges.

    This is deliberately conservative. Real Schumann quiet periods can be dark,
    so we only remove columns that are dark across nearly the entire sampled
    height and are connected to the image edge.
    """

    gray = image.convert("L")
    width, height = gray.size

    max_right_trim = int(width * MAX_RIGHT_EDGE_TRIM_RATIO)
    max_left_trim = int(width * MAX_LEFT_EDGE_TRIM_RATIO)

    left_trim = 0
    for x in range(0, max_left_trim):
        if column_non_dark_ratio(gray, x) <= EDGE_EMPTY_NON_DARK_RATIO:
            left_trim = x + 1
        else:
            break

    right_trim = 0
    for x in range(width - 1, max(width - max_right_trim - 1, left_trim), -1):
        if column_non_dark_ratio(gray, x) <= EDGE_EMPTY_NON_DARK_RATIO:
            right_trim += 1
        else:
            break

    right = max(left_trim + 1, width - right_trim)
    trimmed = image.crop((left_trim, 0, right, height))

    return trimmed, {
        "left_trimmed_px": left_trim,
        "right_trimmed_px": right_trim,
        "original_width": width,
        "original_height": height,
        "trimmed_width": trimmed.width,
        "trimmed_height": trimmed.height,
        "dark_pixel_threshold": DARK_PIXEL_THRESHOLD,
        "edge_empty_non_dark_ratio": EDGE_EMPTY_NON_DARK_RATIO,
    }


def crop_processed_chart(image: Image.Image) -> tuple[Image.Image, dict[str, Any]]:
    chart_crop_box = clamp_crop_box(image, TOMSK_CHART_CROP)
    chart_image = image.crop(chart_crop_box)
    trimmed_chart, trim_info = trim_empty_edge_columns(chart_image)

    return trimmed_chart, {
        "mode": "chart_crop_plus_conservative_edge_trim",
        "chart_crop": {
            "left": chart_crop_box[0],
            "top": chart_crop_box[1],
            "right": chart_crop_box[2],
            "bottom": chart_crop_box[3],
        },
        "edge_trim": trim_info,
        "black_ratio": dark_pixel_ratio(trimmed_chart),
    }


def get_existing_manifest() -> dict[str, Any]:
    key = "schumann/tomsk/manifest.json"

    try:
        obj = s3.get_object(Bucket=R2_BUCKET_NAME, Key=key)
        return json.loads(obj["Body"].read().decode("utf-8"))
    except Exception:
        return {
            "version": 2,
            "source": SOURCE,
            "source_display": SOURCE_DISPLAY,
            "updated_at": None,
            "public_base_url": R2_PUBLIC_BASE_URL,
            "items": [],
            "weekly": [],
            "stitched": None,
        }


def upload_bytes(key: str, data: bytes, content_type: str) -> None:
    s3.put_object(
        Bucket=R2_BUCKET_NAME,
        Key=key,
        Body=data,
        ContentType=content_type,
        CacheControl="public, max-age=3600",
    )


def get_image_from_r2(key: str) -> Image.Image | None:
    try:
        obj = s3.get_object(Bucket=R2_BUCKET_NAME, Key=key)
        return Image.open(BytesIO(obj["Body"].read())).convert("RGB")
    except Exception:
        return None


def normalize_height(image: Image.Image, height: int) -> Image.Image:
    if image.height == height:
        return image

    width = max(1, round(image.width * (height / image.height)))
    return image.resize((width, height), Image.Resampling.BICUBIC)


def resized_for_match(image: Image.Image) -> Image.Image:
    width = max(1, image.width // max(1, STITCH_MATCH_SCALE))
    return image.convert("L").resize(
        (width, STITCH_MATCH_HEIGHT),
        Image.Resampling.BILINEAR,
    )


def mean_abs_difference(left: Image.Image, right: Image.Image) -> float:
    diff = ImageChops.difference(left, right)
    return float(ImageStat.Stat(diff).mean[0])


def find_best_overlap(existing: Image.Image, new: Image.Image) -> dict[str, Any]:
    existing_small = resized_for_match(existing)
    new_small = resized_for_match(new)

    max_possible = min(existing_small.width, new_small.width)
    min_overlap = max(12, int(max_possible * STITCH_MIN_OVERLAP_RATIO))
    max_overlap = max(min_overlap, int(max_possible * STITCH_MAX_OVERLAP_RATIO))

    best_overlap = min_overlap
    best_score = float("inf")

    for overlap in range(min_overlap, max_overlap + 1):
        existing_tail = existing_small.crop(
            (existing_small.width - overlap, 0, existing_small.width, existing_small.height)
        )
        new_head = new_small.crop((0, 0, overlap, new_small.height))
        score = mean_abs_difference(existing_tail, new_head)

        if score < best_score:
            best_score = score
            best_overlap = overlap

    overlap_px = min(new.width, max(1, best_overlap * max(1, STITCH_MATCH_SCALE)))
    append_width = max(0, new.width - overlap_px)

    return {
        "status": "matched",
        "overlap_px": overlap_px,
        "append_width_px": append_width,
        "match_score_mean_abs_difference": round(best_score, 4),
        "match_scale": STITCH_MATCH_SCALE,
        "match_height": STITCH_MATCH_HEIGHT,
        "min_overlap_ratio": STITCH_MIN_OVERLAP_RATIO,
        "max_overlap_ratio": STITCH_MAX_OVERLAP_RATIO,
    }


def append_with_overlap(existing: Image.Image, new: Image.Image) -> tuple[Image.Image, dict[str, Any]]:
    new = normalize_height(new, existing.height)
    overlap = find_best_overlap(existing, new)

    # Same-day reruns or tiny chart shifts can produce a near-full overlap. In
    # that case, keep the existing timeline instead of adding duplicate pixels.
    if overlap["append_width_px"] < 25:
        overlap["status"] = "skipped_near_duplicate"
        return existing, overlap

    appended_region = new.crop((overlap["overlap_px"], 0, new.width, new.height))
    stitched = Image.new("RGB", (existing.width + appended_region.width, existing.height))
    stitched.paste(existing, (0, 0))
    stitched.paste(appended_region, (existing.width, 0))

    if MAX_STITCH_WIDTH and stitched.width > MAX_STITCH_WIDTH:
        trim_left = stitched.width - MAX_STITCH_WIDTH
        stitched = stitched.crop((trim_left, 0, stitched.width, stitched.height))
        overlap["max_width_trimmed_left_px"] = trim_left
        overlap["max_stitch_width"] = MAX_STITCH_WIDTH

    return stitched, overlap


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
            "key": previous_key,
            "url": f"{R2_PUBLIC_BASE_URL}/{previous_key}",
            "status": "skipped_same_day_rerun",
            "reason": "manifest already had this date before the current run",
        }

    existing_image = get_image_from_r2(previous_key)

    if existing_image is None:
        stitched_image = processed_image
        stitch_info = {
            "status": "initialized",
            "overlap_px": 0,
            "append_width_px": processed_image.width,
        }
    else:
        stitched_image, stitch_info = append_with_overlap(existing_image, processed_image)

    stitched_bytes = image_to_webp_bytes(stitched_image)
    upload_bytes(stitched_key, stitched_bytes, "image/webp")

    return stitched_bytes, {
        "key": stitched_key,
        "url": f"{R2_PUBLIC_BASE_URL}/{stitched_key}",
        "updated_at": captured_at,
        "source": SOURCE,
        "last_source_date": date_str,
        "width": stitched_image.width,
        "height": stitched_image.height,
        "sha256": sha256_bytes(stitched_bytes),
        "mode": "overlap_stitched_from_processed_3day_snapshots",
        "latest_append": stitch_info,
    }


def main() -> None:
    now = datetime.now(timezone.utc)
    date_str = now.date().isoformat()
    year = now.strftime("%Y")
    month = now.strftime("%m")
    captured_at = now.isoformat().replace("+00:00", "Z")

    response = requests.get(TOMSK_IMAGE_URL, timeout=30)
    response.raise_for_status()

    original_bytes = response.content
    original_hash = sha256_bytes(original_bytes)

    image = Image.open(BytesIO(original_bytes)).convert("RGB")
    image_width, image_height = image.size

    raw_webp_bytes = image_to_webp_bytes(image)
    processed_image, processing_info = crop_processed_chart(image)
    processed_webp_bytes = image_to_webp_bytes(processed_image)

    raw_key = f"schumann/tomsk/raw/{year}/{month}/{date_str}.webp"
    processed_key = f"schumann/tomsk/processed/{year}/{month}/{date_str}.webp"

    # Backward-compatible alias for existing UI code that expects "daily".
    # This is no longer a fixed 24-hour slice; it is the cleaned 3-day chart.
    daily_key = f"schumann/tomsk/daily/{year}/{month}/{date_str}.webp"

    manifest_key = "schumann/tomsk/manifest.json"

    upload_bytes(raw_key, raw_webp_bytes, "image/webp")
    upload_bytes(processed_key, processed_webp_bytes, "image/webp")
    upload_bytes(daily_key, processed_webp_bytes, "image/webp")

    manifest = get_existing_manifest()

    stitch_warning = None
    stitched_bytes = None
    try:
        stitched_bytes, stitched_meta = build_or_extend_stitched_timeline(
            manifest,
            processed_image,
            date_str,
            captured_at,
        )
    except Exception as exc:
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

    warnings = []
    if processing_info["black_ratio"] > 0.985:
        warnings.append("processed_chart_is_mostly_black")
    if stitch_warning:
        warnings.append("stitched_timeline_update_failed")

    item = {
        "date": date_str,
        "captured_at": captured_at,
        "type": "snapshot_3day",
        "source": SOURCE,
        "raw": raw_key,
        "daily": daily_key,
        "processed": processed_key,
        "raw_url": f"{R2_PUBLIC_BASE_URL}/{raw_key}",
        "daily_url": f"{R2_PUBLIC_BASE_URL}/{daily_key}",
        "processed_url": f"{R2_PUBLIC_BASE_URL}/{processed_key}",
        "sha256": original_hash,
        "raw_sha256": sha256_bytes(raw_webp_bytes),
        "daily_sha256": sha256_bytes(processed_webp_bytes),
        "processed_sha256": sha256_bytes(processed_webp_bytes),
        "source_image_size": {
            "width": image_width,
            "height": image_height,
        },
        "processed_image_size": {
            "width": processed_image.width,
            "height": processed_image.height,
        },
        "processing": processing_info,
        "stitched": stitched_meta,
        "status": "ok" if not warnings else "warning",
        "warnings": warnings,
    }

    if stitch_warning:
        item["stitch_warning"] = stitch_warning

    manifest["items"].insert(0, item)
    manifest["items"] = manifest["items"][:400]

    manifest_bytes = json.dumps(manifest, indent=2).encode("utf-8")
    upload_bytes(manifest_key, manifest_bytes, "application/json")

    print(f"Archived Tomsk Schumann 3-day snapshot for {date_str}")
    print(f"Raw URL: {R2_PUBLIC_BASE_URL}/{raw_key}")
    print(f"Processed URL: {R2_PUBLIC_BASE_URL}/{processed_key}")
    print(f"Daily compatibility URL: {R2_PUBLIC_BASE_URL}/{daily_key}")

    if stitched_bytes:
        print(f"Stitched timeline URL: {stitched_meta['url']}")
    elif stitched_meta:
        print(f"Stitched timeline unchanged: {stitched_meta.get('url')}")

    print(f"Manifest: {R2_PUBLIC_BASE_URL}/{manifest_key}")


if __name__ == "__main__":
    main()
