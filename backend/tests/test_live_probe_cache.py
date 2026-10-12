from __future__ import annotations

import hashlib
import threading
from pathlib import Path
from types import ModuleType

from fwrouter_api.services import live_probe_cache
from fwrouter_api.services.live_probe_cache import clear_live_probe_cache, get_live_probe_cache


# Frozen `git show 8ff2648:backend/fwrouter_api/services/live_probe_cache.py` source.
BASELINE_FIXTURE_SHA256 = "33a2c7aed54a966df03612a4b6ef36cf58267f49c95c938aebb3093384b01200"
BASELINE_FIXTURE_PATH = Path(__file__).with_name("fixtures") / "live_probe_cache_before_identity_fence.py"


def _instrument_first_event_wait(module):
    original_factory = module.Event
    first_wait_entered = threading.Event()
    created = [0]

    class _ObservedEvent:
        def __init__(self):
            self._event = threading.Event()
            created[0] += 1
            self._track_wait = created[0] == 1

        def wait(self, timeout=None):
            if self._track_wait:
                first_wait_entered.set()
            return self._event.wait(timeout)

        def set(self):
            self._event.set()

    module.Event = _ObservedEvent
    return first_wait_entered, lambda: setattr(module, "Event", original_factory)


def _run_clear_race(module):
    module.clear_live_probe_cache()
    old_started = threading.Event()
    new_started = threading.Event()
    release_old = threading.Event()
    release_new = threading.Event()
    old_waiter_entered, restore_event = _instrument_first_event_wait(module)
    old_results = {}
    waiter_results = {}
    new_results = {}

    def old_loader():
        old_started.set()
        if not release_old.wait(5):
            raise TimeoutError("old loader release timed out")
        return {"value": "old"}

    def new_loader():
        new_started.set()
        if not release_new.wait(5):
            raise TimeoutError("new loader release timed out")
        return {"value": "new"}

    def invoke(result_bucket, loader):
        try:
            result_bucket["result"] = module.get_live_probe_cache(
                "race", ttl_seconds=60.0, loader=loader,
            )
        except BaseException as exc:  # thread failures are returned to the main test thread
            result_bucket["error"] = exc

    old_thread = threading.Thread(target=invoke, args=(old_results, old_loader), daemon=True)
    waiter_thread = threading.Thread(target=invoke, args=(waiter_results, lambda: {"value": "unexpected"}), daemon=True)
    new_thread = threading.Thread(target=invoke, args=(new_results, new_loader), daemon=True)
    started_threads = []
    try:
        old_thread.start()
        started_threads.append(old_thread)
        assert old_started.wait(5), "old loader did not start"
        with module._CACHE_LOCK:
            old_entry = module._INFLIGHT.get("race")
            assert old_entry is not None
            old_event = old_entry[0]

        waiter_thread.start()
        started_threads.append(waiter_thread)
        assert old_waiter_entered.wait(5), "old waiter did not join the in-flight load"
        module.clear_live_probe_cache()

        new_thread.start()
        started_threads.append(new_thread)
        assert new_started.wait(5), "post-clear loader did not start"
        with module._CACHE_LOCK:
            new_entry = module._INFLIGHT.get("race")
            assert new_entry is not None and new_entry[0] is not old_event

        release_old.set()
        old_thread.join(5)
        waiter_thread.join(5)
        assert not old_thread.is_alive(), "old leader did not finish"
        assert not waiter_thread.is_alive(), "old waiter did not finish"

        with module._CACHE_LOCK:
            current_entry = module._INFLIGHT.get("race")
        cached_while_new_loader_waits = module.peek_live_probe_cache("race")

        release_new.set()
        new_thread.join(5)
        assert not new_thread.is_alive(), "new leader did not finish"
        return {
            "old_entry": old_entry,
            "new_entry": new_entry,
            "current_entry": current_entry,
            "cached_while_new_loader_waits": cached_while_new_loader_waits,
            "old_result": old_results.get("result"),
            "waiter_result": waiter_results.get("result"),
            "new_result": new_results.get("result"),
            "final_cache": module.peek_live_probe_cache("race"),
            "thread_errors": [old_results.get("error"), waiter_results.get("error"), new_results.get("error")],
        }
    finally:
        release_old.set()
        release_new.set()
        for thread in started_threads:
            thread.join(5)
        restore_event()
        module.clear_live_probe_cache()
        assert all(not thread.is_alive() for thread in started_threads), "cache race thread survived cleanup"


def _load_baseline_module() -> ModuleType:
    source = BASELINE_FIXTURE_PATH.read_bytes()
    assert hashlib.sha256(source).hexdigest() == BASELINE_FIXTURE_SHA256
    module = ModuleType("fwrouter_live_probe_cache_baseline_8ff2648")
    exec(compile(source, str(BASELINE_FIXTURE_PATH), "exec"), module.__dict__)
    return module


