from __future__ import annotations

from datetime import datetime, timezone
from time import monotonic
from typing import Any

import httpx

from fwrouter_api.services.selector import get_vpn_auto_state
from fwrouter_api.services.subject_policy import get_subject_with_effective_state
from fwrouter_api.services.xray_runtime_state import _load_xray_bindings_state


DEFAULT_PROXY_GET_URL = "https://www.gstatic.com/generate_204"


def _result(subject_id: str, *, status: str, error_code: str | None = None, error_message: str | None = None, **extra: Any) -> dict[str, Any]:
    return {"subject_id": subject_id, "status": status, "latency_ms": None, "http_status": None, "checked_at": datetime.now(timezone.utc).isoformat(), "error_code": error_code, "error_message": error_message, **extra}


def check_subject_proxy_get(*, subject_id: str, url: str = DEFAULT_PROXY_GET_URL, timeout_ms: int = 10000) -> dict[str, Any]:
    """GET through the exact Mihomo mixed handoff materialized for one Xray subject."""
    normalized = str(subject_id or "").strip()
    subject = get_subject_with_effective_state(normalized)
    if not isinstance(subject, dict):
        return _result(normalized, status="failed", error_code="SUBJECT_NOT_FOUND", error_message="Subject was not found.")
    if str(subject.get("implementation_kind") or "") != "xray":
        return _result(normalized, status="failed", error_code="SUBJECT_NOT_VLESS", error_message="Subject has no Xray VLESS runtime.")
    effective = subject.get("effective_state") if isinstance(subject.get("effective_state"), dict) else {}
    source = str(effective.get("selected_server_source") or "").strip()
    selected = str(effective.get("selected_server_id") or "").strip()
    state = _load_xray_bindings_state()
    binding = next((item for item in state.get("bindings") or [] if str(item.get("subject_id") or "") == normalized and item.get("status") == "applied"), None)
    if not isinstance(binding, dict):
        return _result(normalized, status="failed", error_code="XRAY_BINDING_MISSING", error_message="Applied Xray binding is missing.", selected_server_source=source, selected_server_id=selected)
    handoff = binding.get("handoff") if isinstance(binding.get("handoff"), dict) else {}
    listen = str(handoff.get("listen") or "").strip()
    port = int(handoff.get("port") or 0)
    if not listen or port <= 0:
        return _result(normalized, status="failed", error_code="HANDOFF_MISSING", error_message="Mihomo handoff listener is missing.", selected_server_source=source, selected_server_id=selected)
    runtime_target = str(binding.get("handoff_proxy_name") or "").strip()
    if runtime_target == "vpn-global":
        runtime_target = str((get_vpn_auto_state().get("selector_runtime") or {}).get("vpn_auto_now") or "vpn-auto")
    started = monotonic()
    try:
        with httpx.Client(proxy=f"http://{listen}:{port}", timeout=max(1.0, timeout_ms / 1000), trust_env=False) as client:
            response = client.get(url)
            response.raise_for_status()
        return _result(normalized, status="success", latency_ms=max(1, int((monotonic() - started) * 1000)), http_status=response.status_code, mode=effective.get("effective_mode"), selected_server_source=source, selected_server_id=selected, effective_runtime_target=runtime_target)
    except httpx.TimeoutException as exc:
        code, message = "CONNECT_TIMEOUT", str(exc)
    except httpx.HTTPStatusError as exc:
        code, message = "HTTP_ERROR", str(exc)
    except httpx.HTTPError as exc:
        code, message = "ROUTE_ERROR", str(exc)
    except Exception as exc:  # pragma: no cover
        code, message = "INTERNAL_ERROR", str(exc)
    return _result(normalized, status="failed", error_code=code, error_message=message, mode=effective.get("effective_mode"), selected_server_source=source, selected_server_id=selected, effective_runtime_target=runtime_target)
