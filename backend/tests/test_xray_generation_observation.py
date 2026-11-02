from __future__ import annotations

import base64
import hashlib
import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from fwrouter_api.services import xray_subscription_service as service
from fwrouter_api.services import xray_generation_observation as observation
from fwrouter_api.services import xray_generation_staging as staging


class _Rows:
    def __init__(self, one: Any = None, many: list[Any] | None = None) -> None:
        self.one = one
        self.many = many or []

    def fetchone(self):
        return self.one

    def fetchall(self):
        return self.many


class _CoreConnection:
    def __init__(self, *, bypass=None, core=None, routing=None, latest=None, jobs=None) -> None:
        self.bypass = bypass
        self.core = core or {"desired_state": "enabled", "lifecycle_mode": "managed", "apply_state": "clean"}
        self.routing = routing or {
            "desired_mode": "direct", "applied_mode": "direct", "selective_default": None,
            "server_mode": "auto", "desired_fixed_server_id": None,
            "applied_fixed_server_id": None, "fixed_server_until": None,
            "active_auto_server_id": None, "apply_state": "clean",
        }
        self.latest = latest or {
            "apply_id": "apply-7", "status": "applied", "core_intent_revision": 7,
        }
        self.jobs = jobs or []
        self.queries: list[str] = []

    def execute(self, sql, _params=()):
        self.queries.append(" ".join(str(sql).split()))
        if "FROM settings WHERE key='core.bypass'" in sql:
            return _Rows(self.bypass)
        if "FROM modules WHERE module_name='core'" in sql:
            return _Rows(self.core)
        if "FROM routing_global_state WHERE id=1" in sql:
            return _Rows(self.routing)
        if "FROM apply_versions ORDER BY" in sql:
            return _Rows(self.latest)
        if "FROM apply_versions WHERE apply_id=?" in sql:
            return _Rows(self.latest)
        if "FROM jobs WHERE status IN" in sql:
            return _Rows(many=self.jobs)
        raise AssertionError(f"unexpected Core observation query: {sql}")


@contextmanager
def _connection_scope(connection):
    yield connection


@pytest.mark.parametrize(
    ("connection_kwargs", "current_revision", "lock_tokens", "expected"),
    [
        ({"bypass": {"value_json": '{"enabled":true}'}}, 7, set(), "core_bypass_active"),
        ({"latest": {"status": "running", "core_intent_revision": 7}}, 7, set(), "core_applied_generation_unowned"),
        ({"latest": {"status": "applied", "core_intent_revision": 6}}, 7, set(), "core_applied_generation_unowned"),
        ({"core": {"desired_state": "disabled", "lifecycle_mode": "managed", "apply_state": "clean"}}, 7, set(), "core_apply_state_unverified"),
        ({"jobs": [{"job_type": "apply", "lock_key": "apply,core"}]}, 7, {"apply", "core"}, "core_apply_inflight"),
        ({}, 7, set(), None),
    ],
)
def test_core_availability_keeps_existing_fail_closed_classification(
    monkeypatch, connection_kwargs, current_revision, lock_tokens, expected,
):
    from fwrouter_api.services import core_intent, jobs

    connection = _CoreConnection(**connection_kwargs)
    monkeypatch.setattr(core_intent, "read_core_intent_revision", lambda _connection: current_revision)
    monkeypatch.setattr(jobs, "_lock_tokens", lambda _lock_key: lock_tokens)

    result = service._legacy_admission_core_unavailable_reason(connection)

    assert result == expected
    assert connection.queries[0] == "SELECT value_json FROM settings WHERE key='core.bypass'"
    if expected == "core_bypass_active":
        assert len(connection.queries) == 1


def test_observation_component_exports_fixed_projection_contract():
    routing = {"desired_mode": "vpn", "server_mode": "fixed", "private": "ignored"}

    assert observation.fixed_routing_projection(routing) == {
        "desired_mode": "vpn", "applied_mode": None, "selective_default": None,
        "server_mode": "fixed", "desired_fixed_server_id": None,
        "applied_fixed_server_id": None, "fixed_server_until": None, "apply_state": None,
    }
    assert observation.FIXED_ROUTING_FIELDS == service._FIXED_XRAY_ROUTING_FIELDS


