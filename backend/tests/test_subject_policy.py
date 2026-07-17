from __future__ import annotations

from pathlib import Path

from fwrouter_api.core.config import get_settings
from fwrouter_api.db.connection import db_session, initialize_database
from fwrouter_api.services.subject_policy import get_subject_with_effective_state


def _configure_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("FWROUTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("FWROUTER_DATABASE_URL", f"sqlite:///{tmp_path}/fwrouter.db")
    get_settings.cache_clear()


def _seed_subject(subject_id: str, *, desired_mode: str = "global") -> None:
    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO subjects (
                subject_id,
                subject_type,
                stable_key,
                display_name,
                desired_mode,
                applied_mode,
                runtime_state,
                is_active
            )
            VALUES (?, 'lan', ?, ?, ?, ?, 'active', 1)
            """,
            (subject_id, subject_id, subject_id, desired_mode, desired_mode),
        )
        connection.execute(
            """
            INSERT INTO subject_lan (
                subject_id,
                mac_address,
                ip_address,
                hostname,
                dhcp_hostname
            )
            VALUES (?, 'aa:bb:cc:dd:ee:ff', '192.168.10.4', ?, 'pytest')
            """,
            (subject_id, subject_id),
        )


def _seed_server(server_id: str) -> None:
    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO servers (
                server_id,
                server_name,
                provider_name,
                inventory_state,
                raw_json
            )
            VALUES (?, ?, 'provider', 'active', '{}')
            """,
            (server_id, server_id),
        )
        connection.execute(
            """
            INSERT INTO server_preferences (
                server_id,
                vpn_auto,
                global_list
            )
            VALUES (?, 1, 1)
            """,
            (server_id,),
        )


def _seed_routing_state(
    *,
    desired_mode: str,
    selective_default: str = "direct",
    server_mode: str = "auto",
    desired_fixed_server_id: str | None = None,
    applied_fixed_server_id: str | None = None,
    active_auto_server_id: str | None = None,
) -> None:
    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO routing_global_state (
                id,
                desired_mode,
                applied_mode,
                selective_default,
                server_mode,
                desired_fixed_server_id,
                applied_fixed_server_id,
                active_auto_server_id,
                apply_state
            )
            VALUES (1, ?, ?, ?, ?, ?, ?, ?, 'clean')
            ON CONFLICT(id) DO UPDATE SET
                desired_mode = excluded.desired_mode,
                applied_mode = excluded.applied_mode,
                selective_default = excluded.selective_default,
                server_mode = excluded.server_mode,
                desired_fixed_server_id = excluded.desired_fixed_server_id,
                applied_fixed_server_id = excluded.applied_fixed_server_id,
                active_auto_server_id = excluded.active_auto_server_id,
                apply_state = 'clean',
                error_code = NULL,
                error_message = NULL,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                desired_mode,
                desired_mode,
                selective_default,
                server_mode,
                desired_fixed_server_id,
                applied_fixed_server_id,
                active_auto_server_id,
            ),
        )


def _seed_user_override(subject_id: str, *, override_mode: str, ttl_hours: int) -> None:
    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO subject_user_overrides (
                subject_id,
                override_mode,
                override_until,
                created_by
            )
            VALUES (
                ?,
                ?,
                datetime('now', '+' || ? || ' hours'),
                'pytest'
            )
            ON CONFLICT(subject_id) DO UPDATE SET
                override_mode = excluded.override_mode,
                override_until = excluded.override_until,
                created_by = excluded.created_by,
                updated_at = CURRENT_TIMESTAMP
            """,
            (subject_id, override_mode, ttl_hours),
        )


def _seed_server_override(subject_id: str, *, server_id: str, ttl_hours: int) -> None:
    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO subject_server_overrides (
                subject_id,
                selected_server_id,
                selected_until,
                apply_state
            )
            VALUES (
                ?,
                ?,
                datetime('now', '+' || ? || ' hours'),
                'pending'
            )
            ON CONFLICT(subject_id) DO UPDATE SET
                selected_server_id = excluded.selected_server_id,
                selected_until = excluded.selected_until,
                apply_state = 'pending',
                updated_at = CURRENT_TIMESTAMP
            """,
            (subject_id, server_id, ttl_hours),
        )


