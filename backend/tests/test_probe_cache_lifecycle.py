"""Lifecycle contract for the process-local live-probe cache.

Ownership and invalidation:

* Owners are the service functions that pass keys to ``get_live_probe_cache``.
  Cache entries are derived observations, never durable intent or native proof.
* Cache owners pass stable keys such as ``db.schema_state``,
  ``ui_state.subject_health``, ``dataplane_status.live_payload``,
  ``runtime.summary``, ``xray.status``, and
  ``dataplane_global.applied_manifest``. Their existing TTLs are the freshness
  boundary for read-only observations; force refresh bypasses completed values
  while retaining current in-flight coalescing behavior.
* External ingress keys are
  ``external_ingress.runtime.<provider>.<connection_id>`` and source observations
  use ``state_snapshot.external_source_observations:<provider>:<connection_id>``.
  External connection create/update/delete clears dot-suffix keys and this
  exact source-observation key family for the matching connection. Apply and
  reconcile owners use the existing global clear. Subscription group aliases
  clear the exact ``ui_state.subscription_clients`` key. There is no generic
  revision field: callers' current keys, owner clears and TTLs remain the
  revision/config-generation boundary; native proof is never cached here.
* The process keeps at most 256 completed entries, 32 MiB of admission-time
  estimated result graphs, and 24 MiB per value. Estimates walk supported
  built-in containers, exact ``SimpleNamespace`` values, and dataclass fields
  without serialization; unknown or over-budget graphs are returned normally
  but not retained. Adapter identity remains encoded in its cache key; cached
  observations do not retain adapter objects. Mutable result identity is
  preserved, so caller mutation after admission can make the estimate lower
  than the graph's later size.
* Expiry is checked on the requested key on every access and the bounded cache
  is swept at most once per second of subsequent cache activity. There is no
  worker and no disk state: restart begins empty and normal requests rebuild
  observations. Loader errors still reach all existing followers and are not
  cached. Followers retain the existing unbounded Event wait contract.
"""

from dataclasses import dataclass
import gc
import importlib.util
from pathlib import Path
import threading
import weakref
from types import SimpleNamespace

from fwrouter_api.services import live_probe_cache as cache


def _put(key: str, value, *, ttl: float = 60.0):
    return cache.get_live_probe_cache(key, ttl_seconds=ttl, loader=lambda: value)


def test_cache_enforces_entry_limit_and_lru_eviction(monkeypatch) -> None:
    cache.clear_live_probe_cache()
    monkeypatch.setattr(cache, "_MAX_CACHE_ENTRIES", 2)
    monkeypatch.setattr(cache, "_MAX_CACHE_BYTES", 1024 * 1024)
    monkeypatch.setattr(cache, "_MAX_ENTRY_BYTES", 1024 * 1024)

    first = _put("owner.first", {"value": "first"})
    _put("owner.second", {"value": "second"})
    assert cache.get_live_probe_cache(
        "owner.first", ttl_seconds=60, loader=lambda: None,
    ) is first
    _put("owner.third", {"value": "third"})

    assert list(cache._CACHE) == ["owner.first", "owner.third"]
    assert len(cache._CACHE) == 2
    assert cache._CACHE_RETAINED_BYTES == sum(entry[2] for entry in cache._CACHE.values())
    cache.clear_live_probe_cache()


def test_cache_enforces_total_estimated_bytes_and_declines_oversized(monkeypatch) -> None:
    cache.clear_live_probe_cache()
    monkeypatch.setattr(cache, "_MAX_CACHE_ENTRIES", 10)
    monkeypatch.setattr(cache, "_MAX_ENTRY_BYTES", 1024)
    monkeypatch.setattr(cache, "_MAX_CACHE_BYTES", 900)

    first_value = {"value": "a" * 100}
    first_size = (
        cache._value_size_bytes(first_value)
        + str.__sizeof__("owner.first")
        + cache._CACHE_ENTRY_OVERHEAD_BYTES
    )
    assert _put("owner.first", first_value) is first_value
    assert cache._CACHE_RETAINED_BYTES == first_size

    second_value = {"value": "b" * 100}
    _put("owner.second", second_value)
    assert "owner.first" not in cache._CACHE
    assert "owner.second" in cache._CACHE
    assert cache._CACHE_RETAINED_BYTES <= cache._MAX_CACHE_BYTES

    monkeypatch.setattr(cache, "_MAX_ENTRY_BYTES", 8)
    oversized = {"value": "too large"}
    assert _put("owner.oversized", oversized) is oversized
    assert "owner.oversized" not in cache._CACHE
    assert cache._CACHE_RETAINED_BYTES <= cache._MAX_CACHE_BYTES
    cache.clear_live_probe_cache()


