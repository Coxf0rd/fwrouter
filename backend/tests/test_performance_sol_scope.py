from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml

from fwrouter_api.services import mihomo_config as mihomo_config_service
from fwrouter_api.services import mihomo_config_paths
from fwrouter_api.services.mihomo_config_status import _summarize_candidate
from fwrouter_api.services.subscription_refresh_job import _redact_subscription_refresh_result


def test_applied_manifest_is_json_and_keeps_default_mark(monkeypatch, tmp_path: Path) -> None:
    path = tmp_path / "applied-manifest.json"
    path.write_text(json.dumps({"vpn_contour": {"proxy_bypass_mark_value": 768}}), encoding="utf-8")
    monkeypatch.setattr(mihomo_config_paths, "_resolved_applied_manifest_path", lambda: str(path))
    assert mihomo_config_paths._resolve_proxy_bypass_mark_value() == 768
    path.write_text("not yaml either", encoding="utf-8")
    assert mihomo_config_paths._resolve_proxy_bypass_mark_value() == 512


def test_safe_load_yaml_uses_c_loader_when_available(monkeypatch, tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("nested:\n  value: 1\n", encoding="utf-8")
    original_load = yaml.load
    loaders = []

    def load(stream, *, Loader):  # noqa: ANN001
        loaders.append(Loader)
        return original_load(stream, Loader=Loader)

    monkeypatch.setattr(mihomo_config_paths.yaml, "load", load)
    assert mihomo_config_paths._safe_load_yaml(str(path)) == {"nested": {"value": 1}}
    assert loaders == [getattr(yaml, "CSafeLoader", yaml.SafeLoader)]


def test_safe_load_yaml_falls_back_to_safe_loader(monkeypatch, tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("nested:\n  value: 1\n", encoding="utf-8")
    original_load = yaml.load
    loaders = []

    def load(stream, *, Loader):  # noqa: ANN001
        loaders.append(Loader)
        return original_load(stream, Loader=Loader)

    monkeypatch.delattr(mihomo_config_paths.yaml, "CSafeLoader", raising=False)
    monkeypatch.setattr(mihomo_config_paths.yaml, "load", load)
    assert mihomo_config_paths._safe_load_yaml(str(path)) == {"nested": {"value": 1}}
    assert loaders == [yaml.SafeLoader]


def test_candidate_summary_drops_ephemeral_config_and_large_arrays() -> None:
    summary = _summarize_candidate(
        {
            "candidate_path": "/tmp/config.next.yaml",
            "_candidate_config": {"rules": ["x"]},
            "config": {"rules": ["x"]},
            "rules": ["x"] * 100,
            "handoff_assignments": [{"proxy": "x"}] * 10,
        }
    )
    assert "_candidate_config" not in summary
    assert "config" not in summary
    assert "rules" not in summary
    assert "handoff_assignments" not in summary
    assert summary["rules_count"] == 100
    assert summary["handoff_assignments_count"] == 10


def test_subscription_job_redaction_keeps_failure_diagnostics_without_servers() -> None:
    result = _redact_subscription_refresh_result(
        {
            "ok": False,
            "error": {"code": "PROVIDER_FAILED", "message": "provider unavailable"},
            "candidate": {"rules": ["x"] * 1000, "_candidate_config": {"large": True}},
            "refresh": {
                "batch": {
                    "items": [
                        {"url": "https://secret.example/a", "refresh": {"servers": [{"name": "x"}]}}
                    ]
                }
            },
        }
    )
    assert result["error"]["code"] == "PROVIDER_FAILED"
    assert "rules" not in result["candidate"]
    assert "_candidate_config" not in result["candidate"]
    assert "servers" not in result["refresh"]["batch"]["items"][0]["refresh"]


def test_candidate_dumper_roundtrip_is_semantic(monkeypatch, tmp_path: Path) -> None:
    candidate_path = tmp_path / "config.next.yaml"
    config = {"allow-lan": True, "rules": ["MATCH,DIRECT"], "nested": {"count": 2}}
    monkeypatch.setattr(mihomo_config_service, "build_mihomo_config", lambda _routing=None: config)
    monkeypatch.setattr(mihomo_config_service, "_collect_xray_handoff_assignments", lambda: [])
    monkeypatch.setattr(mihomo_config_service, "_resolved_candidate_config_path", lambda: str(candidate_path))
    monkeypatch.setattr(mihomo_config_service, "write_technical_log", lambda **_kwargs: None)
    result = mihomo_config_service.write_mihomo_candidate_config()
    assert "_candidate_config" not in _summarize_candidate(result)
    assert yaml.safe_load(candidate_path.read_text(encoding="utf-8")) == config


def test_default_write_is_compact_and_internal_flag_is_ephemeral(monkeypatch, tmp_path: Path) -> None:
    config = {"rules": ["MATCH,DIRECT"], "allow-lan": True}
    monkeypatch.setattr(mihomo_config_service, "build_mihomo_config", lambda _routing=None: config)
    monkeypatch.setattr(mihomo_config_service, "_collect_xray_handoff_assignments", lambda: [])
    monkeypatch.setattr(mihomo_config_service, "_resolved_candidate_config_path", lambda: str(tmp_path / "candidate.yaml"))
    monkeypatch.setattr(mihomo_config_service, "write_technical_log", lambda **_kwargs: None)
    assert "_candidate_config" not in mihomo_config_service.write_mihomo_candidate_config()
    internal = mihomo_config_service.write_mihomo_candidate_config(include_internal_config=True)
    assert internal["_candidate_config"] is config
    assert "_candidate_config" not in _summarize_candidate(internal)


def test_reconcile_reuses_matching_ephemeral_candidate_only(monkeypatch, tmp_path: Path) -> None:
    from fwrouter_api.services import mihomo_reconcile as reconcile
    candidate_path = tmp_path / "candidate.yaml"
    candidate_path.write_text("candidate", encoding="utf-8")
    candidate = {"allow-lan": True, "rules": ["MATCH,DIRECT"]}
    calls: list[dict | None] = []
    monkeypatch.setattr(reconcile.config, "managed_runtime_operation_blocked", lambda *_a, **_k: None)
    monkeypatch.setattr(reconcile, "current_mihomo_input_fingerprint", lambda _routing=None: {"hash": "same"})
    monkeypatch.setattr(reconcile, "mihomo_input_unchanged", lambda _value: False)
    monkeypatch.setattr(reconcile.config, "_resolved_candidate_config_path", lambda: str(candidate_path))
    monkeypatch.setattr(reconcile.config, "_resolved_base_config_path", lambda: str(tmp_path / "base.yaml"))
    monkeypatch.setattr(reconcile.config, "validate_mihomo_candidate_config", lambda _routing=None, candidate_config=None: calls.append(candidate_config) or {"ok": False})
    monkeypatch.setattr(reconcile.config, "_summarize_candidate", lambda value: value or {})
    monkeypatch.setattr(reconcile.config, "_write_mihomo_reconcile_logs", lambda **_kwargs: None)
    monkeypatch.setattr(reconcile.config, "get_mihomo_config_status", lambda **_kwargs: {})
    result = reconcile.reconcile_mihomo_runtime(
        prepared_candidate_metadata={
            "input_fingerprint_hash": "same",
            "candidate_file_hash": reconcile._file_hash(candidate_path),
            "docker_validation_ok": True,
            "_candidate_config": candidate,
        }
    )
    assert result["ok"] is False
    assert calls == [candidate]


def test_live_probe_cache_coalesces_concurrent_loaders(monkeypatch) -> None:
    from fwrouter_api.services import live_probe_cache

    calls = 0

    def loader():
        nonlocal calls
        calls += 1
        time.sleep(0.03)
        return {"value": 1}

    live_probe_cache.clear_live_probe_cache()
    with ThreadPoolExecutor(max_workers=2) as pool:
        values = list(
            pool.map(
                lambda _item: live_probe_cache.get_live_probe_cache(
                    "sol.coalesce", ttl_seconds=2, loader=loader
                ),
                (1, 2),
            )
        )
    assert calls == 1
    assert values == [{"value": 1}, {"value": 1}]


def test_mihomo_health_uses_one_proxies_request(monkeypatch) -> None:
    from fwrouter_api.adapters.mihomo import MihomoHttpAdapter

    adapter = MihomoHttpAdapter(base_url="http://mihomo.test")
    calls = []
    proxies = {
        "vpn-auto": {"all": ["node-a"], "now": "node-a"},
        "vpn-global": {"all": ["vpn-auto"], "now": "vpn-auto"},
    }
    monkeypatch.setattr(adapter, "_get_json", lambda path: calls.append(path) or ({"version": "1"} if path == "/version" else {"proxies": proxies} if path == "/proxies" else {"connections": []}))
    monkeypatch.setattr(adapter, "_config_runtime_details", lambda: {})
    monkeypatch.setattr(adapter, "_transparent_session_observation", lambda value: {})
    health = adapter.health()
    assert calls.count("/proxies") == 1
    assert health.active_server_id == "node-a"
    assert health.details["selectors"]["vpn_global_now"] == "vpn-auto"


def test_ui_settings_uses_compact_subject_url() -> None:
    source = Path(__file__).parents[2].joinpath("ui/static/js/settings.js").read_text(encoding="utf-8")
    assert "/state/subjects?limit=500&include_legacy=false" in source


def test_compact_success_job_result_has_stable_fields_and_no_truncation() -> None:
    from fwrouter_api.services.subscription_refresh_job import _redact_subscription_refresh_result

    result = _redact_subscription_refresh_result(
        {
            "ok": True,
            "candidate": {"rules": ["x"] * 10000, "handoff_assignments": [{"x": 1}] * 1000},
            "timings_ms": {"provider_fetch_total": 12.0},
            "refresh": {"batch": {"items": []}},
            "promoted": True,
        }
    )
    encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    assert result["promoted"] is True
    assert result["timings_ms"]["provider_fetch_total"] == 12.0
    assert len(encoded) < 10_000
    assert "__truncated__" not in encoded


def test_provider_batch_is_bounded_ordered_and_propagates_unexpected_errors(monkeypatch, tmp_path: Path) -> None:
    from fwrouter_api.adapters import subscription as adapter_module
    from fwrouter_api.adapters.subscription import SubscriptionRefreshResult, SubscriptionRefreshStatus
    from fwrouter_api.db.connection import initialize_database
    from fwrouter_api.services import subscription as service

    monkeypatch.setenv("FWROUTER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("FWROUTER_DATABASE_URL", f"sqlite:///{tmp_path}/fwrouter.db")
    from fwrouter_api.core.config import get_settings
    get_settings.cache_clear()
    initialize_database()
    active = 0
    peak = 0
    lock = threading.Lock()

    class Adapter:
        def refresh(self, url):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.02 if url.endswith("/a") else 0.01)
            with lock:
                active -= 1
            if url.endswith("/fail"):
                return SubscriptionRefreshResult(status=SubscriptionRefreshStatus.FAILED, error_code="X", error_message="failed")
            return SubscriptionRefreshResult(status=SubscriptionRefreshStatus.SUCCESS)

    monkeypatch.setattr(adapter_module, "DEFAULT_SUBSCRIPTION_ADAPTER", Adapter())
    result = service.refresh_subscription_inventory_batch(
        ["https://provider.test/a", "https://provider.test/fail", "https://provider.test/c"]
    )
    assert peak <= 2
    assert [item["url"] for item in result["batch"]["items"]] == [
        "https://provider.test/a", "https://provider.test/fail", "https://provider.test/c"
    ]
    assert result["batch"]["items"][1]["error"]["code"] == "X"

    class Broken(Adapter):
        def refresh(self, url):
            raise ValueError("unexpected")

    monkeypatch.setattr(adapter_module, "DEFAULT_SUBSCRIPTION_ADAPTER", Broken())
    try:
        service.refresh_subscription_inventory_batch(["https://provider.test/a", "https://provider.test/b"])
    except ValueError as exc:
        assert str(exc) == "unexpected"
    else:
        raise AssertionError("unexpected provider exception was swallowed")


def test_mihomo_probe_many_is_bounded_and_ordered(monkeypatch) -> None:
    import httpx
    from fwrouter_api.adapters import mihomo as adapter_module

    adapter = adapter_module.MihomoHttpAdapter(base_url="http://mihomo.test")
    active = 0
    peak = 0
    lock = threading.Lock()

    class Response:
        def __init__(self, target):
            self.target = target
            self.status_code = 404 if target == "target-404" else 200

        def raise_for_status(self):
            if self.target.endswith("target-fail"):
                raise httpx.HTTPError("probe failed")

        def json(self):
            return {self.target: {"delay": len(self.target)}}

    class Client:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def get(self, path, **_kwargs):
            nonlocal active, peak
            target = path.split("/group/", 1)[1].rsplit("/delay", 1)[0]
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.01)
            with lock:
                active -= 1
            return Response(target)

    monkeypatch.setattr(adapter_module.httpx, "Client", Client)
    monkeypatch.setattr(adapter, "_delay_json", lambda target, **_kwargs: {"delay": 7})
    monkeypatch.setattr(
        adapter,
        "get_logical_groups_state",
        lambda targets: [
            {"logical_runtime_target": target, "members": [], "ok": True} for target in targets
        ],
    )
    targets = [f"target-{index}" for index in range(6)] + ["target-404", "target-fail"]
    result = adapter.probe_logical_groups(targets, timeout_ms=100)
    assert peak <= 4
    assert peak > 1
    assert [item.get("logical_runtime_target") for item in result] == targets
    assert result[-2]["ok"] is True
    assert result[-1]["ok"] is False
