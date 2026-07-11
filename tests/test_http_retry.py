from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from soccer_bot.http import HttpResponse, NetworkError  # noqa: E402
from soccer_bot.request_executor import (  # noqa: E402
    RequestExecutionError,
    RequestExecutor,
    classify_http_status,
    parse_retry_after,
)


class RequestExecutorTests(unittest.TestCase):
    @staticmethod
    def response(status, body=None, headers=None):
        return HttpResponse(
            "https://provider.invalid/test",
            status,
            headers or {"content-type": "application/json"},
            json.dumps(body or {}).encode(),
        )

    def test_classifies_retryable_and_permanent_http_statuses(self):
        self.assertEqual("rate_limited", classify_http_status(429))
        self.assertEqual("retryable_error", classify_http_status(503))
        self.assertEqual("permanent_error", classify_http_status(401))
        self.assertEqual("succeeded", classify_http_status(200))

    def test_retry_after_seconds_and_http_date(self):
        now = datetime(2026, 7, 11, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(12, parse_retry_after("12", now=now))
        self.assertEqual(
            30,
            parse_retry_after("Sat, 11 Jul 2026 00:00:30 GMT", now=now),
        )

    def test_429_retries_inline_and_records_each_response_hook(self):
        responses = [
            self.response(429, headers={"retry-after": "1"}),
            self.response(200, {"ok": True}),
        ]
        sleeps = []
        stored = []
        executor = RequestExecutor(
            maximum_attempts=3,
            maximum_inline_retry_seconds=2,
            jitter_seconds=0,
            sleep=sleeps.append,
        )
        result = executor.execute(
            lambda: responses.pop(0),
            validate=lambda response: response.json(),
            response_hook=lambda response: stored.append(response.status),
        )
        self.assertEqual({"ok": True}, result.value)
        self.assertEqual([429, 200], stored)
        self.assertEqual([1.0], sleeps)
        self.assertEqual(["rate_limited", "succeeded"], [a.classification for a in result.attempts])

    def test_long_retry_after_is_deferred_without_sleep(self):
        executor = RequestExecutor(
            maximum_attempts=3,
            maximum_inline_retry_seconds=2,
            sleep=lambda value: self.fail("must not sleep"),
        )
        with self.assertRaises(RequestExecutionError) as caught:
            executor.execute(
                lambda: self.response(429, headers={"retry-after": "60"}),
                validate=lambda response: response.json(),
            )
        self.assertEqual("rate_limited", caught.exception.classification)
        self.assertEqual(60, caught.exception.retry_after_seconds)

    def test_network_failure_has_no_fake_response_hook(self):
        called = []
        executor = RequestExecutor(maximum_attempts=1)
        with self.assertRaises(RequestExecutionError) as caught:
            executor.execute(
                lambda: (_ for _ in ()).throw(NetworkError("secret host detail")),
                validate=lambda response: response.json(),
                response_hook=lambda response: called.append(response),
            )
        self.assertEqual([], called)
        self.assertEqual("network request failed", str(caught.exception))

    def test_permanent_4xx_is_not_retried(self):
        calls = []
        executor = RequestExecutor(maximum_attempts=3)
        with self.assertRaises(RequestExecutionError) as caught:
            executor.execute(
                lambda: calls.append(1) or self.response(403),
                validate=lambda response: response.json(),
            )
        self.assertEqual(1, len(calls))
        self.assertEqual("permanent_error", caught.exception.classification)


if __name__ == "__main__":
    unittest.main()
