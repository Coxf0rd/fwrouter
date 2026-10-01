from __future__ import annotations

from dataclasses import dataclass
import time


class ProviderError(RuntimeError):
    """Sanitized provider error safe for service-layer classification."""

    def __init__(self, code: str, *, retryable: bool = False, status_code: int | None = None,
                 retry_after_seconds: float | None = None) -> None:
        self.code = code
        self.retryable = retryable
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds
        super().__init__(code)


@dataclass
class RequestBudget:
    max_requests: int
    deadline_seconds: float
    started_at: float | None = None
    requests: int = 0
    operation: str = "provider_api"

    def __post_init__(self) -> None:
        self.start()

    def start(self, now: float | None = None) -> None:
        if self.started_at is None:
            self.started_at = time.monotonic() if now is None else now

    def consume(self, now: float | None = None) -> None:
        current = time.monotonic() if now is None else now
        self.start(current)
        assert self.started_at is not None
        if self.requests >= self.max_requests or current - self.started_at >= self.deadline_seconds:
            raise ProviderError("BUDGET_EXHAUSTED", retryable=True)
        self.requests += 1

    def remaining_seconds(self, now: float | None = None) -> float:
        current = time.monotonic() if now is None else now
        self.start(current)
        assert self.started_at is not None
        return max(0.0, self.deadline_seconds - (current - self.started_at))
