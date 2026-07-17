from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any
from urllib.parse import quote

import socket
import ipaddress
import httpx
import yaml

DEFAULT_BASE_URL = "http://127.0.0.1:5200"
DEFAULT_CONFIG_PATH = Path("/var/lib/fwrouter-v2/generated/mihomo/config.yaml")
DEFAULT_CONTOURS_PATH = Path("/var/lib/fwrouter-v2/generated/mihomo/contours.json")
TRANSPARENT_REDIR_LISTENER_NAME = "fwrouter-redir"
TRANSPARENT_TPROXY_LISTENER_NAME = "fwrouter-tproxy"
TRANSPARENT_TPROXY_LISTENER_BIND = "0.0.0.0"
TRANSPARENT_TPROXY_PROXY_NAME = "vpn-global"
TRANSPARENT_TPROXY_RULE_NAME = "fwrouter-transparent"
BUILTIN_PROXY_NAMES = {
    "COMPATIBLE",
    "DIRECT",
    "GLOBAL",
    "PASS",
    "REJECT",
    "REJECT-DROP",
    "vpn-auto",
}
GROUP_PROXY_TYPES = {
    "Selector",
    "URLTest",
    "Fallback",
    "LoadBalance",
    "Relay",
}


def _transparent_bind_address_valid(value: str | None) -> bool:
    bind = str(value or "").strip()
    if not bind:
        return False
    if bind == TRANSPARENT_TPROXY_LISTENER_BIND:
        return True
    try:
        parsed = ipaddress.ip_address(bind)
    except ValueError:
        return False
    return parsed.version == 4 and not parsed.is_loopback


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


class MihomoRuntimeState(str, Enum):
    NOT_CONFIGURED = "not_configured"
    RUNNING = "running"
    STOPPED = "stopped"
    FAILED = "failed"
    DEGRADED = "degraded"
    PAUSED = "paused"


@dataclass(frozen=True)
class MihomoServer:
    """VPN server as exposed by Mihomo/provider inventory."""

    server_id: str
    server_name: str
    provider_name: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MihomoHealth:
    """Current Mihomo runtime health as seen by adapter."""

    runtime_state: MihomoRuntimeState
    active_server_id: str | None = None
    message: str = ""
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class MihomoApplyResult:
    """Result of applying Mihomo active server/config."""

    ok: bool
    message: str
    active_server_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "message": self.message,
            "active_server_id": self.active_server_id,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "details": self.details,
        }


@dataclass(frozen=True)
class MihomoDelayResult:
    """Result of checking latency through one Mihomo proxy."""

    ok: bool
    server_id: str
    delay_ms: int | None = None
    test_url: str = "https://www.gstatic.com/generate_204"
    timeout_ms: int = 5000
    error_code: str | None = None
    error_message: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "server_id": self.server_id,
            "delay_ms": self.delay_ms,
            "test_url": self.test_url,
            "timeout_ms": self.timeout_ms,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "details": self.details,
        }


class MihomoAdapter:
    """Base interface for Mihomo VPN egress integration."""

    def _delay_json(
        self,
        server_id: str,
        *,
        test_url: str,
        timeout_ms: int,
    ) -> dict[str, Any]:
        encoded = quote(server_id, safe="")
        request_timeout = max(self.timeout_seconds, timeout_ms / 1000 + 2)

        # The Mihomo controller is always local to the host. Never inherit
        # proxy env here, otherwise health/runtime probes can recurse into the
        # explicit proxy path or hang on a broken upstream proxy env.
        with httpx.Client(timeout=request_timeout, trust_env=False) as client:
            response = client.get(
                f"{self.base_url}/proxies/{encoded}/delay",
                headers=self._headers(),
                params={
                    "timeout": timeout_ms,
                    "url": test_url,
                },
            )
            response.raise_for_status()
            data = response.json()

        if not isinstance(data, dict):
            return {"value": data}

        return data

    def health(self) -> MihomoHealth:
        raise NotImplementedError

    def list_servers(self) -> list[MihomoServer]:
        raise NotImplementedError

    def get_active_server_id(self) -> str | None:
        raise NotImplementedError

    def apply_server(self, server_id: str) -> MihomoApplyResult:
        raise NotImplementedError

    def apply_server_to_selector(
        self,
        selector_name: str,
        server_id: str,
    ) -> MihomoApplyResult:
        raise NotImplementedError

    def check_delay(
        self,
        server_id: str,
        *,
        test_url: str = "https://www.gstatic.com/generate_204",
        timeout_ms: int = 5000,
    ) -> MihomoDelayResult:
        raise NotImplementedError

    def check_port(self, port: int, host: str = "127.0.0.1", timeout: float = 1.0) -> bool:
        raise NotImplementedError


