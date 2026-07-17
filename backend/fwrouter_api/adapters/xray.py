from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from fwrouter_api.core.config import get_settings
from fwrouter_api.services.xray_handoff import (
    XRAY_MANAGED_EGRESS_PREFIX,
    XRAY_MIHOMO_HANDOFF_HOST,
    build_xray_handoff_assignments,
)
from fwrouter_api.services.xray_subscription import build_xray_vless_uri
from fwrouter_api.services.artifacts import atomic_write_text


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

    def materialize_client_bindings(self, bindings: list[dict[str, Any]]) -> XrayApplyResult:  # pragma: no cover - interface only
        raise NotImplementedError


class NoopXrayAdapter(XrayAdapter):
    def health(self) -> XrayHealth:
        return XrayHealth(
            runtime_state=XrayRuntimeState.NOT_CONFIGURED,
            message="Xray runtime adapter is not configured.",
            details={
                "adapter": "noop",
                "config_path": str(_default_xray_config_path()),
                "compose_path": str(XRAY_COMPOSE_PATH),
                "public_host": XRAY_PUBLIC_HOST,
                "public_path": XRAY_PUBLIC_PATH,
                "public_port": XRAY_PUBLIC_PORT,
                "transport": XRAY_TRANSPORT,
                "forced_vpn_ready": False,
                "traffic_available": False,
            },
        )

    def list_clients(self) -> list[XrayClient]:
        return []

    def create_client(
        self,
        *,
        alias: str | None = None,
        email: str | None = None,
        client_uuid: str | None = None,
    ) -> XrayApplyResult:
        return XrayApplyResult(
            ok=False,
            message="Xray create_client is not implemented for noop adapter.",
            error_code="XRAY_CREATE_NOT_IMPLEMENTED",
            details={"alias": alias, "email": email, "client_uuid": client_uuid},
        )

    def delete_client(self, client_id: str) -> XrayApplyResult:
        return XrayApplyResult(
            ok=False,
            message="Xray delete_client is not implemented for noop adapter.",
            error_code="XRAY_DELETE_NOT_IMPLEMENTED",
            details={"client_id": client_id},
        )

    def update_client_alias(self, client_id: str, alias: str | None) -> XrayApplyResult:
        return XrayApplyResult(
            ok=False,
            message="Xray update_client_alias is not implemented for noop adapter.",
            error_code="XRAY_ALIAS_NOT_IMPLEMENTED",
            details={"client_id": client_id, "alias": alias},
        )

    def test_config(self, generated_config_path: str) -> XrayApplyResult:
        return XrayApplyResult(
            ok=False,
            message="Xray config test is not implemented for noop adapter.",
            error_code="XRAY_TEST_NOT_IMPLEMENTED",
            details={"path": generated_config_path},
        )

    def reload(self) -> XrayApplyResult:
        return XrayApplyResult(
            ok=False,
            message="Xray reload is not implemented for noop adapter.",
            error_code="XRAY_RELOAD_NOT_IMPLEMENTED",
        )

    def export_vless_subscription(self, client_id: str) -> XrayApplyResult:
        return XrayApplyResult(
            ok=False,
            message="Xray subscription export is not implemented for noop adapter.",
            error_code="XRAY_EXPORT_NOT_IMPLEMENTED",
            details={"client_id": client_id},
        )

    def materialize_client_bindings(self, bindings: list[dict[str, Any]]) -> XrayApplyResult:
        return XrayApplyResult(
            ok=False,
            message="Xray binding materialization is not implemented for noop adapter.",
            error_code="XRAY_BINDINGS_NOT_IMPLEMENTED",
            details={"bindings_count": len(bindings)},
        )