def test_live_probe_cache_reuses_value_within_ttl() -> None:
    clear_live_probe_cache()
    calls: list[int] = []

    def _load() -> dict[str, int]:
        calls.append(1)
        return {"calls": len(calls)}

    first = get_live_probe_cache("probe", ttl_seconds=10.0, loader=_load)
    second = get_live_probe_cache("probe", ttl_seconds=10.0, loader=_load)

    assert first == {"calls": 1}
    assert second == {"calls": 1}
    assert len(calls) == 1


def test_live_probe_cache_starts_ttl_after_loader_finishes(monkeypatch) -> None:
    clear_live_probe_cache()
    calls: list[int] = []
    clock = [100.0]

    def _monotonic() -> float:
        return clock[0]

    def _load() -> dict[str, int]:
        calls.append(1)
        clock[0] += 3.0
        return {"calls": len(calls)}

    monkeypatch.setattr(live_probe_cache, "monotonic", _monotonic)

    first = get_live_probe_cache("slow-probe", ttl_seconds=2.0, loader=_load)
    second = get_live_probe_cache("slow-probe", ttl_seconds=2.0, loader=_load)

    assert first == {"calls": 1}
    assert second == {"calls": 1}
    assert len(calls) == 1


def test_clear_fences_old_leader_and_preserves_preclear_waiter_result() -> None:
    result = _run_clear_race(live_probe_cache)

    assert result["thread_errors"] == [None, None, None]
    assert result["old_result"] == {"value": "old"}
    assert result["waiter_result"] == {"value": "old"}
    assert result["new_result"] == {"value": "new"}
    assert result["current_entry"] is result["new_entry"]
    assert result["cached_while_new_loader_waits"] is None
    assert result["final_cache"] == {"value": "new"}


def test_identity_fence_regression_is_demonstrated_against_pinned_baseline() -> None:
    baseline_result = _run_clear_race(_load_baseline_module())
    current_result = _run_clear_race(live_probe_cache)

    # The fixture is the exact pre-fence source; it reproduces both stale publication and
    # old-leader removal of the newer in-flight entry. Current code must satisfy both checks.
    assert baseline_result["current_entry"] is not baseline_result["new_entry"]
    assert baseline_result["cached_while_new_loader_waits"] == {"value": "old"}
    assert baseline_result["thread_errors"] == [None, None, None]
    assert baseline_result["old_result"] == {"value": "old"}
    assert baseline_result["waiter_result"] == {"value": "old"}
    assert baseline_result["new_result"] == {"value": "new"}
    assert current_result["current_entry"] is current_result["new_entry"]
    assert current_result["cached_while_new_loader_waits"] is None


def test_matching_clear_invalidates_only_matching_cache_and_inflight_entries() -> None:
    clear_live_probe_cache()
    assert get_live_probe_cache("scope.drop", ttl_seconds=60, loader=lambda: {"value": "stale"}) == {"value": "stale"}
    assert get_live_probe_cache("scope.keep", ttl_seconds=60, loader=lambda: {"value": "cached"}) == {"value": "cached"}
    assert get_live_probe_cache("other.keep", ttl_seconds=60, loader=lambda: {"value": "other-cached"}) == {
        "value": "other-cached",
    }
    matching_started = threading.Event()
    unrelated_started = threading.Event()
    release = threading.Event()
    results = {}
    replacement_started = threading.Event()
    replacement_release = threading.Event()

    def loader(started, value):
        def _load():
            started.set()
            if not release.wait(5):
                raise TimeoutError("matching-clear loader release timed out")
            return {"value": value}
        return _load

    def invoke(key, started, value):
        try:
            results[key] = get_live_probe_cache(
                key,
                ttl_seconds=60,
                loader=loader(started, value),
                force_refresh=(key == "scope.drop"),
            )
        except BaseException as exc:
            results[key] = exc

    def replacement_loader():
        replacement_started.set()
        if not replacement_release.wait(5):
            raise TimeoutError("matching-clear replacement release timed out")
        return {"value": "fresh"}

    matching = threading.Thread(target=invoke, args=("scope.drop", matching_started, "drop"), daemon=True)
    unrelated = threading.Thread(
        target=invoke,
        args=("unrelated.pending", unrelated_started, "unrelated"),
        daemon=True,
    )
    started_threads = []
    replacement = None
    try:
        matching.start()
        started_threads.append(matching)
        unrelated.start()
        started_threads.append(unrelated)
        assert matching_started.wait(5)
        assert unrelated_started.wait(5)
        with live_probe_cache._CACHE_LOCK:
            unrelated_entry = live_probe_cache._INFLIGHT.get("unrelated.pending")
            assert live_probe_cache._INFLIGHT.get("scope.drop") is not None

        live_probe_cache.clear_live_probe_cache_matching(lambda key: key == "scope.drop")

        with live_probe_cache._CACHE_LOCK:
            assert live_probe_cache._INFLIGHT.get("scope.drop") is None
            assert live_probe_cache._INFLIGHT.get("unrelated.pending") is unrelated_entry
            assert "scope.drop" not in live_probe_cache._CACHE
        assert live_probe_cache.peek_live_probe_cache("scope.drop") is None
        assert live_probe_cache.peek_live_probe_cache("scope.keep") == {"value": "cached"}
        assert live_probe_cache.peek_live_probe_cache("other.keep") == {"value": "other-cached"}

        replacement_result = {}

        def invoke_replacement():
            try:
                replacement_result["value"] = get_live_probe_cache(
                    "scope.drop", ttl_seconds=60, loader=replacement_loader,
                )
            except BaseException as exc:
                replacement_result["error"] = exc

        replacement = threading.Thread(target=invoke_replacement, daemon=True)
        replacement.start()
        started_threads.append(replacement)
        assert replacement_started.wait(5)
        with live_probe_cache._CACHE_LOCK:
            replacement_entry = live_probe_cache._INFLIGHT.get("scope.drop")
            assert replacement_entry is not None

        release.set()
        matching.join(5)
        unrelated.join(5)
        assert not matching.is_alive() and not unrelated.is_alive()
        assert results["scope.drop"] == {"value": "drop"}
        assert results["unrelated.pending"] == {"value": "unrelated"}
        with live_probe_cache._CACHE_LOCK:
            assert live_probe_cache._INFLIGHT.get("scope.drop") is replacement_entry
        assert live_probe_cache.peek_live_probe_cache("scope.drop") is None
        assert live_probe_cache.peek_live_probe_cache("unrelated.pending") == {"value": "unrelated"}
        replacement_release.set()
        replacement.join(5)
        assert not replacement.is_alive()
        assert replacement_result == {"value": "fresh"}
        assert live_probe_cache.peek_live_probe_cache("scope.drop") == {"value": "fresh"}
    finally:
        release.set()
        replacement_release.set()
        for thread in started_threads:
            thread.join(5)
        clear_live_probe_cache()
        assert all(not thread.is_alive() for thread in started_threads), "matching-clear thread survived cleanup"


