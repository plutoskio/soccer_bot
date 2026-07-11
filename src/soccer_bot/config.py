from __future__ import annotations

import json
import os
from pathlib import Path


def load_env(path: Path) -> dict[str, str]:
    """Load local dotenv values, overridden by process environment variables."""
    values: dict[str, str] = {}

    if path.exists():
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()

            if not line or line.startswith("#") or "=" not in line:
                continue

            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()

            if (
                len(value) >= 2
                and value[0] == value[-1]
                and value[0] in {"'", '"'}
            ):
                value = value[1:-1]

            values[key] = value

    values.update(os.environ)
    return values


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))

