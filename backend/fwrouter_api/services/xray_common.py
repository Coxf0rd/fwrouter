from __future__ import annotations

from typing import Any

from fwrouter_api.adapters.xray import DEFAULT_XRAY_ADAPTER as ADAPTER_DEFAULT_XRAY_ADAPTER
from fwrouter_api.adapters.xray import XrayApplyResult
from fwrouter_api.services.modules import managed_runtime_operation_blocked
from fwrouter_api.services.xray_runtime_state import _xray_materializable_egress_candidate
from fwrouter_api.services.xray_status import get_xray_status


def _xray_adapter() -> Any:
    from fwrouter_api.services import xray as xray_facade

    return getattr(xray_facade, "DEFAULT_XRAY_ADAPTER", ADAPTER_DEFAULT_XRAY_ADAPTER)


def _materialize_xray_runtime_bindings(**kwargs: Any) -> dict[str, Any]:
    from fwrouter_api.services import xray as xray_facade

    return xray_facade.materialize_xray_runtime_bindings(**kwargs)


def _xray_facade_attr(name: str) -> Any:
    from fwrouter_api.services import xray as xray_facade

    return getattr(xray_facade, name)


def _xray_managed_runtime_blocked(operation: str) -> dict[str, Any] | None:
    return managed_runtime_operation_blocked(
        "xray",
        error_code="XRAY_MANAGED_RUNTIME_REQUIRED",
        operation=operation,
    )


def _xray_client_create_preflight(*, allow_blocked_egress: bool) -> dict[str, Any]:
    status = get_xray_status()
    module = status.get("module") if isinstance(status.get("module"), dict) else {}
    egress = status.get("egress") if isinstance(status.get("egress"), dict) else {}

    if str(module.get("desired_state") or "disabled") != "enabled":
        return {
            "ok": False,
            "code": "XRAY_MODULE_DISABLED",
            "message": "Xray module is disabled. Enable the module before creating client subscriptions.",
            "module": module,
            "egress": egress,
        }

    if bool(egress.get("traffic_available")) or allow_blocked_egress:
        return {
            "ok": True,
            "code": None,
            "message": "Xray client creation preflight passed.",
            "module": module,
            "egress": egress,
        }

    candidate = _xray_materializable_egress_candidate()
    if candidate["ok"]:
        return {
            "ok": True,
            "code": None,
            "message": "Xray egress is not active yet, but it can be materialized after client creation.",
            "module": module,
            "egress": egress,
            "materializable_egress": candidate,
        }

    return {
        "ok": False,
        "code": "XRAY_EGRESS_NOT_READY",
        "message": "Xray egress is not ready and no supported selected VPN server can be materialized.",
        "module": module,
        "egress": egress,
        "materializable_egress": candidate,
    }


def _strip_raw_payload(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _strip_raw_payload(item)
            for key, item in value.items()
            if key != "raw"
        }
    if isinstance(value, list):
        return [_strip_raw_payload(item) for item in value]
    return value


def _reload_failed_result(result: XrayApplyResult) -> bool:
    return bool(result.error_code) and result.error_code.startswith("XRAY_RELOAD")