def test_expired_values_are_released_by_normal_activity(monkeypatch) -> None:
    cache.clear_live_probe_cache()
    clock = [10.0]
    monkeypatch.setattr(cache, "monotonic", lambda: clock[0])
    _put("owner.expired.one", {"value": "one"}, ttl=0.5)
    _put("owner.expired.two", {"value": "two"}, ttl=0.5)
    keep = _put("owner.keep", {"value": "keep"}, ttl=5.0)
    original_bytes = cache._CACHE_RETAINED_BYTES
    assert original_bytes > 0

    clock[0] = 10.6
    assert cache.peek_live_probe_cache("owner.expired.one") is None
    assert "owner.expired.one" not in cache._CACHE
    assert cache._CACHE_RETAINED_BYTES < original_bytes

    clock[0] = 11.01
    assert cache.peek_live_probe_cache("owner.keep") is keep
    assert "owner.expired.two" not in cache._CACHE
    assert cache._CACHE_RETAINED_BYTES == cache._CACHE["owner.keep"][2]
    cache.clear_live_probe_cache()


def test_zero_and_negative_ttl_return_loader_values_without_retaining(monkeypatch) -> None:
    cache.clear_live_probe_cache()
    calls = []

    for ttl in (0.0, -1.0):
        value = {"ttl": ttl}
        result = cache.get_live_probe_cache(
            f"owner.ttl.{ttl}", ttl_seconds=ttl,
            loader=lambda value=value: calls.append(value) or value,
        )
        assert result is value

    assert len(calls) == 2
    assert not cache._CACHE
    assert cache._CACHE_RETAINED_BYTES == 0


def test_cache_preserves_mutable_value_identity_and_admission_estimate() -> None:
    cache.clear_live_probe_cache()
    value = {"items": ["original"]}
    first = _put("owner.mutable", value)
    admitted_bytes = cache._CACHE_RETAINED_BYTES

    first["items"].append("caller-added")
    second = cache.get_live_probe_cache(
        "owner.mutable", ttl_seconds=60, loader=lambda: {"unexpected": True},
    )

    assert second is first is value
    assert second["items"] == ["original", "caller-added"]
    # The ledger is deliberately admission-time estimated; maintaining it on
    # every hit would traverse large read models and change cache cost.
    assert cache._CACHE_RETAINED_BYTES == admitted_bytes
    cache.clear_live_probe_cache()


def test_estimator_handles_cycles_aliases_and_dataclass_fields() -> None:
    @dataclass
    class ProbeResult:
        name: str
        details: dict[str, object]

    shared = ["shared"]
    result = ProbeResult("probe", {"left": shared, "right": shared})
    result.details["self"] = result

    size = cache._value_size_bytes(result)
    assert 0 < size < cache._MAX_ENTRY_BYTES


def test_estimator_node_and_depth_budgets_decline_cache_admission(monkeypatch) -> None:
    cache.clear_live_probe_cache()
    monkeypatch.setattr(cache, "_MAX_SIZE_NODES", 8)
    monkeypatch.setattr(cache, "_MAX_SIZE_DEPTH", 3)

    deep_value = {"nested": [[[["leaf"]]]]}
    broad_value = {"items": list(range(32))}
    for key, value in (("owner.depth", deep_value), ("owner.nodes", broad_value)):
        assert _put(key, value) is value
        assert key not in cache._CACHE

    assert cache._CACHE_RETAINED_BYTES == 0
    cache.clear_live_probe_cache()


def test_unknown_values_are_not_sized_by_user_defined_size_hooks() -> None:
    class Unknown:
        called = False

        def __sizeof__(self):
            self.called = True
            raise AssertionError("custom size hook must not run")

    value = Unknown()
    cache.clear_live_probe_cache()
    assert _put("owner.unknown", value) is value
    assert value.called is False
    assert "owner.unknown" not in cache._CACHE


