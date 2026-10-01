from __future__ import annotations

from collections import deque
import hashlib
import threading
import time
from typing import Any
import httpx
from fwrouter_api.adapters.provider_base import ProviderError, RequestBudget


def _retry_after_seconds(headers: Any, *, wall_now: float) -> float | None:
    value = headers.get("Retry-After")
    is_reset = False
    if value is None:
        value = headers.get("X-RateLimit-Reset")
        is_reset = value is not None
    if value is None:
        return None
    try:
        amount = float(value)
        # Reset headers commonly carry an epoch timestamp; small values are durations.
        seconds = amount - wall_now if is_reset and amount > wall_now else amount
        return min(3600.0, max(0.0, seconds))
    except (TypeError, ValueError):
        return None


def _quota_reset_delay(headers: Any, *, wall_now: float) -> float | None:
    value = headers.get("X-RateLimit-Reset")
    if value is None:
        return None
    try:
        amount = float(value)
        seconds = amount - wall_now if amount > wall_now else amount
        return min(3600.0, max(0.0, seconds))
    except (TypeError, ValueError):
        return None


class _RateGate:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: dict[tuple[str, str], deque[float]] = {}
        self._blocked_until: dict[tuple[str, str], float] = {}
        self._quota: dict[tuple[str, str], tuple[int, float]] = {}

    def block(self, account: str, bucket: str, *, now: float, seconds: float) -> None:
        key = (account, bucket)
        until = now + min(3600.0, max(0.0, seconds))
        with self._lock:
            self._blocked_until[key] = max(self._blocked_until.get(key, 0.0), until)

    def admit(self, account: str, bucket: str, *, limit: int, window: float, now: float) -> None:
        key = (account, bucket)
        with self._lock:
            quota = self._quota.get(key)
            if quota is not None:
                remaining, reset_at = quota
                if now >= reset_at:
                    self._quota.pop(key, None)
                elif remaining <= 0:
                    raise ProviderError("RATE_LIMITED", retryable=True,
                                        retry_after_seconds=min(3600.0, reset_at - now))
                else:
                    self._quota[key] = (remaining - 1, reset_at)
            blocked_until = self._blocked_until.get(key, 0.0)
            if now < blocked_until:
                raise ProviderError("RATE_LIMITED", retryable=True,
                                    retry_after_seconds=min(3600.0, blocked_until - now))
            self._blocked_until.pop(key, None)
            events = self._events.setdefault(key, deque())
            while events and now - events[0] >= window:
                events.popleft()
            if len(events) >= limit:
                raise ProviderError("RATE_LIMITED", retryable=True,
                                    retry_after_seconds=max(0.0, window - (now - events[0])))
            events.append(now)

    def observe_quota(self, account: str, bucket: str, *, remaining: int,
                      reset_at: float) -> None:
        key = (account, bucket)
        with self._lock:
            self._quota[key] = (max(0, remaining), reset_at)


class _BoundedCache:
    def __init__(self, max_entries: int = 512) -> None:
        self._lock = threading.Lock()
        self._values: dict[tuple[object, ...], tuple[float, object]] = {}
        self._flights: dict[tuple[object, ...], threading.Lock] = {}
        self._max_entries = max_entries

    def get(self, key: tuple[object, ...], now: float, max_age: float) -> object | None:
        with self._lock:
            item = self._values.get(key)
            if item is None or max_age <= 0 or now - item[0] >= max_age:
                return None
            return item[1]

    def lookup(self, key: tuple[object, ...], now: float,
               max_age: float) -> tuple[object | None, bool]:
        with self._lock:
            item = self._values.get(key)
            if item is None:
                return None, False
            if max_age <= 0 or now - item[0] >= max_age:
                return None, True
            return item[1], False

    def set(self, key: tuple[object, ...], value: object, now: float) -> None:
        with self._lock:
            if len(self._values) >= self._max_entries and key not in self._values:
                oldest = min(self._values, key=lambda item: self._values[item][0])
                self._values.pop(oldest, None)
            self._values[key] = (now, value)

    def get_or_load(self, key: tuple[object, ...], max_age: float, loader: Any,
                    *, lock_timeout: float) -> Any:
        with self._lock:
            lock = self._flights.setdefault(key, threading.Lock())
        acquired = lock.acquire(timeout=max(0.0, lock_timeout))
        if not acquired:
            raise ProviderError("BUDGET_EXHAUSTED", retryable=True)
        try:
            now = time.monotonic()
            cached = self.get(key, now, max_age)
            if cached is not None:
                return cached
            result = loader()
            self.set(key, result, time.monotonic())
            with self._lock:
                if len(self._flights) > self._max_entries:
                    self._flights.pop(next(iter(self._flights)))
            return result
        finally:
            lock.release()

    def invalidate_account(self, account: str) -> None:
        with self._lock:
            self._values = {key: value for key, value in self._values.items() if key[0] != account}

    def invalidate_matching(self, account: str, cache_class: str, scope: tuple[object, ...] | None = None) -> None:
        with self._lock:
            self._values = {
                key: value for key, value in self._values.items()
                if not (key[0] == account and key[2] == cache_class
                        and (scope is None or key[3:] == scope))
            }


