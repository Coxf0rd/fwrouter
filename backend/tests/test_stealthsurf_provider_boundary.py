from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
import json
import threading
import time

import httpx
import pytest

from fwrouter_api.adapters.stealthsurf import (
    ProviderError,
    RequestBudget,
    StealthSurfClient,
    provider_metrics_snapshot,
    record_provider_mutation_outcome,
)
from fwrouter_api.db.provider_managed import (
    ensure_schema,
    get_binding,
    list_members,
    record_discovery,
    record_config,
    save_binding,
    save_members,
)
from fwrouter_api.services.provider_adapters import ProviderRegistry




FIXTURES = __import__("pathlib").Path(__file__).parent / "fixtures" / "stealthsurf_api" / "2026-10-01"


def _client(handler, token: str = "test-key-for-provider-boundary") -> StealthSurfClient:
    return StealthSurfClient(token, transport=httpx.Client(transport=httpx.MockTransport(handler)))


def test_discovery_is_normalized_cached_and_budgeted() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=__import__("json").loads((FIXTURES / "09_configs_available-servers.json").read_text()))

    client = _client(handler)
    budget = RequestBudget(max_requests=1, deadline_seconds=2)
    first = client.discover(26, "hysteria2", budget=budget)
    second = client.discover(26, "hysteria2")
    assert first == second
    assert len(first) == 5
    assert first[0]["server_id"] == 1033
    assert first[0]["location_id"] == 26
    assert len(calls) == 1
    assert calls[0].url.params["protocol"] == "hysteria2"
    assert "test-key-for-provider-boundary" not in str(calls[0].url)


def test_synthetic_discovery_status_is_explicit_and_never_health_latency() -> None:
    # Synthetic negative/optional fields exercise cases absent from the 2026-10-01 audit fixtures.
    rows = [
        {"id": 1, "ip": "192.0.2.1", "available_slots": 9, "status": "down"},
        {"id": 2, "ip": "192.0.2.2", "available_slots": 8, "status": "busy"},
        {"id": 3, "ip": "192.0.2.3", "available_slots": 7, "available": False},
        {"id": 4, "ip": "192.0.2.4", "available_slots": 6, "available": True},
        {"id": 5, "ip": "192.0.2.5", "available_slots": 5, "status": None},
        {"id": 6, "ip": "192.0.2.6", "available_slots": 4},
        {"id": 7, "ip": "192.0.2.7", "available_slots": 3, "status": "unrecognized"},
        {"id": 8, "ip": "192.0.2.8", "available_slots": 0},
    ]

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": True, "statusCode": 200, "data": rows})

    members = _client(handler, token="synthetic-status-test-key").discover(26, "hysteria2")

    assert [(row["provider_status"], row["provider_status_source"]) for row in members] == [
        ("down", "status:down"), ("busy", "status:busy"), ("unavailable", "available:false"),
        ("available", "available:true"), ("unknown", "status:null"), ("unknown", "absent"),
        ("unknown", "status:invalid"), ("unknown", "absent"),
    ]
    assert all("health" not in row and "latency_ms" not in row for row in members)


def test_api_key_never_appears_in_safe_errors_and_no_mutation_retry() -> None:
    token = "secret-provider-test-key"
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(503, text=token)

    client = _client(handler, token)
    with pytest.raises(ProviderError) as error:
        client.switch_member(309293, 26, 1456)
    assert error.value.code == "PROVIDER_UNAVAILABLE"
    assert token not in str(error.value)
    assert len(requests) == 1
    assert requests[0].method == "PATCH"


def test_mutation_result_is_not_filled_from_requested_values() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": True, "statusCode": 200,
                                         "data": {"server_id": 2001, "connection_url": "credential-material"}})

    result = _client(handler).switch_member(309293, 26, 1456)
    assert result == {"server_id": 2001, "connection_url": "credential-material"}
    assert "location_id" not in result
    assert "protocol" not in result


def test_binding_storage_retains_member_history_and_drops_credential_material() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    ensure_schema(conn)
    binding = save_binding(conn, "subscription-a", "stealthsurf", 309293,
                           "logical-server-a", "hysteria2", True)
    assert binding["enabled"] == 1
    record_discovery(conn, "subscription-a", 1, 26, "hysteria2", [
        {"server_id": 1456, "location_id": 26, "protocol": "hysteria2", "ip": "192.0.2.4", "available_slots": 7},
        {"server_id": 2310, "location_id": 26, "protocol": "hysteria2", "ip": "192.0.2.5", "available_slots": 8},
    ], observed_at=10)
    record_discovery(conn, "subscription-a", 2, 26, "hysteria2", [
        {"server_id": 2310, "location_id": 26, "protocol": "hysteria2", "ip": "192.0.2.5", "available_slots": 6},
    ], observed_at=20)
    record_config(conn, "subscription-a", 1, {
        "server_id": 2310, "location_id": 26, "protocol": "hysteria2",
        "connection_url": "must-not-persist",
    }, observed_at=21)
    members = list_members(conn, "subscription-a")
    assert len(members) == 2
    assert {row["provider_member_id"]: row["advertised"] for row in members} == {"1456": 0, "2310": 1}
    evidence = conn.execute("SELECT safe_json FROM provider_evidence").fetchone()[0]
    assert "must-not-persist" not in evidence
    assert get_binding(conn, "subscription-a")["current_member_id"] == "2310"


