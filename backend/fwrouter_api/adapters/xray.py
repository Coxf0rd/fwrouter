from __future__ import annotations

from fwrouter_api.services.artifacts import atomic_write_text
from fwrouter_api.adapters.xray_common import (
    XRAY_API_PORT as XRAY_API_PORT,
    XRAY_API_TAG as XRAY_API_TAG,
    XRAY_COMPOSE_PATH as XRAY_COMPOSE_PATH,
    XRAY_CONTAINER_NAME as XRAY_CONTAINER_NAME,
    XRAY_FALLBACK_OUTBOUND_TAG as XRAY_FALLBACK_OUTBOUND_TAG,
    XRAY_INBOUND_TAG as XRAY_INBOUND_TAG,
    XRAY_LOG_ROOT as XRAY_LOG_ROOT,
    XRAY_MANAGED_DNS_OUTBOUND_TAG as XRAY_MANAGED_DNS_OUTBOUND_TAG,
    XRAY_PUBLIC_HOST,
    XRAY_PUBLIC_PATH,
    XRAY_PUBLIC_PORT,
    XRAY_TRANSPORT as XRAY_TRANSPORT,
    XrayAdapter,
    XrayAdapterError,
    XrayApplyResult,
    XrayClient,
    XrayHealth,
    XrayRuntimeState,
)
from fwrouter_api.adapters.xray_noop import NoopXrayAdapter
from fwrouter_api.adapters.xray_real import DEFAULT_XRAY_ADAPTER, RealXrayAdapter


__all__ = [
    "DEFAULT_XRAY_ADAPTER",
    "NoopXrayAdapter",
    "RealXrayAdapter",
    "XRAY_PUBLIC_HOST",
    "XRAY_PUBLIC_PATH",
    "XRAY_PUBLIC_PORT",
    "XrayAdapter",
    "XrayAdapterError",
    "XrayApplyResult",
    "XrayClient",
    "XrayHealth",
    "XrayRuntimeState",
    "atomic_write_text",
]