def test_core_native_proof_preserves_live_read_order_and_rejects_stale_context(monkeypatch):
    from fwrouter_api.services import core_intent, dataplane_global, dataplane_live, dataplane_status
    from fwrouter_api.services import live_probe_cache

    trace: list[str] = []
    connection = _CoreConnection()
    manifest = {
        "plan_id": "apply-7",
        "routing_global_state": {
            "desired_mode": "direct", "applied_mode": "direct", "selective_default": None,
            "server_mode": "auto", "desired_fixed_server_id": None,
            "applied_fixed_server_id": None, "fixed_server_until": None,
            "active_auto_server_id": None,
        },
    }
    monkeypatch.setattr(service, "db_session", lambda: _connection_scope(connection))
    contexts = iter(({"runtime": 1}, {"runtime": 2}))
    monkeypatch.setattr(core_intent, "read_core_runtime_context", lambda: trace.append("core_context") or next(contexts))
    monkeypatch.setattr(core_intent, "read_core_intent_revision", lambda _connection: 7)

    def probe(key, *, loader, force_refresh, **_kwargs):
        trace.append(key)
        assert force_refresh is True
        if key == "dataplane_global.applied_manifest":
            return manifest
        assert key == "dataplane_live.global_mode"
        return {"mode": "direct"}

    monkeypatch.setattr(live_probe_cache, "get_live_probe_cache", probe)
    monkeypatch.setattr(dataplane_status, "_read_live_dataplane_payload", lambda: trace.append("payload") or {"ready": True})
    monkeypatch.setattr(dataplane_status, "_live_owned_table_ready", lambda payload: trace.append("owned_table") or payload["ready"])
    monkeypatch.setattr(
        dataplane_status,
        "build_runtime_enforcement_state",
        lambda *, live_payload: trace.append("enforcement") or {
            "enforcement_level": "global_direct_enforced",
            "traffic_enforcement_guaranteed": True,
            "active_mode_matches_intent": True,
            "live_global_mode": "direct",
            "bypass_active": False,
        },
    )
    monkeypatch.setattr(dataplane_global, "_read_applied_manifest_uncached", lambda: manifest)
    monkeypatch.setattr(dataplane_live, "_probe_live_global_mode_uncached", lambda: {"mode": "direct"})

    result = service._collect_legacy_admission_core_native_proof()

    assert result == {"ok": False, "reason": "core_runtime_context_changed"}
    assert trace == [
        "core_context", "dataplane_global.applied_manifest", "payload", "owned_table",
        "dataplane_live.global_mode", "enforcement", "core_context",
    ]


def test_capture_ordinary_fixed_context_keeps_two_sided_proof_and_narrow_projection(monkeypatch):
    trace: list[str] = []
    routing = {
        "server_mode": "fixed", "desired_fixed_server_id": "srv-1", "applied_fixed_server_id": "srv-1",
        "fixed_server_until": "2099-01-01 00:00:00", "desired_mode": "vpn", "applied_mode": "vpn",
        "selective_default": "direct", "apply_state": "clean", "private_extra": "must-not-project",
    }
    selection = {"routing": routing, "selection_revision": 4, "selection_decision_id": "decision-4"}
    connection = SimpleNamespace(execute=lambda *_args: trace.append("ttl") or _Rows((1,)))
    monkeypatch.setattr(service, "db_session", lambda: _connection_scope(connection))
    monkeypatch.setattr(service, "_capture_generation_auto_selection", lambda: trace.append("selection") or selection)
    monkeypatch.setattr(service, "_legacy_admission_core_unavailable_reason", lambda _connection: trace.append("core_state") or None)
    monkeypatch.setattr(service, "_legacy_admission_core_runtime_fingerprint", lambda _connection=None: trace.append("core_fingerprint") or "c" * 64)
    monkeypatch.setattr(service, "_collect_legacy_admission_core_native_proof", lambda: trace.append("native_proof") or {
        "ok": True, "manifest_hash": "m" * 64, "apply_id": "apply-4",
        "enforcement_level": "global_vpn_enforced", "live_global_mode": "vpn", "raw": "omit",
    })

    context, reason = service._capture_ordinary_fixed_xray_context()

    assert reason is None
    assert context == {
        "routing_snapshot": {key: routing[key] for key in service._FIXED_XRAY_ROUTING_FIELDS},
        "selection_snapshot": selection,
        "core_runtime_fingerprint": "c" * 64,
        "core_proof": {
            "manifest_hash": "m" * 64, "apply_id": "apply-4",
            "enforcement_level": "global_vpn_enforced", "live_global_mode": "vpn",
        },
    }
    assert context["selection_snapshot"] is selection
    assert context["selection_snapshot"]["routing"]["private_extra"] == "must-not-project"
    assert "private_extra" not in json.dumps(context["routing_snapshot"])
    assert "raw" not in context["core_proof"]
    assert trace == ["selection", "ttl", "core_state", "core_fingerprint", "native_proof",
                     "selection", "ttl", "core_state", "core_fingerprint"]