def test_config_evidence_tracks_nonadvertised_current_and_partial_identity() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    ensure_schema(conn)
    save_binding(conn, "subscription-current", "stealthsurf", 309293,
                 "logical-current", "hysteria2", True)
    record_config(conn, "subscription-current", 1,
                  {"server_id": 777, "location_id": 26, "protocol": "hysteria2"}, observed_at=10)
    record_config(conn, "subscription-current", 1,
                  {"connection_url": "discarded"}, observed_at=11, outcome="partial")
    member = list_members(conn, "subscription-current")[0]
    binding = get_binding(conn, "subscription-current")
    assert member["provider_member_id"] == "777"
    assert member["advertised"] == 0
    assert binding["current_member_id"] == "777"
    assert binding["current_location_id"] == "26"
    assert binding["last_outcome"] == "partial"


def test_multiple_enabled_bindings_are_source_scoped_and_revision_checked() -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    save_binding(conn, "a", "stealthsurf", 1, "logical-a", "hysteria2", True)
    save_binding(conn, "b", "stealthsurf", 2, "logical-b", "hysteria2", True)
    assert get_binding(conn, "a")["enabled"] == get_binding(conn, "b")["enabled"] == 1
    with pytest.raises(RuntimeError, match="REVISION_CONFLICT"):
        save_binding(conn, "a", "stealthsurf", 1, "logical-a", "hysteria2", True, expected_revision=99)


def test_provider_registry_is_explicit() -> None:
    registry = ProviderRegistry()
    assert registry.ids() == ()
    with pytest.raises(LookupError):
        registry.get("stealthsurf")


def test_singleflight_coalesces_concurrent_cold_cache_reads() -> None:
    calls = 0
    calls_lock = threading.Lock()
    started = threading.Event()

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        with calls_lock:
            calls += 1
        started.set()
        time.sleep(0.1)
        return httpx.Response(200, json={"status": True, "statusCode": 200, "data": []})

    client = _client(handler, token="singleflight-test-key")
    with ThreadPoolExecutor(max_workers=2) as pool:
        left = pool.submit(client.get_locations)
        assert started.wait(1)
        right = pool.submit(client.get_locations)
        assert left.result(timeout=2) == right.result(timeout=2) == []
    assert calls == 1


def test_explicit_fresh_config_fetch_and_mutation_invalidate_cache() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.method == "PATCH":
            return httpx.Response(200, json={"status": True, "statusCode": 200,
                                             "data": {"server_id": 88, "connection_url": "fresh"}})
        server_id = 77 if len(calls) == 1 else 88
        return httpx.Response(200, json={"status": True, "statusCode": 200,
                                         "data": [{"id": 5, "server_id": server_id}]})

    client = _client(handler, token="invalidate-cache-key")
    assert client.get_configs(5)[0]["server_id"] == 77
    assert client.get_configs(5, max_age_s=0)[0]["server_id"] == 88
    assert client.get_configs(5)[0]["server_id"] == 88
    assert len(calls) == 2
    client.switch_member(5, 26, 88, "hysteria2")
    assert calls[-1].method == "PATCH"
    assert json.loads(calls[-1].content) == {"location_id": 26, "protocol": "hysteria2", "server_id": 88}
    client.get_configs(5)
    assert len(calls) == 4


def test_rate_limit_errors_expose_only_bounded_retry_time() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"X-RateLimit-Reset": "8"}, text="private response")

    with pytest.raises(ProviderError) as error:
        _client(handler, token="rate-limit-test-key").get_locations()
    assert error.value.code == "PROVIDER_RATE_LIMIT"
    assert error.value.retry_after_seconds == 8.0
    assert error.value.status_code == 429
    assert "private response" not in str(error.value)


def test_rate_limit_retry_after_blocks_account_before_next_http_request(monkeypatch) -> None:
    now = [100.0]
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(429, headers={"Retry-After": "8"})
        return httpx.Response(200, json={"status": True, "statusCode": 200, "data": []})

    monkeypatch.setattr("fwrouter_api.adapters.stealthsurf.time.monotonic", lambda: now[0])
    client = _client(handler, token="retry-admission-test-key")
    with pytest.raises(ProviderError) as first:
        client.discover(71, "hysteria2", max_age_s=0)
    assert first.value.retry_after_seconds == 8
    with pytest.raises(ProviderError) as blocked:
        client.discover(71, "hysteria2", max_age_s=0)
    assert blocked.value.code == "RATE_LIMITED"
    assert blocked.value.retry_after_seconds == 8
    assert len(requests) == 1

    now[0] += 9
    assert client.discover(71, "hysteria2", max_age_s=0) == []
    assert len(requests) == 2


