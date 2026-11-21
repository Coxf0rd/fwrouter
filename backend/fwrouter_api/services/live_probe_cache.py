from __future__ import annotations

from dataclasses import fields, is_dataclass
from collections import OrderedDict
from enum import Enum
from threading import Event, Lock, get_ident
from time import monotonic
from types import SimpleNamespace
from typing import Any, Callable


# These are admission-time Python object-graph estimates, not RSS limits. Keep
# one large read model usable while bounding total completed-value retention.
_MAX_CACHE_ENTRIES = 256
_MAX_CACHE_BYTES = 32 * 1024 * 1024
_MAX_ENTRY_BYTES = 24 * 1024 * 1024
_CACHE_ENTRY_OVERHEAD_BYTES = 256
_MAX_SIZE_NODES = 50_000
_MAX_SIZE_DEPTH = 64
_EXPIRY_SWEEP_INTERVAL_SECONDS = 1.0

_CACHE_LOCK = Lock()
_CACHE: OrderedDict[str, tuple[float, Any, int]] = OrderedDict()
_CACHE_RETAINED_BYTES = 0
_INFLIGHT: dict[str, tuple[Event, dict[str, Any]]] = {}
_LAST_EXPIRY_SWEEP_AT = 0.0

_READONLY_MIHOMO_HEALTH_CACHE_KEY = "dataplane_global.mihomo_health"


class _UncacheableValue(Exception):
    """The value graph cannot be estimated within the admission budget."""


def get_readonly_mihomo_health(adapter: Any, *, loader: Callable[[], Any] | None = None) -> Any:
    """Return a short-lived, coalesced Mihomo health snapshot for read-only callers."""
    probe = loader if loader is not None else adapter.health
    adapter_identity = (
        f"{type(adapter).__module__}.{type(adapter).__qualname__}.{id(adapter):x}"
    )
    return get_live_probe_cache(
        f"{_READONLY_MIHOMO_HEALTH_CACHE_KEY}.{adapter_identity}",
        ttl_seconds=2.0,
        loader=probe,
    )


def _value_size_bytes(value: Any) -> int:
    """Estimate a supported value's retained Python graph without serialization."""
    seen: set[int] = set()
    visited = 0

    def visit(item: Any, depth: int = 0) -> int:
        nonlocal visited
        visited += 1
        if visited > _MAX_SIZE_NODES or depth > _MAX_SIZE_DEPTH:
            raise _UncacheableValue

        identity = id(item)
        if identity in seen:
            return 0
        seen.add(identity)

        item_type = type(item)
        if item is None or item_type in (bool, int, float, str, bytes):
            # Exact built-ins avoid calling a user-defined __sizeof__ method.
            size = object.__sizeof__(item) if item is None else item_type.__sizeof__(item)
            if size > _MAX_ENTRY_BYTES:
                raise _UncacheableValue
            return size

        if item_type is dict:
            size = dict.__sizeof__(item)
            for key, child in item.items():
                size += visit(key, depth + 1) + visit(child, depth + 1)
                if size > _MAX_ENTRY_BYTES:
                    raise _UncacheableValue
            return size
        if item_type is list:
            size = list.__sizeof__(item)
            for child in item:
                size += visit(child, depth + 1)
                if size > _MAX_ENTRY_BYTES:
                    raise _UncacheableValue
            return size
        if item_type is tuple:
            size = tuple.__sizeof__(item)
            for child in item:
                size += visit(child, depth + 1)
                if size > _MAX_ENTRY_BYTES:
                    raise _UncacheableValue
            return size
        if item_type is set:
            size = set.__sizeof__(item)
            for child in item:
                size += visit(child, depth + 1)
                if size > _MAX_ENTRY_BYTES:
                    raise _UncacheableValue
            return size
        if item_type is frozenset:
            size = frozenset.__sizeof__(item)
            for child in item:
                size += visit(child, depth + 1)
                if size > _MAX_ENTRY_BYTES:
                    raise _UncacheableValue
            return size
        if isinstance(item, Enum):
            size = object.__sizeof__(item)
            return size + visit(object.__getattribute__(item, "_value_"), depth + 1)
        if is_dataclass(item) and not isinstance(item, type):
            # Supported project DTOs store fields plainly; unusual slot
            # descriptors can raise, in which case admission is declined.
            size = object.__sizeof__(item)
            try:
                attributes = object.__getattribute__(item, "__dict__")
            except AttributeError:
                attributes = None
            if type(attributes) is dict:
                size += visit(attributes, depth + 1)
            else:
                for field in fields(item):
                    size += visit(object.__getattribute__(item, field.name), depth + 1)
                    if size > _MAX_ENTRY_BYTES:
                        raise _UncacheableValue
            return size

        if item_type is SimpleNamespace:
            # Exact built-in type only: its instance dictionary is a plain
            # dict, while subclasses and arbitrary objects stay unsupported.
            size = object.__sizeof__(item)
            attributes = object.__getattribute__(item, "__dict__")
            if type(attributes) is not dict:
                raise _UncacheableValue
            return size + visit(attributes, depth + 1)

        # Do not inspect arbitrary objects, invoke custom sizing hooks, or
        # serialize values that may include credentials or runtime details.
        raise _UncacheableValue

    return visit(value)