@pytest.mark.parametrize("source_changes", [False, True])
def test_ordinary_fixed_context_revalidation_rechecks_source_after_native_proof(monkeypatch, source_changes):
    routing = {
        "server_mode": "fixed", "desired_fixed_server_id": "srv-1", "applied_fixed_server_id": "srv-1",
        "fixed_server_until": "2099-01-01 00:00:00", "desired_mode": "vpn", "applied_mode": "vpn",
        "selective_default": "direct", "apply_state": "clean",
    }
    selection = {"routing": routing, "selection_revision": 4, "selection_decision_id": "decision-4"}
    context = {
        "routing_snapshot": {key: routing[key] for key in service._FIXED_XRAY_ROUTING_FIELDS},
        "selection_snapshot": selection,
        "core_proof": {"manifest_hash": "m" * 64, "apply_id": "apply-4",
                       "enforcement_level": "global_vpn_enforced", "live_global_mode": "vpn"},
        "core_runtime_fingerprint": "c" * 64,
        "source_fingerprint": "s" * 64,
    }
    connection = SimpleNamespace(execute=lambda *_args: _Rows((1,)))
    monkeypatch.setattr(service, "db_session", lambda: _connection_scope(connection))
    monkeypatch.setattr(service, "_capture_generation_auto_selection", lambda: selection)
    monkeypatch.setattr(service, "_legacy_admission_core_unavailable_reason", lambda _connection: None)
    monkeypatch.setattr(service, "_legacy_admission_core_runtime_fingerprint", lambda _connection=None: "c" * 64)
    monkeypatch.setattr(service, "_collect_legacy_admission_core_native_proof", lambda: {
        "ok": True, "manifest_hash": "m" * 64, "apply_id": "apply-4",
        "enforcement_level": "global_vpn_enforced", "live_global_mode": "vpn",
    })
    source_values = iter(("s" * 64, "x" * 64 if source_changes else "s" * 64))
    monkeypatch.setattr(service, "_generation_source_fingerprint", lambda: next(source_values))

    assert service._ordinary_fixed_xray_context_matches(context) is (not source_changes)


@pytest.mark.parametrize(
    ("selection", "selector", "routing", "expected_ok", "expected_error"),
    [
        ({"routing": {"server_mode": "auto", "active_auto_server_id": "srv-1"}}, "runtime-1",
         {"active_auto_server_id": "srv-1"}, True, None),
        ({"routing": {"server_mode": "auto", "active_auto_server_id": "srv-1"}}, "other-runtime",
         {"active_auto_server_id": "srv-1"}, False, "SELECTION_RESTORE_READBACK_UNCONFIRMED"),
        ({"routing": {"server_mode": "auto", "active_auto_server_id": "srv-1"}}, "runtime-1",
         {"active_auto_server_id": "srv-2"}, False, "SELECTION_RESTORE_READBACK_UNCONFIRMED"),
        ({"routing": {"server_mode": "auto", "active_auto_server_id": ""}}, "runtime-1",
         {"active_auto_server_id": ""}, False, "SELECTION_BASELINE_UNSELECTED"),
    ],
)
def test_selection_readback_preserves_target_and_error_contract(monkeypatch, selection, selector, routing, expected_ok, expected_error):
    from fwrouter_api.services import logical_topology, selector as selector_service, servers

    monkeypatch.setattr(logical_topology, "get_logical_runtime_name", lambda logical_id: "runtime-1" if logical_id == "srv-1" else None)
    monkeypatch.setattr(selector_service, "get_vpn_auto_state", lambda *, read_only: {
        "selector_runtime": {"vpn_auto_now": selector}, "active_auto_server_id": routing.get("active_auto_server_id"),
    })
    monkeypatch.setattr(servers, "get_routing_global_state", lambda *, expire_ttl: routing)

    result = service._verify_generation_selection_readback(selection)

    assert result["ok"] is expected_ok
    assert result["error_code"] == expected_error
    if result["ok"]:
        assert result["logical_server_id"] == "srv-1"
        assert result["effective_target"] == "runtime-1"


