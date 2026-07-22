#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from soccer_bot.prediction_history import validate_prediction_history


DEFAULT_SNAPSHOT = Path("data/predictions/published_history_v1/latest.json")
DEFAULT_KEY = "published_history_v1/latest.json"


def main() -> None:
    parser = argparse.ArgumentParser(description="Publish verified prediction history.")
    parser.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT)
    parser.add_argument("--bucket", default=os.environ.get("SOCCER_SNAPSHOT_S3_BUCKET"))
    parser.add_argument("--key", default=os.environ.get("SOCCER_HISTORY_S3_KEY", DEFAULT_KEY))
    parser.add_argument("--endpoint", default=os.environ.get("SOCCER_SNAPSHOT_S3_ENDPOINT"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if not args.bucket:
        parser.error("--bucket or SOCCER_SNAPSHOT_S3_BUCKET is required")
    raw = args.snapshot.read_bytes()
    artifact = json.loads(raw)
    validate_prediction_history(artifact)
    summary = {"bucket": args.bucket, "key": args.key, "fixture_count": artifact["fixture_count"], "history_rows_sha256": artifact["history_rows_sha256"]}
    if args.dry_run:
        print(json.dumps({**summary, "status": "validated_not_uploaded"}, indent=2))
        return
    import boto3
    client = boto3.client("s3", endpoint_url=args.endpoint, region_name=os.environ.get("AWS_DEFAULT_REGION", "auto"))
    client.put_object(Bucket=args.bucket, Key=args.key, Body=raw, ContentType="application/json", CacheControl="no-cache", Metadata={"rows-sha256": artifact["history_rows_sha256"]})
    stored = client.get_object(Bucket=args.bucket, Key=args.key)["Body"].read()
    if stored != raw:
        raise RuntimeError("uploaded prediction history failed read-back verification")
    validate_prediction_history(json.loads(stored))
    print(json.dumps({**summary, "status": "uploaded"}, indent=2))


if __name__ == "__main__":
    main()