def test_exception_is_shared_with_preclear_waiter_and_force_refresh_replaces_cache(monkeypatch) -> None:
    clear_live_probe_cache()
    started = threading.Event()
    release = threading.Event()
    loader_calls = []
    waiter_entered, restore_event = _instrument_first_event_wait(live_probe_cache)
    errors = {}
    failure = ValueError("probe failed")

    def failing_loader():
        loader_calls.append("failed")
        started.set()
        if not release.wait(5):
            raise TimeoutError("failure loader release timed out")
        raise failure

    def invoke(name, loader):
        try:
            errors[name] = get_live_probe_cache("exception", ttl_seconds=60, loader=loader)
        except BaseException as exc:
            errors[name] = exc

    leader = threading.Thread(target=invoke, args=("leader", failing_loader), daemon=True)
    waiter = threading.Thread(target=invoke, args=("waiter", lambda: {"value": "unexpected"}), daemon=True)
    started_threads = []
    try:
        leader.start()
        started_threads.append(leader)
        assert started.wait(5)
        waiter.start()
        started_threads.append(waiter)
        assert waiter_entered.wait(5)
        clear_live_probe_cache()
        release.set()
        for thread in started_threads:
            thread.join(5)
        assert not leader.is_alive() and not waiter.is_alive()
        assert errors["leader"] is failure
        assert errors["waiter"] is failure
        assert loader_calls == ["failed"]
        assert live_probe_cache.peek_live_probe_cache("exception") is None
        assert get_live_probe_cache(
            "exception", ttl_seconds=60, loader=lambda: {"value": "recovered"},
        ) == {"value": "recovered"}
    finally:
        release.set()
        for thread in started_threads:
            thread.join(5)
        restore_event()
        clear_live_probe_cache()
        assert all(not thread.is_alive() for thread in started_threads), "exception test thread survived cleanup"

    clock = [10.0]
    monkeypatch.setattr(live_probe_cache, "monotonic", lambda: clock[0])
    calls = []

    def load_value(value):
        return lambda: calls.append(value) or {"value": value}

    assert get_live_probe_cache("refresh", ttl_seconds=5, loader=load_value("initial")) == {
        "value": "initial",
    }
    clock[0] = 16.0
    assert get_live_probe_cache("refresh", ttl_seconds=5, loader=load_value("expired")) == {
        "value": "expired",
    }
    forced = get_live_probe_cache("refresh", ttl_seconds=5, loader=load_value("forced"), force_refresh=True)
    assert forced == {"value": "forced"}
    assert get_live_probe_cache("refresh", ttl_seconds=5, loader=load_value("unused")) == {
        "value": "forced",
    }
    assert calls == ["initial", "expired", "forced"]
    clear_live_probe_cache()
