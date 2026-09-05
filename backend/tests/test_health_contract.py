from __future__ import annotations

from fwrouter_api.services.health_contract import max_health, normalize_health_state


def test_health_contract_inactive_and_disabled_do_not_degrade_system() -> None:
    assert max_health(["healthy", "inactive", "disabled"]) == "healthy"


def test_health_contract_unknown_is_not_failed() -> None:
    assert normalize_health_state("unknown") == "unknown"
    assert max_health(["healthy", "unknown"]) == "unknown"


def test_health_contract_stale_drift_and_failure_mapping() -> None:
    assert normalize_health_state("observation_stale") == "warning"
    assert normalize_health_state("runtime_drift") == "degraded"
    assert normalize_health_state("critical") == "failed"
