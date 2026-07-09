import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any, Iterable

from PIL import Image, ImageChops, ImageStat


SOURCE = "tomsk"
SOURCE_DISPLAY = "Tomsk Space Observing System"

# The current Tomsk shm.jpg image is about 1540x460 and shows about 3 days.
# This coarse crop isolates the useful chart area. Conservative edge trimming
# then removes only almost-empty columns attached to the outside edges.
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
STITCH_MAX_MATCH_SCORE = float(os.environ.get("STITCH_MAX_MATCH_SCORE", "18"))
STITCH_NEAR_DUPLICATE_APPEND_PX = int(os.environ.get("STITCH_NEAR_DUPLICATE_APPEND_PX", "25"))
MAX_STITCH_WIDTH = int(os.environ.get("MAX_STITCH_WIDTH", "0"))

RAW_KEY_RE = re.compile(r"schumann/tomsk/raw/(?P<year>\d{4})/(?P<month>\d{2})/(?P<date>\d{4}-\d{2}-\d{2})\.webp$")


@dataclass
class ProcessedSnapshot:
    date: str
    raw_key: str
    raw_url: str
    daily_key: str
    daily_url: str
    processed_key: str
    processed_url: str
    raw_sha256: str
    daily_sha256: str
    processed_sha256: str
    source_image_size: dict[str, int]
    processed_image_size: dict[str, int]
    processing: dict[str, Any]
    warnings: list[str]
    image: Image.Image


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def image_to_webp_bytes(image: Image.Image, quality: int = 88) -> bytes:
    out = BytesIO()
    image.save(out, format="WEBP", quality=quality, method=6)
    return out.getvalue()


def load_image_bytes(data: bytes) -> Image.Image:
    return Image.open(BytesIO(data)).convert("RGB")


def clamp_crop_box(image: Image.Image, crop: dict[str, int] | None = None) -> tuple[int, int, int, int]:
    crop = crop or TOMSK_CHART_CROP
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

    Black inside the chart may be real quiet Schumann data. This function only
    removes nearly-empty columns attached to the chart edges.
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


def normalize_height(image: Image.Image, height: int) -> Image.Image:
    if image.height == height:
        return image
    width = max(1, round(image.width * (height / image.height)))
    return image.resize((width, height), Image.Resampling.BICUBIC)


