import os
import json
import hashlib
from datetime import datetime, timezone
from io import BytesIO

import boto3
import requests
from PIL import Image


SOURCE = "tomsk"
SOURCE_DISPLAY = "Tomsk Space Observing System"

# The current Tomsk shm.jpg image is 1540x460 and shows ~3 days.
# This crop pulls the newest/rightmost 24-hour strip from the spectrogram.
# If Tomsk changes the image layout, these are the first values to retune.
TOMSK_DAILY_CROP = {
    "left": 1018,
    "top": 30,
    "right": 1500,
    "bottom": 430,
}

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


def clamp_crop_box(image: Image.Image, crop: dict) -> tuple[int, int, int, int]:
    width, height = image.size
    left = max(0, min(width, int(crop["left"])))
    top = max(0, min(height, int(crop["top"])))
    right = max(left + 1, min(width, int(crop["right"])))
    bottom = max(top + 1, min(height, int(crop["bottom"])))
    return left, top, right, bottom


def get_existing_manifest() -> dict:
    key = "schumann/tomsk/manifest.json"

    try:
        obj = s3.get_object(Bucket=R2_BUCKET_NAME, Key=key)
        return json.loads(obj["Body"].read().decode("utf-8"))
    except Exception:
        return {
            "version": 1,
            "source": SOURCE,
            "source_display": SOURCE_DISPLAY,
            "updated_at": None,
            "public_base_url": R2_PUBLIC_BASE_URL,
            "items": [],
            "weekly": [],
        }


def upload_bytes(key: str, data: bytes, content_type: str) -> None:
    s3.put_object(
        Bucket=R2_BUCKET_NAME,
        Key=key,
        Body=data,
        ContentType=content_type,
        CacheControl="public, max-age=3600",
    )


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

    crop_box = clamp_crop_box(image, TOMSK_DAILY_CROP)
    daily_image = image.crop(crop_box)
    daily_webp_bytes = image_to_webp_bytes(daily_image)

    raw_key = f"schumann/tomsk/raw/{year}/{month}/{date_str}.webp"
    daily_key = f"schumann/tomsk/daily/{year}/{month}/{date_str}.webp"
    manifest_key = "schumann/tomsk/manifest.json"

    upload_bytes(raw_key, raw_webp_bytes, "image/webp")
    upload_bytes(daily_key, daily_webp_bytes, "image/webp")

    manifest = get_existing_manifest()
    manifest["version"] = 1
    manifest["source"] = SOURCE
    manifest["source_display"] = SOURCE_DISPLAY
    manifest["updated_at"] = captured_at
    manifest["public_base_url"] = R2_PUBLIC_BASE_URL

    manifest["items"] = [
        item for item in manifest.get("items", [])
        if item.get("date") != date_str
    ]

    manifest["items"].insert(0, {
        "date": date_str,
        "captured_at": captured_at,
        "type": "daily",
        "source": SOURCE,
        "raw": raw_key,
        "daily": daily_key,
        "raw_url": f"{R2_PUBLIC_BASE_URL}/{raw_key}",
        "daily_url": f"{R2_PUBLIC_BASE_URL}/{daily_key}",
        "sha256": original_hash,
        "raw_sha256": sha256_bytes(raw_webp_bytes),
        "daily_sha256": sha256_bytes(daily_webp_bytes),
        "source_image_size": {
            "width": image_width,
            "height": image_height,
        },
        "daily_crop": {
            "left": crop_box[0],
            "top": crop_box[1],
            "right": crop_box[2],
            "bottom": crop_box[3],
        },
        "status": "ok",
    })

    manifest["items"] = manifest["items"][:400]

    manifest_bytes = json.dumps(manifest, indent=2).encode("utf-8")
    upload_bytes(manifest_key, manifest_bytes, "application/json")

    print(f"Archived Tomsk Schumann image for {date_str}")
    print(f"Raw URL: {R2_PUBLIC_BASE_URL}/{raw_key}")
    print(f"Daily crop URL: {R2_PUBLIC_BASE_URL}/{daily_key}")
    print(f"Manifest: {R2_PUBLIC_BASE_URL}/{manifest_key}")


if __name__ == "__main__":
    main()
