from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, runtime_checkable

from fwrouter_api.adapters.provider_base import ProviderError, RequestBudget


@runtime_checkable
class ProviderAdapter(Protocol):
    """Provider service boundary. Protocol parsing belongs to protocol adapters."""

    provider_id: str

    def get_configs(self, config_id: int | None = None, *, budget: RequestBudget | None = None, max_age_s: float | None = None) -> list[dict[str, object]]: ...

    def get_locations(self, *, budget: RequestBudget | None = None) -> list[dict[str, object]]: ...

    def discover(self, location_id: int, protocol: str, *, max_age_s: float = 10.0, budget: RequestBudget | None = None) -> list[dict[str, object]]: ...

    def get_server_stats(self, config_id: int, *, budget: RequestBudget | None = None) -> dict[str, object]: ...

    def switch_member(self, config_id: int, location_id: int, server_id: int, protocol: str = "hysteria2", *, budget: RequestBudget | None = None) -> dict[str, object]: ...

    def change_protocol(self, config_id: int, location_id: int, protocol: str, *, budget: RequestBudget | None = None) -> dict[str, object]: ...

    @property
    def supported_protocols(self) -> tuple[str, ...]: ...

    def close(self) -> None: ...

    def record_operation_outcome(self, outcome: str) -> None: ...


class ProviderRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, ProviderAdapter] = {}

    def register(self, adapter: ProviderAdapter) -> None:
        key = adapter.provider_id.strip().lower()
        if not key or key in self._adapters:
            raise ValueError("provider adapter id is empty or already registered")
        self._adapters[key] = adapter

    def get(self, provider_id: str) -> ProviderAdapter:
        try:
            return self._adapters[provider_id.strip().lower()]
        except KeyError as exc:
            raise LookupError("provider adapter is not registered") from exc

    def ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._adapters))


def provider_adapter(provider_id: str, binding_revision: str | int = "0", *,
                     settings_getter: Callable[[], object] | None = None,
                     client_factory: Callable[..., ProviderAdapter] | None = None) -> ProviderAdapter:
    """Compose the configured concrete adapter at the service boundary."""
    if provider_id.strip().lower() != "stealthsurf":
        raise LookupError("provider adapter is not registered")
    from fwrouter_api.core.config import get_settings

    settings = (settings_getter or get_settings)()
    key = settings.stealthsurf_api_key.get_secret_value()
    if not key:
        raise ProviderError("PROVIDER_NOT_CONFIGURED")
    if client_factory is None:
        from fwrouter_api.adapters.stealthsurf import StealthSurfClient
        client_factory = StealthSurfClient
    return client_factory(key, binding_revision=str(binding_revision))


SUPPORTED_PROTOCOLS: dict[str, tuple[str, ...]] = {"stealthsurf": ("hysteria2",)}


def provider_metrics(provider_id: str) -> dict[str, object]:
    """Provider-neutral metrics projection with no account IDs or request payloads."""
    if provider_id.strip().lower() != "stealthsurf":
        return {"provider_id": provider_id.strip().lower(), "available": False}
    from fwrouter_api.adapters.stealthsurf import provider_metrics_snapshot

    return {"provider_id": "stealthsurf", "available": True, **provider_metrics_snapshot()}


def configured_provider_binding() -> dict[str, object] | None:
    """Operator configuration composition; Core receives a non-secret binding descriptor."""
    from fwrouter_api.core.config import get_settings
    settings = get_settings()
    if not settings.stealthsurf_api_key.get_secret_value() or not settings.stealthsurf_config_id:
        return None
    return {"provider_id": "stealthsurf", "resource_id": settings.stealthsurf_config_id,
            "default_protocol": "hysteria2", "supported_protocols": SUPPORTED_PROTOCOLS["stealthsurf"]}
