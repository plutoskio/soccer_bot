from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import random
import time
from typing import Callable, Generic, TypeVar

from .http import HttpResponse, NetworkError


T = TypeVar("T")
H = TypeVar("H")


RETRYABLE_HTTP_STATUSES = frozenset({429, 500, 502, 503, 504})


@dataclass(frozen=True)
class RequestAttempt:
    number: int
    classification: str
    http_status: int | None
    retry_after_seconds: int | None


@dataclass(frozen=True)
class RequestResult(Generic[T, H]):
    value: T
    response: HttpResponse
    hook_value: H | None
    attempts: tuple[RequestAttempt, ...]


class ProviderResponseError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.retryable = retryable


class RequestExecutionError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        classification: str,
        http_status: int | None = None,
        retry_after_seconds: int | None = None,
        hook_value: object | None = None,
        attempts: tuple[RequestAttempt, ...] = (),
    ) -> None:
        super().__init__(message)
        self.classification = classification
        self.http_status = http_status
        self.retry_after_seconds = retry_after_seconds
        self.hook_value = hook_value
        self.attempts = attempts


class RequestExecutor:
    def __init__(
        self,
        *,
        maximum_attempts: int = 3,
        maximum_inline_retry_seconds: float = 5.0,
        backoff_base_seconds: float = 1.0,
        backoff_cap_seconds: float = 60.0,
        jitter_seconds: float = 0.25,
        sleep: Callable[[float], None] = time.sleep,
        random_value: Callable[[], float] = random.random,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        if maximum_attempts <= 0:
            raise ValueError("maximum_attempts must be positive")
        if min(
            maximum_inline_retry_seconds,
            backoff_base_seconds,
            backoff_cap_seconds,
            jitter_seconds,
        ) < 0:
            raise ValueError("retry timing values must not be negative")
        self.maximum_attempts = maximum_attempts
        self.maximum_inline_retry_seconds = maximum_inline_retry_seconds
        self.backoff_base_seconds = backoff_base_seconds
        self.backoff_cap_seconds = backoff_cap_seconds
        self.jitter_seconds = jitter_seconds
        self.sleep = sleep
        self.random_value = random_value
        self.now = now

    def execute(
        self,
        request: Callable[[], HttpResponse],
        *,
        validate: Callable[[HttpResponse], T],
        response_hook: Callable[[HttpResponse], H] | None = None,
        attempt_hook: Callable[[RequestAttempt, H | None], None] | None = None,
    ) -> RequestResult[T, H]:
        attempts: list[RequestAttempt] = []
        elapsed_inline = 0.0
        for number in range(1, self.maximum_attempts + 1):
            response: HttpResponse | None = None
            hook_value: H | None = None
            try:
                response = request()
                if response_hook is not None:
                    hook_value = response_hook(response)
                classification = classify_http_status(response.status)
                retry_after = parse_retry_after(
                    response.headers.get("retry-after"), now=self.now()
                )
                if classification != "succeeded":
                    raise RequestExecutionError(
                        safe_http_error(classification, response.status),
                        classification=classification,
                        http_status=response.status,
                        retry_after_seconds=retry_after,
                        hook_value=hook_value,
                    )
                value = validate(response)
                succeeded = RequestAttempt(number, "succeeded", response.status, None)
                attempts.append(succeeded)
                if attempt_hook is not None:
                    attempt_hook(succeeded, hook_value)
                return RequestResult(value, response, hook_value, tuple(attempts))
            except NetworkError as error:
                failure = RequestExecutionError(
                    "network request failed",
                    classification="retryable_error",
                )
            except ProviderResponseError as error:
                failure = RequestExecutionError(
                    str(error),
                    classification=(
                        "retryable_error" if error.retryable else "permanent_error"
                    ),
                    http_status=response.status if response else None,
                    hook_value=hook_value,
                )
            except RequestExecutionError as error:
                failure = error

            failed = RequestAttempt(
                number,
                failure.classification,
                failure.http_status,
                failure.retry_after_seconds,
            )
            attempts.append(failed)
            if attempt_hook is not None:
                attempt_hook(failed, failure.hook_value)
            retryable = failure.classification in {"retryable_error", "rate_limited"}
            if not retryable or number >= self.maximum_attempts:
                raise self._with_attempts(failure, attempts)
            delay = (
                float(failure.retry_after_seconds)
                if failure.retry_after_seconds is not None
                else self._backoff(number)
            )
            if elapsed_inline + delay > self.maximum_inline_retry_seconds:
                raise self._with_attempts(failure, attempts)
            self.sleep(delay)
            elapsed_inline += delay
        raise AssertionError("unreachable request executor state")

    def _backoff(self, attempt_number: int) -> float:
        base = min(
            self.backoff_cap_seconds,
            self.backoff_base_seconds * (2 ** max(0, attempt_number - 1)),
        )
        return base + self.random_value() * self.jitter_seconds

    @staticmethod
    def _with_attempts(
        failure: RequestExecutionError, attempts: list[RequestAttempt]
    ) -> RequestExecutionError:
        return RequestExecutionError(
            str(failure),
            classification=failure.classification,
            http_status=failure.http_status,
            retry_after_seconds=failure.retry_after_seconds,
            hook_value=failure.hook_value,
            attempts=tuple(attempts),
        )


def classify_http_status(status: int) -> str:
    if 200 <= status < 300:
        return "succeeded"
    if status == 429:
        return "rate_limited"
    if status in RETRYABLE_HTTP_STATUSES:
        return "retryable_error"
    if 400 <= status < 500:
        return "permanent_error"
    return "retryable_error"


def parse_retry_after(value: str | None, *, now: datetime | None = None) -> int | None:
    if not value:
        return None
    value = value.strip()
    try:
        return max(0, int(value))
    except ValueError:
        pass
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return max(0, int((parsed.astimezone(timezone.utc) - current).total_seconds()))


def safe_http_error(classification: str, status: int) -> str:
    if classification == "rate_limited":
        return f"provider rate limited request (HTTP {status})"
    if classification == "permanent_error":
        return f"provider rejected request (HTTP {status})"
    return f"provider temporarily failed request (HTTP {status})"
