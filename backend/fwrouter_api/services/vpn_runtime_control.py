from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

from fwrouter_api.services.selector import get_vpn_auto_state, select_vpn_auto_server
from fwrouter_api.services.server_ping import check_active_server_delay

MAX_SELECTOR_RESPONSE_BYTES = 64 * 1024


@dataclass
class VpnRuntimeController:
    vpn_adapter: dict[str, Any]
    routing: dict[str, Any] | None = None

    def get_state(self) -> dict[str, Any]:
        source = self.vpn_adapter.get("source") if isinstance(self.vpn_adapter.get("source"), dict) else {}
        active_target_id = str(source.get("system_id") or source.get("module") or "").strip() or None
        return {
            "path_key": self._path_key(active_target_id=active_target_id),
            "ready": bool(self.vpn_adapter.get("ready")),
            "selection_mode": "unknown",
            "active_target_id": active_target_id,
            "active_target_valid": bool(active_target_id),
            "failover_supported": False,
            "initial_select_supported": False,
            "probe_supported": False,
            "adapter": self.vpn_adapter,
        }

    def probe(
        self,
        *,
        update_ping_state: bool,
        timeout_ms: int,
        reason: str,
    ) -> dict[str, Any] | None:
        return None

    def failover(
        self,
        *,
        apply: bool,
        reason: str,
        update_ping_state: bool,
        candidate_limit: int,
        timeout_ms: int,
    ) -> dict[str, Any]:
        state = self.get_state()
        return {
            "ok": False,
            "applied": False,
            "action": "none",
            "reason": "failover_unavailable",
            "error_code": "WATCHDOG_FAILOVER_UNAVAILABLE",
            "error_message": "Active VPN runtime does not expose automatic failover.",
            "runtime_state": state,
        }

    def initial_select(
        self,
        *,
        apply: bool,
        reason: str,
        update_ping_state: bool,
        candidate_limit: int,
        timeout_ms: int,
    ) -> dict[str, Any]:
        state = self.get_state()
        return {
            "ok": False,
            "applied": False,
            "action": "none",
            "reason": "initial_select_unavailable",
            "error_code": "WATCHDOG_INITIAL_SELECTION_UNAVAILABLE",
            "error_message": "Active VPN runtime does not expose automatic initial selection.",
            "runtime_state": state,
        }

    def _path_key(self, *, active_target_id: str | None) -> str:
        source = self.vpn_adapter.get("source") if isinstance(self.vpn_adapter.get("source"), dict) else {}
        adapter_id = str(self.vpn_adapter.get("adapter_id") or "unknown").strip() or "unknown"
        lifecycle = str(self.vpn_adapter.get("lifecycle_mode") or "unknown").strip() or "unknown"
        source_key = str(source.get("system_id") or source.get("module") or source.get("kind") or "unknown").strip() or "unknown"
        target = str(active_target_id or "none").strip() or "none"
        return f"{adapter_id}:{lifecycle}:{source_key}:{target}"