class RealXrayAdapter(XrayAdapter):
    def __init__(
        self,
        *,
        config_path: Path | None = None,
        compose_path: Path | None = None,
        log_root: Path | None = None,
        runner: Callable[[str, dict[str, Any]], Any] | None = None,
    ) -> None:
        self.config_path = config_path or _default_xray_config_path()
        self.compose_path = compose_path or XRAY_COMPOSE_PATH
        self.log_root = log_root or XRAY_LOG_ROOT
        self._runner = runner or self._default_runner

    def _run(self, action: str, **payload: Any) -> XrayApplyResult:
        result = self._runner(action, payload)
        return _coerce_runner_result(result)

    def _default_runner(self, action: str, payload: dict[str, Any]) -> XrayApplyResult:
        if action == "test_config":
            host_path = Path(str(payload["path"])).resolve()
            container_path = "/tmp/fwrouter-xray-candidate.json"
            command = [
                "docker",
                "compose",
                "-f",
                str(self.compose_path),
                "run",
                "--rm",
                "-v",
                f"{host_path}:{container_path}:ro",
                XRAY_CONTAINER_NAME,
                "xray",
                "-test",
                "-config",
                container_path,
            ]
        elif action == "reload":
            command = [
                "docker",
                "compose",
                "-f",
                str(self.compose_path),
                "restart",
                XRAY_CONTAINER_NAME,
            ]
        elif action == "compose_ps":
            command = [
                "docker",
                "compose",
                "-f",
                str(self.compose_path),
                "ps",
                "--format",
                "json",
            ]
        else:
            raise XrayAdapterError(
                "XRAY_RUNNER_ACTION_UNKNOWN",
                f"Unknown Xray runner action: {action}",
                details={"action": action},
            )

        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
        )
        return _coerce_runner_result(completed)

    def _load_config(self) -> dict[str, Any]:
        if not self.config_path.exists():
            raise XrayAdapterError(
                "XRAY_CONFIG_MISSING",
                f"Xray config is missing: {self.config_path}",
                details={"config_path": str(self.config_path)},
            )

        try:
            payload = json.loads(self.config_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise XrayAdapterError(
                "XRAY_CONFIG_INVALID_JSON",
                "Xray config.json is not valid JSON.",
                details={"config_path": str(self.config_path), "line": exc.lineno, "column": exc.colno},
            ) from exc

        if not isinstance(payload, dict):
            raise XrayAdapterError(
                "XRAY_CONFIG_INVALID",
                "Xray config root must be a JSON object.",
                details={"config_path": str(self.config_path)},
            )
        return payload

    def _find_vless_ws_inbound(self, payload: dict[str, Any]) -> dict[str, Any]:
        inbounds = payload.get("inbounds") or []
        if not isinstance(inbounds, list):
            inbounds = []

        for inbound in inbounds:
            if not isinstance(inbound, dict):
                continue
            protocol = str(inbound.get("protocol") or "").lower()
            stream_settings = inbound.get("streamSettings") or {}
            network = str(stream_settings.get("network") or "").lower()
            if protocol == "vless" and network == "ws":
                return inbound

        raise XrayAdapterError(
            "XRAY_VLESS_INBOUND_MISSING",
            "VLESS WS inbound was not found in Xray config.",
            details={"config_path": str(self.config_path)},
        )

    def _client_alias_from_raw(self, raw_client: dict[str, Any]) -> str | None:
        alias = raw_client.get("fwrouterAlias") or raw_client.get("alias")
        if alias:
            return str(alias).strip() or None
        email = raw_client.get("email")
        if not email:
            return None
        local_part = str(email).split("@", 1)[0].strip()
        return local_part or None

    def _clients_from_inbound(self, inbound: dict[str, Any]) -> list[XrayClient]:
        settings = inbound.get("settings") or {}
        raw_clients = settings.get("clients") or []
        if not isinstance(raw_clients, list):
            raw_clients = []

        clients: list[XrayClient] = []
        for raw_client in raw_clients:
            if not isinstance(raw_client, dict):
                continue
            client_uuid = str(raw_client.get("id") or "").strip()
            if not client_uuid:
                continue
            email = raw_client.get("email")
            clients.append(
                XrayClient(
                    client_id=client_uuid,
                    client_uuid=client_uuid,
                    email=str(email).strip() if email else None,
                    alias=self._client_alias_from_raw(raw_client),
                    enabled=bool(raw_client.get("enable", True)),
                    raw=dict(raw_client),
                )
            )
        return clients

    def _load_clients_and_config(self) -> tuple[dict[str, Any], dict[str, Any], list[XrayClient]]:
        payload = self._load_config()
        inbound = self._find_vless_ws_inbound(payload)
        clients = self._clients_from_inbound(inbound)
        return payload, inbound, clients

    def _candidate_path(self) -> Path:
        return self.config_path.parent / f"{self.config_path.name}.candidate"

    def _persist_candidate(self, payload: dict[str, Any]) -> Path:
        candidate_path = self._candidate_path()
        atomic_write_text(candidate_path, _json_dump(payload))
        return candidate_path

    def _write_active_config(self, payload: dict[str, Any]) -> None:
        atomic_write_text(self.config_path, _json_dump(payload))

    def _resolve_client(self, client_id: str) -> tuple[dict[str, Any], dict[str, Any], list[XrayClient], XrayClient]:
        payload, inbound, clients = self._load_clients_and_config()
        for client in clients:
            if client.client_id == client_id or client.client_uuid == client_id:
                return payload, inbound, clients, client
        raise XrayAdapterError(
            "XRAY_CLIENT_NOT_FOUND",
            f"Xray client not found: {client_id}",
            details={"client_id": client_id},
        )

    def _fallback_blackhole_outbound(self) -> dict[str, Any]:
        return {
            "tag": XRAY_FALLBACK_OUTBOUND_TAG,
            "protocol": "blackhole",
            "settings": {},
        }

    def _managed_dns_outbound(self) -> dict[str, Any]:
        return {
            "tag": XRAY_MANAGED_DNS_OUTBOUND_TAG,
            "protocol": "dns",
            "settings": {
                "rewriteNetwork": "udp",
                "rewriteAddress": "1.1.1.1",
                "rewritePort": 53,
            },
        }

    def _managed_api_inbound(self) -> dict[str, Any]:
        return {
            "tag": XRAY_API_TAG,
            "listen": "127.0.0.1",
            "port": XRAY_API_PORT,
            "protocol": "dokodemo-door",
            "settings": {
                "address": "127.0.0.1",
            },
        }

    def _managed_api_outbound(self) -> dict[str, Any]:
        return {
            "tag": XRAY_API_TAG,
            "protocol": "freedom",
            "settings": {},
        }

    def _managed_api_rule(self) -> dict[str, Any]:
        return {
            "type": "field",
            "inboundTag": [XRAY_API_TAG],
            "outboundTag": XRAY_API_TAG,
        }

    def _ensure_runtime_stats(self, payload: dict[str, Any]) -> None:
        payload["stats"] = payload.get("stats") if isinstance(payload.get("stats"), dict) else {}
        payload["api"] = {
            **(payload.get("api") if isinstance(payload.get("api"), dict) else {}),
            "tag": XRAY_API_TAG,
            "services": ["StatsService"],
        }

        policy = payload.get("policy") if isinstance(payload.get("policy"), dict) else {}
        levels = policy.get("levels") if isinstance(policy.get("levels"), dict) else {}
        level_zero = levels.get("0") if isinstance(levels.get("0"), dict) else {}
        levels["0"] = {
            **level_zero,
            "statsUserUplink": True,
            "statsUserDownlink": True,
        }
        policy["levels"] = levels
        payload["policy"] = policy

        inbounds = payload.get("inbounds") if isinstance(payload.get("inbounds"), list) else []
        preserved_inbounds = [
            inbound
            for inbound in inbounds
            if isinstance(inbound, dict) and str(inbound.get("tag") or "") != XRAY_API_TAG
        ]
        payload["inbounds"] = [
            *preserved_inbounds,
            self._managed_api_inbound(),
        ]

    def _build_socks_handoff_outbound(
        self,
        *,
        tag: str,
        port: int,
    ) -> dict[str, Any]:
        return {
            "tag": tag,
            "protocol": "socks",
            "settings": {
                "servers": [
                    {
                        "address": XRAY_MIHOMO_HANDOFF_HOST,
                        "port": port,
                    }
                ]
            },
        }

    def _ensure_managed_inbound_tag(self, inbound: dict[str, Any]) -> None:
        inbound["tag"] = XRAY_INBOUND_TAG

    def _managed_routing_rules_from_bindings(
        self,
        *,
        bindings: list[dict[str, Any]],
        egress_tags_by_server: dict[str, str],
    ) -> list[dict[str, Any]]:
        rules: list[dict[str, Any]] = []

        for binding in bindings:
            client_email = str(binding.get("client_email") or "").strip()
            selected_server_id = str(binding.get("selected_server_id") or "").strip()
            outbound_tag = egress_tags_by_server.get(selected_server_id)
            if not client_email or not outbound_tag:
                continue

            rules.append(
                {
                    "type": "field",
                    "inboundTag": [XRAY_INBOUND_TAG],
                    "user": [client_email],
                    "outboundTag": outbound_tag,
                }
            )
        return rules

    def _is_managed_outbound(self, outbound: dict[str, Any]) -> bool:
        tag = str(outbound.get("tag") or "")
        return (
            tag == XRAY_API_TAG
            or tag == XRAY_FALLBACK_OUTBOUND_TAG
            or tag == XRAY_MANAGED_DNS_OUTBOUND_TAG
            or tag.startswith(XRAY_MANAGED_EGRESS_PREFIX)
        )

    def _is_managed_rule(self, rule: dict[str, Any]) -> bool:
        outbound_tag = str(rule.get("outboundTag") or "")
        inbound_tags = rule.get("inboundTag") or []
        if isinstance(inbound_tags, str):
            inbound_tags = [inbound_tags]
        return (
            outbound_tag == XRAY_API_TAG
            or outbound_tag == XRAY_FALLBACK_OUTBOUND_TAG
            or outbound_tag == XRAY_MANAGED_DNS_OUTBOUND_TAG
            or outbound_tag.startswith(XRAY_MANAGED_EGRESS_PREFIX)
            or XRAY_API_TAG in inbound_tags
            or XRAY_INBOUND_TAG in inbound_tags and "user" in rule
        )

    def _materialize_managed_egress(
        self,
        *,
        payload: dict[str, Any],
        bindings: list[dict[str, Any]],
    ) -> tuple[int, dict[str, Any]]:
        self._ensure_runtime_stats(payload)
        existing_outbounds = payload.get("outbounds") if isinstance(payload.get("outbounds"), list) else []
        preserved_outbounds = [
            outbound
            for outbound in existing_outbounds
            if isinstance(outbound, dict) and not self._is_managed_outbound(outbound)
        ]

        handoff_assignments = build_xray_handoff_assignments(bindings)
        egress_by_server: dict[str, dict[str, Any]] = {}
        egress_tags_by_server: dict[str, str] = {}

        for assignment in handoff_assignments:
            selected_server_id = str(assignment["selected_server_id"])
            tag = str(assignment["tag"])
            port = int(assignment["port"])
            egress_by_server[selected_server_id] = self._build_socks_handoff_outbound(
                tag=tag,
                port=port,
            )
            egress_tags_by_server[selected_server_id] = tag

        managed_rules = self._managed_routing_rules_from_bindings(
            bindings=bindings,
            egress_tags_by_server=egress_tags_by_server,
        )

        routing = payload.get("routing") if isinstance(payload.get("routing"), dict) else {}
        existing_rules = routing.get("rules") if isinstance(routing.get("rules"), list) else []
        preserved_rules = [
            rule
            for rule in existing_rules
            if isinstance(rule, dict) and not self._is_managed_rule(rule)
        ]

        payload["dns"] = {
            **(payload.get("dns") if isinstance(payload.get("dns"), dict) else {}),
            "servers": ["172.17.0.1", "1.1.1.1", "8.8.8.8"], # Use Docker host IP for DNS
            "queryStrategy": "UseIPv4",
        }

        payload["outbounds"] = [
            self._managed_api_outbound(),
            self._fallback_blackhole_outbound(),
            self._managed_dns_outbound(),
            *egress_by_server.values(),
            *preserved_outbounds,
        ]
        payload["routing"] = {
            **routing,
            "domainStrategy": routing.get("domainStrategy") or "AsIs",
            "rules": [
                self._managed_api_rule(),
                *managed_rules,
                *preserved_rules,
            ],
        }

        return len(managed_rules), {
            "managed_outbounds_count": len(egress_by_server),
            "managed_rules_count": len(managed_rules),
            "egress_tags": egress_tags_by_server,
            "handoff_count": len(handoff_assignments),
            "listeners": [
                {
                    "selected_server_id": assignment["selected_server_id"],
                    "listener_name": assignment["listener_name"],
                    "listen": assignment["listen"],
                    "port": assignment["port"],
                    "outbound_tag": assignment["tag"],
                    "client_emails": assignment["client_emails"],
                }
                for assignment in handoff_assignments
            ],
            "ports": [assignment["port"] for assignment in handoff_assignments],
            "selected_server_ids": [
                assignment["selected_server_id"] for assignment in handoff_assignments
            ],
        }

    def _materialize_client_binding_metadata(
        self,
        *,
        raw_clients: list[dict[str, Any]],
        bindings: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], int]:
        binding_map: dict[str, dict[str, Any]] = {}
        for binding in bindings:
            client_uuid = str(binding.get("client_uuid") or "").strip()
            client_id = str(binding.get("client_id") or "").strip()
            if client_uuid:
                binding_map[client_uuid] = binding
            if client_id:
                binding_map[client_id] = binding

        updated_clients: list[dict[str, Any]] = []
        applied_count = 0
        for raw_client in raw_clients:
            if not isinstance(raw_client, dict):
                continue
            updated = dict(raw_client)
            client_uuid = str(updated.get("id") or "").strip()
            binding = binding_map.get(client_uuid)
            if binding is None:
                updated.pop("fwrouterBinding", None)
            else:
                updated["fwrouterBinding"] = {
                    "subject_id": binding.get("subject_id"),
                    "client_id": binding.get("client_id"),
                    "client_uuid": binding.get("client_uuid"),
                    "selected_server_id": binding.get("selected_server_id"),
                    "selected_server_source": binding.get("selected_server_source"),
                    "status": binding.get("status"),
                    "match_key": binding.get("match_key"),
                    "applied_at": binding.get("applied_at"),
                }
                applied_count += 1
            updated_clients.append(updated)

        return updated_clients, applied_count

    def health(self) -> XrayHealth:
        details = {
            "adapter": "xray",
            "config_path": str(self.config_path),
            "compose_path": str(self.compose_path),
            "public_host": XRAY_PUBLIC_HOST,
            "public_path": XRAY_PUBLIC_PATH,
            "public_port": XRAY_PUBLIC_PORT,
            "transport": XRAY_TRANSPORT,
            "forced_vpn_ready": False,
            "traffic_available": False,
        }

        if not self.config_path.exists():
            return XrayHealth(
                runtime_state=XrayRuntimeState.NOT_CONFIGURED,
                message="Xray config is missing.",
                details=details,
            )

        try:
            payload, inbound, clients = self._load_clients_and_config()
        except XrayAdapterError as exc:
            return XrayHealth(
                runtime_state=XrayRuntimeState.FAILED,
                message=exc.message,
                details={**details, **exc.details},
            )

        ws_settings = ((inbound.get("streamSettings") or {}).get("wsSettings") or {})
        details.update(
            {
                "listen": inbound.get("listen") or "0.0.0.0",
                "inbound_port": inbound.get("port"),
                "inbound_path": ws_settings.get("path") or XRAY_PUBLIC_PATH,
                "clients_count": len(clients),
                "config_loaded": isinstance(payload, dict),
            }
        )

        if not self.compose_path.exists():
            return XrayHealth(
                runtime_state=XrayRuntimeState.DEGRADED,
                message="Xray config is ready, but docker-compose file is missing.",
                details=details,
            )

        compose_ps = self._run("compose_ps")
        details["compose"] = compose_ps.details
        if not compose_ps.ok:
            return XrayHealth(
                runtime_state=XrayRuntimeState.DEGRADED,
                message="Xray config is ready, but compose status probe failed.",
                details=details,
            )

        status_text = str(compose_ps.details.get("stdout") or compose_ps.message or "").lower()
        if "running" in status_text:
            return XrayHealth(
                runtime_state=XrayRuntimeState.RUNNING,
                message="Xray runtime is up, but forced VPN dataplane is not enabled yet.",
                details=details,
            )

        return XrayHealth(
            runtime_state=XrayRuntimeState.DEGRADED,
            message="Xray config is ready, but runtime is not confirmed as running.",
            details=details,
        )

    def list_clients(self) -> list[XrayClient]:
        _, _, clients = self._load_clients_and_config()
        return clients

    def create_client(
        self,
        *,
        alias: str | None = None,
        email: str | None = None,
        client_uuid: str | None = None,
    ) -> XrayApplyResult:
        payload, inbound, clients = self._load_clients_and_config()
        candidate_clients = list((inbound.get("settings") or {}).get("clients") or [])
        normalized_email = (email or "").strip() or None

        if normalized_email and any((client.email or "").lower() == normalized_email.lower() for client in clients):
            raise XrayAdapterError(
                "XRAY_DUPLICATE_EMAIL",
                f"Xray client email already exists: {normalized_email}",
                details={"email": normalized_email},
            )

        effective_client_uuid = str(client_uuid or uuid4()).strip()
        if not effective_client_uuid:
            effective_client_uuid = str(uuid4())
        client_email = normalized_email or _default_email(alias, effective_client_uuid)
        raw_client: dict[str, Any] = {
            "id": effective_client_uuid,
            "email": client_email,
        }
        candidate_clients.append(raw_client)
        inbound.setdefault("settings", {})["clients"] = candidate_clients

        candidate_path = self._persist_candidate(payload)
        validation = self.test_config(str(candidate_path))
        if not validation.ok:
            return XrayApplyResult(
                ok=False,
                message="Xray candidate config failed validation.",
                error_code=validation.error_code or "XRAY_CONFIG_TEST_FAILED",
                details={
                        "stage": "test_config",
                        "candidate_path": str(candidate_path),
                        "client": {
                            "client_id": effective_client_uuid,
                            "client_uuid": effective_client_uuid,
                            "email": client_email,
                            "alias": alias,
                            "enabled": True,
                        },
                    "validation": validation.details,
                },
            )

        self._write_active_config(payload)
        reload_result = self.reload()
        success = reload_result.ok
        message = "Xray client created."
        if not success:
            message = "Xray client was saved, but runtime reload failed."

        return XrayApplyResult(
            ok=success,
            message=message,
            error_code=None if success else reload_result.error_code or "XRAY_RELOAD_FAILED",
            details={
                "stage": "reload" if not success else "completed",
                "candidate_path": str(candidate_path),
                "client": {
                    "client_id": effective_client_uuid,
                    "client_uuid": effective_client_uuid,
                    "email": client_email,
                    "alias": alias,
                    "enabled": True,
                    "raw": raw_client,
                },
                "reload": reload_result.details,
            },
        )

    def delete_client(self, client_id: str) -> XrayApplyResult:
        payload, inbound, _, client = self._resolve_client(client_id)
        raw_clients = list((inbound.get("settings") or {}).get("clients") or [])
        inbound.setdefault("settings", {})["clients"] = [
            raw_client
            for raw_client in raw_clients
            if str((raw_client or {}).get("id") or "").strip() != client.client_uuid
        ]

        candidate_path = self._persist_candidate(payload)
        validation = self.test_config(str(candidate_path))
        if not validation.ok:
            return XrayApplyResult(
                ok=False,
                message="Xray candidate config failed validation.",
                error_code=validation.error_code or "XRAY_CONFIG_TEST_FAILED",
                details={
                    "stage": "test_config",
                    "candidate_path": str(candidate_path),
                    "client": {
                        "client_id": client.client_id,
                        "client_uuid": client.client_uuid,
                        "email": client.email,
                        "alias": client.alias,
                        "enabled": client.enabled,
                    },
                    "validation": validation.details,
                },
            )

        self._write_active_config(payload)
        reload_result = self.reload()
        return XrayApplyResult(
            ok=reload_result.ok,
            message="Xray client deleted." if reload_result.ok else "Xray client was removed from config, but runtime reload failed.",
            error_code=None if reload_result.ok else reload_result.error_code or "XRAY_RELOAD_FAILED",
            details={
                "stage": "reload" if not reload_result.ok else "completed",
                "candidate_path": str(candidate_path),
                "client": {
                    "client_id": client.client_id,
                    "client_uuid": client.client_uuid,
                    "email": client.email,
                    "alias": client.alias,
                    "enabled": client.enabled,
                },
                "reload": reload_result.details,
            },
        )

    def update_client_alias(self, client_id: str, alias: str | None) -> XrayApplyResult:
        _, _, _, client = self._resolve_client(client_id)
        return XrayApplyResult(
            ok=True,
            message="Xray client alias updated in FWRouter metadata.",
            details={
                "client": {
                    "client_id": client.client_id,
                    "client_uuid": client.client_uuid,
                    "email": client.email,
                    "alias": alias,
                    "enabled": client.enabled,
                    "raw": client.raw,
                }
            },
        )

    def test_config(self, generated_config_path: str) -> XrayApplyResult:
        return self._run("test_config", path=generated_config_path)

    def reload(self) -> XrayApplyResult:
        return self._run("reload")

    def export_vless_subscription(self, client_id: str) -> XrayApplyResult:
        _, _, _, client = self._resolve_client(client_id)
        label = client.alias or client.email or client.client_uuid
        uri = build_xray_vless_uri(
            client_uuid=client.client_uuid,
            label=label,
        )
        return XrayApplyResult(
            ok=True,
            message="Xray subscription exported.",
            details={
                "client": {
                    "client_id": client.client_id,
                    "client_uuid": client.client_uuid,
                    "email": client.email,
                    "alias": client.alias,
                    "enabled": client.enabled,
                },
                "subscription_uri": uri,
            },
        )

    def materialize_client_bindings(self, bindings: list[dict[str, Any]]) -> XrayApplyResult:
        payload, inbound, _ = self._load_clients_and_config()
        self._ensure_managed_inbound_tag(inbound)
        raw_clients = list((inbound.get("settings") or {}).get("clients") or [])
        updated_clients, metadata_applied_count = self._materialize_client_binding_metadata(
            raw_clients=raw_clients,
            bindings=bindings,
        )
        inbound.setdefault("settings", {})["clients"] = updated_clients

        routing_applied_count, egress_details = self._materialize_managed_egress(
            payload=payload,
            bindings=bindings,
        )
        applied_count = min(metadata_applied_count, routing_applied_count)

        candidate_path = self._persist_candidate(payload)
        validation = self.test_config(str(candidate_path))
        if not validation.ok:
            return XrayApplyResult(
                ok=False,
                message="Xray binding candidate failed validation.",
                error_code=validation.error_code or "XRAY_BINDING_TEST_FAILED",
                details={
                    "stage": "test_config",
                    "candidate_path": str(candidate_path),
                    "bindings_count": len(bindings),
                    "metadata_applied_count": metadata_applied_count,
                    "routing_applied_count": routing_applied_count,
                    "applied_count": applied_count,
                    "egress": egress_details,
                    "validation": validation.details,
                },
            )

        self._write_active_config(payload)
        reload_result = self.reload()
        return XrayApplyResult(
            ok=reload_result.ok,
            message=(
                "Xray binding metadata and managed egress materialized."
                if reload_result.ok
                else "Xray binding metadata and managed egress were saved, but runtime reload failed."
            ),
            error_code=None if reload_result.ok else reload_result.error_code or "XRAY_RELOAD_FAILED",
            details={
                "stage": "completed" if reload_result.ok else "reload",
                "candidate_path": str(candidate_path),
                "bindings_count": len(bindings),
                "metadata_applied_count": metadata_applied_count,
                "routing_applied_count": routing_applied_count,
                "applied_count": applied_count,
                "egress": egress_details,
                "reload": reload_result.details,
            },
        )


DEFAULT_XRAY_ADAPTER: XrayAdapter = RealXrayAdapter()
