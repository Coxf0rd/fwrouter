from __future__ import annotations

from contextlib import contextmanager
import sqlite3
from types import SimpleNamespace
from contextlib import nullcontext
import pytest

from fwrouter_api.db import provider_managed as store
from fwrouter_api.services import provider_managed
from fwrouter_api.adapters.provider_base import ProviderError


CONNECTION_URL = "hysteria2://test-password@vpn.example.test:443?sni=example.test#fixture-node"


class FakeAdapter:
    provider_id = "stealthsurf"
    supported_protocols = ("hysteria2",)

    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.closed = False
        self.mutation_result = {"id": 1234, "server_id": 901, "location_id": 6,
                                "protocol": "hysteria2", "connection_url": CONNECTION_URL}
        self.switch_error: Exception | None = None

    def get_configs(self, config_id=None, *, budget=None, max_age_s=None):
        self.calls.append(("get_configs", config_id, budget, max_age_s))
        return [self.mutation_result]

    def get_locations(self, *, budget=None):
        self.calls.append(("get_locations",))
        return []

    def discover(self, location_id, protocol, *, max_age_s=10, budget=None):
        self.calls.append(("discover", location_id, protocol, max_age_s))
        return [{"server_id": 901, "location_id": location_id, "protocol": protocol,
                 "ip": "192.0.2.9", "available_slots": 2}]

    def get_server_stats(self, config_id, *, budget=None):
        self.calls.append(("stats", config_id))
        return {"status": "up"}

    def switch_member(self, config_id, location_id, server_id, protocol="hysteria2", *, budget=None):
        self.calls.append(("switch", config_id, location_id, server_id, protocol))
        if self.switch_error:
            raise self.switch_error
        if callable(self.mutation_result):
            return self.mutation_result(config_id, location_id, server_id, protocol)
        return self.mutation_result

    def change_protocol(self, config_id, location_id, protocol, *, budget=None):
        self.calls.append(("change_protocol", config_id, location_id, protocol))
        return {"server_id": 901, "connection_url": CONNECTION_URL}

    def close(self):
        self.closed = True


def _db(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    store.ensure_schema(conn)
    conn.execute("CREATE TABLE servers (server_id TEXT PRIMARY KEY, server_name TEXT, raw_json TEXT, inventory_state TEXT, provider_name TEXT)")
    conn.execute("CREATE TABLE server_preferences (server_id TEXT PRIMARY KEY, vpn_auto INTEGER, vpn_auto_priority INTEGER, manually_deleted_at TEXT DEFAULT '')")
    conn.execute("CREATE TABLE subscription_server_memberships (source_id TEXT, server_id TEXT, is_active INTEGER)")
    conn.execute("CREATE TABLE logical_server_members (logical_server_id TEXT, member_id TEXT, member_runtime_name TEXT, is_active INTEGER DEFAULT 1)")
    conn.execute("CREATE TABLE server_custom_https_proxy (server_id TEXT PRIMARY KEY)")
    conn.execute("CREATE TABLE routing_global_state (id INTEGER PRIMARY KEY, server_mode TEXT, active_auto_server_id TEXT)")
    conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value_json TEXT NOT NULL, updated_at TEXT)")

    @contextmanager
    def session():
        yield conn

    monkeypatch.setattr(provider_managed, "db_session", session)
    return conn


def _binding(conn, *, source_ref="source-a", enabled=True):
    return store.save_binding(conn, source_ref, "stealthsurf", 1234, "logical-provider-vpn",
                              "hysteria2", enabled)


def _operation_setup(monkeypatch, *, enabled=True):
    conn = _db(monkeypatch)
    binding = _binding(conn, enabled=enabled)
    conn.execute("UPDATE provider_bindings SET current_member_id='900', current_location_id='6' WHERE source_ref='source-a'")
    store.record_discovery(conn, "source-a", binding["binding_revision"], 6, "hysteria2", [
        {"server_id": 900, "ip": "192.0.2.8", "available_slots": 2},
        {"server_id": 901, "ip": "192.0.2.9", "available_slots": 2},
        {"server_id": 902, "ip": "192.0.2.10", "available_slots": 2},
    ])
    conn.execute("INSERT INTO server_preferences (server_id, vpn_auto, vpn_auto_priority) VALUES ('logical-provider-vpn', 1, 0)")
    fake = FakeAdapter()
    monkeypatch.setattr(provider_managed, "provider_adapter", lambda *_args, **_kwargs: fake)
    monkeypatch.setattr("fwrouter_api.services.subscription._subscription_url_for_source_ref", lambda _source: "saved-source")
    monkeypatch.setattr("fwrouter_api.adapters.xray_common.xray_writer_guard", lambda *args, **kwargs: nullcontext())
    return conn, binding, fake