def test_cached_finalizers_run_after_cache_lock_is_released(monkeypatch) -> None:
    for mode in ("clear", "matching", "expiry", "eviction"):
        cache.clear_live_probe_cache()
        finalized = threading.Event()
        control_key = f"owner.finalizer.control.{mode}"

        @dataclass
        class ReentrantValue:
            marker: str

            def __del__(self):
                cache.peek_live_probe_cache(control_key)
                finalized.set()

        key = f"owner.finalizer.value.{mode}"
        if mode == "expiry":
            clock = [10.0]
            monkeypatch.setattr(cache, "monotonic", lambda: clock[0])
            value = ReentrantValue(mode)
            cache.get_live_probe_cache(key, ttl_seconds=0.5, loader=lambda value=value: value)
            del value
            _put(control_key, {"keeps_sweep_alive": True}, ttl=5.0)
            clock[0] = 11.01
            operation = lambda: cache.peek_live_probe_cache(control_key)
        else:
            if mode == "eviction":
                monkeypatch.setattr(cache, "_MAX_CACHE_ENTRIES", 1)
            value = ReentrantValue(mode)
            cache.get_live_probe_cache(key, ttl_seconds=60, loader=lambda value=value: value)
            del value
            if mode == "eviction":
                operation = lambda: _put("owner.finalizer.replacement", {"ok": True})
            elif mode == "matching":
                operation = lambda: cache.clear_live_probe_cache_matching(lambda item: item == key)
            else:
                operation = cache.clear_live_probe_cache

        worker = threading.Thread(target=operation, daemon=True)
        worker.start()
        worker.join(1.0)
        assert not worker.is_alive(), f"{mode} finalizer deadlocked while cache lock was held"
        assert finalized.wait(1.0), f"{mode} did not release its evicted value"

    cache.clear_live_probe_cache()


def test_connection_invalidation_keeps_other_connection_and_module_keys() -> None:
    cache.clear_live_probe_cache()
    connection_a = _put("external_ingress.runtime.provider.conn-a", {"id": "a"})
    connection_b = _put("external_ingress.runtime.provider.conn-b", {"id": "b"})
    other_owner = _put("runtime.summary", {"ok": True})

    cache.clear_live_probe_cache_for_connection("conn-a")

    assert cache.peek_live_probe_cache("external_ingress.runtime.provider.conn-a") is None
    assert cache.peek_live_probe_cache("external_ingress.runtime.provider.conn-b") is connection_b
    assert cache.peek_live_probe_cache("runtime.summary") is other_owner
    assert cache._CACHE_RETAINED_BYTES == sum(entry[2] for entry in cache._CACHE.values())
    assert connection_a == {"id": "a"}
    cache.clear_live_probe_cache()


def test_connection_invalidation_clears_colon_owner_and_preserves_neighbor_ids() -> None:
    cache.clear_live_probe_cache()
    conn12_a = _put(
        "state_snapshot.external_source_observations:provider-a:connection12",
        {"connection_id": "connection12", "provider": "provider-a"},
    )
    conn12_b = _put(
        "state_snapshot.external_source_observations:provider-b:connection12",
        {"connection_id": "connection12", "provider": "provider-b"},
    )
    conn112 = _put(
        "state_snapshot.external_source_observations:provider-a:connection112",
        {"connection_id": "connection112", "provider": "provider-a"},
    )
    ingress12 = _put("external_ingress.runtime.provider-a.connection12", {"id": "12"})
    ingress112 = _put("external_ingress.runtime.provider-a.connection112", {"id": "112"})

    cache.clear_live_probe_cache_for_connection("connection12")

    assert cache.peek_live_probe_cache(
        "state_snapshot.external_source_observations:provider-a:connection12",
    ) is None
    assert cache.peek_live_probe_cache(
        "state_snapshot.external_source_observations:provider-b:connection12",
    ) is None
    assert cache.peek_live_probe_cache(
        "state_snapshot.external_source_observations:provider-a:connection112",
    ) is conn112
    assert cache.peek_live_probe_cache("external_ingress.runtime.provider-a.connection12") is None
    assert cache.peek_live_probe_cache("external_ingress.runtime.provider-a.connection112") is ingress112
    assert conn12_a["provider"] == "provider-a"
    assert conn12_b["provider"] == "provider-b"
    assert ingress12 == {"id": "12"}
    cache.clear_live_probe_cache()


