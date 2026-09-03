from __future__ import annotations

import json

from fwrouter_api.adapters.xray_common import XrayHealth, XrayRuntimeState
from fwrouter_api.core.config import get_settings
from fwrouter_api.db.connection import db_session
from fwrouter_api.services.artifacts import atomic_write_json
from fwrouter_api.services.state_projection import build_subject_state_projection, build_xray_state_projection


class _XrayAdapter:
    def health(self) -> XrayHealth:
        return XrayHealth(
            runtime_state=XrayRuntimeState.RUNNING,
            message="xray running",
            details={"forced_vpn_ready": True},
        )


def _seed_xray_subject() -> None:
    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO servers (server_id, server_name, inventory_state)
            VALUES ('server-1', 'server-1', 'active')
            """
        )
        connection.execute(
            """
            INSERT INTO subjects (
                subject_id, subject_type, subject_role, implementation_kind, stable_key,
                display_name, desired_mode, apply_state, runtime_state, is_active, metadata_json
            )
            VALUES (
                'xray:test', 'explicit_external_client', 'vless_client', 'xray', 'xray:test',
                'Xray test', 'vpn', 'clean', 'active', 1, json(?)
            )
            """,
            (
                json.dumps(
                    {
                        "provider": "xray",
                        "detail": {
                            "client_id": "client-1",
                            "client_uuid": "uuid-1",
                            "email": "client-1@fwrouter.local",
                            "enabled": True,
                        },
                    },
                    sort_keys=True,
                ),
            ),
        )
        connection.execute(
            """
            INSERT INTO subject_server_overrides (
                subject_id, selected_server_id, selected_until, apply_state
            )
            VALUES ('xray:test', 'server-1', datetime('now', '+1 day'), 'clean')
            """
        )


def test_xray_projection_reports_runtime_and_binding_summary(monkeypatch) -> None:
    monkeypatch.setattr("fwrouter_api.services.state_projection.xray_adapter_module.DEFAULT_XRAY_ADAPTER", _XrayAdapter())
    atomic_write_json(
        get_settings().paths.state_dir / "xray" / "fwrouter-bindings.json",
        {
            "bindings_version": 1,
            "generated_at": "2026-09-03T00:00:00Z",
            "bindings_count": 1,
            "applied_count": 1,
            "bindings": [{"subject_id": "xray:test", "selected_server_id": "server-1", "status": "applied"}],
        },
    )

    xray = build_xray_state_projection()["xray"]

    assert xray["observation"]["state"] == "running"
    assert xray["observation"]["evidence"]["bindings_count"] == 1
    assert xray["reconcile"]["state"] == "in_sync"


def test_active_xray_subject_without_binding_is_not_clean_runtime(monkeypatch) -> None:
    _seed_xray_subject()
    monkeypatch.setattr(
        "fwrouter_api.services.state_projection.build_runtime_enforcement_state",
        lambda **_: {
            "supported_modes": {"direct": True, "selective": True, "vpn": True},
            "traffic_enforcement_guaranteed": True,
            "enforcement_level": "global_vpn_enforced",
            "active_mode_matches_intent": True,
        },
    )

    subject = build_subject_state_projection(subject_id="xray:test")["subject"]

    assert subject["execution"]["state"] == "idle"
    assert subject["reconcile"]["state"] == "intent_newer_than_runtime"
    assert subject["projection"]["state"] == "warning"
