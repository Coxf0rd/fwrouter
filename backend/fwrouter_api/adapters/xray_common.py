from __future__ import annotations

import fcntl
import json
import os
import subprocess
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from functools import wraps
from pathlib import Path
from typing import Any, Callable, Iterator, TypeVar

from fwrouter_api.core.config import get_settings


_XRAY_WRITER_THREAD_LOCK = threading.RLock()
_XRAY_WRITER_LOCAL = threading.local()
_F = TypeVar("_F", bound=Callable[..., Any])


@contextmanager
def xray_writer_guard() -> Iterator[None]:
    """Serialize Xray config read-modify-write operations across threads/processes."""
    _XRAY_WRITER_THREAD_LOCK.acquire()
    depth = int(getattr(_XRAY_WRITER_LOCAL, "depth", 0))
    fd: int | None = None
    lock_acquired = False
    try:
        if depth == 0:
            run_dir = get_settings().paths.run_dir
            run_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(run_dir / "xray-writer.lock", os.O_CREAT | os.O_RDWR, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            lock_acquired = True
            _XRAY_WRITER_LOCAL.fd = fd
        _XRAY_WRITER_LOCAL.depth = depth + 1
        try:
            yield
        finally:
            current_depth = max(0, int(getattr(_XRAY_WRITER_LOCAL, "depth", 1)) - 1)
            _XRAY_WRITER_LOCAL.depth = current_depth
            if current_depth == 0:
                held_fd = getattr(_XRAY_WRITER_LOCAL, "fd", None)
                if held_fd is not None:
                    try:
                        fcntl.flock(held_fd, fcntl.LOCK_UN)
                    finally:
                        os.close(held_fd)
                        del _XRAY_WRITER_LOCAL.fd
    finally:
        if depth == 0 and fd is not None and not lock_acquired:
            os.close(fd)
        _XRAY_WRITER_THREAD_LOCK.release()


def xray_writer_guarded(function: _F) -> _F:
    @wraps(function)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        with xray_writer_guard():
            return function(*args, **kwargs)
    return wrapped  # type: ignore[return-value]


XRAY_PUBLIC_HOST = ""
XRAY_PUBLIC_PATH = "/vless"
XRAY_PUBLIC_PORT = 443
XRAY_TRANSPORT = "ws"
XRAY_LOG_ROOT = Path("/var/log/fwrouter/xray")
XRAY_COMPOSE_PATH = Path("/opt/fwrouter-xray/docker-compose.yml")
XRAY_CONTAINER_NAME = "fwrouter-xray"
XRAY_INBOUND_TAG = "vless-ws"
XRAY_FALLBACK_OUTBOUND_TAG = "blocked-until-fwrouter-dataplane"
XRAY_EXPLICIT_DIRECT_OUTBOUND_TAG = "fwrouter-explicit-direct"
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
        client_modes: list[dict[str, Any]] | None = None,
        force_reload: bool = False,
    ) -> XrayApplyResult:  # pragma: no cover - interface only
        raise NotImplementedError

    def reconcile_clients(
        self,
        *,
        desired_clients: list[dict[str, Any]],
        managed_email_prefixes: list[str] | None = None,
    ) -> XrayApplyResult:  # pragma: no cover - interface only
        raise NotImplementedError