def test_switch_verified_reports_actual_member_when_provider_falls_back(monkeypatch) -> None:
    conn, binding, fake = _operation_setup(monkeypatch)
    fake.mutation_result = {"id": 1234, "server_id": 901, "location_id": 6,
                            "protocol": "hysteria2", "connection_url": CONNECTION_URL}
    callback = {}
    monkeypatch.setattr(provider_managed, "_refresh_with_material", lambda *_args, **kwargs: callback.update(kwargs) or {
        "ok": True, "runtime_verified": True, "last_good_retained": False,
    })

    result = provider_managed.execute_provider_operation("source-a", "switch", member_id="902",
                                                         expected_revision=binding["binding_revision"], _adapter=fake,
                                                         _select_logical=True)

    assert result["ok"] is True
    assert result["outcome"] == "verified"
    assert result["requested_member_id"] == "902"
    assert result["actual_member_id"] == "901"
    assert [call[0] for call in fake.calls] == ["switch"]
    assert callback["select_logical"] is True
    assert type(callback["selection_revision"]) is int
    assert callback["operation_id"]
    assert store.get_binding(conn, "source-a")["current_member_id"] == "901"


def test_incomplete_mutation_material_uses_one_targeted_get(monkeypatch) -> None:
    _conn, binding, fake = _operation_setup(monkeypatch)
    fake.mutation_result = lambda *_args, **_kwargs: {"server_id": 902, "connection_url": CONNECTION_URL}
    fake.get_configs = lambda config_id=None, *, budget=None, max_age_s=None: (
        fake.calls.append(("get_configs", config_id, budget, max_age_s)) or
        [{"id": 1234, "server_id": 902, "location_id": 6, "protocol": "hysteria2",
          "connection_url": CONNECTION_URL}]
    )
    monkeypatch.setattr(provider_managed, "_refresh_with_material", lambda *_args, **_kwargs: {
        "ok": True, "runtime_verified": True,
    })

    result = provider_managed.execute_provider_operation("source-a", "switch", member_id="902",
                                                         expected_revision=binding["binding_revision"], _adapter=fake)

    assert result["outcome"] == "verified"
    assert [call[0] for call in fake.calls].count("get_configs") == 1


def test_accepted_switch_with_local_failure_is_partial_and_keeps_last_good(monkeypatch) -> None:
    _conn, binding, fake = _operation_setup(monkeypatch)
    monkeypatch.setattr(provider_managed, "_refresh_with_material", lambda *_args, **_kwargs: {
        "ok": False, "runtime_verified": False, "last_good_retained": True,
    })

    result = provider_managed.execute_provider_operation("source-a", "switch", member_id="902",
                                                         expected_revision=binding["binding_revision"], _adapter=fake)

    assert result == {
        "ok": False, "outcome": "partial", "runtime_verified": False,
        "last_good_retained": True, "source_ref": "source-a", "actual_member_id": "901",
        "requested_member_id": "902", "error_code": "PROVIDER_LOCAL_VERIFICATION_FAILED", "changed": None,
    }


