from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from fwrouter_api.core.config import get_settings


XRAY_PUBLIC_HOST = "xray.minisk.ru"
XRAY_PUBLIC_PATH = "/vless"
XRAY_PUBLIC_PORT = 443
XRAY_TRANSPORT = "ws"
XRAY_LOG_ROOT = Path("/var/log/fwrouter/xray")
XRAY_COMPOSE_PATH = Path("/opt/fwrouter-xray/docker-compose.yml")
XRAY_CONTAINER_NAME = "fwrouter-xray"
XRAY_INBOUND_TAG = "vless-ws"
XRAY_FALLBACK_OUTBOUND_TAG = "blocked-until-fwrouter-dataplane"
XRAY_MANAGED_DNS_OUTBOUND_TAG = "fwrouter-dns-out"
XRAY_API_TAG = "fwrouter-api"
XRAY_API_PORT = 10085


class XrayRuntimeState(str, Enum):
    RUNNING = "running"
    DEGRADED = "degraded"
    FAILED = "failed"
    NOT_CONFIGURED = "not_configured"


@dataclass(frozen=True)
class XrayClient:
    client_id: str
    client_uuid: str
    email: str | None = None
    alias: str | None = None
    enabled: bool = True
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class XrayHealth:
    runtime_state: XrayRuntimeState
    message: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class XrayApplyResult:
    ok: bool
    message: str
    error_code: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


class XrayAdapterError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


def _default_xray_config_path() -> Path:
    return get_settings().paths.state_dir / "xray" / "config.json"


def _alias_slug(value: str) -> str:
    return "".join(character.lower() if character.isalnum() else "-" for character in value).strip("-")


def _default_email(alias: str | None, client_uuid: str) -> str:
    if alias:
        slug = _alias_slug(alias)
        if slug:
            return f"{slug}@fwrouter.local"
    return f"{client_uuid}@fwrouter.local"


def _json_dump(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _coerce_runner_result(result: Any) -> XrayApplyResult:
    if isinstance(result, XrayApplyResult):
        return result
    if isinstance(result, dict):
        return XrayApplyResult(
            ok=bool(result.get("ok", False)),
            message=str(result.get("message") or ""),
            error_code=result.get("error_code"),
            details=dict(result.get("details") or {}),
        )
    if isinstance(result, subprocess.CompletedProcess):
        return XrayApplyResult(
            ok=result.returncode == 0,
            message=(result.stdout or result.stderr or "").strip() or "command finished",
            error_code=None if result.returncode == 0 else "XRAY_COMMAND_FAILED",
            details={
                "argv": list(result.args) if isinstance(result.args, (list, tuple)) else [str(result.args)],
                "returncode": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
            },
        )
    raise TypeError(f"Unsupported Xray runner result: {type(result)!r}")


class XrayAdapter:
    def health(self) -> XrayHealth:  # pragma: no cover - interface only
        raise NotImplementedError

    def list_clients(self) -> list[XrayClient]:  # pragma: no cover - interface only
        raise NotImplementedError

    def create_client(
        self,
        *,
        alias: str | None = None,
        email: str | None = None,
        client_uuid: str | None = None,
    ) -> XrayApplyResult:  # pragma: no cover - interface only
        raise NotImplementedError

    def delete_client(self, client_id: str) -> XrayApplyResult:  # pragma: no cover - interface only
        raise NotImplementedError

    def update_client_alias(
        self,
        client_id: str,
        alias: str | None,
    ) -> XrayApplyResult:  # pragma: no cover - interface only
        raise NotImplementedError

    def test_config(self, generated_config_path: str) -> XrayApplyResult:  # pragma: no cover - interface only
        raise NotImplementedError

    def reload(self) -> XrayApplyResult:  # pragma: no cover - interface only
        raise NotImplementedError

    def export_vless_subscription(self, client_id: str) -> XrayApplyResult:  # pragma: no cover - interface only
        raise NotImplementedError

    def materialize_client_bindings(
        self,
        bindings: list[dict[str, Any]],
        *,
        force_reload: bool = False,
    ) -> XrayApplyResult:  # pragma: no cover - interface only
        raise NotImplementedError