def resized_for_match(image: Image.Image) -> Image.Image:
    width = max(1, image.width // max(1, STITCH_MATCH_SCALE))
    return image.convert("L").resize((width, STITCH_MATCH_HEIGHT), Image.Resampling.BILINEAR)


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
        existing_tail = existing_small.crop((existing_small.width - overlap, 0, existing_small.width, existing_small.height))
        new_head = new_small.crop((0, 0, overlap, new_small.height))
        score = mean_abs_difference(existing_tail, new_head)
        if score < best_score:
            best_score = score
            best_overlap = overlap

    overlap_px = min(new.width, max(1, best_overlap * max(1, STITCH_MATCH_SCALE)))
    append_width = max(0, new.width - overlap_px)
    confidence = "ok" if best_score <= STITCH_MAX_MATCH_SCORE else "low"
    warnings = []
    if confidence == "low":
        warnings.append("low_confidence_overlap_match")

    return {
        "status": "matched",
        "confidence": confidence,
        "overlap_px": overlap_px,
        "append_width_px": append_width,
        "match_score_mean_abs_difference": round(best_score, 4),
        "match_score_threshold": STITCH_MAX_MATCH_SCORE,
        "match_scale": STITCH_MATCH_SCALE,
        "match_height": STITCH_MATCH_HEIGHT,
        "min_overlap_ratio": STITCH_MIN_OVERLAP_RATIO,
        "max_overlap_ratio": STITCH_MAX_OVERLAP_RATIO,
        "warnings": warnings,
    }


def append_with_overlap(existing: Image.Image, new: Image.Image) -> tuple[Image.Image, dict[str, Any]]:
    new = normalize_height(new, existing.height)
    overlap = find_best_overlap(existing, new)

    if overlap["append_width_px"] < STITCH_NEAR_DUPLICATE_APPEND_PX:
        overlap["status"] = "skipped_near_duplicate"
        return existing, overlap

    if overlap["confidence"] == "low":
        overlap["status"] = "appended_full_width_low_confidence"
        overlap["overlap_px"] = 0
        overlap["append_width_px"] = new.width

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


def rebuild_stitched_timeline(processed_images: Iterable[tuple[str, Image.Image]]) -> tuple[Image.Image | None, list[dict[str, Any]]]:
    timeline: Image.Image | None = None
    diagnostics: list[dict[str, Any]] = []

    for date_str, image in processed_images:
        if timeline is None:
            timeline = image.copy()
            diagnostics.append({
                "date": date_str,
                "status": "initialized",
                "overlap_px": 0,
                "append_width_px": image.width,
                "match_score_mean_abs_difference": None,
            })
            continue

        timeline, stitch_info = append_with_overlap(timeline, image)
        diagnostics.append({"date": date_str, **stitch_info})

    return timeline, diagnostics


def key_for_date(kind: str, date_str: str) -> str:
    year, month, _ = date_str.split("-")
    return f"schumann/tomsk/{kind}/{year}/{month}/{date_str}.webp"


def public_url(public_base_url: str, key: str) -> str:
    return f"{public_base_url.rstrip('/')}/{key}"


def date_from_raw_key(key: str) -> str | None:
    match = RAW_KEY_RE.match(key)
    return match.group("date") if match else None


def build_processed_snapshot(
    *,
    date_str: str,
    raw_key: str,
    raw_bytes: bytes,
    public_base_url: str,
) -> ProcessedSnapshot:
    raw_image = load_image_bytes(raw_bytes)
    processed_image, processing_info = crop_processed_chart(raw_image)
    processed_bytes = image_to_webp_bytes(processed_image)

    warnings = []
    if processing_info["black_ratio"] > 0.985:
        warnings.append("processed_chart_is_mostly_black")

    daily_key = key_for_date("daily", date_str)
    processed_key = key_for_date("processed", date_str)

    return ProcessedSnapshot(
        date=date_str,
        raw_key=raw_key,
        raw_url=public_url(public_base_url, raw_key),
        daily_key=daily_key,
        daily_url=public_url(public_base_url, daily_key),
        processed_key=processed_key,
        processed_url=public_url(public_base_url, processed_key),
        raw_sha256=sha256_bytes(raw_bytes),
        daily_sha256=sha256_bytes(processed_bytes),
        processed_sha256=sha256_bytes(processed_bytes),
        source_image_size={"width": raw_image.width, "height": raw_image.height},
        processed_image_size={"width": processed_image.width, "height": processed_image.height},
        processing=processing_info,
        warnings=warnings,
        image=processed_image,
    )


def item_from_processed_snapshot(snapshot: ProcessedSnapshot, captured_at: str) -> dict[str, Any]:
    return {
        "date": snapshot.date,
        "captured_at": captured_at,
        "type": "snapshot_3day",
        "source": SOURCE,
        "raw": snapshot.raw_key,
        "daily": snapshot.daily_key,
        "processed": snapshot.processed_key,
        "raw_url": snapshot.raw_url,
        "daily_url": snapshot.daily_url,
        "processed_url": snapshot.processed_url,
        "sha256": snapshot.raw_sha256,
        "raw_sha256": snapshot.raw_sha256,
        "daily_sha256": snapshot.daily_sha256,
        "processed_sha256": snapshot.processed_sha256,
        "source_image_size": snapshot.source_image_size,
        "processed_image_size": snapshot.processed_image_size,
        "processing": snapshot.processing,
        "status": "ok" if not snapshot.warnings else "warning",
        "warnings": snapshot.warnings,
    }


def manifest_base(public_base_url: str, captured_at: str) -> dict[str, Any]:
    return {
        "version": 2,
        "source": SOURCE,
        "source_display": SOURCE_DISPLAY,
        "updated_at": captured_at,
        "public_base_url": public_base_url.rstrip("/"),
        "processing_mode": "raw_3day_snapshot_plus_overlap_stitched_timeline",
        "items": [],
        "weekly": [],
        "stitched": None,
    }


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
