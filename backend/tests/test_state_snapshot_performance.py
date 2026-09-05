from __future__ import annotations

from types import SimpleNamespace

from fwrouter_api.services import diagnostics, reconcile, state_snapshot
from fwrouter_api.services.live_probe_cache import clear_live_probe_cache, get_live_probe_cache
from fwrouter_api.services.reconcile import ReconcileResponse
from fwrouter_api.services.state_snapshot import StateSnapshot


def _projection_item(entity_type: str, entity_id: str, *, role: str | None = None) -> dict[str, object]:
    return {
        "entity": {"type": entity_type, "id": entity_id, "role": role},
        "intent": {"state": "enabled", "mode": "vpn", "target_id": "server-1"},
        "execution": {"state": "idle"},
        "observation": {
            "state": "running",
            "observed_at": "2026-09-05T00:00:00Z",
            "stale": False,
            "evidence": {"is_active": True},
        },
        "reconcile": {"state": "in_sync"},
        "projection": {"state": "healthy", "severity": "none"},
        "effective": {},
        "reason": {},
        "legacy": {"raw": {}},
    }


def test_live_probe_cache_reuses_value_and_force_refresh_bypasses() -> None:
    clear_live_probe_cache()
    calls = {"count": 0}

    def loader() -> dict[str, int]:
        calls["count"] += 1
        return {"count": calls["count"]}

    first = get_live_probe_cache("test.performance.force", ttl_seconds=60, loader=loader)
    second = get_live_probe_cache("test.performance.force", ttl_seconds=60, loader=loader)
    forced = get_live_probe_cache(
        "test.performance.force",
        ttl_seconds=60,
        loader=loader,
        force_refresh=True,
    )

    assert first == second
    assert forced == {"count": 2}
    assert calls["count"] == 2


def test_state_snapshot_reuses_live_probes_within_request(monkeypatch) -> None:
    clear_live_probe_cache()
    calls = {"dataplane": 0, "health": 0}

    def dataplane_probe(**kwargs):
        calls["dataplane"] += 1
        return {
            "checked_at": "2026-09-05T00:00:00Z",
            "traffic_enforcement_guaranteed": True,
            "active_mode_matches_intent": True,
            "enforcement_level": "active",
        }

    def live_payload_probe():
        calls["dataplane"] += 1
        return {"ok": True, "checked_at": "2026-09-05T00:00:00Z"}

    def health_probe(adapter):
        calls["health"] += 1
        return {
            "runtime_state": "running",
            "active_server_id": "server-1",
            "checked_at": "2026-09-05T00:00:00Z",
            "details": {},
        }

    def mihomo_model_probe():
        calls["health"] += 1
        return SimpleNamespace(
            runtime_state="running",
            active_server_id="server-1",
            message=None,
            details={},
        )

    monkeypatch.setattr(state_snapshot, "build_runtime_enforcement_state", dataplane_probe)
    monkeypatch.setattr(state_snapshot, "read_live_dataplane_payload", live_payload_probe)
    monkeypatch.setattr(state_snapshot, "adapter_health_snapshot", health_probe)
    monkeypatch.setattr(state_snapshot, "mihomo_health_model_snapshot", mihomo_model_probe)

    snapshot = StateSnapshot()

    assert snapshot.runtime_enforcement() == snapshot.runtime_enforcement()
    assert snapshot.live_dataplane_payload() == snapshot.live_dataplane_payload()
    assert snapshot.mihomo_health() == snapshot.mihomo_health()
    assert snapshot.xray_health() == snapshot.xray_health()

    assert snapshot.probe_counts["dataplane_runtime_enforcement"] == 1
    assert snapshot.probe_counts["live_dataplane_payload"] == 1
    assert snapshot.probe_counts["mihomo_health_model"] == 1
    assert snapshot.probe_counts["xray_health"] == 1
    assert calls == {"dataplane": 2, "health": 2}


