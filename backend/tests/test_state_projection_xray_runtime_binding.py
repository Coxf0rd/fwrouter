from __future__ import annotations

import json

from fwrouter_api.adapters.xray_common import XrayHealth, XrayRuntimeState
from fwrouter_api.core.config import get_settings
from fwrouter_api.db.connection import db_session
from fwrouter_api.services.artifacts import atomic_write_json
from fwrouter_api.services.state_projection import build_subject_state_projection, build_xray_state_projection


class _RunningXrayAdapter:
    def health(self) -> XrayHealth:
        return XrayHealth(
            runtime_state=XrayRuntimeState.RUNNING,
            message="xray running",
            details={"forced_vpn_ready": True},
        )


def _seed_active_xray_subject_with_pending_override() -> None:
    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO modules (module_name, desired_state, lifecycle_mode, runtime_state, apply_state)
            VALUES ('xray', 'enabled', 'managed', 'running', 'clean')
            ON CONFLICT(module_name) DO UPDATE SET
                desired_state = excluded.desired_state,
                lifecycle_mode = excluded.lifecycle_mode,
                runtime_state = excluded.runtime_state,
                apply_state = excluded.apply_state,
                error_code = NULL,
                error_message = NULL
            """
        )
        connection.execute(
            """
            INSERT INTO servers (server_id, server_name, inventory_state)
            VALUES ('server-1', 'server-1', 'active')
            ON CONFLICT(server_id) DO UPDATE SET inventory_state = 'active'
            """
        )
        connection.execute(
            """
            INSERT INTO subjects (
                subject_id, subject_type, subject_role, implementation_kind, stable_key,
                display_name, desired_mode, applied_mode, apply_state, runtime_state, is_active, metadata_json
            )
            VALUES (
                'xray:runtime-binding', 'explicit_external_client', 'vless_client', 'xray', 'xray:runtime-binding',
                'Xray runtime binding', 'vpn', 'vpn', 'clean', 'active', 1, json(?)
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
                subject_id, selected_server_id, selected_until, apply_state, error_code
            )
            VALUES (
                'xray:runtime-binding',
                'server-1',
                datetime('now', '+1 day'),
                'pending',
                'SCOPED_RUNTIME_PENDING_INACTIVE_SUBJECT'
            )
            ON CONFLICT(subject_id) DO UPDATE SET
                selected_server_id = excluded.selected_server_id,
                selected_until = excluded.selected_until,
                apply_state = excluded.apply_state,
                error_code = excluded.error_code
            """
        )


def test_projection_xray_runtime_binding_applied_wins_over_stale_pending_db(monkeypatch) -> None:
    _seed_active_xray_subject_with_pending_override()
    monkeypatch.setattr(
        "fwrouter_api.services.state_projection.xray_adapter_module.DEFAULT_XRAY_ADAPTER",
        _RunningXrayAdapter(),
    )
    monkeypatch.setattr(
        "fwrouter_api.services.state_projection.build_runtime_enforcement_state",
        lambda **_: {
            "supported_modes": {"direct": True, "selective": True, "vpn": True},
            "traffic_enforcement_guaranteed": True,
            "enforcement_level": "global_selective_enforced",
            "active_mode_matches_intent": True,
        },
    )
    atomic_write_json(
        get_settings().paths.state_dir / "xray" / "fwrouter-bindings.json",
        {
            "bindings_version": 1,
            "generated_at": "2026-09-04T00:00:00Z",
            "bindings_count": 1,
            "applied_count": 1,
            "bindings": [
                {
                    "subject_id": "xray:runtime-binding",
                    "selected_server_id": "server-1",
                    "status": "applied",
                }
            ],
        },
    )

    subject = build_subject_state_projection(subject_id="xray:runtime-binding")["subject"]
    xray = build_xray_state_projection()["xray"]

    assert subject["execution"]["details"]["server_override"]["apply_state"] == "pending"
    assert subject["effective"]["scoped_runtime_status"] == "applied"
    assert subject["reconcile"]["state"] == "in_sync"
    assert xray["observation"]["evidence"]["active_bound_count"] == 1
    assert xray["reconcile"]["state"] == "in_sync"
