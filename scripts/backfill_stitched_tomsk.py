import argparse
import json
import os
from pathlib import Path
from typing import Any

from tomsk_archive_utils import (
    SOURCE,
    build_processed_snapshot,
    date_from_raw_key,
    image_to_webp_bytes,
    item_from_processed_snapshot,
    key_for_date,
    manifest_base,
    public_url,
    rebuild_stitched_timeline,
    utc_now_iso,
    write_json,
)


def build_s3_client():
    import boto3

    account_id = os.environ["R2_ACCOUNT_ID"]
    return boto3.client(
        "s3",
        endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )


def list_raw_r2_keys(s3, bucket: str) -> list[str]:
    keys: list[str] = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix="schumann/tomsk/raw/"):
        for item in page.get("Contents", []):
            key = item.get("Key", "")
            if date_from_raw_key(key):
                keys.append(key)
    return sorted(keys, key=lambda key: date_from_raw_key(key) or "")


def get_r2_object_bytes(s3, bucket: str, key: str) -> bytes:
    obj = s3.get_object(Bucket=bucket, Key=key)
    return obj["Body"].read()


def put_r2_object_bytes(s3, bucket: str, key: str, data: bytes, content_type: str) -> None:
    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=data,
        ContentType=content_type,
        CacheControl="public, max-age=3600",
    )


def r2_key_exists(s3, bucket: str, key: str) -> bool:
    try:
        s3.head_object(Bucket=bucket, Key=key)
        return True
    except Exception:
        return False


def load_existing_manifest(s3, bucket: str, public_base_url: str, captured_at: str) -> dict[str, Any]:
    try:
        obj = s3.get_object(Bucket=bucket, Key="schumann/tomsk/manifest.json")
        return json.loads(obj["Body"].read().decode("utf-8"))
    except Exception:
        return manifest_base(public_base_url, captured_at)


def parse_local_date(path: Path) -> str | None:
    stem = path.stem
    if len(stem) >= 10 and stem[:4].isdigit() and stem[4] == "-" and stem[7] == "-":
        return stem[:10]
    return None


def local_raw_inputs(input_dir: Path) -> list[tuple[str, str, bytes]]:
    results: list[tuple[str, str, bytes]] = []
    for path in sorted(input_dir.glob("*")):
        if not path.is_file() or path.suffix.lower() not in {".webp", ".jpg", ".jpeg", ".png"}:
            continue
        date_str = parse_local_date(path)
        if not date_str:
            continue
        raw_key = key_for_date("raw", date_str)
        results.append((date_str, raw_key, path.read_bytes()))
    return results


def print_debug(snapshot, stitch_info: dict[str, Any] | None = None) -> None:
    processing = snapshot.processing
    edge = processing["edge_trim"]
    print(
        f"{snapshot.date}: "
        f"crop={processing['chart_crop']} "
        f"left_trim={edge['left_trimmed_px']} "
        f"right_trim={edge['right_trimmed_px']} "
        f"black_ratio={processing['black_ratio']}"
    )
    if stitch_info:
        print(
            f"  stitch status={stitch_info.get('status')} "
            f"overlap={stitch_info.get('overlap_px')} "
            f"append={stitch_info.get('append_width_px')} "
            f"score={stitch_info.get('match_score_mean_abs_difference')} "
            f"confidence={stitch_info.get('confidence')}"
        )


def rebuild_from_inputs(
    inputs: list[tuple[str, str, bytes]],
    *,
    public_base_url: str,
    captured_at: str,
) -> tuple[list[Any], Any, list[dict[str, Any]]]:
    snapshots = [
        build_processed_snapshot(
            date_str=date_str,
            raw_key=raw_key,
            raw_bytes=raw_bytes,
            public_base_url=public_base_url,
        )
        for date_str, raw_key, raw_bytes in inputs
    ]
    snapshots.sort(key=lambda snapshot: snapshot.date)
    timeline, stitch_steps = rebuild_stitched_timeline((snapshot.date, snapshot.image) for snapshot in snapshots)

    step_by_date = {step.get("date"): step for step in stitch_steps}
    for snapshot in snapshots:
        print_debug(snapshot, step_by_date.get(snapshot.date))

    return snapshots, timeline, stitch_steps