def test_successful_remaining_quota_is_bound_to_endpoint_bucket(monkeypatch) -> None:
    now = [200.0]
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        remaining = "1" if len(requests) == 1 else "0"
        return httpx.Response(200, headers={"X-RateLimit-Remaining": remaining,
                                            "X-RateLimit-Reset": "8"},
                             json={"status": True, "statusCode": 200, "data": {"status": "up"} if request.url.path.endswith("serverStats") else []})

    monkeypatch.setattr("fwrouter_api.adapters.stealthsurf.time.monotonic", lambda: now[0])
    client = _client(handler, token="successful-quota-test-key")
    assert client.discover(72, "hysteria2", max_age_s=0) == []
    # The positive remaining value admitted exactly one further discovery request.
    assert client.discover(72, "hysteria2", max_age_s=0) == []
    assert len(requests) == 2
    with pytest.raises(ProviderError) as blocked:
        client.discover(72, "hysteria2", max_age_s=0)
    assert blocked.value.code == "RATE_LIMITED"
    assert blocked.value.retry_after_seconds == 8
    assert len(requests) == 2
    # The discovery bucket restriction does not consume the separate stats quota.
    client.get_server_stats(73)
    assert len(requests) == 3


@pytest.mark.parametrize(("provider_status", "expected"), [
    ("up", "up"), ("down", "down"), ("offline", "down"),
    ("unavailable", "unavailable"),
    ("credential leaked in arbitrary provider text", "unknown"),
    (None, "unknown"),
])
def test_server_stats_exposes_only_normalized_status(provider_status, expected) -> None:
    client = _client(lambda _request: httpx.Response(
        200, json={"status": True, "statusCode": 200,
                   "data": {"status": provider_status, "host": "sensitive.example", "stats": {"token": "secret"}}}),
        token=f"stats-safe-{expected}-{provider_status}")

    result = client.get_server_stats(78)

    assert result == {"status": expected}
    assert "sensitive" not in repr(result)
    assert "secret" not in repr(result)


def test_redirect_is_not_followed_with_provider_authorization() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(302, headers={"Location": "https://attacker.invalid/collect"})

    with pytest.raises(ProviderError):
        _client(handler, token="redirect-test-key").get_locations()
    assert len(requests) == 1
    assert str(requests[0].url).startswith("https://api.stealthsurf.net/")


def test_switch_invalidates_current_member_stats_cache() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method + " " + request.url.path)
        if request.method == "PATCH":
            return httpx.Response(200, json={"status": True, "statusCode": 200,
                                             "data": {"server_id": 88, "connection_url": "fresh"}})
        return httpx.Response(200, json={"status": True, "statusCode": 200,
                                         "data": {"status": "up"}})

    client = _client(handler, token="stats-invalidation-key")
    client.get_server_stats(5)
    client.get_server_stats(5)
    assert calls == ["GET /configs/5/serverStats"]
    client.switch_member(5, 26, 88)
    client.get_server_stats(5)
    assert calls.count("GET /configs/5/serverStats") == 2


def test_request_budget_caps_calls_and_deadline() -> None:
    budget = RequestBudget(max_requests=1, deadline_seconds=10)
    budget.started_at = 100
    budget.consume(now=100)
    with pytest.raises(ProviderError, match="BUDGET_EXHAUSTED"):
        budget.consume(now=101)
    expired = RequestBudget(max_requests=2, deadline_seconds=0.5)
    expired.started_at = 10
    expired.consume(now=10)
    with pytest.raises(ProviderError, match="BUDGET_EXHAUSTED"):
        expired.consume(now=10.5)


def test_singleflight_wait_is_bounded_by_operation_deadline() -> None:
    started = threading.Event()

    def handler(_request: httpx.Request) -> httpx.Response:
        started.set()
        time.sleep(0.2)
        return httpx.Response(200, json={"status": True, "statusCode": 200, "data": []})

    client = _client(handler, token="singleflight-deadline-test-key")
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(client.get_locations)
        assert started.wait(1)
        budget = RequestBudget(max_requests=1, deadline_seconds=0.05)
        second = pool.submit(client.get_locations, budget=budget)
        with pytest.raises(ProviderError, match="BUDGET_EXHAUSTED"):
            second.result(timeout=0.5)
        assert first.result(timeout=1) == []


def test_metrics_are_bounded_to_safe_endpoint_templates() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": True, "statusCode": 200,
                                         "data": [{"id": 4, "ip": "192.0.2.1", "available_slots": 2}]})

    before = provider_metrics_snapshot()
    client = _client(handler, token="metrics-secret-token")
    client.discover(44, "hysteria2", budget=RequestBudget(1, 2, operation="refresh"))
    record_provider_mutation_outcome("verified")
    after = provider_metrics_snapshot()

    assert after["discoveries"] >= before["discoveries"] + 1
    assert after["requests"]["refresh GET /configs/available-servers"] >= 1
    assert after["mutation_outcomes"]["verified"] >= 1
    assert all("44" not in endpoint for endpoint in after["requests"])
    assert "metrics-secret-token" not in repr(after)
    assert "192.0.2.1" not in repr(after)
