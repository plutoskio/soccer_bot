from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import gzip
import hashlib
import json
from pathlib import Path
from typing import Mapping

from .http import HttpResponse


SAFE_RESPONSE_HEADERS = {
    "content-encoding",
    "content-type",
    "content-length",
    "date",
    "etag",
    "last-modified",
    "retry-after",
    "x-ratelimit-limit",
    "x-ratelimit-remaining",
    "x-ratelimit-requests-limit",
    "x-ratelimit-requests-remaining",
}


@dataclass(frozen=True)
class StoredArtifact:
    content_sha256: str
    data_path: Path
    metadata_path: Path
    duplicate: bool


class RawArtifactStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def store(
        self,
        *,
        source: str,
        resource: str,
        response: HttpResponse,
        request_params: Mapping[str, object] | None = None,
    ) -> StoredArtifact:
        retrieved_at = datetime.now(timezone.utc)
        digest = hashlib.sha256(response.body).hexdigest()
        artifact_dir = (
            self.root
            / source
            / resource
            / f"ingest_date={retrieved_at.date().isoformat()}"
        )
        artifact_dir.mkdir(parents=True, exist_ok=True)
        extension = self._extension(response)
        data_path = artifact_dir / f"{digest}.{extension}.gz"
        duplicate = data_path.exists()
        if not duplicate:
            with gzip.GzipFile(
                filename=str(data_path), mode="wb", compresslevel=6, mtime=0
            ) as handle:
                handle.write(response.body)

        observation_id = hashlib.sha256(
            f"{retrieved_at.isoformat()}:{source}:{resource}:{digest}".encode("utf-8")
        ).hexdigest()[:20]
        metadata_path = artifact_dir / f"{digest}.{observation_id}.meta.json"
        safe_headers = {
            key: value
            for key, value in response.headers.items()
            if key.lower() in SAFE_RESPONSE_HEADERS
        }
        metadata = {
            "source": source,
            "resource": resource,
            "retrieved_at": retrieved_at.isoformat(),
            "request_url": response.url,
            "request_parameters": dict(request_params or {}),
            "http_status": response.status,
            "response_headers": safe_headers,
            "content_sha256": digest,
            "uncompressed_bytes": len(response.body),
            "data_path": str(data_path),
            "duplicate_content": duplicate,
        }
        metadata_path.write_text(
            json.dumps(metadata, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        return StoredArtifact(digest, data_path, metadata_path, duplicate)

    @staticmethod
    def _extension(response: HttpResponse) -> str:
        content_type = response.headers.get("content-type", "").lower()
        url_path = response.url.split("?", 1)[0].lower()
        if "json" in content_type or url_path.endswith(".json"):
            return "json"
        if "csv" in content_type or url_path.endswith(".csv"):
            return "csv"
        if "html" in content_type or url_path.endswith((".html", ".htm")):
            return "html"
        return "bin"