_GATE = _RateGate()
_CACHE = _BoundedCache()
_BUCKETS = {"general": (60, 60.0), "discovery": (5, 5.0), "stats": (3, 5.0), "mutation": (1, 1.0)}
_CACHE_TTLS = {"topology": 86400.0, "config": 30.0, "discovery": 10.0, "stats": 15.0}
_METRIC_LOCK = threading.Lock()
_METRICS: dict[str, Any] = {
    "requests": {}, "cache_hits": {}, "cache_misses": {}, "stale_rejections": {},
    "rate_limit_rejections": 0, "timeouts": 0, "errors": {}, "discoveries": 0,
    "mutation_outcomes": {},
}


def _metric_endpoint(method: str, path: str) -> str:
    if path == "/configs/available-servers":
        return f"{method} /configs/available-servers"
    if path.startswith("/configs/") and path.endswith("/serverStats"):
        return f"{method} /configs/:config_id/serverStats"
    if path.startswith("/configs/") and path.endswith("/settings"):
        return f"{method} /configs/:config_id/settings"
    if path.startswith("/configs/"):
        return f"{method} /configs/:config_id"
    return f"{method} {path}"


def _increment(bucket: str, key: str | None = None, amount: float = 1) -> None:
    with _METRIC_LOCK:
        if key is None:
            _METRICS[bucket] = _METRICS.get(bucket, 0) + amount
        else:
            values = _METRICS.setdefault(bucket, {})
            values[key] = values.get(key, 0) + amount


def provider_metrics_snapshot() -> dict[str, Any]:
    """Return bounded process-local counters with endpoint templates only."""
    with _METRIC_LOCK:
        return {key: dict(value) if isinstance(value, dict) else value for key, value in _METRICS.items()}


def record_provider_mutation_outcome(outcome: str) -> None:
    safe = outcome if outcome in {"verified", "partial", "failed", "unconfirmed", "deferred", "noop"} else "other"
    _increment("mutation_outcomes", safe)