class MihomoVpnRuntimeController(VpnRuntimeController):
    def get_state(self) -> dict[str, Any]:
        routing = self.routing or {}
        selector_state = get_vpn_auto_state()
        server_mode = str(routing.get("server_mode") or selector_state.get("server_mode") or "auto").strip().lower()
        selection_mode = "auto" if server_mode == "auto" else "manual"
        active_target_id = str(selector_state.get("active_auto_server_id") or routing.get("active_auto_server_id") or "").strip() or None
        ready = bool(self.vpn_adapter.get("ready")) and str(selector_state.get("mihomo_runtime_state") or "running") == "running"
        return {
            "path_key": self._path_key(active_target_id=active_target_id),
            "ready": ready,
            "selection_mode": selection_mode,
            "active_target_id": active_target_id,
            "active_target_valid": bool(selector_state.get("active_auto_server_valid")),
            "failover_supported": True,
            "initial_select_supported": True,
            "probe_supported": True,
            "adapter": self.vpn_adapter,
            "selector_state": selector_state,
        }

    def probe(
        self,
        *,
        update_ping_state: bool,
        timeout_ms: int,
        reason: str,
    ) -> dict[str, Any] | None:
        state = self.get_state()
        active_target_id = state.get("active_target_id")
        if not active_target_id:
            return None
        return check_active_server_delay(
            update_state=update_ping_state,
            checked_by=f"watchdog_active_check:{reason}",
            timeout_ms=timeout_ms,
        )

    def failover(
        self,
        *,
        apply: bool,
        reason: str,
        update_ping_state: bool,
        candidate_limit: int,
        timeout_ms: int,
    ) -> dict[str, Any]:
        selector = select_vpn_auto_server(
            apply=apply,
            reason=f"watchdog_failover:{reason}",
            check_on_demand=True,
            update_ping_state=update_ping_state,
            on_demand_limit=candidate_limit,
            timeout_ms=timeout_ms,
            exclude_active=True,
            post_check=True,
        )
        return {
            "ok": bool(selector.get("ok")),
            "applied": bool(selector.get("applied") or (apply and selector.get("ok"))),
            "action": "switch_vpn_auto" if apply else "dry_run_only",
            "reason": reason,
            "previous_target_id": selector.get("active_before"),
            "selected_target_id": selector.get("active_after") or selector.get("selected_server_id"),
            "selector": selector,
            "runtime_state": self.get_state(),
        }

    def initial_select(
        self,
        *,
        apply: bool,
        reason: str,
        update_ping_state: bool,
        candidate_limit: int,
        timeout_ms: int,
    ) -> dict[str, Any]:
        state = self.get_state()
        selector = select_vpn_auto_server(
            apply=apply,
            reason=f"watchdog_initial_select:{reason}",
            check_on_demand=True,
            update_ping_state=update_ping_state,
            on_demand_limit=candidate_limit,
            timeout_ms=timeout_ms,
            exclude_active=bool(state.get("active_target_id")),
            post_check=True,
        )
        return {
            "ok": bool(selector.get("ok")),
            "applied": bool(selector.get("applied") or (apply and selector.get("ok"))),
            "action": "switch_vpn_auto" if apply else "dry_run_only",
            "reason": reason,
            "previous_target_id": selector.get("active_before"),
            "selected_target_id": selector.get("active_after") or selector.get("selected_server_id"),
            "selector": selector,
            "runtime_state": self.get_state(),
        }


