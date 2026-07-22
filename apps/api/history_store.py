from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import threading
import time
from typing import Any

from soccer_bot.prediction_history import (
    PredictionHistoryError,
    validate_prediction_history as validate_history_contract,
)


DEFAULT_HISTORY_PATH = Path("data/predictions/published_history_v1/latest.json")


class HistoryUnavailableError(RuntimeError):
    """Raised when no verified published history artifact is available."""


class HistoryValidationError(ValueError):
    """Raised when a published history artifact is unsafe to serve."""


class HistoryStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._cached_mtime_ns: int | None = None
        self._cached: dict[str, Any] | None = None

    def load(self) -> dict[str, Any]:
        try:
            mtime = self.path.stat().st_mtime_ns
        except OSError as error:
            raise HistoryUnavailableError("Published prediction history is not available yet") from error
        with self._lock:
            if self._cached is not None and self._cached_mtime_ns == mtime:
                return deepcopy(self._cached)
            try:
                value = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise HistoryUnavailableError("Published prediction history could not be read") from error
            validate_prediction_history(value)
            self._cached = value
            self._cached_mtime_ns = mtime
            return deepcopy(value)


class S3HistoryStore:
    def __init__(self, *, client: Any, bucket: str, key: str, cache_seconds: float = 30.0) -> None:
        self.client = client
        self.bucket = bucket
        self.key = key
        self.cache_seconds = cache_seconds
        self._lock = threading.Lock()
        self._cached: dict[str, Any] | None = None
        self._cached_etag: str | None = None
        self._refresh_after = 0.0

    def load(self) -> dict[str, Any]:
        now = time.monotonic()
        with self._lock:
            if self._cached is not None and now < self._refresh_after:
                return deepcopy(self._cached)
            try:
                response = self.client.get_object(Bucket=self.bucket, Key=self.key)
                body = response["Body"].read()
                etag = str(response.get("ETag", ""))
            except Exception as error:
                if self._cached is not None:
                    self._refresh_after = now + min(5.0, self.cache_seconds)
                    return deepcopy(self._cached)
                raise HistoryUnavailableError("Published prediction history could not be read from object storage") from error
            if self._cached is not None and etag and etag == self._cached_etag:
                self._refresh_after = now + self.cache_seconds
                return deepcopy(self._cached)
            try:
                value = json.loads(body)
            except (TypeError, UnicodeDecodeError, json.JSONDecodeError) as error:
                raise HistoryUnavailableError("Object-storage history is invalid JSON") from error
            validate_prediction_history(value)
            self._cached = value
            self._cached_etag = etag
            self._refresh_after = now + self.cache_seconds
            return deepcopy(value)


def validate_prediction_history(value: object) -> None:
    try:
        validate_history_contract(value)
    except PredictionHistoryError as error:
        raise HistoryValidationError(str(error)) from error