def test_colon_connection_clear_fences_old_flight_from_replacement() -> None:
    cache.clear_live_probe_cache()
    key = "state_snapshot.external_source_observations:provider:connection12"
    old_started = threading.Event()
    release_old = threading.Event()
    new_started = threading.Event()
    release_new = threading.Event()
    waiter_entered = threading.Event()
    event_factory = cache.Event
    created = [0]

    class ObservedEvent:
        def __init__(self):
            self.event = threading.Event()
            created[0] += 1
            self.observe_wait = created[0] == 1

        def wait(self, timeout=None):
            if self.observe_wait:
                waiter_entered.set()
            return self.event.wait(timeout)

        def set(self):
            self.event.set()

    cache.Event = ObservedEvent
    results = {}

    def old_loader():
        old_started.set()
        assert release_old.wait(5)
        return {"value": "old"}

    def new_loader():
        new_started.set()
        assert release_new.wait(5)
        return {"value": "new"}

    def invoke(name, loader):
        try:
            results[name] = cache.get_live_probe_cache(key, ttl_seconds=60, loader=loader)
        except BaseException as exc:
            results[name] = exc

    old = threading.Thread(target=invoke, args=("old", old_loader), daemon=True)
    waiter = threading.Thread(target=invoke, args=("waiter", lambda: {"value": "unexpected"}), daemon=True)
    replacement = threading.Thread(target=invoke, args=("replacement", new_loader), daemon=True)
    threads = [old, waiter, replacement]
    try:
        old.start()
        assert old_started.wait(5)
        with cache._CACHE_LOCK:
            old_entry = cache._INFLIGHT[key]
        waiter.start()
        assert waiter_entered.wait(5)

        cache.clear_live_probe_cache_for_connection("connection12")
        replacement.start()
        assert new_started.wait(5)
        with cache._CACHE_LOCK:
            replacement_entry = cache._INFLIGHT[key]
            assert replacement_entry is not old_entry

        release_old.set()
        old.join(5)
        waiter.join(5)
        assert not old.is_alive() and not waiter.is_alive()
        assert results["old"] == {"value": "old"}
        assert results["waiter"] == {"value": "old"}
        with cache._CACHE_LOCK:
            assert cache._INFLIGHT[key] is replacement_entry
        assert cache.peek_live_probe_cache(key) is None

        release_new.set()
        replacement.join(5)
        assert not replacement.is_alive()
        assert results["replacement"] == {"value": "new"}
        assert cache.peek_live_probe_cache(key) == {"value": "new"}
    finally:
        release_old.set()
        release_new.set()
        for thread in threads:
            if thread.ident is not None:
                thread.join(5)
        cache.Event = event_factory
        cache.clear_live_probe_cache()
        assert all(not thread.is_alive() for thread in threads)
    cache.clear_live_probe_cache()


def test_nonweak_adapter_results_are_reused_and_isolated_by_owner() -> None:
    class Adapter:
        __slots__ = ("calls",)

        def __init__(self):
            self.calls = 0

        def health(self):
            self.calls += 1
            return SimpleNamespace(observation=self.calls)

    adapter = Adapter()
    other = Adapter()
    first = cache.get_readonly_mihomo_health(adapter)
    assert cache.get_readonly_mihomo_health(adapter) is first
    other_first = cache.get_readonly_mihomo_health(other)
    assert cache.get_readonly_mihomo_health(other) is other_first
    assert first is not other_first
    assert adapter.calls == other.calls == 1


def test_discarded_adapter_graph_is_not_retained_by_cached_health_entry() -> None:
    class Adapter:
        def __init__(self):
            self.large_runtime_state = bytearray(8 * 1024 * 1024)

        def health(self):
            return {"healthy": True}

    cache.clear_live_probe_cache()
    adapter = Adapter()
    adapter_ref = weakref.ref(adapter)
    cached_health = cache.get_readonly_mihomo_health(adapter)
    key = next(key for key in cache._CACHE if key.startswith("dataplane_global.mihomo_health."))
    assert cache.peek_live_probe_cache(key) is cached_health

    del adapter
    gc.collect()

    assert adapter_ref() is None
    assert any(value is cached_health for _expiry, value, _size in cache._CACHE.values())
    assert cache._CACHE_RETAINED_BYTES <= cache._MAX_CACHE_BYTES


