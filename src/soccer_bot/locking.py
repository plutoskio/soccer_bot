from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import socket
import subprocess
import threading
import time


@dataclass(frozen=True)
class LockResult:
    acquired: bool
    reason: str | None = None
    owner: dict[str, object] | None = None


class CollectorLock:
    """Cross-process advisory lock acquired before writable DuckDB access."""

    def __init__(
        self,
        path: Path,
        *,
        stale_timeout_seconds: int = 900,
        heartbeat_interval_seconds: int = 30,
    ) -> None:
        if stale_timeout_seconds <= 0 or heartbeat_interval_seconds <= 0:
            raise ValueError("lock timing values must be positive")
        self.path = Path(path)
        self.stale_timeout_seconds = stale_timeout_seconds
        self.heartbeat_interval_seconds = heartbeat_interval_seconds
        self._handle = None
        self._metadata: dict[str, object] | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._write_guard = threading.Lock()

    def acquire(self) -> LockResult:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)
        handle = self.path.open("r+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            owner = self._read_metadata(handle)
            handle.close()
            return LockResult(False, "already_running", owner)

        now = datetime.now(timezone.utc).isoformat()
        self._handle = handle
        self._metadata = {
            "pid": os.getpid(),
            "hostname": socket.gethostname(),
            "process_start_marker": _process_start_marker(os.getpid()),
            "acquired_at": now,
            "heartbeat_at": now,
        }
        self._write_metadata()
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._heartbeat_loop,
            name="collector-lock-heartbeat",
            daemon=True,
        )
        self._thread.start()
        return LockResult(True)

    def release(self) -> None:
        if self._handle is None:
            return
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(1, self.heartbeat_interval_seconds + 1))
        with self._write_guard:
            self._handle.seek(0)
            self._handle.truncate()
            self._handle.flush()
            os.fsync(self._handle.fileno())
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            self._handle.close()
        self._handle = None
        self._metadata = None
        self._thread = None

    def __enter__(self):
        result = self.acquire()
        if not result.acquired:
            raise RuntimeError("collector is already running")
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.release()
        return False

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(self.heartbeat_interval_seconds):
            if self._metadata is None or self._handle is None:
                return
            self._metadata["heartbeat_at"] = datetime.now(timezone.utc).isoformat()
            self._write_metadata()

    def _write_metadata(self) -> None:
        if self._handle is None or self._metadata is None:
            return
        encoded = json.dumps(self._metadata, sort_keys=True, separators=(",", ":"))
        with self._write_guard:
            self._handle.seek(0)
            self._handle.write(encoded)
            self._handle.truncate()
            self._handle.flush()
            os.fsync(self._handle.fileno())

    @staticmethod
    def _read_metadata(handle) -> dict[str, object] | None:
        try:
            handle.seek(0)
            value = json.loads(handle.read() or "null")
            return value if isinstance(value, dict) else None
        except (OSError, json.JSONDecodeError):
            return None


def _process_start_marker(pid: int) -> str:
    try:
        result = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(pid)],
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
        marker = result.stdout.strip()
        if marker:
            return marker
    except (OSError, subprocess.SubprocessError):
        pass
    return f"monotonic-{time.monotonic_ns()}"