def test_ambiguous_switch_timeout_is_single_attempt_without_replay(monkeypatch) -> None:
    _conn, binding, fake = _operation_setup(monkeypatch)
    fake.switch_error = ProviderError("PROVIDER_TIMEOUT", retryable=True)
    monkeypatch.setattr(provider_managed, "_refresh_with_material", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not refresh")))

    result = provider_managed.execute_provider_operation("source-a", "switch", member_id="902",
                                                         expected_revision=binding["binding_revision"], _adapter=fake)

    assert result["outcome"] == "unconfirmed"
    assert result["last_good_retained"] is True
    assert result["error_code"] == "PROVIDER_TIMEOUT"
    assert [call[0] for call in fake.calls] == ["switch"]


def test_failed_enable_preserves_explicit_intent_without_claiming_apply(monkeypatch) -> None:
    conn, binding, fake = _operation_setup(monkeypatch, enabled=True)
    fake.get_configs = lambda *_args, **_kwargs: (_ for _ in ()).throw(ProviderError("PROVIDER_UNAVAILABLE", retryable=True))

    result = provider_managed.execute_provider_operation("source-a", "enable", _adapter=fake)

    assert not result["ok"]
    assert store.get_binding(conn, "source-a")["enabled"] == 1
    assert result["last_good_retained"]
    assert store.get_binding(conn, "source-a")["logical_server_id"] == binding["logical_server_id"]


def test_targeted_fetch_uses_only_bound_config_and_stable_logical_identity(monkeypatch) -> None:
    conn = _db(monkeypatch)
    binding = _binding(conn)
    fake = FakeAdapter()
    monkeypatch.setattr(provider_managed, "binding_for", lambda _source: binding)
    monkeypatch.setattr(provider_managed, "provider_adapter", lambda *_args, **_kwargs: fake)

    result = provider_managed.fetch_provider_subscription("source-a")

    assert result.ok
    assert len(result.servers) == 1
    assert result.servers[0].server_id == "logical-provider-vpn"
    assert result.servers[0].raw_identity == "logical-provider-vpn"
    assert fake.calls == [("get_configs", 1234, fake.calls[0][2], 0)]
    assert not any(call[0] in {"discover", "get_locations", "stats"} for call in fake.calls)
    assert fake.closed
    stored = store.get_binding(conn, "source-a")
    assert stored["current_member_id"] is None
    assert stored["logical_server_id"] == "logical-provider-vpn"
    assert result.to_dict()["metadata"].get("_fwrouter_provider_handoff") is None
    assert result._fwrouter_provider_handoff["member_id"] == "901"


def test_material_handoff_skips_provider_api_and_preserves_current_evidence(monkeypatch) -> None:
    conn = _db(monkeypatch)
    binding = _binding(conn)
    store.record_config(conn, "source-a", binding["binding_revision"],
                        {"server_id": 800, "location_id": 6, "protocol": "hysteria2"})
    monkeypatch.setattr(provider_managed, "binding_for", lambda _source: binding)
    monkeypatch.setattr(provider_managed, "provider_adapter", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("unexpected provider call")))

    with provider_managed.material_handoff(binding, {
        "id": 1234, "server_id": 800, "location_id": 6, "protocol": "hysteria2",
        "connection_url": CONNECTION_URL,
    }):
        result = provider_managed.fetch_provider_subscription("source-a")

    assert result.ok
    assert store.get_binding(conn, "source-a")["current_member_id"] == "800"


def test_provider_candidates_are_local_and_do_not_require_member_runtime_latency(monkeypatch) -> None:
    conn = _db(monkeypatch)
    binding = _binding(conn)
    conn.execute("UPDATE provider_bindings SET current_member_id='old', current_location_id='6' WHERE source_ref='source-a'")
    store.record_discovery(conn, "source-a", binding["binding_revision"], 6, "hysteria2", [
        {"server_id": 901, "ip": "192.0.2.9", "available_slots": 2},
    ], observed_at=100)
    conn.execute("INSERT INTO server_preferences (server_id, vpn_auto, vpn_auto_priority) VALUES ('logical-provider-vpn', 1, 0)")
    monkeypatch.setattr(provider_managed.time, "time", lambda: 101)
    monkeypatch.setattr(provider_managed, "provider_adapter", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("selector must not fetch provider")))

    candidates = provider_managed.provider_candidates("source-a")

    assert len(candidates) == 1
    assert candidates[0]["server_id"] == "logical-provider-vpn"
    assert candidates[0]["member_id"] == "901"
    assert candidates[0]["runtime_evidence"] == "not_observed"
    assert candidates[0]["ping"]["status"] == "unknown"
    assert candidates[0]["ping"]["last_ping_ms"] is None


