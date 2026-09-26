"""Base classes for FWRouter v2 backend jobs."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"


def utc_now() -> datetime:
    """Return a timezone-aware UTC timestamp for job state."""

    return datetime.now(UTC)
