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

    out = BytesIO()
    image.save(out, format="WEBP", quality=88, method=6)
    webp_bytes = out.getvalue()

    raw_key = f"schumann/tomsk/raw/{year}/{month}/{date_str}.webp"
    daily_key = f"schumann/tomsk/daily/{year}/{month}/{date_str}.webp"
    manifest_key = "schumann/tomsk/manifest.json"

    upload_bytes(raw_key, webp_bytes, "image/webp")
    upload_bytes(daily_key, webp_bytes, "image/webp")

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
        "status": "ok",
    })

    manifest["items"] = manifest["items"][:400]

    manifest_bytes = json.dumps(manifest, indent=2).encode("utf-8")
    upload_bytes(manifest_key, manifest_bytes, "application/json")

    print(f"Archived Tomsk Schumann image for {date_str}")
    print(f"Daily URL: {R2_PUBLIC_BASE_URL}/{daily_key}")
    print(f"Manifest: {R2_PUBLIC_BASE_URL}/{manifest_key}")


if __name__ == "__main__":
    main()