def _remove_cache_entry_locked(key: str, retired: list[tuple[float, Any, int]]) -> None:
    global _CACHE_RETAINED_BYTES
    cached = _CACHE.pop(key, None)
    if cached is not None:
        _CACHE_RETAINED_BYTES = max(0, _CACHE_RETAINED_BYTES - cached[2])
        # Keep arbitrary cached finalizers and weakref callbacks alive until
        # the caller has released _CACHE_LOCK.
        retired.append(cached)


def _sweep_expired_locked(
    now: float,
    retired: list[tuple[float, Any, int]],
    *,
    requested_key: str | None = None,
) -> None:
    global _LAST_EXPIRY_SWEEP_AT
    if requested_key is not None:
        cached = _CACHE.get(requested_key)
        if cached is not None and not cached[0] > now:
            _remove_cache_entry_locked(requested_key, retired)

    if now - _LAST_EXPIRY_SWEEP_AT < _EXPIRY_SWEEP_INTERVAL_SECONDS:
        return
    _LAST_EXPIRY_SWEEP_AT = now
    for key, cached in tuple(_CACHE.items()):
        if not cached[0] > now:
            _remove_cache_entry_locked(key, retired)


def _admit_cache_entry_locked(
    key: str,
    expires_at: float,
    value: Any,
    retained_bytes: int,
    retired: list[tuple[float, Any, int]],
) -> None:
    global _CACHE_RETAINED_BYTES
    _remove_cache_entry_locked(key, retired)
    if retained_bytes > _MAX_ENTRY_BYTES or retained_bytes > _MAX_CACHE_BYTES:
        return

    while _CACHE and (
        len(_CACHE) >= _MAX_CACHE_ENTRIES
        or _CACHE_RETAINED_BYTES + retained_bytes > _MAX_CACHE_BYTES
    ):
        oldest_key = next(iter(_CACHE))
        _remove_cache_entry_locked(oldest_key, retired)

    _CACHE[key] = (expires_at, value, retained_bytes)
    _CACHE_RETAINED_BYTES += retained_bytes


