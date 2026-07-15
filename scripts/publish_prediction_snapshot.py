#!/usr/bin/env python3
"""Publish one validated prediction snapshot to S3-compatible object storage."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from apps.api.snapshot_store import validate_snapshot


DEFAULT_SNAPSHOT = Path("data/predictions/regulation_champion_v1/latest.json")
DEFAULT_KEY = "regulation_champion_v1/latest.json"


def upload_and_verify(client, *, bucket: str, key: str, raw: bytes, snapshot: dict) -> None:
    """Replace the object, then prove the stored bytes are the validated candidate."""

    client.put_object(
        Bucket=bucket,
        Key=key,
        Body=raw,
        ContentType="application/json",
        CacheControl="no-cache",
        Metadata={
            "model-version": snapshot["model_version"],
            "rows-sha256": snapshot["prediction_rows_sha256"],
        },
    )
    stored = client.get_object(Bucket=bucket, Key=key)["Body"].read()
    if stored != raw:
        raise RuntimeError("uploaded snapshot failed byte-for-byte read-back verification")
    validate_snapshot(json.loads(stored))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate and atomically publish the latest prediction JSON object."
    )
    parser.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT)
    parser.add_argument(
        "--bucket", default=os.environ.get("SOCCER_SNAPSHOT_S3_BUCKET")
    )
    parser.add_argument(
        "--key", default=os.environ.get("SOCCER_SNAPSHOT_S3_KEY", DEFAULT_KEY)
    )
    parser.add_argument(
        "--endpoint", default=os.environ.get("SOCCER_SNAPSHOT_S3_ENDPOINT")
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if not args.bucket:
        parser.error("--bucket or SOCCER_SNAPSHOT_S3_BUCKET is required")

    raw = args.snapshot.read_bytes()
    snapshot = json.loads(raw)
    validate_snapshot(snapshot)
    summary = {
        "bucket": args.bucket,
        "key": args.key,
        "model_version": snapshot["model_version"],
        "as_of": snapshot["as_of"],
        "prediction_rows": len(snapshot["predictions"]),
        "prediction_rows_sha256": snapshot["prediction_rows_sha256"],
    }
    if args.dry_run:
        print(json.dumps({**summary, "status": "validated_not_uploaded"}, indent=2))
        return

    import boto3

    client = boto3.client(
        "s3",
        endpoint_url=args.endpoint,
        region_name=os.environ.get("AWS_DEFAULT_REGION", "auto"),
    )
    upload_and_verify(
        client,
        bucket=args.bucket,
        key=args.key,
        raw=raw,
        snapshot=snapshot,
    )
    print(json.dumps({**summary, "status": "uploaded"}, indent=2))


if __name__ == "__main__":
    main()