def test_slow_nonweak_adapter_load_remains_coalesced_past_health_ttl(monkeypatch) -> None:
    cache.clear_live_probe_cache()
    clock = [10.0]
    monkeypatch.setattr(cache, "monotonic", lambda: clock[0])
    started = threading.Event()
    release = threading.Event()
    follower_waiting = threading.Event()
    event_factory = cache.Event

    class ObservedEvent:
        def __init__(self):
            self.event = threading.Event()

        def wait(self, timeout=None):
            follower_waiting.set()
            return self.event.wait(timeout)

        def set(self):
            self.event.set()

    class Adapter:
        __slots__ = ("calls",)

        def __init__(self):
            self.calls = 0

        def health(self):
            self.calls += 1
            started.set()
            assert release.wait(5)
            return {"calls": self.calls}

    cache.Event = ObservedEvent
    adapter = Adapter()
    results = {}

    def invoke(name):
        results[name] = cache.get_readonly_mihomo_health(adapter)

    leader = threading.Thread(target=invoke, args=("leader",), daemon=True)
    follower = threading.Thread(target=invoke, args=("follower",), daemon=True)
    try:
        leader.start()
        assert started.wait(2)
        clock[0] = 12.1
        follower.start()
        assert follower_waiting.wait(2)
        release.set()
        leader.join(2)
        follower.join(2)
        assert not leader.is_alive() and not follower.is_alive()
        assert results["leader"] is results["follower"]
        assert results["leader"] == {"calls": 1}
        assert adapter.calls == 1
    finally:
        release.set()
        leader.join(2)
        if follower.ident is not None:
            follower.join(2)
        cache.Event = event_factory
        cache.clear_live_probe_cache()
        assert not leader.is_alive() and not follower.is_alive()


def test_simple_namespace_result_graph_is_safely_estimable() -> None:
    result = SimpleNamespace(runtime_state="running", details={"healthy": True})
    assert 0 < cache._value_size_bytes(result) < cache._MAX_ENTRY_BYTES


def test_simple_namespace_subclasses_remain_unsupported() -> None:
    class ExtendedNamespace(SimpleNamespace):
        pass

    try:
        cache._value_size_bytes(ExtendedNamespace(value="unknown"))
    except cache._UncacheableValue:
        pass
    else:
        raise AssertionError("SimpleNamespace subclasses must not be introspected")


def test_fresh_module_starts_empty_and_rebuilds_after_restart() -> None:
    source = Path(cache.__file__)
    spec = importlib.util.spec_from_file_location("live_probe_cache_restart_test", source)
    assert spec is not None and spec.loader is not None
    fresh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fresh)

    assert fresh._CACHE == {}
    assert fresh._CACHE_RETAINED_BYTES == 0
    value = {"restart": "rebuilt"}
    assert fresh.get_live_probe_cache(
        "owner.restart", ttl_seconds=60, loader=lambda: value,
    ) is value
    assert fresh.peek_live_probe_cache("owner.restart") is value
    assert fresh._CACHE_RETAINED_BYTES > 0


def test_oversized_result_is_shared_by_inflight_followers_but_not_retained(monkeypatch) -> None:
    cache.clear_live_probe_cache()
    monkeypatch.setattr(cache, "_MAX_ENTRY_BYTES", 8)
    monkeypatch.setattr(cache, "_MAX_CACHE_BYTES", 64)
    started = threading.Event()
    release = threading.Event()
    follower_waiting = threading.Event()
    calls = []
    results = {}
    event_factory = cache.Event

    class ObservedEvent:
        def __init__(self):
            self.event = threading.Event()

        def wait(self, timeout=None):
            follower_waiting.set()
            return self.event.wait(timeout)

        def set(self):
            self.event.set()

    cache.Event = ObservedEvent
    value = b"larger than the admission bound"

    def loader():
        calls.append("load")
        started.set()
        assert release.wait(5)
        return value

    def invoke(name):
        results[name] = cache.get_live_probe_cache(
            "owner.oversized.flight", ttl_seconds=60, loader=loader,
        )

    leader = threading.Thread(target=invoke, args=("leader",), daemon=True)
    follower = threading.Thread(target=invoke, args=("follower",), daemon=True)
    threads = (leader, follower)
    try:
        leader.start()
        assert started.wait(5)
        follower.start()
        assert follower_waiting.wait(5)
        release.set()
        for thread in threads:
            thread.join(5)
        assert all(not thread.is_alive() for thread in threads)
        assert results == {"leader": value, "follower": value}
        assert calls == ["load"]
        assert "owner.oversized.flight" not in cache._CACHE
        assert cache._CACHE_RETAINED_BYTES == 0
    finally:
        release.set()
        for thread in threads:
            if thread.ident is not None:
                thread.join(5)
        cache.Event = event_factory
        cache.clear_live_probe_cache()
