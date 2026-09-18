from __future__ import annotations

import httpx

from fwrouter_api.services import subject_proxy_get


class _Response:
    status_code = 204

    def raise_for_status(self):
        return None


class _Client:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get(self, url):
        return _Response()


def _subject(kind="xray", source="vpn_auto", selected="vpn-global"):
    return {
        "implementation_kind": kind,
        "effective_state": {
            "effective_mode": "VPN",
            "selected_server_source": source,
            "selected_server_id": selected,
        },
    }


def test_proxy_get_auto_uses_binding_handoff_and_runtime_selector(monkeypatch):
    monkeypatch.setattr(subject_proxy_get, "get_subject_with_effective_state", lambda _: _subject())
    monkeypatch.setattr(subject_proxy_get, "_load_xray_bindings_state", lambda: {"bindings": [{"subject_id": "xray:auto", "status": "applied", "handoff_proxy_name": "vpn-global", "handoff": {"listen": "172.18.0.1", "port": 53001}}]})
    monkeypatch.setattr(subject_proxy_get, "get_vpn_auto_state", lambda: {"selector_runtime": {"vpn_auto_now": "concrete-us"}})
    monkeypatch.setattr(subject_proxy_get.httpx, "Client", _Client)

    result = subject_proxy_get.check_subject_proxy_get(subject_id="xray:auto")

    assert result["status"] == "success"
    assert result["http_status"] == 204
    assert result["latency_ms"] > 0
    assert result["selected_server_id"] == "vpn-global"
    assert result["effective_runtime_target"] == "concrete-us"


def test_proxy_get_fixed_failure_never_uses_auto_selector(monkeypatch):
    monkeypatch.setattr(subject_proxy_get, "get_subject_with_effective_state", lambda _: _subject(source="subject_override", selected="sub:dead"))
    monkeypatch.setattr(subject_proxy_get, "_load_xray_bindings_state", lambda: {"bindings": [{"subject_id": "xray:fixed", "status": "applied", "handoff_proxy_name": "concrete-dead", "handoff": {"listen": "127.0.0.1", "port": 53002}}]})
    monkeypatch.setattr(subject_proxy_get, "get_vpn_auto_state", lambda: (_ for _ in ()).throw(AssertionError("fixed route must not resolve vpn-auto")))

    class TimeoutClient(_Client):
        def get(self, url):
            raise httpx.ConnectTimeout("timeout")

    monkeypatch.setattr(subject_proxy_get.httpx, "Client", TimeoutClient)
    result = subject_proxy_get.check_subject_proxy_get(subject_id="xray:fixed")

    assert result["status"] == "failed"
    assert result["error_code"] == "CONNECT_TIMEOUT"
    assert result["selected_server_id"] == "sub:dead"
    assert result["effective_runtime_target"] == "concrete-dead"


def test_proxy_get_reports_missing_subject_and_binding(monkeypatch):
    monkeypatch.setattr(subject_proxy_get, "get_subject_with_effective_state", lambda _: None)
    assert subject_proxy_get.check_subject_proxy_get(subject_id="missing")["error_code"] == "SUBJECT_NOT_FOUND"

    monkeypatch.setattr(subject_proxy_get, "get_subject_with_effective_state", lambda _: _subject())
    monkeypatch.setattr(subject_proxy_get, "_load_xray_bindings_state", lambda: {"bindings": []})
    assert subject_proxy_get.check_subject_proxy_get(subject_id="xray:missing")["error_code"] == "XRAY_BINDING_MISSING"
