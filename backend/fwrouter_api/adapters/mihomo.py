from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import quote

from concurrent.futures import ThreadPoolExecutor
import ipaddress
import json
import re
import socket
import threading
import httpx
import yaml
from fwrouter_api.services.mihomo_config_yaml import (
    MAX_MIHOMO_CONFIG_BYTES,
    MihomoYamlError,
    MihomoYamlInvalidError,
    parse_mihomo_yaml_mapping,
    read_mihomo_yaml_snapshot_text,
    read_mihomo_yaml_text,
)

DEFAULT_BASE_URL = "http://127.0.0.1:5200"
DEFAULT_CONFIG_PATH = Path("/var/lib/fwrouter-v2/generated/mihomo/config.yaml")
DEFAULT_CONTOURS_PATH = Path("/var/lib/fwrouter-v2/generated/mihomo/contours.json")
TRANSPARENT_REDIR_LISTENER_NAME = "fwrouter-redir"
TRANSPARENT_TPROXY_LISTENER_NAME = "fwrouter-tproxy"
TRANSPARENT_TPROXY_LISTENER_BIND = "0.0.0.0"
TRANSPARENT_TPROXY_PROXY_NAME = "vpn-global"
TRANSPARENT_TPROXY_RULE_NAME = "fwrouter-transparent"
MAX_CACHED_POLICY_CONFIG_BYTES = MAX_MIHOMO_CONFIG_BYTES
MAX_CACHED_POLICY_RULE_COUNT = 250_000
_SIMPLE_DOMAIN_UDP_GUARD = re.compile(
    r"\(\((?:Network|NETWORK),udp\)(?:,| && )\((Domain|DOMAIN|DomainSuffix|DOMAIN-SUFFIX|DomainKeyword|DOMAIN-KEYWORD),([^,()]+)\)\)"
)
BUILTIN_PROXY_NAMES = {
    "COMPATIBLE",
    "DIRECT",
    "GLOBAL",
    "PASS",
    "PASS-RULE",
    "REJECT",
    "REJECT-DROP",
    "vpn-auto",
}
NON_SERVER_PROXY_TARGETS = frozenset({"DIRECT", "REJECT", "REJECT-DROP", "PASS", "PASS-RULE", "COMPATIBLE"})
GROUP_PROXY_TYPES = {
    "Selector",
    "URLTest",
    "Fallback",
    "LoadBalance",
    "Relay",
}
_CONFIG_NON_LF_ASCII_BOUNDARIES = ("\r", "\v", "\f", "\x1c", "\x1d", "\x1e")
_CONFIG_LAZY_SCAN_PREFIX_CHARS = 32 * 1024


def _iter_config_lines(text: str) -> Iterator[str]:
    """Yield ``str.splitlines()``-equivalent lines for configured scans.

    Config scanners often stop near the header. ``splitlines()`` eagerly
    materializes every line first, so use ``str.find`` for large LF-only
    configs. Small and non-LF/Unicode text keeps the exact C implementation.
    Callers also retain that implementation when the first matching header is
    far into the document, where a full scan is expected.
    """
    if not text:
        return
    if (len(text) <= 32 * 1024 or not text.isascii()
            or any(boundary in text for boundary in _CONFIG_NON_LF_ASCII_BOUNDARIES)):
        yield from text.splitlines()
        return
    start = 0
    while True:
        boundary = text.find("\n", start)
        if boundary < 0:
            break
        yield text[start:boundary]
        start = boundary + 1
    if start < len(text):
        yield text[start:]