def test_reconcile_builds_subject_projection_once_for_many_subjects(monkeypatch) -> None:
    subjects = [
        {
            "subject_id": f"lan:client-{index}",
            "desired_mode": "global",
            "runtime_state": "running",
            "apply_state": "clean",
            "is_active": True,
            "is_deleted": False,
            "updated_at": "2026-09-05T00:00:00Z",
        }
        for index in range(25)
    ]
    snapshot = StateSnapshot()
    snapshot._values.update(
        {
            "modules": [
                {"module_name": "core", "desired_state": "enabled", "runtime_state": "running", "apply_state": "clean"},
                {"module_name": "vpn", "desired_state": "enabled", "runtime_state": "running", "apply_state": "clean"},
                {"module_name": "xray", "desired_state": "enabled", "runtime_state": "running", "apply_state": "clean"},
                {"module_name": "watchdog", "desired_state": "disabled", "runtime_state": "stopped", "apply_state": "clean"},
            ],
            "subjects:False:1000": subjects,
            "routing_global_state": {"desired_mode": "direct", "apply_state": "clean"},
            "live_dataplane_payload": {"ok": True},
            "dataplane_runtime_enforcement": {
                "traffic_enforcement_guaranteed": True,
                "active_mode_matches_intent": True,
                "live_global_mode": "direct",
            },
            "mihomo_health": {"runtime_state": "running", "active_server_id": "server-1"},
            "xray_health": {"runtime_state": "running"},
            "xray_bindings": {"bindings": []},
            "watchdog_runtime": {"present": False},
            "rules_state": {},
            "rules_metadata": [],
            "applied_manifest": None,
        }
    )
    calls = {"subjects": 0}

    monkeypatch.setattr(
        reconcile,
        "build_module_state_projection",
        lambda **kwargs: {"items": [_projection_item("module", item["module_name"]) for item in snapshot.modules()]},
    )

    def subject_projection(**kwargs):
        calls["subjects"] += 1
        return {"items": [_projection_item("subject", item["subject_id"]) for item in subjects]}

    monkeypatch.setattr(reconcile, "build_subject_state_projection", subject_projection)
    monkeypatch.setattr(reconcile, "build_xray_state_projection", lambda **kwargs: {"xray": _projection_item("xray", "xray")})
    monkeypatch.setattr(reconcile, "build_routing_state_projection", lambda **kwargs: {"routing": _projection_item("routing", "global")})
    monkeypatch.setattr(reconcile, "build_vpn_state_projection", lambda **kwargs: {"vpn": _projection_item("vpn", "vpn")})
    monkeypatch.setattr(reconcile, "build_watchdog_state_projection", lambda **kwargs: {"watchdog": _projection_item("watchdog", "watchdog")})

    response = reconcile.build_reconcile_response(snapshot=snapshot)

    assert calls["subjects"] == 1
    assert len([item for item in response.entities if item.entity_type == "subject"]) == len(subjects)


def test_diagnose_reuses_same_snapshot_for_projection_and_reconcile(monkeypatch) -> None:
    seen_snapshot_ids: list[int] = []
    projection_calls = {"modules": 0, "subjects": 0, "routing": 0, "vpn": 0, "xray": 0, "watchdog": 0}

    monkeypatch.setattr(diagnostics, "_check_database", lambda: ({"status": "healthy", "affected_entity_count": 0}, []))
    monkeypatch.setattr(diagnostics, "_build_events_section", lambda: ({"status": "healthy"}, []))
    monkeypatch.setattr(
        diagnostics,
        "_build_external_connections_section",
        lambda xray_section, snapshot=None: (xray_section | {"overall_impact": True}, []),
    )

    def record(name: str, payload: dict[str, object]):
        def loader(*, snapshot=None, **kwargs):
            projection_calls[name] += 1
            seen_snapshot_ids.append(id(snapshot))
            return payload

        return loader

    monkeypatch.setattr(
        diagnostics,
        "build_module_state_projection",
        record("modules", {"items": [_projection_item("module", "core")], "summary": {"total_count": 1}}),
    )
    monkeypatch.setattr(
        diagnostics,
        "build_subject_state_projection",
        record("subjects", {"items": [_projection_item("subject", "lan:laptop")], "summary": {"total_count": 1}}),
    )
    monkeypatch.setattr(diagnostics, "build_routing_state_projection", record("routing", {"routing": _projection_item("routing", "global")}))
    monkeypatch.setattr(diagnostics, "build_vpn_state_projection", record("vpn", {"vpn": _projection_item("vpn", "vpn")}))
    monkeypatch.setattr(diagnostics, "build_xray_state_projection", record("xray", {"xray": _projection_item("xray", "xray")}))
    monkeypatch.setattr(diagnostics, "build_watchdog_state_projection", record("watchdog", {"watchdog": _projection_item("watchdog", "watchdog")}))

    def fake_reconcile(*, snapshot=None):
        seen_snapshot_ids.append(id(snapshot))
        return ReconcileResponse(entities=[], summary={"healthy": 0, "drift": 0, "stale": 0, "failed": 0})

    monkeypatch.setattr(diagnostics, "build_reconcile_response", fake_reconcile)

    report = diagnostics.build_diagnostic_report(snapshot=StateSnapshot())

    assert report.status == "healthy"
    assert projection_calls == {"modules": 1, "subjects": 1, "routing": 1, "vpn": 1, "xray": 1, "watchdog": 1}
    assert len(set(seen_snapshot_ids)) == 1
