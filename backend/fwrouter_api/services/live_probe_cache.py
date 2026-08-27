from __future__ import annotations

from threading import Lock
from time import monotonic
from typing import Any, Callable


_CACHE_LOCK = Lock()
_CACHE: dict[str, tuple[float, Any]] = {}


def get_live_probe_cache(
    key: str,
    *,
    ttl_seconds: float,
    loader: Callable[[], Any],
) -> Any:
    now = monotonic()
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if cached is not None:
            expires_at, value = cached
            if expires_at > now:
                return value

    value = loader()
    expires_at = monotonic() + max(float(ttl_seconds), 0.0)

    with _CACHE_LOCK:
        _CACHE[key] = (expires_at, value)

    return value


def clear_live_probe_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()


def clear_live_probe_cache_matching(predicate: Callable[[str], bool]) -> None:
    with _CACHE_LOCK:
        for key in list(_CACHE):
            if predicate(key):
                _CACHE.pop(key, None)


def clear_live_probe_cache_for_connection(connection_id: str) -> None:
    normalized = str(connection_id or "").strip()
    if not normalized:
        return
    suffix = f".{normalized}"
    clear_live_probe_cache_matching(lambda key: key.endswith(suffix))