def get_live_probe_cache(
    key: str,
    *,
    ttl_seconds: float,
    loader: Callable[[], Any],
    force_refresh: bool = False,
    wait_timeout_seconds: float | None = None,
) -> Any:
    now = monotonic()
    if not force_refresh:
        retired: list[tuple[float, Any, int]] = []
        with _CACHE_LOCK:
            _sweep_expired_locked(now, retired, requested_key=key)
            cached = _CACHE.get(key)
            if cached is not None and cached[0] > now:
                _CACHE.move_to_end(key)
                return cached[1]
            if cached is not None:
                _remove_cache_entry_locked(key, retired)

    retired: list[tuple[float, Any, int]] = []
    with _CACHE_LOCK:
        _sweep_expired_locked(now, retired, requested_key=key)
        cached = _CACHE.get(key) if not force_refresh else None
        if cached is not None and cached[0] > now:
            _CACHE.move_to_end(key)
            return cached[1]
        inflight = _INFLIGHT.get(key)
        if inflight is None:
            event = Event()
            holder: dict[str, Any] = {"owner_thread_id": get_ident()}
            _INFLIGHT[key] = (event, holder)
            leader = True
        else:
            event, holder = inflight
            leader = False
    retired.clear()

    if not leader:
        if holder.get("owner_thread_id") == get_ident():
            raise RuntimeError("LIVE_PROBE_CACHE_REENTRANT_LOAD")
        if not event.wait(timeout=wait_timeout_seconds):
            raise TimeoutError("LIVE_PROBE_CACHE_WAIT_TIMEOUT")
        if "error" in holder:
            raise holder["error"]
        return holder.get("value")

    try:
        value = loader()
        ttl = max(float(ttl_seconds), 0.0)
        finished_at = monotonic()
        expires_at = finished_at + ttl
        holder["value"] = value

        with _CACHE_LOCK:
            current = _INFLIGHT.get(key)
            should_size = (
                current is not None
                and current[0] is event
                and current[1] is holder
                and expires_at > finished_at
            )

        if should_size:
            try:
                if type(key) is not str:
                    raise _UncacheableValue
                retained_bytes = (
                    _value_size_bytes(value)
                    + str.__sizeof__(key)
                    + _CACHE_ENTRY_OVERHEAD_BYTES
                )
            except Exception:
                # Sizing is an admission optimization. An unmeasurable value
                # still reaches the loader's original callers.
                retained_bytes = _MAX_ENTRY_BYTES + 1

            with _CACHE_LOCK:
                current = _INFLIGHT.get(key)
                admitted_at = monotonic()
                if (current is not None and current[0] is event and current[1] is holder
                        and expires_at > admitted_at):
                    _sweep_expired_locked(admitted_at, retired)
                    _admit_cache_entry_locked(key, expires_at, value, retained_bytes, retired)
        return value
    except BaseException as exc:
        holder["error"] = exc
        raise
    finally:
        with _CACHE_LOCK:
            current = _INFLIGHT.get(key)
            if current is not None and current[0] is event and current[1] is holder:
                _INFLIGHT.pop(key, None)
        event.set()