class MihomoHttpAdapter(MihomoAdapter):
    """Mihomo controller adapter.

    This adapter reads Mihomo controller state and can switch the FWRouter-owned
    vpn-auto selector. It does not mutate generated config files.
    """

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        config_path: Path = DEFAULT_CONFIG_PATH,
        contours_path: Path = DEFAULT_CONTOURS_PATH,
        timeout_seconds: float = 3.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.config_path = config_path
        self.contours_path = contours_path
        self.timeout_seconds = timeout_seconds

    def _config_text(self) -> str:
        if not self.config_path.exists():
            return ""
        return self.config_path.read_text(encoding="utf-8")

    def _config_top_level_scalar(self, key: str, *, text: str | None = None) -> str | None:
        source = text if text is not None else self._config_text()
        prefix = f"{key}:"
        for line in source.splitlines():
            if not line or line.startswith((" ", "\t", "-")):
                continue
            if line.startswith(prefix):
                return line[len(prefix) :].strip()
        return None

    def _scan_listener_ports(self, *, text: str | None = None) -> dict[str, int | None]:
        source = text if text is not None else self._config_text()
        mixed_port: int | None = None
        tproxy_port: int | None = None
        redir_port: int | None = None
        in_listeners = False
        current_type: str | None = None

        def _commit(listener_type: str | None, port_value: int | None) -> None:
            nonlocal mixed_port, tproxy_port, redir_port
            if listener_type == "mixed" and mixed_port is None:
                mixed_port = port_value
            elif listener_type == "tproxy" and tproxy_port is None:
                tproxy_port = port_value
            elif listener_type == "redir" and redir_port is None:
                redir_port = port_value

        current_port: int | None = None
        for raw_line in source.splitlines():
            line = raw_line.rstrip("\n")
            stripped = line.strip()
            if not stripped:
                continue

            if in_listeners and line.startswith("- "):
                _commit(current_type, current_port)
                current_type = None
                current_port = None
                candidate = line[2:].strip()
                if candidate.startswith("type:"):
                    current_type = candidate.split(":", 1)[1].strip().lower()
                elif candidate.startswith("port:"):
                    value = candidate.split(":", 1)[1].strip()
                    current_port = int(value) if value.isdigit() else None
                continue

            if not line.startswith((" ", "\t")):
                if in_listeners and not line.startswith("listeners:"):
                    _commit(current_type, current_port)
                    break
                in_listeners = line.startswith("listeners:")
                current_type = None
                current_port = None
                continue

            if not line.startswith((" ", "\t")):
                continue

            if not in_listeners:
                continue

            if stripped.startswith("type:"):
                current_type = stripped.split(":", 1)[1].strip().lower()
            elif stripped.startswith("port:"):
                value = stripped.split(":", 1)[1].strip()
                current_port = int(value) if value.isdigit() else None

        if in_listeners:
            _commit(current_type, current_port)

        return {
            "mixed_port": mixed_port,
            "tproxy_port": tproxy_port,
            "redir_port": redir_port,
        }

    def _scan_transparent_listeners(self, *, text: str | None = None) -> dict[str, dict[str, Any]]:
        source = text if text is not None else self._config_text()
        in_listeners = False
        current: dict[str, Any] | None = None
        result: dict[str, dict[str, Any]] = {}

        def _commit() -> None:
            nonlocal current
            if not isinstance(current, dict):
                return
            name = str(current.get("name") or "").strip()
            listener_type = str(current.get("type") or "").strip().lower()
            if name == TRANSPARENT_REDIR_LISTENER_NAME and listener_type == "redir":
                result["redir"] = dict(current)
            if name == TRANSPARENT_TPROXY_LISTENER_NAME and listener_type == "tproxy":
                result["tproxy"] = dict(current)

        for raw_line in source.splitlines():
            line = raw_line.rstrip("\n")
            stripped = line.strip()
            if not stripped:
                continue

            if in_listeners and stripped.startswith("- "):
                _commit()
                current = {}
                candidate = stripped[2:].strip()
                if ":" in candidate:
                    key, value = candidate.split(":", 1)
                    current[key.strip()] = value.strip()
                continue

            if not line.startswith((" ", "\t")):
                if in_listeners and not line.startswith("listeners:"):
                    _commit()
                    break
                in_listeners = line.startswith("listeners:")
                current = None
                continue

            if not in_listeners:
                continue

            if current is None:
                continue

            if ":" not in stripped:
                continue
            key, value = stripped.split(":", 1)
            current[key.strip()] = value.strip()

        _commit()
        return result

    def _scan_tun_enabled(self, *, text: str | None = None) -> bool:
        source = text if text is not None else self._config_text()
        in_tun = False
        for raw_line in source.splitlines():
            line = raw_line.rstrip("\n")
            stripped = line.strip()
            if not stripped:
                continue
            if not line.startswith((" ", "\t")):
                if in_tun and not line.startswith("tun:"):
                    break
                in_tun = line.startswith("tun:")
                continue
            if in_tun and stripped.startswith("enable:"):
                value = stripped.split(":", 1)[1].strip().lower()
                return value in {"1", "true", "yes", "on"}
        return False

    def _transparent_listener_socket_present(
        self,
        *,
        bind: str | None,
        port: int | None,
    ) -> bool:
        if not _transparent_bind_address_valid(bind) or not isinstance(port, int) or port <= 0:
            return False
        target_host = "127.0.0.1" if str(bind).strip() == TRANSPARENT_TPROXY_LISTENER_BIND else str(bind).strip()
        return self.check_port(port, host=target_host, timeout=0.5)

    def _transparent_session_observation(self, payload: dict[str, Any] | None) -> dict[str, Any]:
        connections = payload.get("connections") if isinstance(payload, dict) else None
        if not isinstance(connections, list):
            return {
                "connections_api_available": False,
                "transparent_tcp_session_materialized": False,
                "transparent_udp_session_materialized": False,
                "transparent_tcp_sessions_count": 0,
                "transparent_udp_sessions_count": 0,
            }

        tcp_count = 0
        udp_count = 0
        for item in connections:
            if not isinstance(item, dict):
                continue
            network = str(item.get("network") or item.get("type") or "").strip().lower()
            fields = [
                str(item.get("metadata") or ""),
                str(item.get("rule") or ""),
                str(item.get("chains") or ""),
                str(item.get("inbound") or ""),
                str(item.get("inboundName") or ""),
                str(item.get("proxy") or ""),
            ]
            haystack = " ".join(field.lower() for field in fields if field)
            transparent = any(
                token in haystack
                for token in (
                    TRANSPARENT_REDIR_LISTENER_NAME,
                    TRANSPARENT_TPROXY_LISTENER_NAME,
                    "redir",
                    "tproxy",
                    TRANSPARENT_TPROXY_PROXY_NAME,
                )
            )
            if not transparent:
                continue
            if network.startswith("tcp"):
                tcp_count += 1
            elif network.startswith("udp"):
                udp_count += 1

        return {
            "connections_api_available": True,
            "transparent_tcp_session_materialized": tcp_count > 0,
            "transparent_udp_session_materialized": udp_count > 0,
            "transparent_tcp_sessions_count": tcp_count,
            "transparent_udp_sessions_count": udp_count,
        }

    def _secret(self) -> str:
        secret = self._config_top_level_scalar("secret")
        return str(secret or "")

    def _headers(self) -> dict[str, str]:
        secret = self._secret()
        if not secret:
            return {}
        return {"Authorization": f"Bearer {secret}"}

    def _config_runtime_details(self) -> dict[str, Any]:
        contours = {}
        if self.contours_path.exists():
            try:
                loaded_contours = yaml.safe_load(self.contours_path.read_text(encoding="utf-8")) or {}
            except yaml.YAMLError:
                loaded_contours = {}
            if isinstance(loaded_contours, dict):
                contours = loaded_contours

        if not self.config_path.exists():
            return {
                "config_path": str(self.config_path),
                "contours_path": str(self.contours_path),
                "secret_present": False,
                "mixed_port": None,
                "redir_port": None,
                "tproxy_port": None,
                "tun_enabled": False,
                "transparent_tcp_listener_present": False,
                "transparent_udp_listener_present": False,
                "transparent_tcp_listener_bind": None,
                "transparent_udp_listener_bind": None,
                "transparent_tcp_listener_socket_present": False,
                "transparent_udp_listener_socket_present": False,
                "fwrouter_contours": contours,
            }

        config_text = self._config_text()
        listener_ports = self._scan_listener_ports(text=config_text)
        mixed_port = listener_ports["mixed_port"]
        tproxy_port = listener_ports["tproxy_port"]
        redir_port = listener_ports["redir_port"]
        tun_enabled = self._scan_tun_enabled(text=config_text)
        transparent_listeners = self._scan_transparent_listeners(text=config_text)
        transparent_redir_listener = transparent_listeners.get("redir") if isinstance(transparent_listeners, dict) else None
        transparent_tproxy_listener = transparent_listeners.get("tproxy") if isinstance(transparent_listeners, dict) else None
        bind_address = self._config_top_level_scalar("bind-address", text=config_text)
        transparent_redir_bind = None
        transparent_tproxy_bind = None
        if isinstance(transparent_redir_listener, dict):
            transparent_redir_bind = str(transparent_redir_listener.get("listen") or "").strip() or None
            port_value = _int_or_none(transparent_redir_listener.get("port"))
            if isinstance(port_value, int):
                redir_port = port_value
        if isinstance(transparent_tproxy_listener, dict):
            transparent_tproxy_bind = str(transparent_tproxy_listener.get("listen") or "").strip() or None
            port_value = _int_or_none(transparent_tproxy_listener.get("port"))
            if isinstance(port_value, int):
                tproxy_port = port_value
        if transparent_redir_bind is None and redir_port is not None:
            transparent_redir_bind = str(bind_address or "").strip() or None
        if transparent_tproxy_bind is None and tproxy_port is not None:
            transparent_tproxy_bind = str(bind_address or "").strip() or None
        transparent_listener_bind = transparent_redir_bind or transparent_tproxy_bind
        transparent_listener_loopback_bound = (transparent_listener_bind or "") in {"127.0.0.1", "::1", "localhost"}
        transparent_listener_bind_valid = _transparent_bind_address_valid(transparent_listener_bind)
        transparent_tcp_listener_present = isinstance(transparent_redir_listener, dict)
        transparent_udp_listener_present = isinstance(transparent_tproxy_listener, dict)
        transparent_tcp_listener_bind_valid = _transparent_bind_address_valid(transparent_redir_bind)
        transparent_udp_listener_bind_valid = _transparent_bind_address_valid(transparent_tproxy_bind)
        transparent_tcp_listener_socket_present = self._transparent_listener_socket_present(
            bind=transparent_redir_bind,
            port=redir_port,
        )
        transparent_udp_listener_socket_present = self._transparent_listener_socket_present(
            bind=transparent_tproxy_bind,
            port=tproxy_port,
        )
        transparent_contours = dict(contours)
        transparent_vpn = dict(transparent_contours.get("transparent_vpn") or {})
        if isinstance(transparent_redir_listener, dict) or isinstance(transparent_tproxy_listener, dict):
            redir_rule = (
                str(transparent_redir_listener.get("rule") or "").strip()
                if isinstance(transparent_redir_listener, dict)
                else ""
            )
            tproxy_rule = (
                str(transparent_tproxy_listener.get("rule") or "").strip()
                if isinstance(transparent_tproxy_listener, dict)
                else ""
            )
            redir_proxy = (
                str(transparent_redir_listener.get("proxy") or "").strip()
                if isinstance(transparent_redir_listener, dict)
                else ""
            )
            tproxy_proxy = (
                str(transparent_tproxy_listener.get("proxy") or "").strip()
                if isinstance(transparent_tproxy_listener, dict)
                else ""
            )
            transparent_tcp_target_valid = redir_proxy == TRANSPARENT_TPROXY_PROXY_NAME or redir_rule == TRANSPARENT_TPROXY_RULE_NAME
            transparent_udp_target_valid = tproxy_proxy == TRANSPARENT_TPROXY_PROXY_NAME or tproxy_rule == TRANSPARENT_TPROXY_RULE_NAME
            transparent_vpn["listener_name"] = TRANSPARENT_TPROXY_LISTENER_NAME if isinstance(transparent_tproxy_listener, dict) else TRANSPARENT_REDIR_LISTENER_NAME
            transparent_vpn["listener_listen"] = transparent_listener_bind
            transparent_vpn["listener_port"] = (
                transparent_tproxy_listener.get("port")
                if isinstance(transparent_tproxy_listener, dict)
                else transparent_redir_listener.get("port")
            )
            transparent_vpn["listener_rule"] = tproxy_rule or redir_rule or None
            transparent_vpn["listener_proxy"] = tproxy_proxy or redir_proxy or None
            transparent_vpn["listener_loopback_bound"] = transparent_listener_loopback_bound
            transparent_vpn["listener_bind_valid"] = transparent_listener_bind_valid
            transparent_vpn["listener_count"] = int(isinstance(transparent_redir_listener, dict)) + int(isinstance(transparent_tproxy_listener, dict))
            transparent_vpn["listener_redir_name"] = (
                str(transparent_redir_listener.get("name") or "").strip() if isinstance(transparent_redir_listener, dict) else None
            )
            transparent_vpn["listener_tproxy_name"] = (
                str(transparent_tproxy_listener.get("name") or "").strip() if isinstance(transparent_tproxy_listener, dict) else None
            )
            transparent_vpn["redir_port"] = redir_port
            transparent_vpn["tproxy_port"] = tproxy_port
            transparent_vpn["transparent_tcp_listener_present"] = transparent_tcp_listener_present
            transparent_vpn["transparent_udp_listener_present"] = transparent_udp_listener_present
            transparent_vpn["transparent_tcp_listener_bind"] = transparent_redir_bind
            transparent_vpn["transparent_udp_listener_bind"] = transparent_tproxy_bind
            transparent_vpn["transparent_tcp_listener_bind_valid"] = transparent_tcp_listener_bind_valid
            transparent_vpn["transparent_udp_listener_bind_valid"] = transparent_udp_listener_bind_valid
            transparent_vpn["transparent_tcp_listener_socket_present"] = transparent_tcp_listener_socket_present
            transparent_vpn["transparent_udp_listener_socket_present"] = transparent_udp_listener_socket_present
            transparent_vpn["transparent_tcp_ready"] = (
                transparent_tcp_listener_present
                and transparent_tcp_listener_bind_valid
                and isinstance(redir_port, int)
                and transparent_tcp_target_valid
            )
            transparent_vpn["transparent_udp_ready"] = (
                transparent_udp_listener_present
                and transparent_udp_listener_bind_valid
                and isinstance(tproxy_port, int)
                and transparent_udp_target_valid
            )
            transparent_vpn["ready"] = bool(
                transparent_vpn["transparent_tcp_ready"] and transparent_vpn["transparent_udp_ready"]
            )
        if transparent_vpn:
            transparent_contours["transparent_vpn"] = transparent_vpn

        return {
            "config_path": str(self.config_path),
            "contours_path": str(self.contours_path),
            "secret_present": bool(self._config_top_level_scalar("secret", text=config_text)),
            "mixed_port": mixed_port,
            "redir_port": redir_port,
            "tproxy_port": tproxy_port,
            "tun_enabled": tun_enabled,
            "transparent_listener_bind": transparent_listener_bind,
            "transparent_listener_loopback_bound": transparent_listener_loopback_bound,
            "transparent_listener_bind_valid": transparent_listener_bind_valid,
            "transparent_tcp_listener_present": transparent_tcp_listener_present,
            "transparent_udp_listener_present": transparent_udp_listener_present,
            "transparent_tcp_listener_bind": transparent_redir_bind,
            "transparent_udp_listener_bind": transparent_tproxy_bind,
            "transparent_tcp_listener_socket_present": transparent_tcp_listener_socket_present,
            "transparent_udp_listener_socket_present": transparent_udp_listener_socket_present,
            "fwrouter_contours": transparent_contours,
        }

    def _get_json(self, path: str) -> dict[str, Any]:
        with httpx.Client(timeout=self.timeout_seconds, trust_env=False) as client:
            response = client.get(
                f"{self.base_url}{path}",
                headers=self._headers(),
            )
            response.raise_for_status()
            data = response.json()

        if not isinstance(data, dict):
            return {"value": data}

        return data

    def _put_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        with httpx.Client(timeout=self.timeout_seconds, trust_env=False) as client:
            response = client.put(
                f"{self.base_url}{path}",
                headers=self._headers(),
                json=payload,
            )
            response.raise_for_status()

            if not response.content:
                return {}

            data = response.json()

        if not isinstance(data, dict):
            return {"value": data}

        return data

    def check_port(self, port: int, host: str = "127.0.0.1", timeout: float = 1.0) -> bool:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except (OSError, socket.timeout):
            return False

    def health(self) -> MihomoHealth:
        config_details = self._config_runtime_details()
        try:
            version = self._get_json("/version")
            proxies = self._proxies()
            active_server_id = self.get_active_server_id()
            try:
                connection_details = self._transparent_session_observation(self._get_json("/connections"))
            except (httpx.HTTPError, OSError, yaml.YAMLError):
                connection_details = self._transparent_session_observation(None)
        except (httpx.HTTPError, OSError, yaml.YAMLError) as exc:
            return MihomoHealth(
                runtime_state=MihomoRuntimeState.DEGRADED,
                active_server_id=None,
                message="Mihomo controller is not reachable.",
                details={
                    "adapter": "http",
                    "base_url": self.base_url,
                    "error": str(exc),
                    "config": config_details,
                    "selectors": {},
                },
            )

        contours = config_details.get("fwrouter_contours") if isinstance(config_details.get("fwrouter_contours"), dict) else {}
        transparent_vpn = contours.get("transparent_vpn") if isinstance(contours.get("transparent_vpn"), dict) else {}
        transparent_listener_bind_valid = bool(config_details.get("transparent_listener_bind_valid", True))
        transparent_listener_proxy = str((transparent_vpn.get("listener_proxy") or "")).strip()
        transparent_listener_rule = str((transparent_vpn.get("listener_rule") or "")).strip()
        transparent_redir_port = config_details.get("redir_port")
        transparent_tproxy_port = config_details.get("tproxy_port")
        transparent_tcp_ready = bool(transparent_vpn.get("transparent_tcp_ready"))
        transparent_udp_ready = bool(transparent_vpn.get("transparent_udp_ready"))
        runtime_state = MihomoRuntimeState.RUNNING
        message = "Mihomo controller is reachable."
        if transparent_vpn and not transparent_listener_bind_valid:
            runtime_state = MihomoRuntimeState.DEGRADED
            message = (
                "Mihomo controller is reachable, but the transparent TPROXY listener bind is invalid."
            )
        elif transparent_vpn and (not isinstance(transparent_redir_port, int) or not isinstance(transparent_tproxy_port, int)):
            runtime_state = MihomoRuntimeState.DEGRADED
            message = (
                "Mihomo controller is reachable, but the transparent REDIR/TPROXY split contour is incomplete."
            )
        elif (
            transparent_vpn
            and transparent_listener_proxy not in {"", TRANSPARENT_TPROXY_PROXY_NAME}
            and transparent_listener_rule != TRANSPARENT_TPROXY_RULE_NAME
        ):
            runtime_state = MihomoRuntimeState.DEGRADED
            message = (
                "Mihomo controller is reachable, but the transparent listener target is invalid."
            )
        elif transparent_vpn and not transparent_tcp_ready:
            runtime_state = MihomoRuntimeState.DEGRADED
            message = (
                "Mihomo controller is reachable, but the transparent TCP REDIR contour is not ready."
            )
        elif transparent_vpn and not transparent_udp_ready:
            runtime_state = MihomoRuntimeState.DEGRADED
            message = (
                "Mihomo controller is reachable, but the transparent UDP TPROXY contour is not ready."
            )

        return MihomoHealth(
            runtime_state=runtime_state,
            active_server_id=active_server_id,
            message=message,
            details={
                "adapter": "http",
                "base_url": self.base_url,
                "version": version,
                "config": config_details,
                "transparent_runtime": connection_details,
                "selectors": {
                    "vpn_auto_exists": "vpn-auto" in proxies,
                    "vpn_auto_targets_count": len(self._selector_targets("vpn-auto")),
                    "vpn_auto_targets": sorted(self._selector_targets("vpn-auto")),
                    "vpn_auto_now": self._selected_proxy_id("vpn-auto"),
                    "vpn_global_exists": "vpn-global" in proxies,
                    "vpn_global_targets_count": len(self._selector_targets("vpn-global")),
                    "vpn_global_targets": sorted(self._selector_targets("vpn-global")),
                    "vpn_global_has_vpn_auto": "vpn-auto" in self._selector_targets("vpn-global"),
                    "vpn_global_now": self._selected_proxy_id("vpn-global"),
                },
            },
        )

    def _proxies(self) -> dict[str, Any]:
        data = self._get_json("/proxies")
        proxies = data.get("proxies", {})
        if not isinstance(proxies, dict):
            return {}
        return proxies

    def list_servers(self) -> list[MihomoServer]:
        servers: list[MihomoServer] = []

        for name, raw in self._proxies().items():
            if not isinstance(raw, dict):
                continue

            proxy_type = str(raw.get("type") or "")
            if name in BUILTIN_PROXY_NAMES or proxy_type in GROUP_PROXY_TYPES:
                continue

            servers.append(
                MihomoServer(
                    server_id=name,
                    server_name=name,
                    provider_name=raw.get("provider-name") or None,
                    raw=raw,
                )
            )

        return sorted(servers, key=lambda item: item.server_name)

    def _selector_targets(self, selector_name: str) -> set[str]:
        proxies = self._proxies()

        selector = proxies.get(selector_name)
        if not isinstance(selector, dict):
            return set()

        targets = selector.get("all") or []
        return {str(target) for target in targets if target}

    def _selected_proxy_id(self, selector_name: str) -> str | None:
        proxies = self._proxies()

        selector = proxies.get(selector_name)
        if not isinstance(selector, dict):
            return None

        selected = selector.get("now")
        if not selected or selected == "DIRECT":
            return None

        return str(selected)

    def get_active_server_id(self) -> str | None:
        vpn_global_selected = self._selected_proxy_id("vpn-global")
        if vpn_global_selected:
            if vpn_global_selected == "vpn-auto":
                vpn_auto_selected = self._selected_proxy_id("vpn-auto")
                if vpn_auto_selected:
                    return vpn_auto_selected
            else:
                return vpn_global_selected

        vpn_auto_selected = self._selected_proxy_id("vpn-auto")
        if vpn_auto_selected:
            return vpn_auto_selected

        global_selected = self._selected_proxy_id("GLOBAL")
        if global_selected:
            return global_selected

        return None

    def apply_server(self, server_id: str) -> MihomoApplyResult:
        return self.apply_server_to_selector("vpn-auto", server_id)

    def apply_server_to_selector(
        self,
        selector_name: str,
        server_id: str,
    ) -> MihomoApplyResult:
        encoded = quote(selector_name, safe="")
        selector_endpoint = f"/proxies/{encoded}"

        try:
            selector_targets = self._selector_targets(selector_name)
            active_before = self.get_active_server_id()

            if not selector_targets:
                return MihomoApplyResult(
                    ok=False,
                    message="Requested Mihomo selector is not present in runtime inventory.",
                    active_server_id=active_before,
                    error_code="MIHOMO_SELECTOR_NOT_FOUND",
                    error_message=f"Mihomo selector not found or has no targets: {selector_name}",
                    details={
                        "adapter": "http",
                        "selector": selector_name,
                        "selector_endpoint": selector_endpoint,
                        "requested_server_id": server_id,
                    },
                )

            if server_id not in selector_targets:
                return MihomoApplyResult(
                    ok=False,
                    message="Requested Mihomo target is not present in selector inventory.",
                    active_server_id=active_before,
                    error_code="MIHOMO_TARGET_NOT_FOUND",
                    error_message=f"Mihomo target not found in {selector_name}: {server_id}",
                    details={
                        "adapter": "http",
                        "selector": selector_name,
                        "selector_endpoint": selector_endpoint,
                        "requested_server_id": server_id,
                        "selector_targets_count": len(selector_targets),
                    },
                )

            response_body = self._put_json(selector_endpoint, {"name": server_id})
            active_after = self.get_active_server_id()
            selector_after = self._selected_proxy_id(selector_name)
        except (httpx.HTTPError, OSError, yaml.YAMLError) as exc:
            return MihomoApplyResult(
                ok=False,
                message="Mihomo server switching failed.",
                active_server_id=None,
                error_code="MIHOMO_APPLY_FAILED",
                error_message=str(exc),
                details={
                    "adapter": "http",
                    "selector": selector_name,
                    "selector_endpoint": selector_endpoint,
                    "requested_server_id": server_id,
                },
            )

        return MihomoApplyResult(
            ok=selector_after == server_id,
            message=(
                f"Mihomo {selector_name} selector switched."
                if selector_after == server_id
                else "Mihomo accepted selector update but selector state did not match request."
            ),
            active_server_id=active_after,
            error_code=None if selector_after == server_id else "MIHOMO_ACTIVE_SERVER_MISMATCH",
            error_message=None if selector_after == server_id else (
                f"Requested {server_id}, {selector_name} selected {selector_after}, active server is {active_after}."
            ),
            details={
                "adapter": "http",
                "selector": selector_name,
                "selector_endpoint": selector_endpoint,
                "requested_server_id": server_id,
                "active_before": active_before,
                "active_after": active_after,
                "selector_after": selector_after,
                "controller_response": response_body,
            },
        )

    def check_delay(
        self,
        server_id: str,
        *,
        test_url: str = "https://www.gstatic.com/generate_204",
        timeout_ms: int = 5000,
    ) -> MihomoDelayResult:
        try:
            known_servers = {server.server_id for server in self.list_servers()}

            if server_id not in known_servers:
                return MihomoDelayResult(
                    ok=False,
                    server_id=server_id,
                    test_url=test_url,
                    timeout_ms=timeout_ms,
                    error_code="MIHOMO_SERVER_NOT_FOUND",
                    error_message=f"Mihomo server not found: {server_id}",
                    details={
                        "adapter": "http",
                        "known_servers_count": len(known_servers),
                    },
                )

            response_body = self._delay_json(
                server_id,
                test_url=test_url,
                timeout_ms=timeout_ms,
            )
            delay = response_body.get("delay")

            if isinstance(delay, int):
                return MihomoDelayResult(
                    ok=True,
                    server_id=server_id,
                    delay_ms=delay,
                    test_url=test_url,
                    timeout_ms=timeout_ms,
                    details={
                        "adapter": "http",
                        "controller_response": response_body,
                    },
                )

            return MihomoDelayResult(
                ok=False,
                server_id=server_id,
                test_url=test_url,
                timeout_ms=timeout_ms,
                error_code="MIHOMO_DELAY_RESPONSE_INVALID",
                error_message="Mihomo delay response did not contain integer delay.",
                details={
                    "adapter": "http",
                    "controller_response": response_body,
                },
            )
        except (httpx.HTTPError, OSError, yaml.YAMLError) as exc:
            return MihomoDelayResult(
                ok=False,
                server_id=server_id,
                test_url=test_url,
                timeout_ms=timeout_ms,
                error_code="MIHOMO_DELAY_FAILED",
                error_message=str(exc),
                details={
                    "adapter": "http",
                },
            )


DEFAULT_MIHOMO_ADAPTER = MihomoHttpAdapter()