def test_provider_candidates_exclude_explicit_unavailable_states(monkeypatch) -> None:
    conn = _db(monkeypatch)
    binding = _binding(conn)
    conn.execute("UPDATE provider_bindings SET current_member_id='old', current_location_id='6' WHERE source_ref='source-a'")
    store.record_discovery(conn, "source-a", binding["binding_revision"], 6, "hysteria2", [
        {"server_id": 901, "available_slots": 4, "provider_status": "busy"},
        {"server_id": 902, "available_slots": 4, "provider_status": "down"},
        {"server_id": 903, "available_slots": 4, "provider_status": "unavailable"},
        {"server_id": 904, "available_slots": 4, "provider_status": "unknown"},
        {"server_id": 905, "available_slots": 4, "provider_status": "available"},
    ], observed_at=100)
    conn.execute("INSERT INTO server_preferences (server_id, vpn_auto, vpn_auto_priority) VALUES ('logical-provider-vpn', 1, 0)")
    monkeypatch.setattr(provider_managed.time, "time", lambda: 101)

    candidates = provider_managed.provider_candidates("source-a")

    assert {candidate["member_id"] for candidate in candidates} == {"904", "905"}


def test_preferences_partial_updates_preserve_unspecified_fields(monkeypatch) -> None:
    conn, binding, _fake = _operation_setup(monkeypatch)
    store.update_member_preference(conn, "source-a", 901, 6, "hysteria2", auto_enabled=False,
                                   priority=3, expected_binding_revision=binding["binding_revision"])

    auto_result = provider_managed.execute_provider_operation(
        "source-a", "preferences", member_id="901", auto=True,
        expected_revision=binding["binding_revision"])
    member = next(row for row in store.list_members(conn, "source-a") if row["provider_member_id"] == "901")
    assert auto_result["ok"] is True
    assert member["auto_enabled"] == 1
    assert member["priority"] == 3

    priority_result = provider_managed.execute_provider_operation(
        "source-a", "preferences", member_id="901", priority=2,
        expected_revision=binding["binding_revision"])
    member = next(row for row in store.list_members(conn, "source-a") if row["provider_member_id"] == "901")
    assert priority_result["ok"] is True
    assert member["auto_enabled"] == 1
    assert member["priority"] == 2


def test_unexpected_targeted_fetch_error_is_sanitized(monkeypatch) -> None:
    binding = _binding(_db(monkeypatch))
    monkeypatch.setattr(provider_managed, "binding_for", lambda _source: binding)
    monkeypatch.setattr(provider_managed, "provider_adapter",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("secret-url?token=fixture-secret")))

    result = provider_managed.fetch_provider_subscription("source-a")

    assert not result.ok
    assert result.error_code == "PROVIDER_OPERATION_FAILED"
    assert "fixture-secret" not in repr(result)


def test_disabled_binding_does_not_enter_provider_path(monkeypatch) -> None:
    _db(monkeypatch)
    binding = _binding(_db(monkeypatch), enabled=False)
    monkeypatch.setattr(provider_managed, "binding_for", lambda _source: binding)
    monkeypatch.setattr(provider_managed, "provider_adapter", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("disabled source called provider")))

    result = provider_managed.fetch_provider_subscription("source-a")

    assert result is None


def test_revision_changes_fail_closed_before_material_is_parsed(monkeypatch) -> None:
    binding = _binding(_db(monkeypatch))
    current = {**binding, "binding_revision": binding["binding_revision"] + 1}
    reads = iter((binding, current))
    monkeypatch.setattr(provider_managed, "binding_for", lambda _source: next(reads))
    fake = FakeAdapter()
    monkeypatch.setattr(provider_managed, "provider_adapter", lambda *_args, **_kwargs: fake)

    result = provider_managed.fetch_provider_subscription("source-a")

    assert not result.ok
    assert result.error_code == "PROVIDER_BINDING_REVISION_CONFLICT"
    assert fake.closed