def get_live_probe_cache_many(
    keys: list[str],
    *,
    ttl_seconds: float,
    loader: Callable[[list[str]], dict[str, Any]],
    wait_timeout_seconds: float | None = None,
    include_cached_keys: bool = False,
) -> dict[str, Any] | tuple[dict[str, Any], set[str]]:
    """Coalesce overlapping target sets while allowing one native batch load."""
    unique_keys = list(dict.fromkeys(str(key) for key in keys if str(key)))
    if not unique_keys:
        return ({}, set()) if include_cached_keys else {}

    now = monotonic()
    retired: list[tuple[float, Any, int]] = []
    cached_values: dict[str, Any] = {}
    owned: dict[str, tuple[Event, dict[str, Any]]] = {}
    waiting: dict[str, tuple[Event, dict[str, Any]]] = {}
    reused_keys: set[str] = set()
    with _CACHE_LOCK:
        _sweep_expired_locked(now, retired)
        for key in unique_keys:
            cached = _CACHE.get(key)
            if cached is not None and cached[0] > now:
                _CACHE.move_to_end(key)
                cached_values[key] = cached[1]
                reused_keys.add(key)
                continue
            if cached is not None:
                _remove_cache_entry_locked(key, retired)
            inflight = _INFLIGHT.get(key)
            if inflight is None:
                event = Event()
                holder: dict[str, Any] = {"owner_thread_id": get_ident()}
                _INFLIGHT[key] = (event, holder)
                owned[key] = (event, holder)
            else:
                waiting[key] = inflight
    retired.clear()

    try:
        if owned:
            loaded = loader(list(owned))
            if not isinstance(loaded, dict) or any(key not in loaded for key in owned):
                raise ValueError("LIVE_PROBE_CACHE_BATCH_RESULT_INCOMPLETE")
            finished_at = monotonic()
            expires_at = finished_at + max(float(ttl_seconds), 0.0)
            for key, (event, holder) in owned.items():
                value = loaded[key]
                holder["value"] = value
                try:
                    retained_bytes = _value_size_bytes(value) + str.__sizeof__(key) + _CACHE_ENTRY_OVERHEAD_BYTES
                except Exception:
                    retained_bytes = _MAX_ENTRY_BYTES + 1
                with _CACHE_LOCK:
                    current = _INFLIGHT.get(key)
                    if current is not None and current[0] is event and current[1] is holder:
                        _sweep_expired_locked(monotonic(), retired)
                        _admit_cache_entry_locked(key, expires_at, value, retained_bytes, retired)
                cached_values[key] = value
        deadline = None if wait_timeout_seconds is None else monotonic() + max(0.0, wait_timeout_seconds)
        for key, (event, holder) in waiting.items():
            if holder.get("owner_thread_id") == get_ident():
                raise RuntimeError("LIVE_PROBE_CACHE_REENTRANT_LOAD")
            remaining = None if deadline is None else max(0.0, deadline - monotonic())
            if not event.wait(timeout=remaining):
                continue
            if "error" in holder:
                raise holder["error"]
            if "value" in holder:
                cached_values[key] = holder["value"]
                reused_keys.add(key)
    except BaseException as exc:
        for event, holder in owned.values():
            holder["error"] = exc
        raise
    finally:
        with _CACHE_LOCK:
            for key, (event, holder) in owned.items():
                current = _INFLIGHT.get(key)
                if current is not None and current[0] is event and current[1] is holder:
                    _INFLIGHT.pop(key, None)
                event.set()
        retired.clear()
    return (cached_values, reused_keys) if include_cached_keys else cached_values


def peek_live_probe_cache(key: str) -> Any | None:
    now = monotonic()
    retired: list[tuple[float, Any, int]] = []
    with _CACHE_LOCK:
        _sweep_expired_locked(now, retired, requested_key=key)
        cached = _CACHE.get(key)
        if cached is None:
            return None
        expires_at, value, _retained_bytes = cached
        if expires_at <= now:
            _remove_cache_entry_locked(key, retired)
            return None
        _CACHE.move_to_end(key)
        return value


def clear_live_probe_cache() -> None:
    global _CACHE_RETAINED_BYTES, _LAST_EXPIRY_SWEEP_AT
    retired: list[tuple[float, Any, int]] = []
    with _CACHE_LOCK:
        retired.extend(_CACHE.values())
        _CACHE.clear()
        _CACHE_RETAINED_BYTES = 0
        _LAST_EXPIRY_SWEEP_AT = 0.0
        _INFLIGHT.clear()
    retired.clear()


def clear_live_probe_cache_matching(predicate: Callable[[str], bool]) -> None:
    global _CACHE_RETAINED_BYTES
    retired: list[tuple[float, Any, int]] = []
    with _CACHE_LOCK:
        for key in tuple(_CACHE):
            if predicate(key):
                _remove_cache_entry_locked(key, retired)
        for key in tuple(_INFLIGHT):
            if predicate(key):
                _INFLIGHT.pop(key, None)
    retired.clear()


def clear_live_probe_cache_for_connection(connection_id: str) -> None:
    normalized = str(connection_id or "").strip()
    if not normalized:
        return
    dot_suffix = f".{normalized}"
    observation_prefix = "state_snapshot.external_source_observations:"
    observation_suffix = f":{normalized}"
    clear_live_probe_cache_matching(
        lambda key: key.endswith(dot_suffix)
        or (key.startswith(observation_prefix) and key.endswith(observation_suffix))
    )
