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


def recovery_evidence_code(error: "ProviderError") -> str:
    """Return a stable recovery code without treating API failure as member DOWN."""
    code = str(error.code or "").upper()
    if code in {"PROVIDER_TIMEOUT", "TIMEOUT", "APITIMEOUT"}:
        return "provider_api_timeout"
    if code in {"RATE_LIMITED", "PROVIDER_RATE_LIMIT", "PROVIDER_RATE_LIMITED"} or error.status_code == 429:
        return "provider_api_rate_limited"
    if code in {"PROVIDER_TRANSPORT_ERROR", "PROVIDER_UNAVAILABLE", "PROVIDER_CONNECT_ERROR"}:
        if error.status_code is not None and error.status_code >= 500:
            return "provider_api_5xx"
        return "provider_api_unreachable"
    if code == "PROVIDER_AUTH_FAILED" or error.status_code in {401, 403}:
        return "provider_api_auth_failed"
    if error.status_code is not None and error.status_code >= 500:
        return "provider_api_5xx"
    if code == "PROVIDER_REQUEST_REJECTED" or (error.status_code is not None and error.status_code >= 400):
        return "provider_api_request_rejected"
    if code in {"PROVIDER_INVALID_RESPONSE", "PROVIDER_OPERATION_FAILED", "MUTATION_RESULT_INCOMPLETE"}:
        return "provider_response_unknown"
    return "provider_response_unknown"


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