def _configure_staging(monkeypatch, tmp_path: Path, *, image_id: str | None, drift_at: int | None = None, mutate_candidate: bool = False, native_ok: bool = True):
    from fwrouter_api.services import mihomo_config, mihomo_config_inbounds, mihomo_reconcile_fingerprint, subscription_pipeline

    config_path = tmp_path / "xray" / "config.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text("{}\n", encoding="utf-8")
    writes: list[str] = []
    validations: list[tuple[str, str]] = []
    logs: list[dict[str, Any]] = []
    fingerprint_calls = {"count": 0}

    class Adapter:
        def __init__(self):
            self.config_path = config_path

        def stage_subscription_generation(self, *, candidate_path: Path, **_kwargs):
            candidate_path.parent.mkdir(parents=True, exist_ok=True)
            candidate_path.write_text("{}\n", encoding="utf-8")
            candidate_path.chmod(0o600)
            writes.append("xray")
            return SimpleNamespace(ok=True, details={"candidate_sha256": hashlib.sha256(candidate_path.read_bytes()).hexdigest()})

    def write_candidate(*, candidate_path, xray_handoff_assignments, **_kwargs):
        path = Path(candidate_path)
        payload = {"handoffs": xray_handoff_assignments}
        path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        path.chmod(0o600)
        writes.append(path.name)
        return {"candidate_path": str(path), "_candidate_config": payload}

    def local_validate(*, candidate_path, **_kwargs):
        validations.append((Path(candidate_path).name, "local"))
        return {"ok": True, "error_code": None}

    def native_validate(candidate_path, **kwargs):
        path = Path(candidate_path)
        validations.append((path.name, "native"))
        if image_id is not None:
            assert kwargs.get("image_reference") == image_id
        else:
            assert "image_reference" not in kwargs
        if mutate_candidate and len([item for item in validations if item[1] == "native"]) == 1:
            path.write_text("tampered after validation\n", encoding="utf-8")
        return {"ok": native_ok, "returncode": 0 if native_ok else 1}

    def fingerprint(*_args, **_kwargs):
        fingerprint_calls["count"] += 1
        digest = "a" * 64 if drift_at is None or fingerprint_calls["count"] < drift_at else "b" * 64
        return {"hash": digest, "version": 1}

    monkeypatch.setattr(service, "_applied_handoff_assignments", lambda: [{"selected_server_id": "old", "port": 53123}])
    monkeypatch.setattr(service, "_fixed_xray_generation_context_matches", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(service, "_fixed_xray_routing_snapshot_matches", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(service, "_log_mihomo_input_context_change", lambda **kwargs: logs.append(kwargs))
    monkeypatch.setattr(mihomo_config_inbounds, "_candidate_base_config_paths", lambda: [])
    monkeypatch.setattr(mihomo_reconcile_fingerprint, "current_mihomo_input_fingerprint", fingerprint)
    monkeypatch.setattr(mihomo_reconcile_fingerprint, "_file_hash", lambda _path: "file-hash")
    monkeypatch.setattr(mihomo_config, "write_mihomo_candidate_config", write_candidate)
    monkeypatch.setattr(mihomo_config, "validate_mihomo_candidate_config", local_validate)
    monkeypatch.setattr(subscription_pipeline, "resolve_mihomo_validator_image_id", lambda: image_id)
    monkeypatch.setattr(subscription_pipeline, "validate_mihomo_candidate_config", native_validate)
    return Adapter(), writes, validations, logs


def test_staged_candidate_validates_transition_then_final_and_uses_unpinned_fallback(monkeypatch, tmp_path):
    adapter, writes, validations, _logs = _configure_staging(monkeypatch, tmp_path, image_id=None)
    assignments = [{"selected_server_id": "new", "port": 53124}]
    monkeypatch.setattr(service, "_applied_handoff_assignments", lambda: assignments)

    result = service._stage_profile_native_candidates(
        adapter=adapter, desired_clients=[], managed_email_prefixes=[], bindings=[], client_modes=[],
        assignments=assignments,
    )

    assert result["ok"] is True
    assert writes == ["xray", "mihomo-transition.candidate.yaml"]
    assert validations == [
        ("mihomo-transition.candidate.yaml", "local"),
        ("mihomo-transition.candidate.yaml", "native"),
        ("mihomo-final.candidate.yaml", "local"),
        ("mihomo-final.candidate.yaml", "native"),
    ]
    assert result["native_validation"]["transition"]["validation"]["native_validation_reused"] is False
    assert result["native_validation"]["final"]["validation"]["candidate_write_reused"] is True
    assert Path(result["stage_dir"]).stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize(
    ("drift_at", "mutate_candidate", "native_ok", "expected_code"),
    [
        (3, False, True, "MIHOMO_STAGE_INPUTS_CHANGED_AFTER_VALIDATION"),
        (None, True, True, "MIHOMO_STAGE_CANDIDATE_CHANGED_DURING_VALIDATION"),
        (None, False, False, "MIHOMO_CANDIDATE_INVALID"),
    ],
)
def test_staged_candidate_fails_closed_on_context_hash_or_native_validation_failure(
    monkeypatch, tmp_path, drift_at, mutate_candidate, native_ok, expected_code,
):
    adapter, writes, validations, _logs = _configure_staging(
        monkeypatch, tmp_path, image_id="sha256:" + "a" * 64,
        drift_at=drift_at, mutate_candidate=mutate_candidate, native_ok=native_ok,
    )
    result = service._stage_profile_native_candidates(
        adapter=adapter, desired_clients=[], managed_email_prefixes=[], bindings=[], client_modes=[],
        assignments=[{"selected_server_id": "new", "port": 53124}],
    )

    assert result["ok"] is False
    assert result["error_code"] == expected_code
    assert writes[0] == "xray"
    assert validations[0] == ("mihomo-transition.candidate.yaml", "local")
    assert not any("restart" in event for event in writes)


def test_mihomo_input_summary_has_closed_safe_shape_and_never_returns_payload_or_paths():
    fingerprint = {
        "version": 3, "hash": "a" * 64,
        "payload": {"credentials": "secret-value", "tables": {"subjects": [{"token": "private"}]}},
    }
    summary = staging.summarize_input_context(
        (fingerprint, (("/private/source.yaml", "b" * 64),), object())
    )

    assert set(summary) == {
        "available", "fingerprint_version", "fingerprint_sha256", "category_sha256",
        "base_config_input_count", "base_config_inputs_sha256",
    }
    serialized = json.dumps(summary)
    assert "secret-value" not in serialized
    assert "private" not in serialized
    assert "/private/source.yaml" not in serialized
    assert summary["available"] is True


def test_staging_component_exports_existing_safe_summary_compatibly():
    context = ({"version": 1, "hash": "a" * 64, "payload": {"tables": {"owners": [1]}}}, ())

    assert staging.summarize_input_context(context) == service._safe_mihomo_input_context_summary(context)


@pytest.mark.parametrize("context", [None, (), ({}, "not-a-list"), ({"hash": "short"}, "not-a-list")])
def test_mihomo_input_summary_rejects_malformed_context(context):
    assert service._safe_mihomo_input_context_summary(context) == {"available": False}


def _safe_mihomo_preimage_bytes(monkeypatch):
    from fwrouter_api.services import mihomo_config_paths, mihomo_config_rules

    routing = {"server_mode": "auto", "active_auto_server_id": "server-1"}
    expected_rules = [{"MATCH": "vpn-global"}]
    expected_policy = {"full_vpn_rules": [{"MATCH": "vpn-auto"}], "transparent_rules": [{"MATCH": "vpn-auto"}]}
    monkeypatch.setattr(mihomo_config_rules, "_build_mihomo_config_with_source", lambda _routing: (expected_rules, expected_policy))
    candidate = {
        "rules": expected_rules,
        "sub-rules": {
            mihomo_config_paths.FULL_VPN_RULE_NAME: expected_policy["full_vpn_rules"],
            mihomo_config_paths.TRANSPARENT_TPROXY_RULE_NAME: expected_policy["transparent_rules"],
        },
        "proxy-groups": [
            {"name": "vpn-auto", "proxies": ["edge-proxy", "REJECT"]},
            {"name": "vpn-global", "proxies": ["vpn-auto", "REJECT"]},
            {"name": "fwrouter-subject-1", "proxies": ["vpn-auto", "REJECT"]},
        ],
    }
    return routing, json.dumps(candidate).encode("utf-8")


@pytest.mark.parametrize(
    ("encoded", "routing", "expected"),
    [
        ("!", {"server_mode": "auto"}, {"ok": False, "reason": "mihomo_preimage_invalid"}),
        (base64.b64encode(b"[]").decode("ascii"), {"server_mode": "auto"},
         {"ok": False, "reason": "mihomo_preimage_invalid"}),
    ],
)
def test_mihomo_preimage_rejects_malformed_encoding_or_non_mapping_before_validation(monkeypatch, encoded, routing, expected):
    from fwrouter_api.services import subscription_pipeline

    calls: list[str] = []
    monkeypatch.setattr(subscription_pipeline, "validate_mihomo_candidate_config", lambda **_kwargs: calls.append("native") or {"ok": True})
    assert service._validate_rollback_mihomo_preimage(encoded, routing_snapshot=routing) == expected
    assert calls == []


def test_mihomo_preimage_policy_mismatch_fails_before_native_validation(monkeypatch):
    from fwrouter_api.services import subscription_pipeline

    routing, raw = _safe_mihomo_preimage_bytes(monkeypatch)
    candidate = json.loads(raw)
    candidate["rules"] = [{"MATCH": "different-policy"}]
    calls: list[str] = []
    monkeypatch.setattr(subscription_pipeline, "validate_mihomo_candidate_config", lambda **_kwargs: calls.append("native") or {"ok": True})

    result = service._validate_rollback_mihomo_preimage(
        base64.b64encode(json.dumps(candidate).encode()).decode("ascii"), routing_snapshot=routing,
    )

    assert result == {"ok": False, "reason": "mihomo_preimage_policy_unproven"}
    assert calls == []


@pytest.mark.parametrize("native_result", [{"ok": False}, RuntimeError("validator unavailable")])
def test_mihomo_preimage_native_failure_cleans_private_candidate(monkeypatch, tmp_path, native_result):
    from fwrouter_api.services import mihomo_config, subscription_pipeline

    routing, raw = _safe_mihomo_preimage_bytes(monkeypatch)
    base_path = tmp_path / "mihomo" / "config.yaml"
    base_path.parent.mkdir()
    base_path.write_text("active config is untouched\n", encoding="utf-8")
    candidates: list[Path] = []
    monkeypatch.setattr(mihomo_config, "_resolved_base_config_path", lambda: base_path)

    def validate(*, candidate_path):
        path = Path(candidate_path)
        candidates.append(path)
        assert path.stat().st_mode & 0o777 == 0o600
        if isinstance(native_result, BaseException):
            raise native_result
        return native_result

    monkeypatch.setattr(subscription_pipeline, "validate_mihomo_candidate_config", validate)

    result = service._validate_rollback_mihomo_preimage(
        base64.b64encode(raw).decode("ascii"), routing_snapshot=routing,
    )

    assert result == {"ok": False, "reason": "mihomo_preimage_native_invalid"}
    assert candidates and all(not path.exists() for path in candidates)
    assert base_path.read_text(encoding="utf-8") == "active config is untouched\n"


def test_mihomo_preimage_cleanup_failure_cannot_return_success(monkeypatch, tmp_path):
    from fwrouter_api.services import mihomo_config, subscription_pipeline

    routing, raw = _safe_mihomo_preimage_bytes(monkeypatch)
    base_path = tmp_path / "mihomo" / "config.yaml"
    base_path.parent.mkdir()
    base_path.write_text("active config\n", encoding="utf-8")
    monkeypatch.setattr(mihomo_config, "_resolved_base_config_path", lambda: base_path)
    candidates: list[Path] = []
    monkeypatch.setattr(
        subscription_pipeline,
        "validate_mihomo_candidate_config",
        lambda *, candidate_path: candidates.append(Path(candidate_path)) or {"ok": True},
    )
    original_unlink = Path.unlink

    def deny_candidate_cleanup(path, *args, **kwargs):
        if path.name.startswith(".rollback-mihomo-preflight-"):
            raise OSError("isolated cleanup failure")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", deny_candidate_cleanup)
    try:
        result = service._validate_rollback_mihomo_preimage(
            base64.b64encode(raw).decode("ascii"), routing_snapshot=routing,
        )
    finally:
        monkeypatch.setattr(Path, "unlink", original_unlink)
        for path in candidates:
            original_unlink(path, missing_ok=True)

    assert result == {"ok": False, "reason": "mihomo_preimage_native_invalid"}
    assert candidates