def test_storage_preference_and_empty_discovery_keep_historical_member(monkeypatch) -> None:
    conn = _db(monkeypatch)
    binding = _binding(conn)
    store.record_discovery(conn, "source-a", 1, 6, "hysteria2", [
        {"server_id": 901, "ip": "192.0.2.9", "available_slots": 2},
    ], observed_at=100, expected_binding_revision=binding["binding_revision"])
    store.update_member_preference(conn, "source-a", 901, 6, "hysteria2", auto_enabled=False,
                                   priority=3, expected_binding_revision=binding["binding_revision"])
    store.record_discovery(conn, "source-a", 2, 6, "hysteria2", [], observed_at=110,
                           expected_binding_revision=binding["binding_revision"])
    member = store.list_members(conn, "source-a")[0]
    assert member["advertised"] == 0
    assert member["auto_enabled"] == 0
    assert member["priority"] == 3

    store.update_member_preference(conn, "source-a", 901, 6, "hysteria2", priority=-1,
                                   expected_binding_revision=binding["binding_revision"])
    member = store.list_members(conn, "source-a")[0]
    assert member["priority"] == -1


def test_current_member_provider_evidence_is_scoped_and_safe(monkeypatch) -> None:
    conn = _db(monkeypatch)
    binding = _binding(conn)
    store.update_observation(conn, "source-a", kind="server_stats",
                             safe_data={"status": "up", "member_id": "901", "protocol": "hysteria2",
                                        "authorization": "never-store"},
                             revision=binding["binding_revision"], scope_key="member:901:hysteria2",
                             expected_binding_revision=binding["binding_revision"])
    latest = store.latest_evidence(conn, "source-a", "server_stats")
    assert latest["scope_key"] == "member:901:hysteria2"
    assert latest["data"] == {"status": "up", "member_id": "901", "protocol": "hysteria2"}


def test_candidate_status_filter_rejects_only_explicit_negative_provider_evidence() -> None:
    assert not store.provider_status_allows_candidate("down")
    assert not store.provider_status_allows_candidate("unavailable")
    assert not store.provider_status_allows_candidate("busy")
    assert store.provider_status_allows_candidate("available")
    assert store.provider_status_allows_candidate("unknown")
    assert store.provider_status_allows_candidate(None)


def test_discovery_persists_status_provenance_without_local_health_semantics(monkeypatch) -> None:
    conn = _db(monkeypatch)
    _binding(conn)
    store.record_discovery(conn, "source-a", 1, 6, "hysteria2", [
        {"server_id": 901, "ip": "192.0.2.9", "available_slots": 5,
         "provider_status": "busy", "provider_status_source": "status:busy"},
        {"server_id": 902, "ip": "192.0.2.10", "available_slots": 3,
         "provider_status": "unknown", "provider_status_source": "status:null"},
    ], observed_at=100)
    members = {row["provider_member_id"]: row for row in store.list_members(conn, "source-a")}
    assert members["901"]["provider_status"] == "busy"
    assert members["901"]["provider_status_source"] == "status:busy"
    assert members["902"]["provider_status"] == "unknown"
    assert "health" not in members["901"]
    assert "latency_ms" not in members["901"]


def test_provider_observed_member_and_runtime_applied_member_are_separate(monkeypatch) -> None:
    conn = _db(monkeypatch)
    binding = _binding(conn)
    store.record_config(conn, "source-a", binding["binding_revision"],
                        {"server_id": 901, "location_id": 6, "protocol": "hysteria2"})
    store.record_applied(conn, "source-a", revision=14, member_id=900, protocol="hysteria2",
                         applied_at=200, expected_binding_revision=binding["binding_revision"])

    current = store.get_binding(conn, "source-a")

    assert current["current_member_id"] == "901"
    assert current["applied_member_id"] == "900"
    assert current["applied_protocol"] == "hysteria2"
    assert current["applied_revision"] == 14
    assert current["applied_at"] == 200