class StealthSurfClient:
    """Bounded StealthSurf API client; all transport and mutation calls are explicit."""

    provider_id = "stealthsurf"
    @property
    def supported_protocols(self) -> tuple[str, ...]:
        from fwrouter_api.adapters.stealthsurf_protocols import supported_protocols
        return supported_protocols()

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api.stealthsurf.net",
        transport: httpx.Client | None = None,
        timeout_seconds: float = 8.0,
        binding_revision: str = "0",
    ) -> None:
        if not api_key or not api_key.strip():
            raise ValueError("StealthSurf API key is required")
        self._api_key = api_key.strip()
        self._base_url = base_url.rstrip("/")
        self._transport = transport or httpx.Client(
            timeout=httpx.Timeout(timeout_seconds), trust_env=False, follow_redirects=False
        )
        self._owns_transport = transport is None
        self._timeout = timeout_seconds
        self._account = hashlib.sha256(self._api_key.encode()).hexdigest()
        self.binding_revision = str(binding_revision)

    def close(self) -> None:
        if self._owns_transport:
            self._transport.close()

    def invalidate(self, *, binding_revision: str | None = None) -> None:
        if binding_revision is not None:
            self.binding_revision = str(binding_revision)
        _CACHE.invalidate_account(self._account)

    def record_operation_outcome(self, outcome: str) -> None:
        record_provider_mutation_outcome(outcome)

    def _invalidate_scope(self, cache_class: str, *scope: object) -> None:
        _CACHE.invalidate_matching(self._account, cache_class, scope)

    def _get(self, path: str, *, params: dict[str, object] | None = None, bucket: str = "general",
             budget: RequestBudget | None = None) -> Any:
        return self._request("GET", path, params=params, bucket=bucket, budget=budget)

    def _request(self, method: str, path: str, *, params: dict[str, object] | None = None,
                 json: dict[str, object] | None = None, bucket: str = "general",
                 budget: RequestBudget | None = None) -> Any:
        now = time.monotonic()
        started = time.perf_counter()
        op_name = budget.operation if budget and budget.operation in {
            "enable", "refresh", "targeted_refresh", "recovery", "recovery_refresh",
            "recovery_confirmation_2", "switch", "protocol", "diagnostics", "provider_api",
        } else "provider_api"
        endpoint = f"{op_name} {_metric_endpoint(method, path)}"
        if budget is not None:
            budget.consume(now)
        limit, window = _BUCKETS[bucket]
        try:
            _GATE.admit(self._account, "general", limit=60, window=60.0, now=now)
            if bucket != "general":
                _GATE.admit(self._account, bucket, limit=limit, window=window, now=now)
        except ProviderError:
            _increment("rate_limit_rejections")
            raise
        timeout = self._timeout
        if budget is not None and budget.started_at is not None:
            remaining = budget.deadline_seconds - (now - budget.started_at)
            if remaining <= 0:
                raise ProviderError("BUDGET_EXHAUSTED", retryable=True)
            timeout = min(timeout, remaining)
        try:
            response = self._transport.request(
                method, f"{self._base_url}{path}", params=params,
                headers={"Authorization": f"Bearer {self._api_key}", "Accept": "application/json"},
                json=json, timeout=timeout,
            )
        except httpx.TimeoutException:
            _increment("requests", endpoint)
            _increment("request_latency_ms_total", endpoint, (time.perf_counter() - started) * 1000)
            _increment("timeouts")
            _increment("errors", f"{endpoint}:timeout")
            raise ProviderError("PROVIDER_TIMEOUT", retryable=True) from None
        except httpx.HTTPError:
            _increment("requests", endpoint)
            _increment("request_latency_ms_total", endpoint, (time.perf_counter() - started) * 1000)
            _increment("errors", f"{endpoint}:transport")
            raise ProviderError("PROVIDER_TRANSPORT_ERROR", retryable=True) from None
        duration_ms = (time.perf_counter() - started) * 1000
        _increment("requests", endpoint)
        _increment("request_latency_ms_total", endpoint, duration_ms)
        if method == "PATCH":
            _increment("mutation_requests", endpoint)
        if path == "/configs/available-servers":
            _increment("discoveries")
        if 200 <= response.status_code < 300 and response.headers.get("X-RateLimit-Remaining") is not None:
            try:
                server_remaining = max(0, int(response.headers["X-RateLimit-Remaining"]))
            except (ValueError, TypeError):
                server_remaining = None
            if server_remaining is not None:
                window = _quota_reset_delay(response.headers, wall_now=time.time())
                limit_window = _BUCKETS.get(bucket, _BUCKETS["general"])[1]
                _GATE.observe_quota(self._account, bucket, remaining=server_remaining,
                                    reset_at=now + (window if window is not None else limit_window))
        if response.status_code == 429:
            retry_after_seconds = _retry_after_seconds(response.headers, wall_now=time.time())
            if retry_after_seconds is not None:
                _GATE.block(self._account, "general", now=now, seconds=retry_after_seconds)
                if bucket != "general":
                    _GATE.block(self._account, bucket, now=now, seconds=retry_after_seconds)
            _increment("rate_limit_rejections")
            _increment("errors", f"{endpoint}:429")
            raise ProviderError("PROVIDER_RATE_LIMIT", retryable=True, status_code=429,
                                retry_after_seconds=retry_after_seconds)
        if response.status_code in (401, 403):
            _increment("errors", f"{endpoint}:auth")
            raise ProviderError("PROVIDER_AUTH_FAILED", status_code=response.status_code)
        if response.status_code >= 500:
            _increment("errors", f"{endpoint}:server")
            raise ProviderError("PROVIDER_UNAVAILABLE", retryable=True, status_code=response.status_code)
        if response.status_code >= 400:
            _increment("errors", f"{endpoint}:request")
            raise ProviderError("PROVIDER_REQUEST_REJECTED", status_code=response.status_code)
        try:
            payload = response.json()
        except (ValueError, TypeError) as exc:
            _increment("errors", f"{endpoint}:invalid_response")
            raise ProviderError("PROVIDER_INVALID_RESPONSE") from exc
        if not isinstance(payload, dict) or payload.get("status") is not True:
            _increment("errors", f"{endpoint}:provider_error")
            raise ProviderError("PROVIDER_OPERATION_FAILED", status_code=response.status_code)
        if payload.get("statusCode") not in (None, 200, 201):
            _increment("errors", f"{endpoint}:provider_error")
            raise ProviderError("PROVIDER_OPERATION_FAILED", status_code=response.status_code)
        return payload.get("data")

    def _cached(self, cache_class: str, scope: tuple[object, ...], loader: Any,
                max_age: float | None = None, budget: RequestBudget | None = None) -> Any:
        key = (self._account, self.binding_revision, cache_class, *scope)
        freshness = max_age if max_age is not None else _CACHE_TTLS[cache_class]
        cached, stale = _CACHE.lookup(key, time.monotonic(), freshness)
        metric = f"{cache_class}"
        if cached is not None:
            _increment("cache_hits", metric)
            return cached
        _increment("cache_misses", metric)
        if stale:
            _increment("stale_rejections", metric)
        lock_timeout = budget.remaining_seconds() if budget is not None else self._timeout
        if lock_timeout <= 0:
            raise ProviderError("BUDGET_EXHAUSTED", retryable=True)
        return _CACHE.get_or_load(key, freshness, loader, lock_timeout=lock_timeout)

    def get_configs(self, config_id: int | None = None, *, budget: RequestBudget | None = None,
                    max_age_s: float | None = None) -> list[dict[str, Any]]:
        if config_id is not None:
            _validate_positive_id(config_id)
        def load() -> list[dict[str, Any]]:
            data = self._get("/configs", budget=budget)
            if not isinstance(data, list):
                raise ProviderError("PROVIDER_INVALID_RESPONSE")
            rows = [row for row in data if isinstance(row, dict)]
            if config_id is not None:
                rows = [row for row in rows if row.get("id") == config_id]
            from fwrouter_api.adapters.stealthsurf_protocols import normalize_config
            return [normalize_config(row) for row in rows]
        return self._cached("config", (config_id,), load, max_age_s, budget)

    def get_locations(self, *, budget: RequestBudget | None = None) -> list[dict[str, Any]]:
        def load() -> list[dict[str, Any]]:
            data = self._get("/locations", budget=budget)
            if not isinstance(data, list):
                raise ProviderError("PROVIDER_INVALID_RESPONSE")
            return [row for row in data if isinstance(row, dict)]
        return self._cached("topology", (), load, budget=budget)

    def discover(self, location_id: int, protocol: str, *, max_age_s: float = 10.0,
                 budget: RequestBudget | None = None) -> list[dict[str, Any]]:
        _validate_positive_id(location_id)
        from fwrouter_api.adapters.stealthsurf_protocols import wire_protocol
        provider_protocol = wire_protocol(protocol)
        def load() -> list[dict[str, Any]]:
            data = self._get("/configs/available-servers", params={"location_id": location_id,
                               "protocol": provider_protocol}, bucket="discovery", budget=budget)
            if not isinstance(data, list):
                raise ProviderError("PROVIDER_INVALID_RESPONSE")
            members: list[dict[str, Any]] = []
            for row in data:
                if (not isinstance(row, dict) or not isinstance(row.get("id"), int)
                        or isinstance(row.get("id"), bool)):
                    raise ProviderError("PROVIDER_INVALID_RESPONSE")
                slots = row.get("available_slots")
                if slots is not None and (not isinstance(slots, int) or isinstance(slots, bool) or slots < 0):
                    raise ProviderError("PROVIDER_INVALID_RESPONSE")
                provider_status, status_source = _normalize_discovery_status(row)
                members.append({"server_id": row["id"], "ip": row.get("ip"),
                                "available_slots": slots, "provider_status": provider_status,
                                "provider_status_source": status_source,
                                "location_id": location_id, "protocol": protocol})
            return members
        return self._cached("discovery", (location_id, protocol), load, max_age_s, budget)

    def get_server_stats(self, config_id: int, *, budget: RequestBudget | None = None) -> dict[str, Any]:
        _validate_positive_id(config_id)
        data = self._cached("stats", (config_id,), lambda: self._get(
            f"/configs/{config_id}/serverStats", bucket="stats", budget=budget), budget=budget)
        if not isinstance(data, dict):
            raise ProviderError("PROVIDER_INVALID_RESPONSE")
        value = data.get("status")
        normalized = str(value).strip().lower() if isinstance(value, str) else ""
        status = {
            "up": "up", "online": "up", "available": "up", "connected": "up",
            "down": "down", "offline": "down",
            "unavailable": "unavailable", "busy": "unavailable",
        }.get(normalized, "unknown")
        return {"status": status}

    def switch_member(self, config_id: int, location_id: int, server_id: int, protocol: str = "hysteria2",
                      *, budget: RequestBudget | None = None) -> dict[str, Any]:
        _validate_positive_id(config_id)
        _validate_positive_id(location_id)
        _validate_positive_id(server_id)
        if protocol not in self.supported_protocols:
            raise ProviderError("PROVIDER_PROTOCOL_UNSUPPORTED")
        from fwrouter_api.adapters.stealthsurf_protocols import wire_protocol
        # Documented config settings mutation; caller is responsible for admission.
        data = self._request("PATCH", f"/configs/{config_id}/settings",
                             json={"location_id": location_id, "protocol": wire_protocol(protocol), "server_id": server_id},
                             bucket="mutation", budget=budget)
        self._invalidate_scope("config", config_id)
        self._invalidate_scope("discovery", location_id, protocol)
        self._invalidate_scope("stats", config_id)
        if not isinstance(data, dict):
            raise ProviderError("MUTATION_RESULT_INCOMPLETE")
        # Return only actual response material; never fill absent fields from request.
        from fwrouter_api.adapters.stealthsurf_protocols import normalize_config
        return normalize_config(data)

    def change_protocol(self, config_id: int, location_id: int, protocol: str,
                        *, budget: RequestBudget | None = None) -> dict[str, Any]:
        _validate_positive_id(config_id)
        _validate_positive_id(location_id)
        if protocol not in self.supported_protocols:
            raise ProviderError("PROVIDER_PROTOCOL_UNSUPPORTED")
        from fwrouter_api.adapters.stealthsurf_protocols import wire_protocol
        data = self._request("PATCH", f"/configs/{config_id}/settings",
                             json={"location_id": location_id, "protocol": wire_protocol(protocol)},
                             bucket="mutation", budget=budget)
        self._invalidate_scope("config", config_id)
        self._invalidate_scope("discovery", location_id, protocol)
        self._invalidate_scope("stats", config_id)
        if not isinstance(data, dict):
            raise ProviderError("MUTATION_RESULT_INCOMPLETE")
        from fwrouter_api.adapters.stealthsurf_protocols import normalize_config
        return normalize_config(data)


def _validate_positive_id(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("positive numeric provider ID is required")


def _normalize_discovery_status(row: dict[str, Any]) -> tuple[str, str]:
    """Interpret only explicit availability fields; never infer from slots or stats."""
    raw_status = row.get("status")
    normalized = str(raw_status).strip().lower() if isinstance(raw_status, str) else ""
    known = {
        "up": "available", "online": "available", "available": "available",
        "down": "down", "offline": "down", "unavailable": "unavailable", "busy": "busy",
    }
    if normalized in {"down", "offline", "unavailable", "busy"}:
        return known[normalized], f"status:{normalized}"
    available = row.get("available")
    if isinstance(available, bool):
        return ("available", "available:true") if available else ("unavailable", "available:false")
    if normalized in known:
        return known[normalized], f"status:{normalized}"
    if "status" in row:
        return "unknown", "status:null" if raw_status is None else "status:invalid"
    if "available" in row:
        return "unknown", "available:null" if available is None else "available:invalid"
    return "unknown", "absent"
