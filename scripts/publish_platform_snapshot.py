#!/usr/bin/env python3
"""Publish one validated specialized-platform snapshot to object storage."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from soccer_bot.platform_snapshot import validate_platform_snapshot


DEFAULT_SNAPSHOT = Path("data/predictions/specialized_platform_v1/latest.json")
DEFAULT_KEY = "specialized_platform_v1/latest.json"


def upload_and_verify(client, *, bucket: str, key: str, raw: bytes, snapshot: dict) -> None:
    client.put_object(
        Bucket=bucket,
        Key=key,
        Body=raw,
        ContentType="application/json",
        CacheControl="no-cache",
        Metadata={
            "registry-version": snapshot["family_registry_version"],
            "rows-sha256": snapshot["state_rows_sha256"],
        },
    )
    stored = client.get_object(Bucket=bucket, Key=key)["Body"].read()
    if stored != raw:
        raise RuntimeError("uploaded platform snapshot failed byte-for-byte read-back verification")
    validate_platform_snapshot(json.loads(stored))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate and publish the specialized-platform JSON object."
    )
    parser.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT)
    parser.add_argument("--bucket", default=os.environ.get("SOCCER_SNAPSHOT_S3_BUCKET"))
    parser.add_argument(
        "--key",
        default=os.environ.get("SOCCER_PLATFORM_SNAPSHOT_S3_KEY", DEFAULT_KEY),
    )
    parser.add_argument("--endpoint", default=os.environ.get("SOCCER_SNAPSHOT_S3_ENDPOINT"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if not args.bucket:
        parser.error("--bucket or SOCCER_SNAPSHOT_S3_BUCKET is required")

    raw = args.snapshot.read_bytes()
    snapshot = json.loads(raw)
    validate_platform_snapshot(snapshot)
    summary = {
        "bucket": args.bucket,
        "key": args.key,
        "family_registry_version": snapshot["family_registry_version"],
        "as_of": snapshot["as_of"],
        "state_rows": len(snapshot["states"]),
        "state_rows_sha256": snapshot["state_rows_sha256"],
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
    upload_and_verify(client, bucket=args.bucket, key=args.key, raw=raw, snapshot=snapshot)
    print(json.dumps({**summary, "status": "uploaded"}, indent=2))


if __name__ == "__main__":
    main()