class ExternalVpnRuntimeController(VpnRuntimeController):
    def _source(self) -> dict[str, Any]:
        source = self.vpn_adapter.get("source") if isinstance(self.vpn_adapter.get("source"), dict) else {}
        return source

    def _capabilities(self) -> dict[str, Any]:
        source = self._source()
        capabilities = source.get("capabilities") if isinstance(source.get("capabilities"), dict) else {}
        return capabilities

    def _endpoints(self) -> dict[str, Any]:
        source = self._source()
        endpoints = source.get("endpoints") if isinstance(source.get("endpoints"), dict) else {}
        return endpoints

    def _selector_supported(self) -> bool:
        endpoints = self._endpoints()
        return (
            bool(self._capabilities().get("supports_selector_api"))
            and bool(str(endpoints.get("selector_state_url") or "").strip())
            and bool(str(endpoints.get("selector_failover_url") or "").strip())
        )

    def _read_selector_state(self) -> dict[str, Any] | None:
        url = str(self._endpoints().get("selector_state_url") or "").strip()
        if not url:
            return None
        request = Request(url, headers={"accept": "application/json", "user-agent": "FWRouterWatchdog/1.0"})
        with urlopen(request, timeout=2.0) as response:
            raw = response.read(MAX_SELECTOR_RESPONSE_BYTES + 1)
            if len(raw) > MAX_SELECTOR_RESPONSE_BYTES:
                raise ValueError("External selector state response is too large.")
            if response.status >= 400:
                raise ValueError(f"External selector state returned HTTP {response.status}.")
        payload = json.loads(raw.decode("utf-8"))
        return payload if isinstance(payload, dict) else None

    def _post_selector_failover(self, payload: dict[str, Any]) -> dict[str, Any]:
        url = str(self._endpoints().get("selector_failover_url") or "").strip()
        if not url:
            raise ValueError("External selector failover URL is not configured.")
        raw_body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = Request(
            url,
            data=raw_body,
            headers={
                "accept": "application/json",
                "content-type": "application/json",
                "user-agent": "FWRouterWatchdog/1.0",
            },
            method="POST",
        )
        with urlopen(request, timeout=5.0) as response:
            raw = response.read(MAX_SELECTOR_RESPONSE_BYTES + 1)
            if len(raw) > MAX_SELECTOR_RESPONSE_BYTES:
                raise ValueError("External selector failover response is too large.")
            if response.status >= 400:
                raise ValueError(f"External selector failover returned HTTP {response.status}.")
        loaded = json.loads(raw.decode("utf-8") or "{}")
        return loaded if isinstance(loaded, dict) else {}

    def get_state(self) -> dict[str, Any]:
        source = self._source()
        active_target_id = str(source.get("system_id") or "").strip() or None
        selection_mode = "unknown"
        selector_state = None
        error_message = None
        if self._selector_supported():
            try:
                selector_state = self._read_selector_state()
            except (OSError, URLError, ValueError, json.JSONDecodeError) as exc:
                error_message = str(exc)
            if isinstance(selector_state, dict):
                active_target_id = (
                    str(
                        selector_state.get("active_target_id")
                        or selector_state.get("active_server_id")
                        or selector_state.get("active")
                        or active_target_id
                        or ""
                    ).strip()
                    or None
                )
                raw_mode = str(selector_state.get("selection_mode") or selector_state.get("mode") or "").strip().lower()
                if raw_mode in {"auto", "manual", "none"}:
                    selection_mode = raw_mode
        return {
            "path_key": self._path_key(active_target_id=active_target_id),
            "ready": bool(self.vpn_adapter.get("ready")) and (error_message is None or not self._selector_supported()),
            "selection_mode": selection_mode,
            "active_target_id": active_target_id,
            "active_target_valid": bool(active_target_id),
            "failover_supported": self._selector_supported(),
            "initial_select_supported": False,
            "probe_supported": False,
            "adapter": self.vpn_adapter,
            "selector_state": selector_state,
            "error_message": error_message,
        }

    def failover(
        self,
        *,
        apply: bool,
        reason: str,
        update_ping_state: bool,
        candidate_limit: int,
        timeout_ms: int,
    ) -> dict[str, Any]:
        state = self.get_state()
        if not bool(state.get("failover_supported")):
            return super().failover(
                apply=apply,
                reason=reason,
                update_ping_state=update_ping_state,
                candidate_limit=candidate_limit,
                timeout_ms=timeout_ms,
            )
        payload = {
            "apply": bool(apply),
            "reason": f"watchdog_failover:{reason}",
            "requested_by": "fwrouter_watchdog",
            "exclude_target_id": state.get("active_target_id"),
            "candidate_limit": candidate_limit,
            "timeout_ms": timeout_ms,
        }
        try:
            response = self._post_selector_failover(payload)
        except (OSError, URLError, ValueError, json.JSONDecodeError) as exc:
            return {
                "ok": False,
                "applied": False,
                "action": "none",
                "reason": reason,
                "error_code": "WATCHDOG_EXTERNAL_FAILOVER_FAILED",
                "error_message": str(exc),
                "request": payload,
                "runtime_state": state,
            }
        ok = bool(response.get("ok", True))
        selected_target_id = (
            str(response.get("selected_target_id") or response.get("active_after") or response.get("target_id") or "").strip()
            or None
        )
        return {
            "ok": ok,
            "applied": bool(response.get("applied") or (apply and ok)),
            "action": "external_vpn_failover" if apply else "dry_run_only",
            "reason": reason,
            "previous_target_id": state.get("active_target_id"),
            "selected_target_id": selected_target_id,
            "selector": response,
            "request": payload,
            "runtime_state": self.get_state(),
        }


def get_vpn_runtime_controller(
    vpn_adapter: dict[str, Any],
    *,
    routing: dict[str, Any] | None = None,
) -> VpnRuntimeController:
    adapter_id = str(vpn_adapter.get("adapter_id") or "").strip()
    if adapter_id == "mihomo":
        return MihomoVpnRuntimeController(vpn_adapter=vpn_adapter, routing=routing)
    if adapter_id == "external_vpn_module":
        return ExternalVpnRuntimeController(vpn_adapter=vpn_adapter, routing=routing)
    return VpnRuntimeController(vpn_adapter=vpn_adapter, routing=routing)