def test_projection_is_backend_safe_and_does_not_expose_resource_or_material(monkeypatch) -> None:
    conn = _db(monkeypatch)
    _binding(conn)
    store.record_discovery(conn, "source-a", 1, 6, "hysteria2", [
        {"server_id": 901, "ip": "192.0.2.9", "available_slots": 2},
    ], observed_at=100)
    monkeypatch.setattr(provider_managed.time, "time", lambda: 101)
    monkeypatch.setattr("fwrouter_api.services.subscription.get_subscription_state", lambda: {})
    monkeypatch.setattr("fwrouter_api.services.provider_recovery.emergency_override", lambda: None)
    monkeypatch.setattr("fwrouter_api.core.config.get_settings", lambda: SimpleNamespace(
        stealthsurf_api_key=SimpleNamespace(get_secret_value=lambda: "never-project-this-key"),
        stealthsurf_config_id=1234,
    ))

    projection = provider_managed.provider_projection()

    serialized = repr(projection)
    assert "never-project-this-key" not in serialized
    assert "connection_url" not in serialized
    assert projection["bindings"][0]["resource_id"] == 1234
    assert projection["bindings"][0]["members"][0]["latency_ms"] is None


def test_projection_offers_intent_toggle_for_every_saved_source(monkeypatch) -> None:
    from fwrouter_api.services.subscription import _source_id
    conn = _db(monkeypatch)
    ordinary = "https://ordinary.example.test/private-token"
    managed = "https://connect.stealthsurf.net/private-token"
    monkeypatch.setattr(provider_managed, "configured_provider_binding", lambda: {
        "provider_id": "stealthsurf", "resource_id": 1234,
        "default_protocol": "hysteria2", "supported_protocols": ("hysteria2",),
    })
    monkeypatch.setattr("fwrouter_api.services.subscription.get_subscription_state", lambda: {
        "url": ordinary, "metadata": {"subscriptions": {"items": [
            {"url": ordinary}, {"url": managed},
            {"url": "https://connect.stealthsurf.net.evil.test/private-token"},
        ]}},
    })
    monkeypatch.setattr("fwrouter_api.services.provider_recovery.emergency_override", lambda: None)
    monkeypatch.setattr(provider_managed, "provider_adapter", lambda *_, **_kwargs: (_ for _ in ()).throw(AssertionError("No provider I/O")))
    projection = provider_managed.provider_projection()
    assert {item["source_ref"] for item in projection["bindings"]} == {_source_id(managed), _source_id(ordinary), _source_id("https://connect.stealthsurf.net.evil.test/private-token")}
    assert projection["bindings"][0]["enabled"] is False
    assert "private-token" not in repr(projection)
    assert store.list_bindings(conn) == []


def test_enable_requires_explicit_binding_before_any_provider_call(monkeypatch) -> None:
    conn = _db(monkeypatch)
    monkeypatch.setattr("fwrouter_api.adapters.xray_common.xray_writer_guard", lambda: nullcontext())
    monkeypatch.setattr("fwrouter_api.services.subscription._subscription_url_for_source_ref", lambda _: "https://ordinary.example.test/sub")
    monkeypatch.setattr(provider_managed, "configured_provider_binding", lambda: {
        "provider_id": "stealthsurf", "resource_id": 1234,
    })
    fake = FakeAdapter()
    result = provider_managed.execute_provider_operation("ordinary", "enable", _adapter=fake)
    assert result["error_code"] == "PROVIDER_BINDING_NOT_FOUND"
    assert fake.calls == []
    assert store.list_bindings(conn) == []


def test_provider_factory_can_be_injected_and_never_returns_key_in_errors() -> None:
    from pydantic import SecretStr
    from fwrouter_api.services.provider_adapters import provider_adapter

    settings = SimpleNamespace(stealthsurf_api_key=SecretStr("factory-test-secret"))
    created = {}

    def factory(key, **kwargs):
        created.update(key=key, kwargs=kwargs)
        return object()

    result = provider_adapter("stealthsurf", 7, settings_getter=lambda: settings,
                              client_factory=factory)

    assert result is not None
    assert created == {"key": "factory-test-secret", "kwargs": {"binding_revision": "7"}}


