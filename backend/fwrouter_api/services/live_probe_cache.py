from __future__ import annotations

from dataclasses import fields, is_dataclass
from collections import OrderedDict
from enum import Enum
from itertools import count
from threading import Event, Lock, RLock
from time import monotonic
from typing import Any, Callable
import weakref


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
_ADAPTER_TOKEN_LOCK = RLock()
_CACHE: OrderedDict[str, tuple[float, Any, int]] = OrderedDict()
_CACHE_RETAINED_BYTES = 0
_INFLIGHT: dict[str, tuple[Event, dict[str, Any]]] = {}
_LAST_EXPIRY_SWEEP_AT = 0.0

_READONLY_MIHOMO_HEALTH_CACHE_KEY = "dataplane_global.mihomo_health"
_ADAPTER_TOKENS: dict[int, tuple[weakref.ReferenceType[Any], int]] = {}
_NEXT_ADAPTER_TOKEN = count(1)


class _UncacheableValue(Exception):
    """The value graph cannot be estimated within the admission budget."""


def _readonly_adapter_token(adapter: Any) -> int | None:
    """Return a lifetime-unique token without keeping the adapter alive."""
    adapter_id = id(adapter)
    try:
        weakref.ref(adapter)
    except TypeError:
        # A strong-reference fallback would make this cache an adapter owner.
        # Bypass the optional snapshot cache for objects that cannot be fenced.
        return None

    with _ADAPTER_TOKEN_LOCK:
        current = _ADAPTER_TOKENS.get(adapter_id)
        if current is not None and current[0]() is adapter:
            return current[1]

        token = next(_NEXT_ADAPTER_TOKEN)

        def discard(reference: weakref.ReferenceType[Any], *, key: int = adapter_id, value: int = token) -> None:
            with _ADAPTER_TOKEN_LOCK:
                registered = _ADAPTER_TOKENS.get(key)
                if registered is not None and registered[0] is reference and registered[1] == value:
                    _ADAPTER_TOKENS.pop(key, None)

        reference = weakref.ref(adapter, discard)
        _ADAPTER_TOKENS[adapter_id] = (reference, token)
        return token


def get_readonly_mihomo_health(adapter: Any, *, loader: Callable[[], Any] | None = None) -> Any:
    """Return a short-lived, coalesced Mihomo health snapshot for read-only callers."""
    adapter_token = _readonly_adapter_token(adapter)
    probe = loader if loader is not None else adapter.health
    if adapter_token is None:
        return probe()
    adapter_type = type(adapter)
    adapter_identity = f"{adapter_type.__module__}.{adapter_type.__qualname__}.{adapter_token}"
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

    retired = []
    with _CACHE_LOCK:
        _sweep_expired_locked(now, retired, requested_key=key)
        inflight = _INFLIGHT.get(key)
        if inflight is None:
            event = Event()
            holder: dict[str, Any] = {}
            _INFLIGHT[key] = (event, holder)
            leader = True
        else:
            event, holder = inflight
            leader = False
    retired.clear()

    if not leader:
        event.wait()
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
    except Exception as exc:
        holder["error"] = exc
        raise
    finally:
        with _CACHE_LOCK:
            current = _INFLIGHT.get(key)
            if current is not None and current[0] is event and current[1] is holder:
                _INFLIGHT.pop(key, None)
        event.set()


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