def run_local_dry_run(args) -> None:
    input_dir = Path(args.local_input_dir)
    out_dir = Path(args.out_dir)
    captured_at = utc_now_iso()
    public_base_url = args.public_base_url or "https://local.example"
    inputs = local_raw_inputs(input_dir)
    if not inputs:
        raise SystemExit(f"No dated raw images found in {input_dir}")

    snapshots, timeline, stitch_steps = rebuild_from_inputs(
        inputs,
        public_base_url=public_base_url,
        captured_at=captured_at,
    )

    for snapshot in snapshots:
        year, month, _ = snapshot.date.split("-")
        processed_path = out_dir / "schumann" / "tomsk" / "processed" / year / month / f"{snapshot.date}.webp"
        daily_path = out_dir / "schumann" / "tomsk" / "daily" / year / month / f"{snapshot.date}.webp"
        processed_path.parent.mkdir(parents=True, exist_ok=True)
        daily_path.parent.mkdir(parents=True, exist_ok=True)
        data = image_to_webp_bytes(snapshot.image)
        processed_path.write_bytes(data)
        daily_path.write_bytes(data)

    stitched_meta = None
    if timeline:
        stitched_key = "schumann/tomsk/stitched/timeline.webp"
        stitched_path = out_dir / stitched_key
        stitched_path.parent.mkdir(parents=True, exist_ok=True)
        stitched_bytes = image_to_webp_bytes(timeline)
        stitched_path.write_bytes(stitched_bytes)
        stitched_meta = {
            "key": stitched_key,
            "url": public_url(public_base_url, stitched_key),
            "updated_at": captured_at,
            "source": SOURCE,
            "last_source_date": snapshots[-1].date if snapshots else None,
            "width": timeline.width,
            "height": timeline.height,
            "mode": "rebuilt_from_raw_snapshots",
            "steps": stitch_steps,
            "status": "rebuilt",
        }

    manifest = manifest_base(public_base_url, captured_at)
    manifest["stitched"] = stitched_meta
    manifest["items"] = [
        {**item_from_processed_snapshot(snapshot, captured_at), "stitched": stitched_meta}
        for snapshot in reversed(snapshots)
    ]
    write_json(out_dir / "schumann" / "tomsk" / "manifest.json", manifest)
    print(f"Dry run complete: {out_dir}")


def run_r2_backfill(args) -> None:
    captured_at = utc_now_iso()
    bucket = os.environ["R2_BUCKET_NAME"]
    public_base_url = os.environ["R2_PUBLIC_BASE_URL"].rstrip("/")
    s3 = build_s3_client()
    raw_keys = list_raw_r2_keys(s3, bucket)
    if not raw_keys:
        raise SystemExit("No R2 raw snapshots found under schumann/tomsk/raw/")

    inputs = [
        (date_from_raw_key(key), key, get_r2_object_bytes(s3, bucket, key))
        for key in raw_keys
        if date_from_raw_key(key)
    ]
    snapshots, timeline, stitch_steps = rebuild_from_inputs(
        inputs,
        public_base_url=public_base_url,
        captured_at=captured_at,
    )

    for snapshot in snapshots:
        data = image_to_webp_bytes(snapshot.image)
        if args.write_processed or not r2_key_exists(s3, bucket, snapshot.processed_key):
            put_r2_object_bytes(s3, bucket, snapshot.processed_key, data, "image/webp")
        if args.write_processed or not r2_key_exists(s3, bucket, snapshot.daily_key):
            put_r2_object_bytes(s3, bucket, snapshot.daily_key, data, "image/webp")

    stitched_meta = None
    if timeline:
        stitched_key = "schumann/tomsk/stitched/timeline.webp"
        stitched_bytes = image_to_webp_bytes(timeline)
        put_r2_object_bytes(s3, bucket, stitched_key, stitched_bytes, "image/webp")
        stitched_meta = {
            "key": stitched_key,
            "url": public_url(public_base_url, stitched_key),
            "updated_at": captured_at,
            "source": SOURCE,
            "last_source_date": snapshots[-1].date if snapshots else None,
            "width": timeline.width,
            "height": timeline.height,
            "mode": "rebuilt_from_raw_snapshots",
            "steps": stitch_steps,
            "status": "rebuilt",
        }

    manifest = load_existing_manifest(s3, bucket, public_base_url, captured_at)
    manifest["version"] = 2
    manifest["source"] = SOURCE
    manifest["updated_at"] = captured_at
    manifest["public_base_url"] = public_base_url
    manifest["processing_mode"] = "raw_3day_snapshot_plus_overlap_stitched_timeline"
    manifest["stitched"] = stitched_meta
    manifest["items"] = [
        {**item_from_processed_snapshot(snapshot, captured_at), "stitched": stitched_meta}
        for snapshot in reversed(snapshots)
    ][:400]

    manifest_bytes = json.dumps(manifest, indent=2).encode("utf-8")
    put_r2_object_bytes(s3, bucket, "schumann/tomsk/manifest.json", manifest_bytes, "application/json")
    print(f"Backfilled {len(snapshots)} raw snapshots into stitched timeline.")
    if stitched_meta:
        print(f"Stitched timeline: {stitched_meta['url']}")
    print(f"Manifest: {public_url(public_base_url, 'schumann/tomsk/manifest.json')}")


def parse_args():
    parser = argparse.ArgumentParser(description="Backfill Tomsk Schumann stitched timeline from raw snapshots.")
    parser.add_argument("--local-input-dir", help="Process local dated raw images instead of R2. Filenames must start YYYY-MM-DD.")
    parser.add_argument("--out-dir", default="out", help="Local output folder for --local-input-dir dry runs.")
    parser.add_argument("--public-base-url", default="", help="Public base URL used in dry-run manifest output.")
    parser.add_argument("--write-processed", action="store_true", help="Overwrite processed/daily images during R2 backfill.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.local_input_dir:
        run_local_dry_run(args)
    else:
        run_r2_backfill(args)


if __name__ == "__main__":
    main()