def _configured_scan_lines(text: str, marker: str) -> Iterator[str]:
    """Choose lazy iteration only when the first marker is a plausible header.

    A marker first seen inside a comment, nested value, or other line content
    cannot satisfy the scanners' column-zero header checks. Keep those cases
    on ``splitlines()``: traversing a large document lazily to a missing header
    is slower than the C implementation and saves no useful early-return work.
    """
    marker_offset = text.find(marker)
    if marker_offset < 0:
        return iter(())
    if (marker_offset > _CONFIG_LAZY_SCAN_PREFIX_CHARS
            or (marker_offset > 0 and text[marker_offset - 1] != "\n")):
        return iter(text.splitlines())
    return _iter_config_lines(text)


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

    def runtime_incarnation(self, *, timeout_seconds: float | None = None) -> str | None:
        from fwrouter_api.services.mihomo_runtime import get_mihomo_runtime_incarnation

        return (get_mihomo_runtime_incarnation() if timeout_seconds is None
                else get_mihomo_runtime_incarnation(timeout_seconds=timeout_seconds))

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

    def _delay_json_with_zero_readback(
        self,
        server_id: str,
        *,
        test_url: str,
        timeout_ms: int,
    ) -> dict[str, Any]:
        """Confirm Mihomo's ambiguous generic 503 only from a fresh same-URL zero sample."""
        probe_started_at = datetime.now(timezone.utc)
        try:
            return self._delay_json(server_id, test_url=test_url, timeout_ms=timeout_ms)
        except httpx.HTTPStatusError as exc:
            if not self._is_generic_zero_delay_error(exc):
                raise

            try:
                state = self._get_json(
                    f"/proxies/{quote(server_id, safe='')}",
                    timeout_seconds=1.5,
                )
            except (httpx.HTTPError, OSError, ValueError, yaml.YAMLError):
                raise exc
            observed_end = datetime.now(timezone.utc)
            if not self._has_fresh_zero_delay_for_url(
                state,
                test_url=test_url,
                probe_started_at=probe_started_at,
                observed_end=observed_end,
            ):
                raise exc
            return {
                "delay": 0,
                "native_zero_readback": {
                    "evidence_source": "runtime_native_url_history",
                    "controller_http_status": 503,
                    "confirmed_at": observed_end.isoformat(),
                },
            }

    @staticmethod
    def _is_generic_zero_delay_error(exc: httpx.HTTPStatusError) -> bool:
        response = exc.response
        if response.status_code != 503:
            return False
        try:
            body = response.json()
        except (ValueError, json.JSONDecodeError):
            return False
        return isinstance(body, dict) and body.get("message") == "An error occurred in the delay test"

    @staticmethod
    def _has_fresh_zero_delay_for_url(
        proxy: dict[str, Any],
        *,
        test_url: str,
        probe_started_at: datetime,
        observed_end: datetime,
    ) -> bool:
        extra = proxy.get("extra")
        sample = extra.get(test_url) if isinstance(extra, dict) else None
        if not isinstance(sample, dict) or sample.get("alive") is not True:
            return False
        history = sample.get("history")
        latest = history[-1] if isinstance(history, list) and history else None
        if not isinstance(latest, dict) or type(latest.get("delay")) is not int or latest["delay"] != 0:
            return False
        raw_time = latest.get("time")
        if not isinstance(raw_time, str) or not raw_time.strip():
            return False
        try:
            checked_at = datetime.fromisoformat(raw_time.strip().replace("Z", "+00:00"))
        except ValueError:
            return False
        if checked_at.tzinfo is None or checked_at.utcoffset() is None:
            return False
        checked_at = checked_at.astimezone(timezone.utc)
        return probe_started_at <= checked_at <= observed_end

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

    def get_proxy_state(self, proxy_name: str) -> dict[str, Any]:
        raise NotImplementedError

    def get_logical_group_state(self, logical_runtime_target: str) -> dict[str, Any]:
        raise NotImplementedError

    def get_recovery_selection_snapshot(self, logical_runtime_target: str, *, timeout_seconds: float = 1.5) -> dict[str, Any]:
        raise NotImplementedError

    # Provider-neutral health/recovery boundary.  Concrete adapters translate
    # these operations to their runtime-native API.
    def get_active_member_state(self, logical_runtime_target: str) -> dict[str, Any]:
        return self.get_logical_group_state(logical_runtime_target)

    def get_member_health(
        self, logical_runtime_target: str, member_runtime_identity: str
    ) -> dict[str, Any]:
        snapshot = self.get_logical_group_state(logical_runtime_target)
        member = next(
            (
                item
                for item in snapshot.get("members", [])
                if isinstance(item, dict)
                and str(item.get("runtime_identity") or "") == member_runtime_identity
            ),
            {"runtime_identity": member_runtime_identity, "status": "unknown"},
        )
        result = dict(member)
        result.setdefault("source", snapshot.get("evidence_source") or "runtime_native")
        result.setdefault(
            "freshness",
            "fresh" if result.get("status") in {"healthy", "failed"} and result.get("checked_at") else "unknown",
        )
        return result

    def get_member_latency(
        self, logical_runtime_target: str, member_runtime_identity: str
    ) -> int | None:
        value = self.get_member_health(logical_runtime_target, member_runtime_identity).get("latency_ms")
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    def request_member_reselection(
        self, logical_runtime_target: str, *, exclude_member_runtime_identity: str | None = None
    ) -> dict[str, Any]:
        raise NotImplementedError

    def request_group_health_refresh(
        self,
        logical_runtime_target: str,
        *,
        test_url: str = "https://www.gstatic.com/generate_204",
        timeout_ms: int = 5000,
    ) -> dict[str, Any]:
        return self.probe_logical_group(logical_runtime_target, test_url=test_url, timeout_ms=timeout_ms)

    def get_logical_groups_state(
        self,
        logical_runtime_targets: list[str],
    ) -> list[dict[str, Any]]:
        raise NotImplementedError

    def probe_logical_group(
        self,
        logical_runtime_target: str,
        *,
        test_url: str = "https://www.gstatic.com/generate_204",
        timeout_ms: int = 5000,
    ) -> dict[str, Any]:
        raise NotImplementedError

    def probe_logical_member(
        self,
        logical_runtime_target: str,
        member_runtime_identity: str,
        *,
        test_url: str = "https://www.gstatic.com/generate_204",
        timeout_ms: int = 5000,
    ) -> dict[str, Any]:
        raise NotImplementedError

    def probe_logical_groups(
        self,
        logical_runtime_targets: list[str],
        *,
        test_url: str = "https://www.gstatic.com/generate_204",
        timeout_ms: int = 5000,
    ) -> list[dict[str, Any]]:
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
        self._secret_cache_mtime_ns: int | None = None
        self._secret_cache_value: str = ""
        self._policy_config_cache_lock = threading.Lock()
        self._policy_config_cache: tuple[str, type[Any], dict[str, Any], dict[str, Any]] | None = None

    def _config_text(self) -> str:
        """Read config under the strict path-stability contract used by health proof."""
        try:
            self.config_path.lstat()
        except FileNotFoundError:
            return ""
        except OSError:
            raise MihomoYamlInvalidError("file_unavailable") from None
        return read_mihomo_yaml_text(self.config_path)

    def _configured_metadata_text(self) -> str:
        """Read one bounded complete file snapshot for configured metadata scans.

        This observation may describe the old inode during an atomic publish;
        callers must not treat it as current native or admission proof.
        """
        try:
            self.config_path.lstat()
        except FileNotFoundError:
            return ""
        except OSError:
            raise MihomoYamlInvalidError("file_unavailable") from None
        return read_mihomo_yaml_snapshot_text(self.config_path)

    def _config_top_level_scalar(self, key: str, *, text: str | None = None) -> str | None:
        source = text if text is not None else self._configured_metadata_text()
        prefix = f"{key}:"
        lines = _configured_scan_lines(source, prefix)
        for line in lines:
            if not line or line.startswith((" ", "\t", "-")):
                continue
            if line.startswith(prefix):
                return line[len(prefix) :].strip()
        return None

    def _scan_listener_ports(self, *, text: str | None = None) -> dict[str, int | None]:
        source = text if text is not None else self._configured_metadata_text()
        lines = _configured_scan_lines(source, "listeners:")
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
        for raw_line in lines:
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
        source = text if text is not None else self._configured_metadata_text()
        lines = _configured_scan_lines(source, "listeners:")
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

        for raw_line in lines:
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
        source = text if text is not None else self._configured_metadata_text()
        lines = _configured_scan_lines(source, "tun:")
        in_tun = False
        for raw_line in lines:
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
        try:
            mtime_ns = self.config_path.stat().st_mtime_ns
        except OSError:
            self._secret_cache_mtime_ns = None
            self._secret_cache_value = ""
            return ""
        if self._secret_cache_mtime_ns == mtime_ns:
            return self._secret_cache_value
        secret = str(self._config_top_level_scalar("secret") or "")
        self._secret_cache_mtime_ns = mtime_ns
        self._secret_cache_value = secret
        return secret

    def _headers(self) -> dict[str, str]:
        secret = self._secret()
        if not secret:
            return {}
        return {"Authorization": f"Bearer {secret}"}

    def _config_runtime_details(self, *, config_text: str | None = None) -> dict[str, Any]:
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

        config_text = self._configured_metadata_text() if config_text is None else config_text
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

    @staticmethod
    def _parse_vpn_udp_policy_config(config_text: str) -> dict[str, Any]:
        """Parse policy config with the same safe loader and fail-closed behavior."""
        try:
            return parse_mihomo_yaml_mapping(config_text.encode("utf-8"))
        except UnicodeError:
            raise MihomoYamlInvalidError("invalid_utf8") from None

    def _parsed_vpn_udp_policy_config(self, config_text: str) -> dict[str, Any]:
        loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
        with self._policy_config_cache_lock:
            cached = self._policy_config_cache
            if cached is not None and cached[0] == config_text and cached[1] is loader:
                return cached[2]

            config = self._parse_vpn_udp_policy_config(config_text)
            if len(config_text.encode("utf-8")) <= MAX_CACHED_POLICY_CONFIG_BYTES:
                self._policy_config_cache = (config_text, loader, config, {})
            else:
                self._policy_config_cache = None
            return config

    def _readback_with_config_analysis(
        self, config_text: str, proxies: dict[str, Any],
        native_rules: list[dict[str, Any]] | None, parsed_config: dict[str, Any],
    ) -> dict[str, Any]:
        """Reuse only immutable expected-config analysis, never native observations."""
        from fwrouter_api.services.mihomo_config_rules import _mandatory_vpn_udp_guard
        validator_identity = (type(self)._vpn_udp_policy_readback, _mandatory_vpn_udp_guard)
        with self._policy_config_cache_lock:
            cached = self._policy_config_cache
            configured_rules = parsed_config.get("rules")
            if (cached is not None and cached[0] == config_text and cached[2] is parsed_config
                    and isinstance(configured_rules, list) and len(configured_rules) <= MAX_CACHED_POLICY_RULE_COUNT):
                analysis = cached[3]
                cacheable_analysis = True
                if analysis.get("validator_identity") != validator_identity:
                    # YAML scalar aliases can expand a small document into a
                    # large expected-rule vector. Bound expanded UTF-8 input,
                    # not just file bytes/count, before retaining analysis.
                    expanded_bytes = 0
                    for rule in configured_rules:
                        if not isinstance(rule, str):
                            cacheable_analysis = False
                            break
                        try:
                            expanded_bytes += len(rule.encode("utf-8"))
                        except UnicodeEncodeError:
                            cacheable_analysis = False
                            break
                        if expanded_bytes > MAX_CACHED_POLICY_CONFIG_BYTES:
                            cacheable_analysis = False
                            break
                    if cacheable_analysis:
                        analysis.clear()
                        analysis["validator_identity"] = validator_identity
                if cacheable_analysis:
                    return self._vpn_udp_policy_readback(
                        config_text, proxies, native_rules, _parsed_config=parsed_config,
                        _configuration_analysis=analysis,
                    )
        return self._vpn_udp_policy_readback(
            config_text, proxies, native_rules, _parsed_config=parsed_config,
        )

    @staticmethod
    def _vpn_udp_policy_readback(
        config_text: str,
        proxies: dict[str, Any],
        native_rules: list[dict[str, Any]] | None = None,
        *,
        _parsed_config: dict[str, Any] | None = None,
        _configuration_analysis: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Prove the active config and controller selectors are fail-closed for VPN UDP."""
        # `health()` passes only an internal parse of the exact bytes it just
        # read. Direct callers retain the original parse-on-every-call behavior.
        if _parsed_config is not None:
            config = _parsed_config
        else:
            try:
                config = MihomoHttpAdapter._parse_vpn_udp_policy_config(config_text)
            except MihomoYamlError:
                config = {}
        configured_groups = [
            item for item in (config.get("proxy-groups") or [])
            if isinstance(item, dict) and str(item.get("name") or "").strip()
        ]
        groups = {
            str(item.get("name") or ""): item
            for item in configured_groups
        }
        owned_names = {
            name for name in groups
            if name in {"vpn-auto", "vpn-global"} or name.startswith("fwrouter-subject-")
        }
        group_readback_ok = bool({"vpn-auto", "vpn-global"} <= owned_names)
        owned_name_counts: dict[str, int] = {}
        for item in configured_groups:
            name = str(item.get("name") or "")
            if name in {"vpn-auto", "vpn-global"} or name.startswith("fwrouter-subject-"):
                owned_name_counts[name] = owned_name_counts.get(name, 0) + 1
        if any(count != 1 for count in owned_name_counts.values()):
            group_readback_ok = False
        group_names_match: dict[str, bool] = {}
        for name in sorted(owned_names):
            configured = groups[name].get("proxies")
            configured = [str(value) for value in configured] if isinstance(configured, list) else []
            actual = proxies.get(name) if isinstance(proxies.get(name), dict) else {}
            native = actual.get("all") if isinstance(actual.get("all"), list) else []
            selected = str(actual.get("now") or "").strip()
            fixed_global_default = bool(
                name == "vpn-global"
                and len(configured) == 2
                and configured[0] == "REJECT"
                and configured[1] not in NON_SERVER_PROXY_TARGETS
            )
            group_ok = bool(
                            configured
                            and (configured[-1] == "REJECT" or fixed_global_default)
                            and "DIRECT" not in configured
                            and set(configured) == {str(value) for value in native}
                            and selected in configured
                            and selected not in {"DIRECT", "PASS", "PASS-RULE", "COMPATIBLE"}
                        )
            group_names_match[name] = group_ok
            group_readback_ok = group_readback_ok and group_ok

        from fwrouter_api.services.mihomo_config_rules import _mandatory_vpn_udp_guard

        guarded_sequences: dict[str, bool] = {}
        sub_rules = config.get("sub-rules") if isinstance(config.get("sub-rules"), dict) else {}
        sequences = {
            "rules": config.get("rules"),
            "fwrouter-transparent": sub_rules.get("fwrouter-transparent"),
            "fwrouter-full-vpn": sub_rules.get("fwrouter-full-vpn"),
        }
        prepared_guards = (_configuration_analysis or {}).get("guarded_sequences")
        if isinstance(prepared_guards, dict):
            guarded_sequences = dict(prepared_guards)
        else:
            for name, rules in sequences.items():
                valid = isinstance(rules, list) and bool(rules)
                if valid:
                    for index, raw_rule in enumerate(rules):
                        rule = str(raw_rule)
                        guard = _mandatory_vpn_udp_guard(rule)
                        if guard and (index + 1 >= len(rules) or str(rules[index + 1]) != guard):
                            valid = False
                            break
                if name == "fwrouter-full-vpn":
                    valid = bool(valid and len(rules) >= 2 and str(rules[-2]) == "MATCH,vpn-global"
                                 and str(rules[-1]) == "NETWORK,udp,REJECT")
                guarded_sequences[name] = bool(valid)
            if _configuration_analysis is not None:
                _configuration_analysis["guarded_sequences"] = dict(guarded_sequences)
        rules_readback_ok = all(guarded_sequences.values())
        native_top_level_rules_ok: bool | None = None
        if native_rules is not None:
            # Pinned to Mihomo v1.19.31 API rule.Type.String values and
            # AND.String payloads from rules/logic/logic.go and constant/rule.go.
            native_type_to_config = {
                "Domain": "DOMAIN", "DomainSuffix": "DOMAIN-SUFFIX", "DomainKeyword": "DOMAIN-KEYWORD",
                "GeoIP": "GEOIP", "IPCIDR": "IP-CIDR", "IPCIDR6": "IP-CIDR6",
                "SrcIPCIDR": "SRC-IP-CIDR", "SrcPort": "SRC-PORT", "DstPort": "DST-PORT",
                "ProcessName": "PROCESS-NAME", "Network": "NETWORK", "AND": "AND", "Match": "MATCH",
            }
            config_type_to_native = {
                "DOMAIN": "Domain", "DOMAIN-SUFFIX": "DomainSuffix", "DOMAIN-KEYWORD": "DomainKeyword",
                "GEOIP": "GeoIP", "IP-CIDR": "IPCIDR", "IP-CIDR6": "IPCIDR",
                "SRC-IP-CIDR": "SrcIPCIDR", "SRC-PORT": "SrcPort", "DST-PORT": "DstPort",
                "PROCESS-NAME": "ProcessName", "NETWORK": "Network", "AND": "AND", "MATCH": "Match",
            }
            native_condition_type_to_config = {**native_type_to_config, "SrcPort": "SRC-PORT", "DstPort": "DST-PORT"}

            def canonicalize_nested_cidrs(payload: str) -> str | None:
                if "IP-CIDR" not in payload:
                    return payload  # The exact CIDR regex cannot match this input.
                def replace(match: re.Match[str]) -> str:
                    rule_type, value, raw_params = match.groups()
                    params = [item for item in raw_params.split(",") if item]
                    if len(params) != len(set(params)) or any(
                        item not in {"no-resolve", "src"} for item in params
                    ):
                        raise ValueError("invalid CIDR rule parameter")
                    try:
                        network = ipaddress.ip_network(value, strict=False)
                    except ValueError:
                        raise ValueError("invalid CIDR in native compound rule")
                    if "src" in params:
                        rule_type = "SRC-IP-CIDR"
                    elif rule_type == "IP-CIDR" and network.version == 6:
                        rule_type = "IP-CIDR6"
                    elif rule_type == "IP-CIDR6" and network.version != 6:
                        raise ValueError("IPv6 CIDR condition contains an IPv4 network")
                    return f"{rule_type},{network}"

                try:
                    return re.sub(
                        r"(?<=[(,])((?:IP-CIDR6|IP-CIDR|SRC-IP-CIDR)),([^,()]+)((?:,(?:no-resolve|src))*)(?=[),])",
                        replace,
                        payload,
                    )
                except ValueError:
                    return None

            def normalize_native_rule_type(rule_type: str, payload: str) -> tuple[str, str] | None:
                config_type = native_type_to_config.get(rule_type)
                if config_type is None:
                    return None
                if config_type == "IP-CIDR":
                    try:
                        if ipaddress.ip_network(payload, strict=False).version == 6:
                            config_type = "IP-CIDR6"
                    except ValueError:
                        return None
                if config_type == "AND":
                    simple_guard = _SIMPLE_DOMAIN_UDP_GUARD.fullmatch(payload)
                    if simple_guard is not None:
                        condition, value = simple_guard.groups()
                        condition = native_condition_type_to_config.get(condition, condition)
                        return config_type, f"((NETWORK,udp),({condition},{value}))"
                    payload = payload.replace(" && ", ",")
                    payload = re.sub(
                        r"(?<=\(|,)([A-Za-z][A-Za-z0-9]*)(?=,)",
                        lambda match: native_condition_type_to_config.get(match.group(1), match.group(1)),
                        payload,
                    )
                    canonical_nested = canonicalize_nested_cidrs(payload)
                    if canonical_nested is None:
                        return None
                    payload = canonical_nested
                if config_type in {"IP-CIDR", "IP-CIDR6", "SRC-IP-CIDR"}:
                    try:
                        payload = str(ipaddress.ip_network(payload, strict=False))
                    except ValueError:
                        return None
                return config_type, payload

            def normalize_config_rule(rule: str) -> tuple[str, str, str] | None:
                parts = [part.strip() for part in rule.split(",")]
                if len(parts) < 2:
                    return None
                rule_type = parts[0].upper()
                if rule_type == "MATCH":
                    if len(parts) != 2 or not parts[1]:
                        return None
                    target = parts[1]
                    payload = ""
                elif rule_type == "AND":
                    if len(parts) < 3 or parts[-1].lower() in {"no-resolve", "src"}:
                        return None
                    target = parts[-1]
                    payload = ",".join(parts[1:-1])
                else:
                    params = [value.lower() for value in parts[3:]]
                    if (
                        len(parts) < 3
                        or any(value not in {"no-resolve", "src"} for value in params)
                        or len(params) != len(set(params))
                        or ("src" in params and rule_type not in {"IP-CIDR", "IP-CIDR6"})
                    ):
                        return None
                    target = parts[2]
                    payload = parts[1]
                    if "src" in params:
                        rule_type = "SRC-IP-CIDR"
                native_type = config_type_to_native.get(rule_type)
                if native_type is None:
                    return None
                canonical = normalize_native_rule_type(native_type, payload)
                return (*canonical, target) if canonical is not None else None

            has_native_indexes = any(isinstance(item, dict) and "index" in item for item in native_rules)
            configured_rules = sequences["rules"]
            prepared_config_rules = (_configuration_analysis or {}).get("normalized_config_rules")
            if (prepared_config_rules is None and _configuration_analysis is not None
                    and isinstance(configured_rules, list)):
                prepared_config_rules = [normalize_config_rule(str(rule)) for rule in configured_rules]
                _configuration_analysis["normalized_config_rules"] = prepared_config_rules
            native_top_level_rules_ok = isinstance(configured_rules, list) and len(native_rules) == len(configured_rules)
            for position, item in enumerate(native_rules):
                if not native_top_level_rules_ok:
                    break
                if not isinstance(item, dict):
                    native_top_level_rules_ok = False
                    break
                native_rule_type = str(item.get("type") or "").strip()
                payload = str(item.get("payload") or "").strip()
                target = str(item.get("proxy") or "").strip()
                extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
                if native_rule_type not in native_type_to_config or not target:
                    native_top_level_rules_ok = False
                    break
                if "disabled" in extra and type(extra.get("disabled")) is not bool:
                    native_top_level_rules_ok = False
                    break
                if extra.get("disabled") is True:
                    native_top_level_rules_ok = False
                    break
                if has_native_indexes:
                    index = item.get("index")
                    if type(index) is not int or index != position:
                        native_top_level_rules_ok = False
                        break
                canonical = normalize_native_rule_type(native_rule_type, payload)
                configured = (prepared_config_rules[position] if prepared_config_rules is not None
                              else normalize_config_rule(str(configured_rules[position])))
                if canonical is None or configured is None or (*canonical, target) != configured:
                    native_top_level_rules_ok = False
                    break
        return {
            "policy_installed": bool(group_readback_ok and rules_readback_ok),
            "selector_membership_matches": group_readback_ok,
            "guarded_rules_match": rules_readback_ok,
            "native_top_level_rules_match": native_top_level_rules_ok,
            "selector_groups": group_names_match,
            "guarded_sequences": guarded_sequences,
        }

    def _get_json(self, path: str, *, timeout_seconds: float | None = None) -> dict[str, Any]:
        timeout = self.timeout_seconds if timeout_seconds is None else max(0.1, float(timeout_seconds))
        with httpx.Client(timeout=timeout, trust_env=False) as client:
            # Avoid Response.json(): its buffered content can stay reachable through
            # httpx's response/stream cycle until cyclic GC runs on large payloads.
            with client.stream(
                "GET",
                f"{self.base_url}{path}",
                headers=self._headers(),
            ) as response:
                response.raise_for_status()
                data = json.loads(b"".join(response.iter_bytes()))

        if not isinstance(data, dict):
            return {"value": data}

        return data

    def controller_ready(self, *, timeout_seconds: float | None = None) -> dict[str, Any]:
        """Check controller availability without reading or proving runtime policy."""
        version = self._get_json("/version", timeout_seconds=timeout_seconds)
        value = version.get("version")
        if not isinstance(value, str) or not value.strip():
            raise ValueError("Mihomo controller returned an invalid version response.")
        return {"ok": True, "version": value.strip()}

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
        try:
            config_text = self._config_text()
            parsed_policy_config = self._parsed_vpn_udp_policy_config(config_text)
            config_details = self._config_runtime_details(config_text=config_text)
        except MihomoYamlError as exc:
            return MihomoHealth(
                runtime_state=MihomoRuntimeState.DEGRADED,
                active_server_id=None,
                message="Mihomo configuration is unavailable or cannot be safely verified.",
                details={
                    "adapter": "http",
                    "base_url": self.base_url,
                    "error": str(exc),
                    "config": {"readable": False},
                    "selectors": {},
                },
            )
        try:
            version = self._get_json("/version")
            proxies = self._proxies()
            try:
                native_rules_response = self._get_json("/rules")
                native_rules_value = native_rules_response.get("rules")
                native_rules = native_rules_value if isinstance(native_rules_value, list) else None
            except (httpx.HTTPError, OSError, ValueError):
                native_rules = None
            udp_policy = self._readback_with_config_analysis(
                config_text, proxies, native_rules, parsed_policy_config,
            )
            udp_policy["wiring_proven"] = udp_policy["policy_installed"]
            try:
                from fwrouter_api.services.mihomo_reconcile_fingerprint import mihomo_udp_policy_receipt_matches

                udp_policy["reconcile_receipt_matches"] = mihomo_udp_policy_receipt_matches()
            except Exception:
                udp_policy["reconcile_receipt_matches"] = False
            udp_policy["policy_installed"] = bool(
                udp_policy["wiring_proven"]
                and udp_policy["native_top_level_rules_match"] is True
                and udp_policy["reconcile_receipt_matches"]
            )
            active_server_id = self._active_server_id_from_proxies(proxies)
            try:
                connection_details = self._transparent_session_observation(self._get_json("/connections"))
            except (httpx.HTTPError, OSError, yaml.YAMLError):
                connection_details = self._transparent_session_observation(None)
        except (httpx.HTTPError, OSError, yaml.YAMLError, MihomoYamlError) as exc:
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
                "vpn_udp_policy": udp_policy,
                "transparent_runtime": connection_details,
                "selectors": {
                    "vpn_auto_exists": "vpn-auto" in proxies,
                    "vpn_auto_targets_count": len(self._selector_targets("vpn-auto", proxies=proxies)),
                    "vpn_auto_targets": sorted(self._selector_targets("vpn-auto", proxies=proxies)),
                    "vpn_auto_now": self._selected_proxy_id("vpn-auto", proxies=proxies),
                    "vpn_auto_selected_target": self._selected_target("vpn-auto", proxies=proxies),
                    "vpn_global_exists": "vpn-global" in proxies,
                    "vpn_global_targets_count": len(self._selector_targets("vpn-global", proxies=proxies)),
                    "vpn_global_targets": sorted(self._selector_targets("vpn-global", proxies=proxies)),
                    "vpn_global_has_vpn_auto": "vpn-auto" in self._selector_targets("vpn-global", proxies=proxies),
                    "vpn_global_now": self._selected_proxy_id("vpn-global", proxies=proxies),
                    "vpn_global_selected_target": self._selected_target("vpn-global", proxies=proxies),
                    "subject_selector_targets": {
                        name: sorted(self._selector_targets(name, proxies=proxies))
                        for name in sorted(proxies)
                        if str(name).startswith("fwrouter-subject-")
                    },
                    "subject_selector_selected_targets": {
                        name: self._selected_target(name, proxies=proxies)
                        for name in sorted(proxies)
                        if str(name).startswith("fwrouter-subject-")
                    },
                },
            },
        )

    def _proxies(self, *, timeout_seconds: float | None = None) -> dict[str, Any]:
        data = (self._get_json("/proxies") if timeout_seconds is None
                else self._get_json("/proxies", timeout_seconds=timeout_seconds))
        proxies = data.get("proxies", {})
        if not isinstance(proxies, dict):
            return {}
        return proxies

    def _proxy(self, proxy_name: str) -> dict[str, Any]:
        encoded = quote(proxy_name, safe="")
        data = self._get_json(f"/proxies/{encoded}")
        return data if isinstance(data, dict) else {}

    def get_proxy_state(self, proxy_name: str) -> dict[str, Any]:
        return self._proxy(proxy_name)

    @staticmethod
    def _history_observation(proxy: dict[str, Any]) -> tuple[int | None, str | None]:
        history = proxy.get("history") if isinstance(proxy.get("history"), list) else []
        latest = history[-1] if history and isinstance(history[-1], dict) else {}
        delay = latest.get("delay")
        raw_checked_at = latest.get("time")
        if type(delay) is not int or not 0 <= delay <= 65535 or not isinstance(raw_checked_at, str):
            return None, None
        checked_at = raw_checked_at.strip()
        if not checked_at:
            return None, None
        try:
            parsed = datetime.fromisoformat(checked_at.replace("Z", "+00:00"))
        except ValueError:
            return None, None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None, None
        return delay, checked_at

    def _logical_group_state_from_proxies(
        self,
        logical_runtime_target: str,
        proxies: dict[str, Any],
    ) -> dict[str, Any]:
        group = proxies.get(logical_runtime_target)
        if not isinstance(group, dict):
            return {
                "ok": False,
                "logical_runtime_target": logical_runtime_target,
                "effective_member_runtime_identity": None,
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "evidence_source": "runtime_native",
                "members": [],
                "error_code": "RUNTIME_LOGICAL_TARGET_NOT_FOUND",
                "error_message": "Logical runtime target is not present in runtime inventory.",
            }
        member_names = group.get("all") if isinstance(group.get("all"), list) else [logical_runtime_target]
        effective = str(group.get("now") or logical_runtime_target).strip() or None
        members: list[dict[str, Any]] = []
        for member_name in member_names:
            runtime_identity = str(member_name or "").strip()
            member = proxies.get(runtime_identity)
            if not runtime_identity or not isinstance(member, dict):
                continue
            latency_ms, checked_at = self._history_observation(member)
            alive = member.get("alive")
            if checked_at is None or latency_ms is None or type(alive) is not bool:
                status = "unknown"
            elif alive is False:
                status = "failed"
            else:
                status = "healthy"
            members.append(
                {
                    "runtime_identity": runtime_identity,
                    "status": status,
                    "latency_ms": latency_ms,
                    "checked_at": checked_at,
                    "error_code": "RUNTIME_MEMBER_UNAVAILABLE" if status == "failed" else None,
                    "error_message": None,
                }
            )
        return {
            "ok": True,
            "logical_runtime_target": logical_runtime_target,
            "effective_member_runtime_identity": effective,
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "evidence_source": "runtime_native",
            "members": members,
            "error_code": None,
            "error_message": None,
        }

    def get_logical_group_state(self, logical_runtime_target: str) -> dict[str, Any]:
        return self._logical_group_state_from_proxies(
            logical_runtime_target,
            self._proxies(),
        )

    def get_recovery_selection_snapshot(self, logical_runtime_target: str, *, timeout_seconds: float = 1.5) -> dict[str, Any]:
        """Read selector target and effective member using one bounded Mihomo API request."""
        proxies = self._proxies(timeout_seconds=timeout_seconds)
        global_target = self._selected_proxy_id("vpn-global", proxies=proxies)
        active_target = (self._selected_proxy_id("vpn-auto", proxies=proxies)
                         if global_target == "vpn-auto" else global_target)
        group = self._logical_group_state_from_proxies(logical_runtime_target, proxies)
        return {"active_target": active_target,
                "effective_member_runtime_identity": group.get("effective_member_runtime_identity"),
                "ok": bool(group.get("ok"))}

    def get_active_member_state(self, logical_runtime_target: str) -> dict[str, Any]:
        return self.get_logical_group_state(logical_runtime_target)

    def request_member_reselection(
        self, logical_runtime_target: str, *, exclude_member_runtime_identity: str | None = None
    ) -> dict[str, Any]:
        snapshot = self.get_logical_group_state(logical_runtime_target)
        members = [
            str(item.get("runtime_identity") or "")
            for item in snapshot.get("members", [])
            if isinstance(item, dict) and item.get("runtime_identity")
        ]
        current = str(snapshot.get("effective_member_runtime_identity") or "")
        excluded = {str(exclude_member_runtime_identity or ""), current}
        candidate = next((item for item in members if item not in excluded), None)
        if candidate is None:
            return {"ok": False, "logical_runtime_target": logical_runtime_target, "error_code": "NO_ALTERNATE_MEMBER"}
        try:
            self._put_json(f"/proxies/{quote(logical_runtime_target, safe='')}", {"name": candidate})
            refreshed = self.get_logical_group_state(logical_runtime_target)
            return {
                "ok": True,
                "logical_runtime_target": logical_runtime_target,
                "previous_member_runtime_identity": current or None,
                "active_member_runtime_identity": refreshed.get("effective_member_runtime_identity") or candidate,
                "checked_at": refreshed.get("observed_at"),
                "source": "runtime_native",
            }
        except (httpx.HTTPError, OSError, ValueError, yaml.YAMLError) as exc:
            return {"ok": False, "logical_runtime_target": logical_runtime_target, "error_code": "MEMBER_RESELECTION_FAILED", "error_message": str(exc)}

    def get_logical_groups_state(
        self,
        logical_runtime_targets: list[str],
    ) -> list[dict[str, Any]]:
        proxies = self._proxies()
        return [
            self._logical_group_state_from_proxies(target, proxies)
            for target in logical_runtime_targets
        ]

    @staticmethod
    def _apply_probe_delays(
        snapshot: dict[str, Any],
        delays: dict[str, Any],
        *,
        checked_at: str,
    ) -> dict[str, Any]:
        updated = dict(snapshot)
        members: list[dict[str, Any]] = []
        for member in snapshot.get("members") or []:
            item = dict(member)
            delay = delays.get(str(item.get("runtime_identity") or ""))
            if type(delay) is int and 0 <= delay <= 65535:
                status = "healthy"
                latency_ms = delay
                runtime_timestamp_matches = bool(
                    item.get("checked_at")
                    and item.get("status") == status
                    and item.get("latency_ms") == latency_ms
                )
                item["status"] = status
                item["latency_ms"] = latency_ms
                if not runtime_timestamp_matches:
                    item["checked_at"] = checked_at
                item["error_code"] = None
                item["error_message"] = None
            members.append(item)
        updated["members"] = members
        updated["observed_at"] = checked_at
        return updated

    def probe_logical_group(
        self,
        logical_runtime_target: str,
        *,
        test_url: str = "https://www.gstatic.com/generate_204",
        timeout_ms: int = 5000,
    ) -> dict[str, Any]:
        try:
            encoded = quote(logical_runtime_target, safe="")
            request_timeout = max(self.timeout_seconds, timeout_ms / 1000 + 2)
            with httpx.Client(timeout=request_timeout, trust_env=False) as client:
                response = client.get(
                    f"{self.base_url}/group/{encoded}/delay",
                    headers=self._headers(),
                    params={"timeout": timeout_ms, "url": test_url},
                )
                response.raise_for_status()
                delays = response.json()
            checked_at = datetime.now(timezone.utc).isoformat()
            snapshot = self.get_logical_group_state(logical_runtime_target)
            return self._apply_probe_delays(
                snapshot,
                delays if isinstance(delays, dict) else {},
                checked_at=checked_at,
            )
        except (httpx.HTTPError, OSError, ValueError, yaml.YAMLError) as exc:
            return {
                "ok": False,
                "logical_runtime_target": logical_runtime_target,
                "effective_member_runtime_identity": None,
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "evidence_source": "runtime_native",
                "members": [],
                "error_code": "RUNTIME_LOGICAL_GROUP_PROBE_FAILED",
                "error_message": str(exc),
            }

    def probe_logical_member(
        self,
        logical_runtime_target: str,
        member_runtime_identity: str,
        *,
        test_url: str = "https://www.gstatic.com/generate_204",
        timeout_ms: int = 5000,
    ) -> dict[str, Any]:
        try:
            response = self._delay_json_with_zero_readback(
                member_runtime_identity,
                test_url=test_url,
                timeout_ms=timeout_ms,
            )
            checked_at = datetime.now(timezone.utc).isoformat()
            snapshot = self.get_logical_group_state(logical_runtime_target)
            delay = response.get("delay")
            return self._apply_probe_delays(
                snapshot,
                {member_runtime_identity: delay},
                checked_at=checked_at,
            ) | (
                {"probe_evidence": response["native_zero_readback"]}
                if isinstance(response.get("native_zero_readback"), dict)
                else {}
            )
        except (httpx.HTTPError, OSError, ValueError, yaml.YAMLError) as exc:
            return {
                "ok": False,
                "logical_runtime_target": logical_runtime_target,
                "effective_member_runtime_identity": None,
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "evidence_source": "runtime_native",
                "members": [],
                "error_code": "RUNTIME_LOGICAL_MEMBER_PROBE_FAILED",
                "error_message": str(exc),
            }

    def probe_logical_groups(
        self,
        logical_runtime_targets: list[str],
        *,
        test_url: str = "https://www.gstatic.com/generate_204",
        timeout_ms: int = 5000,
    ) -> list[dict[str, Any]]:
        def probe_one(target: str) -> tuple[str, dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None]:
            try:
                encoded = quote(target, safe="")
                request_timeout = max(self.timeout_seconds, timeout_ms / 1000 + 2)
                with httpx.Client(timeout=request_timeout, trust_env=False) as client:
                    response = client.get(
                        f"{self.base_url}/group/{encoded}/delay",
                        headers=self._headers(),
                        params={"timeout": timeout_ms, "url": test_url},
                    )
                    if response.status_code == 404:
                        member_delay = self._delay_json_with_zero_readback(
                            target,
                            test_url=test_url,
                            timeout_ms=timeout_ms,
                        )
                        evidence = member_delay.get("native_zero_readback")
                        return target, {target: member_delay.get("delay")}, None, evidence if isinstance(evidence, dict) else None
                    else:
                        response.raise_for_status()
                        payload = response.json()
                        return target, payload if isinstance(payload, dict) else {}, None, None
            except (httpx.HTTPError, OSError, ValueError, yaml.YAMLError) as exc:
                return target, None, {
                    "ok": False,
                    "logical_runtime_target": target,
                    "effective_member_runtime_identity": None,
                    "observed_at": datetime.now(timezone.utc).isoformat(),
                    "evidence_source": "runtime_native",
                    "members": [],
                    "error_code": "RUNTIME_LOGICAL_GROUP_PROBE_FAILED",
                    "error_message": str(exc),
                }, None
        with ThreadPoolExecutor(max_workers=4, thread_name_prefix="mihomo-group-probe") as executor:
            probe_results = list(executor.map(probe_one, logical_runtime_targets))
        delays_by_target = {
            target: delays
            for target, delays, _failure, _evidence in probe_results
            if delays is not None
        }
        failures = {
            target: failure
            for target, _delays, failure, _evidence in probe_results
            if failure is not None
        }
        evidence_by_target = {
            target: evidence
            for target, _delays, failure, evidence in probe_results
            if failure is None and evidence is not None
        }
        checked_at = datetime.now(timezone.utc).isoformat()
        snapshots = {
            str(item.get("logical_runtime_target") or ""): item
            for item in self.get_logical_groups_state(logical_runtime_targets)
        }
        results: list[dict[str, Any]] = []
        for target in logical_runtime_targets:
            if target in failures:
                results.append(failures[target])
                continue
            result = self._apply_probe_delays(
                snapshots.get(target) or {},
                delays_by_target.get(target) or {},
                checked_at=checked_at,
            )
            if target in evidence_by_target:
                result["probe_evidence"] = evidence_by_target[target]
            results.append(result)
        return results

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

    def _selector_targets(self, selector_name: str, *, proxies: dict[str, Any] | None = None) -> set[str]:
        selector = proxies.get(selector_name) if isinstance(proxies, dict) else self._proxy(selector_name)
        if not isinstance(selector, dict):
            return set()

        targets = selector.get("all") or []
        return {str(target) for target in targets if target}

    def _selected_proxy_id(self, selector_name: str, *, proxies: dict[str, Any] | None = None) -> str | None:
        selector = proxies.get(selector_name) if isinstance(proxies, dict) else self._proxy(selector_name)
        if not isinstance(selector, dict):
            return None

        selected = selector.get("now")
        if not selected or str(selected) in NON_SERVER_PROXY_TARGETS:
            return None

        return str(selected)

    @staticmethod
    def _selected_target(selector_name: str, *, proxies: dict[str, Any]) -> str | None:
        selector = proxies.get(selector_name)
        if not isinstance(selector, dict):
            return None
        selected = str(selector.get("now") or "").strip()
        return selected or None

    def get_active_server_id(self) -> str | None:
        return self._active_server_id_from_proxies(self._proxies())

    def _active_server_id_from_proxies(self, proxies: dict[str, Any]) -> str | None:
        if "vpn-global" in proxies:
            global_selected = self._selected_proxy_id("vpn-global", proxies=proxies)
            if global_selected == "vpn-auto":
                return self._selected_proxy_id("vpn-auto", proxies=proxies)
            # An owned selector pointed at a built-in reject/direct target or
            # with no selection is not proof of VPN egress. Never mask it with
            # an unrelated GLOBAL selector.
            return global_selected
        if "vpn-auto" in proxies:
            return self._selected_proxy_id("vpn-auto", proxies=proxies)
        return self._selected_proxy_id("GLOBAL", proxies=proxies)

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
            selector_before = self._selected_proxy_id(selector_name)

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

            if str(server_id) in NON_SERVER_PROXY_TARGETS:
                return MihomoApplyResult(
                    ok=False,
                    message="A built-in DIRECT/REJECT target is not a VPN server selection.",
                    active_server_id=active_before,
                    error_code="MIHOMO_TARGET_NOT_FOUND",
                    error_message=f"Mihomo target is not an eligible VPN server in {selector_name}: {server_id}",
                    details={"adapter": "http", "selector": selector_name, "requested_server_id": server_id},
                )

            response_body = self._put_json(selector_endpoint, {"name": server_id})
            active_after = self.get_active_server_id()
            selector_after = self._selected_proxy_id(selector_name)
        except (httpx.HTTPError, OSError, yaml.YAMLError) as exc:
            if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 404:
                return MihomoApplyResult(
                    ok=False,
                    message="Requested Mihomo selector is not present in runtime inventory.",
                    active_server_id=None,
                    error_code="MIHOMO_SELECTOR_NOT_FOUND",
                    error_message=f"Mihomo selector not found or has no targets: {selector_name}",
                    details={
                        "adapter": "http",
                        "selector": selector_name,
                        "selector_endpoint": selector_endpoint,
                        "requested_server_id": server_id,
                        "http_status": 404,
                    },
                )
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
                "selector_before": selector_before,
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
            proxies = self._proxies()
            known_servers = {
                name
                for name, raw in proxies.items()
                if isinstance(raw, dict)
                and name not in BUILTIN_PROXY_NAMES
                and str(raw.get("type") or "") not in GROUP_PROXY_TYPES
            }
            target_raw = proxies.get(server_id)
            is_logical_group = (
                isinstance(target_raw, dict)
                and server_id not in BUILTIN_PROXY_NAMES
                and str(target_raw.get("type") or "") in GROUP_PROXY_TYPES
            )

            if server_id not in known_servers and not is_logical_group:
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

            response_body = self._delay_json_with_zero_readback(
                server_id,
                test_url=test_url,
                timeout_ms=timeout_ms,
            )
            delay = response_body.get("delay")

            if type(delay) is int and 0 <= delay <= 65535:
                return MihomoDelayResult(
                    ok=True,
                    server_id=server_id,
                    delay_ms=delay,
                    test_url=test_url,
                    timeout_ms=timeout_ms,
                    details={
                        "adapter": "http",
                        "logical_group": is_logical_group,
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