def test_selector_accepts_explicit_generic_provider_candidate_but_not_ordinary_unknown(monkeypatch) -> None:
    from fwrouter_api.services.selector import _select_candidate_with_priority

    provider = {"server_id": "logical-provider", "server_name": "Provider VPN", "member_id": "123",
                "execution_capability": "provider_switch", "runtime_evidence": "not_observed",
                "provider_eligible": True, "vpn_auto": True, "vpn_auto_priority": 0,
                "inventory_state": "active", "ping": {"status": "unknown", "last_ping_ms": None}}
    ordinary_unknown = {"server_id": "legacy-unknown", "server_name": "Unknown", "vpn_auto": True,
                        "vpn_auto_priority": 0, "inventory_state": "unknown",
                        "ping": {"status": "unknown", "last_ping_ms": None}}

    selected, latency_alternative = _select_candidate_with_priority([ordinary_unknown, provider])

    assert selected == provider
    assert latency_alternative is None
    assert _select_candidate_with_priority([ordinary_unknown]) == (None, None)


def test_provider_job_logs_only_safe_error_code_on_unexpected_exception(monkeypatch) -> None:
    from fwrouter_api.services import events, live_probe_cache, provider_jobs

    secret = "unsafe-fixture-api-key"
    recorded = []
    monkeypatch.setattr(provider_managed, "execute_provider_operation",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError(secret)))
    monkeypatch.setattr(events, "write_operational_event", lambda **kwargs: recorded.append(kwargs))
    monkeypatch.setattr(live_probe_cache, "clear_live_probe_cache", lambda: None)

    result = provider_jobs.run_provider_job({
        "job_id": "job-test", "input": {"source_ref": "src:" + "a" * 64, "action": "switch"},
    })

    assert result["error_code"] == "PROVIDER_OPERATION_FAILED"
    assert secret not in repr(result)
    assert secret not in repr(recorded)
    assert recorded[0]["details"]["error_code"] == "PROVIDER_OPERATION_FAILED"


def test_provider_material_is_only_used_by_canonical_verification_callback(monkeypatch) -> None:
    conn = _db(monkeypatch)
    binding = _binding(conn)
    conn.execute("UPDATE provider_bindings SET current_member_id='901', current_location_id='6', observed_protocol='hysteria2' WHERE source_ref='source-a'")
    binding.update(current_member_id="901", current_location_id="6", observed_protocol="hysteria2")
    member_id = "sub:" + __import__("hashlib").sha256(
        b"provider:stealthsurf:source-a:901:hysteria2"
    ).hexdigest()
    conn.execute("CREATE TABLE IF NOT EXISTS logical_server_members (logical_server_id TEXT, member_id TEXT, member_runtime_name TEXT, is_active INTEGER)")
    conn.execute("INSERT INTO logical_server_members VALUES (?, ?, ?, 1)",
                 (binding["logical_server_id"], member_id, "provider-member-runtime"))
    monkeypatch.setattr(provider_managed, "binding_for", lambda _source: binding)

    class Runtime:
        def get_logical_group_state(self, _name):
            return {"effective_member_runtime_identity": "provider-member-runtime"}

    runtime = Runtime()
    monkeypatch.setattr("fwrouter_api.services.logical_topology.get_logical_runtime_name", lambda _logical: "provider-group-runtime")
    monkeypatch.setattr("fwrouter_api.services.runtime_adapters.active_runtime_adapter", lambda _role: {})
    monkeypatch.setattr("fwrouter_api.services.runtime_adapters.runtime_adapter_operations", lambda _adapter: runtime)
    monkeypatch.setattr("fwrouter_api.services.server_ping.check_server_delay", lambda *_args, **_kwargs: {"ok": True})
    secret = "transient-connection-url-secret"

    with provider_managed.material_handoff(binding, {
        "server_id": 901, "location_id": 6, "protocol": "hysteria2", "connection_url": secret,
    }):
        result = provider_managed.verify_provider_handoff()

    assert result == {"ok": True, "error_code": None}
    assert secret not in repr(result)
    assert store.latest_evidence(conn, "source-a", "config") is None


