from __future__ import annotations

from threading import Event, Lock
from time import monotonic
from typing import Any, Callable


_CACHE_LOCK = Lock()
_CACHE: dict[str, tuple[float, Any]] = {}
_INFLIGHT: dict[str, tuple[Event, dict[str, Any]]] = {}
_READONLY_MIHOMO_HEALTH_CACHE_KEY = "dataplane_global.mihomo_health"


def get_readonly_mihomo_health(adapter: Any, *, loader: Callable[[], Any] | None = None) -> Any:
    """Return a short-lived, coalesced Mihomo health snapshot for read-only callers."""
    adapter_identity = (
        f"{type(adapter).__module__}.{type(adapter).__qualname__}.{id(adapter):x}"
    )
    return get_live_probe_cache(
        f"{_READONLY_MIHOMO_HEALTH_CACHE_KEY}.{adapter_identity}",
        ttl_seconds=2.0,
        loader=loader if loader is not None else adapter.health,
    )


def get_live_probe_cache(
    key: str,
    *,
    ttl_seconds: float,
    loader: Callable[[], Any],
    force_refresh: bool = False,
) -> Any:
    now = monotonic()
    if not force_refresh:
        with _CACHE_LOCK:
            cached = _CACHE.get(key)
            if cached is not None:
                expires_at, value = cached
                if expires_at > now:
                    return value

    with _CACHE_LOCK:
        inflight = _INFLIGHT.get(key)
        if inflight is None:
            event = Event()
            holder: dict[str, Any] = {}
            _INFLIGHT[key] = (event, holder)
            leader = True
        else:
            event, holder = inflight
            leader = False

    if not leader:
        event.wait()
        if "error" in holder:
            raise holder["error"]
        return holder.get("value")

    try:
        value = loader()
        expires_at = monotonic() + max(float(ttl_seconds), 0.0)
        holder["value"] = value

        with _CACHE_LOCK:
            current = _INFLIGHT.get(key)
            if current is not None and current[0] is event and current[1] is holder:
                _CACHE[key] = (expires_at, value)
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
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if cached is None:
            return None
        expires_at, value = cached
        if expires_at <= now:
            return None
        return value


def clear_live_probe_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()
        _INFLIGHT.clear()


def clear_live_probe_cache_matching(predicate: Callable[[str], bool]) -> None:
    with _CACHE_LOCK:
        for key in set(_CACHE) | set(_INFLIGHT):
            if predicate(key):
                _CACHE.pop(key, None)
                _INFLIGHT.pop(key, None)


def clear_live_probe_cache_for_connection(connection_id: str) -> None:
    normalized = str(connection_id or "").strip()
    if not normalized:
        return
    suffix = f".{normalized}"
    clear_live_probe_cache_matching(lambda key: key.endswith(suffix))
