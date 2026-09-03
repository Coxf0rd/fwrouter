from __future__ import annotations

from fwrouter_api.db.connection import db_session
from fwrouter_api.services.state_projection import build_rules_state_projection


def test_rules_projection_reports_missing_artifacts_without_writing_files(tmp_path) -> None:
    missing = tmp_path / "missing.json"
    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO rules_state (id, status, selective_default, effective_json_path, effective_text_path, metadata_path, error_code)
            VALUES (1, 'success', 'direct', ?, ?, ?, NULL)
            ON CONFLICT(id) DO UPDATE SET
                status = excluded.status,
                effective_json_path = excluded.effective_json_path,
                effective_text_path = excluded.effective_text_path,
                metadata_path = excluded.metadata_path,
                error_code = NULL
            """,
            (str(missing), str(tmp_path / "missing.txt"), str(tmp_path / "metadata.json")),
        )

    rules = build_rules_state_projection()["rules"]

    assert rules["observation"]["state"] == "missing"
    assert "effective_json_path" in rules["observation"]["evidence"]["missing_paths"]
    assert rules["reconcile"]["state"] == "observation_stale"


def test_rules_projection_reports_error_code_as_drift() -> None:
    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO rules_state (id, status, selective_default, error_code, error_message)
            VALUES (1, 'failed', 'direct', 'RULES_UPDATE_FAILED', 'failed')
            ON CONFLICT(id) DO UPDATE SET
                status = excluded.status,
                error_code = excluded.error_code,
                error_message = excluded.error_message
            """
        )

    rules = build_rules_state_projection()["rules"]

    assert rules["execution"]["state"] == "failed"
    assert rules["reconcile"]["state"] == "runtime_drift"
    assert rules["projection"]["state"] == "error"
