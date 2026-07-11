from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from soccer_bot.locking import CollectorLock  # noqa: E402


class CollectorLockTests(unittest.TestCase):
    def test_contention_reports_active_owner_without_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "collector.lock"
            first = CollectorLock(path, heartbeat_interval_seconds=1)
            self.assertTrue(first.acquire().acquired)
            try:
                second = CollectorLock(path)
                result = second.acquire()
                self.assertFalse(result.acquired)
                self.assertEqual("already_running", result.reason)
                self.assertEqual(os.getpid(), result.owner["pid"])
                self.assertEqual(
                    {
                        "pid", "hostname", "process_start_marker",
                        "acquired_at", "heartbeat_at",
                    },
                    set(result.owner),
                )
            finally:
                first.release()
            self.assertEqual("", path.read_text())

    def test_crashed_owner_is_reclaimed_by_advisory_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "collector.lock"
            code = (
                "import os,sys; from pathlib import Path; "
                "sys.path.insert(0,sys.argv[2]); "
                "from soccer_bot.locking import CollectorLock; "
                "lock=CollectorLock(Path(sys.argv[1])); "
                "assert lock.acquire().acquired; os._exit(0)"
            )
            subprocess.run(
                [sys.executable, "-c", code, str(path), str(ROOT / "src")],
                check=True,
            )
            replacement = CollectorLock(path)
            self.assertTrue(replacement.acquire().acquired)
            replacement.release()

    def test_heartbeat_advances(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "collector.lock"
            lock = CollectorLock(path, heartbeat_interval_seconds=1)
            lock.acquire()
            try:
                before = json.loads(path.read_text())["heartbeat_at"]
                time.sleep(1.2)
                after = json.loads(path.read_text())["heartbeat_at"]
                self.assertNotEqual(before, after)
            finally:
                lock.release()


if __name__ == "__main__":
    unittest.main()