def test_provider_handoff_rejects_newer_same_identity_observation(monkeypatch) -> None:
    conn = _db(monkeypatch)
    binding = _binding(conn)
    material = {"server_id": 901, "location_id": 6, "protocol": "hysteria2"}
    store.record_config(conn, "source-a", binding["binding_revision"], material,
                        observed_at=100.0)
    captured = store.get_binding(conn, "source-a")
    with provider_managed.material_handoff(captured, material, selection_revision=0):
        # The provider returned the same IDs, but a newer observed receipt owns
        # the handoff and must prevent this stale operation from writing inventory.
        store.record_config(conn, "source-a", binding["binding_revision"], material,
                            observed_at=101.0)
        with pytest.raises(ProviderError, match="PROVIDER_MATERIAL_HANDOFF_STALE"):
            provider_managed.validate_provider_material_handoff(conn)


def test_provider_api_mutation_is_guarded_but_local_refresh_probe_phase_is_not(monkeypatch) -> None:
    from fwrouter_api.adapters import xray_common
    from fwrouter_api.adapters.xray_common import xray_writer_guard_is_held

    real_guard = xray_common.xray_writer_guard
    conn, _binding_value, fake = _operation_setup(monkeypatch)
    # Restore the isolated process/thread guard after the fixture's in-memory
    # database helper disables it for simpler store-only cases.
    monkeypatch.setattr(xray_common, "xray_writer_guard", real_guard)
    mutation_guard: list[bool] = []
    refresh_guard: list[bool] = []
    original_switch = fake.switch_member
    def switch(*args, **kwargs):
        mutation_guard.append(xray_writer_guard_is_held())
        return original_switch(*args, **kwargs)
    fake.switch_member = switch
    def local_refresh(*_args, **_kwargs):
        refresh_guard.append(xray_writer_guard_is_held())
        return {"ok": True, "runtime_verified": True, "outcome": "verified", "last_good_retained": False}
    monkeypatch.setattr(provider_managed, "_refresh_with_material", local_refresh)

    result = provider_managed.execute_provider_operation(
        "source-a", "switch", member_id="901", expected_revision=_binding_value["binding_revision"], _adapter=fake,
    )

    assert result["ok"] is True
    assert mutation_guard == [True]
    assert refresh_guard == [False]


def test_route_request_model_projects_only_allowlisted_operation_fields() -> None:
    from fwrouter_api.routes.subscription import ProviderOperationRequest

    request = ProviderOperationRequest.model_validate({
        "action": "switch", "member_id": "901", "api_key": "secret-route-field",
        "connection_url": "hysteria2://credentials@example.test:443",
    })

    assert request.model_dump(exclude_none=True) == {"action": "switch", "member_id": "901"}


def test_targeted_fetch_for_one_source_never_reads_a_peer_binding(monkeypatch) -> None:
    conn = _db(monkeypatch)
    binding = _binding(conn, source_ref="source-a")
    _binding(conn, source_ref="source-b", enabled=False)
    fake = FakeAdapter()
    monkeypatch.setattr(provider_managed, "binding_for", lambda source: binding if source == "source-a" else None)
    monkeypatch.setattr(provider_managed, "provider_adapter", lambda *_args, **_kwargs: fake)

    result = provider_managed.fetch_provider_subscription("source-a")

    assert result.ok
    assert [call[1] for call in fake.calls if call[0] == "get_configs"] == [1234]


def test_manual_provider_switch_retains_manual_only_preferences(monkeypatch):
    conn, binding, fake = _operation_setup(monkeypatch)
    conn.execute("UPDATE provider_members SET auto_enabled=0, priority=-1 WHERE provider_member_id='901'")
    conn.execute("UPDATE server_preferences SET vpn_auto=0, vpn_auto_priority=-1")
    assert provider_managed.provider_candidates("source-a") == []
    manual = provider_managed.provider_candidates("source-a", automatic=False)
    assert "901" in {m["member_id"] for m in manual}
    monkeypatch.setattr(provider_managed, "_refresh_with_material", lambda *_args, **_kwargs: {"ok": True, "runtime_verified": True})
    result = provider_managed.execute_provider_operation("source-a", "switch", member_id="901")
    assert result["ok"] is True
    assert [call[0] for call in fake.calls] == ["switch"]
    row = conn.execute("SELECT auto_enabled, priority FROM provider_members WHERE provider_member_id='901'").fetchone()
    assert tuple(row) == (0, -1)
