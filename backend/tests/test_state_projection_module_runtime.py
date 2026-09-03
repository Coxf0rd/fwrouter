from __future__ import annotations

from fwrouter_api.db.connection import db_session
from fwrouter_api.services.state_projection import build_module_state_projection


def test_projection_module_runtime_uses_probe_over_database(monkeypatch) -> None:
    with db_session() as connection:
        connection.execute(
            """
            UPDATE modules
            SET desired_state = 'enabled',
                runtime_state = 'running',
                apply_state = 'clean',
                error_code = NULL
            WHERE module_name = 'core'
            """
        )
    monkeypatch.setattr(
        "fwrouter_api.services.state_projection._module_runtime_context",
        lambda: {
            "core": {
                "observed_state": "stopped",
                "source": "dataplane_probe",
                "observed_at": "2026-09-04T00:00:00Z",
                "stale_after_seconds": 300,
                "evidence": {"enforcement_level": "owned_table_missing"},
            }
        },
    )

    modules = {item["entity"]["id"]: item for item in build_module_state_projection()["items"]}

    core = modules["core"]
    assert core["intent"]["state"] == "enabled"
    assert core["observation"]["state"] == "stopped"
    assert core["observation"]["source"] == "dataplane_probe"
    assert core["effective"]["observed_state"] == "stopped"
    assert core["effective"]["stale_after"] == "2026-09-04T00:05:00Z"
    assert core["reconcile"]["state"] == "observation_stale"
