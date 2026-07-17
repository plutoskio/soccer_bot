#!/usr/bin/env python3
"""Download and validate one champion snapshot from S3-compatible storage."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from apps.api.snapshot_store import validate_snapshot
from scripts.publish_prediction_snapshot import DEFAULT_KEY


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read, validate, and atomically save the current champion snapshot."
    )
    parser.add_argument(
        "--bucket", default=os.environ.get("SOCCER_SNAPSHOT_S3_BUCKET")
    )
    parser.add_argument(
        "--key", default=os.environ.get("SOCCER_SNAPSHOT_S3_KEY", DEFAULT_KEY)
    )
    parser.add_argument(
        "--endpoint", default=os.environ.get("SOCCER_SNAPSHOT_S3_ENDPOINT")
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=(
            ROOT
            / "data"
            / "predictions"
            / "regulation_champion_v1"
            / "production_latest.json"
        ),
    )
    args = parser.parse_args()
    if not args.bucket:
        parser.error("--bucket or SOCCER_SNAPSHOT_S3_BUCKET is required")
    return args


def main() -> int:
    args = parse_args()
    import boto3

    client = boto3.client(
        "s3",
        endpoint_url=args.endpoint,
        region_name=os.environ.get("AWS_DEFAULT_REGION", "auto"),
    )
    raw = client.get_object(Bucket=args.bucket, Key=args.key)["Body"].read()
    snapshot = json.loads(raw)
    validate_snapshot(snapshot)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_name(f".{args.output.name}.tmp")
    temporary.write_bytes(raw)
    os.replace(temporary, args.output)
    print(
        json.dumps(
            {
                "status": "downloaded_and_validated",
                "output": str(args.output.resolve()),
                "as_of": snapshot["as_of"],
                "model_version": snapshot["model_version"],
                "prediction_rows": len(snapshot["predictions"]),
                "prediction_rows_sha256": snapshot["prediction_rows_sha256"],
                "object_bytes_sha256": hashlib.sha256(raw).hexdigest(),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
