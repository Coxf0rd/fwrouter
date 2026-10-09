from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import yaml

from fwrouter_api.adapters.mihomo import (
    DEFAULT_BASE_URL,
    MihomoHttpAdapter,
    MihomoRuntimeState,
)
from fwrouter_api.adapters import mihomo as mihomo_module


def _use_adapter_clock(monkeypatch, *times: datetime) -> None:
    class SequenceDateTime(datetime):
        _times = list(times)

        @classmethod
        def now(cls, tz=None):
            if not cls._times:
                raise AssertionError("adapter clock exhausted")
            value = cls._times.pop(0)
            return value.replace(tzinfo=None) if tz is None else value.astimezone(tz)

    monkeypatch.setattr(mihomo_module, "datetime", SequenceDateTime)


def _write_yaml(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def test_mihomo_health_marks_loopback_bound_transparent_listener_degraded(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    contours_path = tmp_path / "contours.yaml"
    _write_yaml(
        config_path,
        {
            "secret": "secret",
            "bind-address": "127.0.0.1",
            "listeners": [
                {"name": "fwrouter-mixed", "type": "mixed", "listen": "127.0.0.1", "port": 5201},
                {"name": "fwrouter-redir", "type": "redir", "listen": "127.0.0.1", "port": 5202, "proxy": "vpn-global"},
                {"name": "fwrouter-tproxy", "type": "tproxy", "listen": "127.0.0.1", "port": 5203, "proxy": "vpn-global", "udp": True},
            ],
        },
    )
    _write_yaml(
        contours_path,
        {
            "transparent_vpn": {"ready": True, "isolated_from_explicit_proxy": True, "redir_port": 5202, "tproxy_port": 5203},
            "explicit_proxy": {"preserved": True},
        },
    )

    adapter = MihomoHttpAdapter(base_url=DEFAULT_BASE_URL, config_path=config_path, contours_path=contours_path)
    adapter._get_json = lambda path: (  # type: ignore[method-assign]
        {"version": "test"}
        if path == "/version"
        else {"proxies": {"vpn-auto": {"all": ["DIRECT"]}, "vpn-global": {"all": ["vpn-auto", "DIRECT"], "now": "vpn-auto"}}}
    )

    health = adapter.health()

    assert health.runtime_state == MihomoRuntimeState.DEGRADED
    assert "transparent TPROXY listener bind is invalid" in health.message
    contours = health.details["config"]["fwrouter_contours"]
    assert contours["transparent_vpn"]["listener_loopback_bound"] is True
    assert contours["transparent_vpn"]["listener_bind_valid"] is False
    assert contours["transparent_vpn"]["ready"] is False


def test_mihomo_health_uses_managed_split_listeners_as_canonical_source(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    contours_path = tmp_path / "contours.yaml"
    _write_yaml(
        config_path,
        {
            "secret": "secret",
            "redir-port": 6202,
            "tproxy-port": 6203,
            "listeners": [
                {"name": "fwrouter-mixed", "type": "mixed", "listen": "127.0.0.1", "port": 5201},
                {"name": "fwrouter-redir", "type": "redir", "listen": "0.0.0.0", "port": 5202, "proxy": "vpn-global"},
                {"name": "fwrouter-tproxy", "type": "tproxy", "listen": "0.0.0.0", "port": 5203, "proxy": "vpn-global", "udp": True},
            ],
        },
    )
    _write_yaml(contours_path, {"transparent_vpn": {"ready": True}})

    adapter = MihomoHttpAdapter(base_url=DEFAULT_BASE_URL, config_path=config_path, contours_path=contours_path)
    adapter.check_port = lambda port, host="127.0.0.1", timeout=1.0: True  # type: ignore[method-assign]
    adapter._get_json = lambda path: (  # type: ignore[method-assign]
        {"version": "test"}
        if path == "/version"
        else {
            "connections": [
                {"network": "tcp", "inbound": "fwrouter-redir", "proxy": "vpn-global"},
            ]
        }
        if path == "/connections"
        else {"proxies": {"vpn-auto": {"all": ["DIRECT"]}, "vpn-global": {"all": ["vpn-auto", "DIRECT"], "now": "vpn-auto"}}}
    )

    health = adapter.health()

    assert health.runtime_state == MihomoRuntimeState.RUNNING
    assert health.details["config"]["redir_port"] == 5202
    assert health.details["config"]["tproxy_port"] == 5203
    assert health.details["config"]["fwrouter_contours"]["transparent_vpn"]["transparent_tcp_ready"] is True
    assert health.details["config"]["fwrouter_contours"]["transparent_vpn"]["transparent_udp_ready"] is True
    assert health.details["transparent_runtime"]["transparent_tcp_session_materialized"] is True


def test_mihomo_health_accepts_rule_based_transparent_listeners(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    contours_path = tmp_path / "contours.yaml"
    _write_yaml(
        config_path,
        {
            "secret": "secret",
            "listeners": [
                {"name": "fwrouter-mixed", "type": "mixed", "listen": "127.0.0.1", "port": 5201, "proxy": "vpn-global"},
                {"name": "fwrouter-redir", "type": "redir", "listen": "0.0.0.0", "port": 5202, "rule": "fwrouter-transparent"},
                {"name": "fwrouter-tproxy", "type": "tproxy", "listen": "0.0.0.0", "port": 5203, "rule": "fwrouter-transparent", "udp": True},
            ],
        },
    )
    _write_yaml(contours_path, {"transparent_vpn": {"ready": True}})

    adapter = MihomoHttpAdapter(base_url=DEFAULT_BASE_URL, config_path=config_path, contours_path=contours_path)
    adapter.check_port = lambda port, host="127.0.0.1", timeout=1.0: True  # type: ignore[method-assign]
    adapter._get_json = lambda path: (  # type: ignore[method-assign]
        {"version": "test"}
        if path == "/version"
        else {"connections": []}
        if path == "/connections"
        else {"proxies": {"vpn-auto": {"all": ["DIRECT"]}, "vpn-global": {"all": ["vpn-auto", "DIRECT"], "now": "vpn-auto"}}}
    )

    health = adapter.health()

    transparent = health.details["config"]["fwrouter_contours"]["transparent_vpn"]
    assert health.runtime_state == MihomoRuntimeState.RUNNING
    assert transparent["listener_rule"] == "fwrouter-transparent"
    assert transparent["listener_proxy"] is None
    assert transparent["transparent_tcp_ready"] is True
    assert transparent["transparent_udp_ready"] is True


def test_apply_server_to_selector_treats_controller_404_as_missing_selector(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    contours_path = tmp_path / "contours.yaml"
    _write_yaml(config_path, {"secret": "secret"})

    adapter = MihomoHttpAdapter(base_url=DEFAULT_BASE_URL, config_path=config_path, contours_path=contours_path)

    def _raise_404(path: str) -> dict:  # noqa: ANN001
        request = httpx.Request("GET", f"{DEFAULT_BASE_URL}{path}")
        response = httpx.Response(404, request=request)
        raise httpx.HTTPStatusError("not found", request=request, response=response)

    adapter._get_json = _raise_404  # type: ignore[method-assign]

    result = adapter.apply_server_to_selector("fwrouter-subject-missing", "vpn-global")

    assert result.ok is False
    assert result.error_code == "MIHOMO_SELECTOR_NOT_FOUND"
    assert result.details["http_status"] == 404


def test_mihomo_delay_accepts_logical_fallback_group(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    contours_path = tmp_path / "contours.yaml"
    _write_yaml(config_path, {"secret": "secret"})
    adapter = MihomoHttpAdapter(base_url=DEFAULT_BASE_URL, config_path=config_path, contours_path=contours_path)
    adapter._proxies = lambda: {  # type: ignore[method-assign]
        "logical profile": {"type": "Fallback", "all": ["member-a", "member-b"]},
        "member-a": {"type": "Vless"},
        "member-b": {"type": "Vless"},
    }
    adapter._delay_json = lambda *_args, **_kwargs: {"delay": 42}  # type: ignore[method-assign]

    result = adapter.check_delay("logical profile", timeout_ms=1000)

    assert result.ok is True
    assert result.delay_ms == 42
    assert result.details["logical_group"] is True


def test_mihomo_logical_group_state_normalizes_native_member_history(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    contours_path = tmp_path / "contours.yaml"
    _write_yaml(config_path, {"secret": "secret"})
    adapter = MihomoHttpAdapter(base_url=DEFAULT_BASE_URL, config_path=config_path, contours_path=contours_path)
    adapter._proxies = lambda: {
        "logical profile": {
            "type": "Fallback",
            "now": "member-b",
            "all": ["member-a", "member-b", "member-c"],
        },
        "member-a": {
            "type": "Vless",
            "alive": True,
            "history": [{"time": "2026-09-19T10:00:00Z", "delay": 41}],
        },
        "member-b": {
            "type": "Vless",
            "alive": False,
            "history": [{"time": "2026-09-19T10:01:00Z", "delay": 0}],
        },
        "member-c": {"type": "Vless", "alive": True, "history": []},
    }

    result = adapter.get_logical_group_state("logical profile")

    assert result["ok"] is True
    assert result["effective_member_runtime_identity"] == "member-b"
    assert result["evidence_source"] == "runtime_native"
    assert result["members"] == [
        {
            "runtime_identity": "member-a",
            "status": "healthy",
            "latency_ms": 41,
            "checked_at": "2026-09-19T10:00:00Z",
            "error_code": None,
            "error_message": None,
        },
        {
            "runtime_identity": "member-b",
            "status": "failed",
            "latency_ms": 0,
            "checked_at": "2026-09-19T10:01:00Z",
            "error_code": "RUNTIME_MEMBER_UNAVAILABLE",
            "error_message": None,
        },
        {
            "runtime_identity": "member-c",
            "status": "unknown",
            "latency_ms": None,
            "checked_at": None,
            "error_code": None,
            "error_message": None,
        },
    ]


def test_mihomo_zero_history_is_healthy_only_when_native_alive_and_well_formed(tmp_path: Path) -> None:
    adapter = MihomoHttpAdapter(
        base_url=DEFAULT_BASE_URL,
        config_path=tmp_path / "config.yaml",
        contours_path=tmp_path / "contours.json",
    )
    valid_time = "2026-10-10T12:00:00Z"
    adapter._proxies = lambda: {
        "group": {"type": "Fallback", "all": ["zero-live", "zero-dead", "missing", "bool-delay", "negative", "bad-time", "naive-time"]},
        "zero-live": {"alive": True, "history": [{"time": valid_time, "delay": 0}]},
        "zero-dead": {"alive": False, "history": [{"time": valid_time, "delay": 0}]},
        "missing": {"alive": True, "history": []},
        "bool-delay": {"alive": True, "history": [{"time": valid_time, "delay": True}]},
        "negative": {"alive": True, "history": [{"time": valid_time, "delay": -1}]},
        "bad-time": {"alive": True, "history": [{"time": "not-a-time", "delay": 1}]},
        "naive-time": {"alive": True, "history": [{"time": "2026-10-10T12:00:00", "delay": 1}]},
    }

    members = {item["runtime_identity"]: item for item in adapter.get_logical_group_state("group")["members"]}

    assert (members["zero-live"]["status"], members["zero-live"]["latency_ms"]) == ("healthy", 0)
    assert (members["zero-dead"]["status"], members["zero-dead"]["latency_ms"]) == ("failed", 0)
    for name in ("missing", "bool-delay", "negative", "bad-time", "naive-time"):
        assert members[name]["status"] == "unknown"
        assert members[name]["latency_ms"] is None


def test_mihomo_group_probe_accepts_zero_and_preserves_last_good_for_invalid_samples() -> None:
    checked_at = "2026-10-10T12:00:00+00:00"
    snapshot = {
        "ok": True,
        "members": [
            {"runtime_identity": "zero", "status": "unknown", "latency_ms": None, "checked_at": None},
            {"runtime_identity": "positive", "status": "unknown", "latency_ms": None, "checked_at": None},
            {"runtime_identity": "bad", "status": "healthy", "latency_ms": 17, "checked_at": "previous"},
            {"runtime_identity": "missing", "status": "unknown", "latency_ms": None, "checked_at": None},
        ],
    }

    result = MihomoHttpAdapter._apply_probe_delays(
        snapshot,
        {"zero": 0, "positive": 23, "bad": True},
        checked_at=checked_at,
    )
    members = {item["runtime_identity"]: item for item in result["members"]}

    assert (members["zero"]["status"], members["zero"]["latency_ms"]) == ("healthy", 0)
    assert (members["positive"]["status"], members["positive"]["latency_ms"]) == ("healthy", 23)
    assert members["bad"] == snapshot["members"][2]
    assert members["missing"] == snapshot["members"][3]


def test_mihomo_member_zero_503_requires_fresh_exact_url_state_readback(tmp_path: Path, monkeypatch) -> None:
    adapter = MihomoHttpAdapter(
        base_url=DEFAULT_BASE_URL,
        config_path=tmp_path / "config.yaml",
        contours_path=tmp_path / "contours.json",
    )
    adapter._proxies = lambda: {
        "group": {"type": "Fallback", "now": "member-a", "all": ["member-a"]},
        "member-a": {"type": "Vless", "alive": True, "history": []},
    }  # type: ignore[method-assign]
    probe_url = "https://probe.example.test/generate_204"
    request = httpx.Request("GET", f"{DEFAULT_BASE_URL}/proxies/member-a/delay")
    response = httpx.Response(
        503,
        request=request,
        json={"message": "An error occurred in the delay test"},
    )
    status_error = httpx.HTTPStatusError("503", request=request, response=response)
    adapter._delay_json = lambda *_args, **_kwargs: (_ for _ in ()).throw(status_error)  # type: ignore[method-assign]
    reads: list[tuple[str, float | None]] = []
    probe_start = datetime.fromisoformat("2026-10-10T12:00:00+00:00")
    sample_time = datetime.fromisoformat("2026-10-10T12:00:00.500000+00:00")
    observed_end = datetime.fromisoformat("2026-10-10T12:00:01+00:00")

    def read_proxy_state(path: str, *, timeout_seconds: float | None = None) -> dict:
        reads.append((path, timeout_seconds))
        return {
            "extra": {
                probe_url: {
                    "alive": True,
                    "history": [{"time": sample_time.isoformat(), "delay": 0}],
                },
            },
        }

    adapter._get_json = read_proxy_state  # type: ignore[method-assign]

    _use_adapter_clock(monkeypatch, probe_start, observed_end)
    result = adapter.check_delay("member-a", test_url=probe_url, timeout_ms=1000)

    assert result.ok is True
    assert result.delay_ms == 0
    assert reads == [("/proxies/member-a", 1.5)]
    zero_evidence = result.details["controller_response"]["native_zero_readback"]
    assert zero_evidence["evidence_source"] == "runtime_native_url_history"
    assert zero_evidence["controller_http_status"] == 503
    assert datetime.fromisoformat(zero_evidence["confirmed_at"]) == observed_end
    assert probe_url not in repr(zero_evidence)

    _use_adapter_clock(monkeypatch, probe_start, observed_end, observed_end, observed_end)
    member_probe = adapter.probe_logical_member("group", "member-a", test_url=probe_url, timeout_ms=1000)
    assert member_probe["ok"] is True
    assert member_probe["members"][0]["status"] == "healthy"
    assert member_probe["members"][0]["latency_ms"] == 0
    assert member_probe["probe_evidence"]["controller_http_status"] == 503
    assert reads == [("/proxies/member-a", 1.5), ("/proxies/member-a", 1.5)]


def test_mihomo_member_zero_503_without_exact_fresh_proof_stays_failed(tmp_path: Path, monkeypatch) -> None:
    adapter = MihomoHttpAdapter(
        base_url=DEFAULT_BASE_URL,
        config_path=tmp_path / "config.yaml",
        contours_path=tmp_path / "contours.json",
    )
    adapter._proxies = lambda: {
        "group": {"type": "Fallback", "now": "member-a", "all": ["member-a"]},
        "member-a": {"type": "Vless", "alive": True, "history": []},
    }  # type: ignore[method-assign]
    probe_url = "https://probe.example.test/generate_204"
    request = httpx.Request("GET", f"{DEFAULT_BASE_URL}/proxies/member-a/delay")
    generic_error = httpx.HTTPStatusError(
        "503",
        request=request,
        response=httpx.Response(
            503,
            request=request,
            json={"message": "An error occurred in the delay test"},
        ),
    )

    probe_start = datetime.fromisoformat("2026-10-10T12:00:00+00:00")
    sample_time = datetime.fromisoformat("2026-10-10T12:00:00.500000+00:00")
    observed_end = datetime.fromisoformat("2026-10-10T12:00:01+00:00")
    stale_time = datetime.fromisoformat("2026-10-10T11:59:59+00:00")
    future_time = datetime.fromisoformat("2026-10-10T12:00:02+00:00")

    def check_state(state_or_factory: Any) -> None:
        _use_adapter_clock(monkeypatch, probe_start, observed_end)
        adapter._delay_json = lambda *_args, **_kwargs: (_ for _ in ()).throw(generic_error)  # type: ignore[method-assign]
        state_factory = state_or_factory if callable(state_or_factory) else lambda: state_or_factory
        adapter._get_json = lambda *_args, **_kwargs: state_factory()  # type: ignore[method-assign]
        result = adapter.check_delay("member-a", test_url=probe_url, timeout_ms=1000)
        assert result.ok is False
        assert result.delay_ms is None
        assert result.error_code == "MIHOMO_DELAY_FAILED"

    fresh = lambda alive=True, delay=0, checked_at=sample_time: {
        "alive": alive,
        "history": [{"time": checked_at.isoformat(), "delay": delay}],
    }
    check_state(lambda: {"extra": {"another-url": fresh()}})
    check_state(lambda: {"extra": {probe_url: fresh(alive=False)}})
    check_state(lambda: {"extra": {probe_url: {"alive": True, "history": [{"time": sample_time.isoformat(), "delay": True}]}}})
    check_state(lambda: {"extra": {probe_url: fresh(delay=-1)}})
    check_state(lambda: {"extra": {probe_url: fresh(delay=65536)}})
    check_state(lambda: {"extra": {probe_url: {"alive": True, "history": [{"time": "not-a-time", "delay": 0}]}}})
    check_state(lambda: {"extra": {probe_url: {"alive": True, "history": [{"time": "2026-10-10T12:00:00", "delay": 0}]}}})
    check_state(lambda: {"extra": {probe_url: {"alive": True, "history": [{"delay": 0}]}}})
    check_state(lambda: {"extra": {probe_url: {"history": [{"time": sample_time.isoformat(), "delay": 0}]}}})
    check_state(lambda: {"alive": True, "history": [{"time": sample_time.isoformat(), "delay": 0}]})
    check_state(lambda: {"extra": {probe_url: fresh(checked_at=stale_time)}})
    check_state(lambda: {"extra": {probe_url: fresh(checked_at=future_time)}})

    _use_adapter_clock(monkeypatch, probe_start, observed_end, observed_end)
    adapter._delay_json = lambda *_args, **_kwargs: (_ for _ in ()).throw(generic_error)  # type: ignore[method-assign]
    adapter._get_json = lambda *_args, **_kwargs: {"extra": {}}  # type: ignore[method-assign]
    member_probe = adapter.probe_logical_member("group", "member-a", test_url=probe_url, timeout_ms=1000)
    assert member_probe["ok"] is False
    assert member_probe["members"] == []


def test_mihomo_delay_zero_fallback_does_not_probe_on_other_errors(tmp_path: Path, monkeypatch) -> None:
    adapter = MihomoHttpAdapter(
        base_url=DEFAULT_BASE_URL,
        config_path=tmp_path / "config.yaml",
        contours_path=tmp_path / "contours.json",
    )
    adapter._proxies = lambda: {"member-a": {"type": "Vless"}}  # type: ignore[method-assign]
    request = httpx.Request("GET", f"{DEFAULT_BASE_URL}/proxies/member-a/delay")
    calls: list[str] = []
    adapter._get_json = lambda path, **_kwargs: calls.append(path) or {"extra": {}}  # type: ignore[method-assign]

    adapter._delay_json = lambda *_args, **_kwargs: {"delay": 0}  # type: ignore[method-assign]
    normal_zero = adapter.check_delay("member-a", test_url="https://probe.example.test/generate_204")
    assert normal_zero.ok is True
    assert normal_zero.delay_ms == 0
    assert normal_zero.details["controller_response"] == {"delay": 0}
    assert calls == []

    for invalid_delay in (True, -1, 65536, 1.5, "0", None):
        adapter._delay_json = lambda *_args, _delay=invalid_delay, **_kwargs: {"delay": _delay}  # type: ignore[method-assign]
        invalid_result = adapter.check_delay("member-a", test_url="https://probe.example.test/generate_204")
        assert invalid_result.ok is False
        assert invalid_result.error_code == "MIHOMO_DELAY_RESPONSE_INVALID"

    for exception in (
        httpx.HTTPStatusError("429", request=request, response=httpx.Response(429, request=request)),
        httpx.HTTPStatusError("500", request=request, response=httpx.Response(500, request=request)),
        httpx.ReadTimeout("timeout", request=request),
        httpx.HTTPStatusError(
            "503 other",
            request=request,
            response=httpx.Response(503, request=request, json={"message": "unrelated"}),
        ),
    ):
        adapter._delay_json = lambda *_args, _exception=exception, **_kwargs: (_ for _ in ()).throw(_exception)  # type: ignore[method-assign]
        result = adapter.check_delay("member-a", test_url="https://probe.example.test/generate_204")
        assert result.ok is False

    assert calls == []

    generic_error = httpx.HTTPStatusError(
        "503",
        request=request,
        response=httpx.Response(
            503,
            request=request,
            json={"message": "An error occurred in the delay test"},
        ),
    )
    adapter._delay_json = lambda *_args, **_kwargs: (_ for _ in ()).throw(generic_error)  # type: ignore[method-assign]
    adapter._get_json = lambda *_args, **_kwargs: (_ for _ in ()).throw(httpx.ConnectTimeout("readback timeout"))  # type: ignore[method-assign]
    _use_adapter_clock(monkeypatch, datetime.fromisoformat("2026-10-10T12:00:00+00:00"))
    no_readback = adapter.check_delay("member-a", test_url="https://probe.example.test/generate_204")
    assert no_readback.ok is False
    assert no_readback.error_code == "MIHOMO_DELAY_FAILED"


def test_mihomo_bulk_logical_state_uses_one_runtime_inventory_snapshot(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    contours_path = tmp_path / "contours.yaml"
    _write_yaml(config_path, {"secret": "secret"})
    adapter = MihomoHttpAdapter(base_url=DEFAULT_BASE_URL, config_path=config_path, contours_path=contours_path)
    calls: list[bool] = []

    def _proxies() -> dict:
        calls.append(True)
        return {
            "group-a": {"type": "Fallback", "now": "member-a", "all": ["member-a"]},
            "group-b": {"type": "Fallback", "now": "member-b", "all": ["member-b"]},
            "member-a": {"alive": True, "history": [{"time": "2026-09-19T10:00:00Z", "delay": 41}]},
            "member-b": {"alive": True, "history": [{"time": "2026-09-19T10:00:00Z", "delay": 52}]},
        }

    adapter._proxies = _proxies

    result = adapter.get_logical_groups_state(["group-a", "group-b"])

    assert len(result) == 2
    assert calls == [True]


def test_mihomo_probe_delay_preserves_matching_runtime_timestamp() -> None:
    snapshot = {
        "members": [
            {
                "runtime_identity": "member-a",
                "status": "healthy",
                "latency_ms": 41,
                "checked_at": "2026-09-20T04:06:49.742098336Z",
                "error_code": None,
                "error_message": None,
            }
        ]
    }

    result = MihomoHttpAdapter._apply_probe_delays(
        snapshot,
        {"member-a": 41},
        checked_at="2026-09-20T04:06:50.100000+00:00",
    )

    assert result["members"][0]["checked_at"] == "2026-09-20T04:06:49.742098336Z"


def test_recovery_selection_snapshot_uses_one_bounded_nested_selector_read(monkeypatch, tmp_path):
    adapter = MihomoHttpAdapter(
        base_url=DEFAULT_BASE_URL,
        config_path=tmp_path / "config.yaml",
        contours_path=tmp_path / "contours.json",
    )
    proxies = {
        "vpn-global": {"type": "Selector", "now": "vpn-auto", "all": ["vpn-auto", "DIRECT"]},
        "vpn-auto": {"type": "Selector", "now": "logical-runtime-name", "all": ["logical-runtime-name"]},
        "logical-runtime-name": {"type": "Fallback", "now": "member-runtime-name", "all": ["member-runtime-name"]},
        "member-runtime-name": {"type": "Shadowsocks", "alive": True, "history": []},
    }
    reads = []
    monkeypatch.setattr(adapter, "_proxies", lambda *, timeout_seconds=None: reads.append(timeout_seconds) or proxies)

    result = adapter.get_recovery_selection_snapshot("logical-runtime-name", timeout_seconds=1.5)

    assert result == {
        "active_target": "logical-runtime-name",
        "effective_member_runtime_identity": "member-runtime-name",
        "ok": True,
    }
    assert reads == [1.5]
    assert result["active_target"] != "logical-provider-id"
    assert result["effective_member_runtime_identity"] != "provider-member-id"