def test_selective_default_is_capture_fallback_only(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _seed_subject("lan-selective-direct", desired_mode="selective")
    _seed_routing_state(desired_mode="selective", selective_default="direct")

    monkeypatch.setattr(
        "fwrouter_api.services.subject_policy._default_subject_runtime_enforcement",
        lambda **kwargs: {"supported_modes": {"direct": True, "selective": True, "vpn": True}},
    )

    subject = get_subject_with_effective_state("lan-selective-direct")
    assert subject is not None
    state = subject["effective_state"]
    assert state["capture_mode"] == "selective"
    assert state["selective_default"] == "direct"
    assert state["vpn_target_id"] is None
    assert state["vpn_target_source"] is None
    assert state["selected_server_source"] == "direct"


def test_selective_default_vpn_keeps_vpn_bound_and_no_server_target(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _seed_subject("lan-selective-vpn", desired_mode="selective")
    _seed_routing_state(desired_mode="selective", selective_default="vpn")

    monkeypatch.setattr(
        "fwrouter_api.services.subject_policy._default_subject_runtime_enforcement",
        lambda **kwargs: {"supported_modes": {"direct": True, "selective": True, "vpn": True}},
    )

    subject = get_subject_with_effective_state("lan-selective-vpn")
    assert subject is not None
    state = subject["effective_state"]
    assert state["capture_mode"] == "selective"
    assert state["selective_default"] == "vpn"
    assert state["vpn_target_id"] is None
    assert state["vpn_target_source"] is None
    assert state["selected_server_source"] == "vpn"


def test_subject_read_model_keeps_selective_when_global_mode_is_direct(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _seed_subject("lan-scoped-selective", desired_mode="selective")
    _seed_routing_state(desired_mode="direct", selective_default="direct")

    monkeypatch.setattr(
        "fwrouter_api.services.subject_policy._default_subject_runtime_enforcement",
        lambda **kwargs: {"supported_modes": {"direct": True, "selective": True, "vpn": True}},
    )

    subject = get_subject_with_effective_state("lan-scoped-selective")
    assert subject is not None
    state = subject["effective_state"]
    assert state["effective_mode"] == "selective"
    assert state["dataplane_path"] == "selective"
    assert state["selected_server_source"] == "direct"


def test_scoped_selective_not_demoted_when_global_selective_contract_is_degraded(
    monkeypatch,
    tmp_path: Path,
) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _seed_subject("lan-scoped-selective-degraded-global", desired_mode="selective")
    _seed_routing_state(desired_mode="direct", selective_default="direct")

    monkeypatch.setattr(
        "fwrouter_api.services.subject_policy._default_subject_runtime_enforcement",
        lambda **kwargs: {
            "supported_modes": {"direct": True, "selective": False, "vpn": True},
            "missing_runtime_requirements": ["dnsmasq_domain_selective_contract_not_ready"],
        },
    )

    subject = get_subject_with_effective_state("lan-scoped-selective-degraded-global")
    assert subject is not None
    state = subject["effective_state"]
    assert state["effective_mode"] == "selective"
    assert state["dataplane_path"] == "selective"
    assert state["selected_server_source"] == "direct"
    assert state["runtime_enforcement"]["supported_modes"]["selective"] is False
    assert state["scoped_runtime"]["status"] == "applied"
    assert state["scoped_runtime"]["resolution_reason"] == "subject_selective_runtime_materialized"


def test_vpn_target_priority_prefers_subject_override_over_global_fixed(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _seed_subject("lan-vpn-priority", desired_mode="vpn")
    _seed_server("server-fixed")
    _seed_server("server-user")
    _seed_routing_state(
        desired_mode="vpn",
        server_mode="fixed",
        desired_fixed_server_id="server-fixed",
        applied_fixed_server_id="server-fixed",
    )
    _seed_server_override("lan-vpn-priority", server_id="server-user", ttl_hours=24)

    monkeypatch.setattr(
        "fwrouter_api.services.subject_policy._default_subject_runtime_enforcement",
        lambda **kwargs: {"supported_modes": {"direct": True, "selective": True, "vpn": True}},
    )

    subject = get_subject_with_effective_state("lan-vpn-priority")
    assert subject is not None
    state = subject["effective_state"]
    assert state["capture_mode"] == "vpn"
    assert state["vpn_target_id"] == "server-user"
    assert state["vpn_target_source"] == "subject_override"
    assert state["selected_server_source"] == "subject_override"


def test_expired_user_mode_override_is_ignored(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _seed_subject("lan-user-expired", desired_mode="global")
    _seed_routing_state(desired_mode="direct")
    _seed_user_override("lan-user-expired", override_mode="vpn", ttl_hours=-1)

    monkeypatch.setattr(
        "fwrouter_api.services.subject_policy._default_subject_runtime_enforcement",
        lambda **kwargs: {"supported_modes": {"direct": True, "selective": True, "vpn": True}},
    )

    subject = get_subject_with_effective_state("lan-user-expired")
    assert subject is not None
    state = subject["effective_state"]
    assert state["capture_mode"] == "direct"
    assert state["mode_source"] == "global"


def test_expired_subject_server_override_is_ignored(monkeypatch, tmp_path: Path) -> None:
    _configure_env(monkeypatch, tmp_path)
    initialize_database()
    _seed_subject("lan-server-expired", desired_mode="vpn")
    _seed_routing_state(desired_mode="vpn")
    _seed_server("server-old")
    _seed_server_override("lan-server-expired", server_id="server-old", ttl_hours=-1)

    monkeypatch.setattr(
        "fwrouter_api.services.subject_policy._default_subject_runtime_enforcement",
        lambda **kwargs: {"supported_modes": {"direct": True, "selective": True, "vpn": True}},
    )

    subject = get_subject_with_effective_state("lan-server-expired")
    assert subject is not None
    state = subject["effective_state"]
    assert state["vpn_target_id"] == "vpn-global"
    assert state["vpn_target_source"] == "vpn_auto"
