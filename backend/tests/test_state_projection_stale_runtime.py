from __future__ import annotations

from datetime import UTC, datetime

from fwrouter_api.services.state_projection import compute_staleness


def test_projection_stale_runtime_computes_stale_after_and_age() -> None:
    result = compute_staleness(
        "2026-09-04T00:00:00Z",
        stale_after_seconds=60,
        now=datetime(2026, 9, 4, 0, 2, 0, tzinfo=UTC),
    )

    assert result["stale"] is True
    assert result["stale_after"] == "2026-09-04T00:01:00Z"
    assert result["age_seconds"] == 120


def test_projection_stale_runtime_treats_missing_timestamp_as_unknown_not_stale() -> None:
    result = compute_staleness(None, stale_after_seconds=60)

    assert result == {"stale": False, "stale_after": None, "age_seconds": None}
