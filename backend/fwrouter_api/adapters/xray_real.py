from __future__ import annotations

import json
import hashlib
import io
import os
import selectors
import subprocess
import tarfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from fwrouter_api.adapters.xray_common import (
    XRAY_API_PORT,
    XRAY_API_TAG,
    XRAY_COMPOSE_PATH,
    XRAY_CONTAINER_NAME,
    XRAY_EXPLICIT_DIRECT_OUTBOUND_TAG,
    XRAY_FALLBACK_OUTBOUND_TAG,
    XRAY_INBOUND_TAG,
    XRAY_LOG_ROOT,
    XRAY_MANAGED_DNS_OUTBOUND_TAG,
    XRAY_PUBLIC_HOST,
    XRAY_PUBLIC_PATH,
    XRAY_PUBLIC_PORT,
    XRAY_TRANSPORT,
    XrayAdapter,
    XrayAdapterError,
    XrayApplyResult,
    XrayClient,
    XrayHealth,
    XrayRuntimeState,
    _coerce_runner_result,
    _default_email,
    _default_xray_config_path,
    _json_dump,
    xray_writer_guarded,
)
from fwrouter_api.services.artifacts import atomic_write_text
from fwrouter_api.services.xray_handoff import (
    XRAY_MANAGED_EGRESS_PREFIX,
    XRAY_MIHOMO_HANDOFF_HOST,
    build_xray_handoff_assignments,
)
from fwrouter_api.services.xray_subscription import build_xray_vless_uri
from fwrouter_api.services.xray_subscription import configured_xray_public_endpoint


DOCKER_CLI_STATE_DIR = Path("/run/fwrouter-v2/docker-cli")


def _atomic_write_text(path: Path, text: str) -> None:
    from fwrouter_api.adapters import xray as xray_facade

    writer = getattr(xray_facade, "atomic_write_text", atomic_write_text)
    writer(path, text)


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
        self._last_good_config_text: str | None = None

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
        elif action == "api_inbound_users":
            command = [
                "docker", "compose", "-f", str(self.compose_path),
                "exec", "-T", XRAY_CONTAINER_NAME, "xray", "api", "inbounduser",
                "--server=127.0.0.1:10085", "-timeout=3", f"-tag={XRAY_INBOUND_TAG}",
            ]
        elif action == "runtime_container_id":
            command = [
                "docker", "compose", "-f", str(self.compose_path),
                "ps", "-q", XRAY_CONTAINER_NAME,
            ]
        elif action == "runtime_inspect":
            container_id = str(payload.get("container_id") or "")
            if not container_id or any(char not in "0123456789abcdefABCDEF" for char in container_id):
                raise XrayAdapterError(
                    "XRAY_RUNTIME_ID_INVALID", "Xray runtime container identity is invalid.",
                )
            command = [
                "docker", "inspect", "--format", "{{.Id}}|{{.State.Running}}|{{.State.StartedAt}}", container_id,
            ]
        elif action == "runtime_config_archive":
            container_id = str(payload.get("container_id") or "")
            if not container_id or any(char not in "0123456789abcdefABCDEF" for char in container_id):
                raise XrayAdapterError("XRAY_RUNTIME_ID_INVALID", "Xray runtime identity is invalid.")
            command = ["docker", "cp", f"{container_id}:/etc/xray/config.json", "-"]
        else:
            raise XrayAdapterError(
                "XRAY_RUNNER_ACTION_UNKNOWN",
                f"Unknown Xray runner action: {action}",
                details={"action": action},
            )

        DOCKER_CLI_STATE_DIR.mkdir(parents=True, exist_ok=True)
        env = {
            **os.environ,
            "DOCKER_CONFIG": str(DOCKER_CLI_STATE_DIR),
            "HOME": str(DOCKER_CLI_STATE_DIR),
        }

        if action == "runtime_config_archive":
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env, bufsize=0)
            output = bytearray()
            try:
                assert process.stdout is not None
                with selectors.DefaultSelector() as selector:
                    selector.register(process.stdout, selectors.EVENT_READ)
                    deadline = time.monotonic() + 5.0
                    while selector.get_map():
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            process.kill()
                            process.wait(timeout=1)
                            return XrayApplyResult(ok=False, message="Mounted Xray config read timed out.",
                                                   error_code="XRAY_RUNTIME_READBACK_TIMEOUT", details={"action": action})
                        if not selector.select(remaining):
                            continue
                        chunk = process.stdout.read(64 * 1024)
                        if not chunk:
                            selector.unregister(process.stdout)
                            break
                        output.extend(chunk)
                        if len(output) > 4 * 1024 * 1024:
                            process.kill()
                            process.wait(timeout=1)
                            return XrayApplyResult(ok=False, message="Mounted Xray config archive exceeded the size limit.",
                                                   error_code="XRAY_RUNTIME_CONFIG_TOO_LARGE", details={"action": action})
                    return_code = process.wait(timeout=max(0.1, deadline - time.monotonic()))
            except (OSError, subprocess.TimeoutExpired):
                process.kill()
                process.wait(timeout=1)
                return XrayApplyResult(ok=False, message="Mounted Xray config read failed.",
                                       error_code="XRAY_RUNTIME_CONFIG_READ_FAILED", details={"action": action})
            return XrayApplyResult(ok=return_code == 0, message="Mounted Xray config archive read.",
                                   error_code=None if return_code == 0 else "XRAY_RUNTIME_CONFIG_READ_FAILED",
                                   details={"archive_bytes": bytes(output)})

        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                env=env,
                timeout=8 if action == "api_inbound_users" else 3 if action in {
                    "runtime_container_id", "runtime_inspect",
                } else None,
            )
        except subprocess.TimeoutExpired:
            return XrayApplyResult(
                ok=False,
                message=("Timed out reading loaded Xray inbound users." if action == "api_inbound_users"
                         else "Timed out reading Xray runtime state."),
                error_code=("XRAY_API_READBACK_TIMEOUT" if action == "api_inbound_users"
                            else "XRAY_RUNTIME_READBACK_TIMEOUT"),
                details={"action": action},
            )
        if action == "api_inbound_users" and len(completed.stdout or "") > 4 * 1024 * 1024:
            return XrayApplyResult(
                ok=False,
                message="Loaded Xray inbound user response exceeded the size limit.",
                error_code="XRAY_API_READBACK_TOO_LARGE",
                details={"action": action},
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
        _atomic_write_text(candidate_path, _json_dump(payload))
        return candidate_path

    def _write_active_config(self, payload: dict[str, Any]) -> None:
        _atomic_write_text(self.config_path, _json_dump(payload))

    def _remember_active_config(self) -> str:
        text = self.config_path.read_text(encoding="utf-8")
        self._last_good_config_text = text
        return text

    def restore_last_good_config(self) -> XrayApplyResult:
        if self._last_good_config_text is None:
            return XrayApplyResult(
                ok=False,
                message="No last-good Xray config is available for rollback.",
                error_code="XRAY_LAST_GOOD_CONFIG_UNAVAILABLE",
            )
        _atomic_write_text(self.config_path, self._last_good_config_text)
        reload_result = self.reload()
        return XrayApplyResult(
            ok=reload_result.ok,
            message="Xray last-good config restored." if reload_result.ok else "Xray last-good config was written but reload failed.",
            error_code=None if reload_result.ok else reload_result.error_code or "XRAY_ROLLBACK_RELOAD_FAILED",
            details={"reload": reload_result.details},
        )

    def _active_config_matches(self, text: str) -> bool:
        try:
            return self.config_path.read_text(encoding="utf-8") == text
        except FileNotFoundError:
            return False

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

    def _explicit_direct_outbound(self) -> dict[str, Any]:
        return {"tag": XRAY_EXPLICIT_DIRECT_OUTBOUND_TAG, "protocol": "freedom", "settings": {}}

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
            "services": ["StatsService", "HandlerService"],
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
            or tag == XRAY_EXPLICIT_DIRECT_OUTBOUND_TAG
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
            or outbound_tag == XRAY_EXPLICIT_DIRECT_OUTBOUND_TAG
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
        client_modes: list[dict[str, Any]],
        handoff_assignments: list[dict[str, Any]] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        self._ensure_runtime_stats(payload)
        existing_outbounds = payload.get("outbounds") if isinstance(payload.get("outbounds"), list) else []
        preserved_outbounds = [
            outbound
            for outbound in existing_outbounds
            if isinstance(outbound, dict) and not self._is_managed_outbound(outbound)
        ]

        handoff_assignments = (
            build_xray_handoff_assignments(bindings)
            if handoff_assignments is None
            else [dict(item) for item in handoff_assignments]
        )
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

        managed_mode_rules: list[dict[str, Any]] = []
        for directive in client_modes:
            email = str(directive.get("client_email") or "").strip()
            mode = str(directive.get("effective_mode") or "").strip().lower()
            if not email or mode not in {"direct", "disabled", "unsupported_selective"}:
                continue
            managed_mode_rules.append({
                "type": "field",
                "inboundTag": [XRAY_INBOUND_TAG],
                "user": [email],
                "outboundTag": (
                    XRAY_EXPLICIT_DIRECT_OUTBOUND_TAG if mode == "direct"
                    else XRAY_FALLBACK_OUTBOUND_TAG
                ),
            })
        managed_rules = managed_mode_rules + self._managed_routing_rules_from_bindings(
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
            self._explicit_direct_outbound(),
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
                }
                applied_count += 1
            updated_clients.append(updated)

        return updated_clients, applied_count

    def health(self) -> XrayHealth:
        public_endpoint = configured_xray_public_endpoint()
        details = {
            "adapter": "xray",
            "config_path": str(self.config_path),
            "compose_path": str(self.compose_path),
            "public_host": public_endpoint["host"],
            "public_path": public_endpoint["path"],
            "public_port": public_endpoint["port"],
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

    def list_loaded_client_identities(self) -> list[tuple[str, str]]:
        """Read exact VLESS user identities from the running Xray HandlerService."""
        result = self._run("api_inbound_users", tag=XRAY_INBOUND_TAG)
        if not result.ok:
            raise XrayAdapterError(
                result.error_code or "XRAY_API_READBACK_FAILED",
                "Could not read loaded Xray inbound users.",
                details={"stage": "loaded_user_readback"},
            )
        output = str(result.details.get("stdout") or "")
        if len(output) > 4 * 1024 * 1024:
            raise XrayAdapterError(
                "XRAY_API_READBACK_TOO_LARGE",
                "Loaded Xray inbound user response exceeded the size limit.",
                details={"stage": "loaded_user_readback"},
            )
        try:
            payload = json.loads(output)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise XrayAdapterError(
                "XRAY_API_READBACK_INVALID_JSON",
                "Loaded Xray inbound user response was not valid JSON.",
                details={"stage": "loaded_user_readback"},
            ) from exc
        users = payload.get("users") if isinstance(payload, dict) else None
        if not isinstance(users, list):
            raise XrayAdapterError(
                "XRAY_API_READBACK_INVALID_SHAPE",
                "Loaded Xray inbound user response had an invalid shape.",
                details={"stage": "loaded_user_readback"},
            )
        identities: list[tuple[str, str]] = []
        for user in users:
            account = user.get("account") if isinstance(user, dict) else None
            if not isinstance(account, dict) or account.get("_TypedMessage_") != "xray.proxy.vless.Account":
                raise XrayAdapterError(
                    "XRAY_API_READBACK_INVALID_ACCOUNT",
                    "Loaded Xray inbound user response contained an unsupported account type.",
                    details={"stage": "loaded_user_readback"},
                )
            email = str(user.get("email") or "").strip()
            client_uuid = str(account.get("id") or "").strip()
            if not email or not client_uuid:
                raise XrayAdapterError(
                    "XRAY_API_READBACK_INVALID_IDENTITY",
                    "Loaded Xray inbound user response contained an incomplete identity.",
                    details={"stage": "loaded_user_readback"},
                )
            identities.append((client_uuid, email))
        return sorted(identities)

    def get_runtime_incarnation(self) -> str:
        """Return a stable opaque token for the currently running Xray process."""
        listed = self._run("runtime_container_id")
        container_id = str(listed.details.get("stdout") or "").strip().splitlines()
        if not listed.ok or not container_id:
            raise XrayAdapterError(
                listed.error_code or "XRAY_RUNTIME_ID_UNAVAILABLE",
                "Could not identify the running Xray container.",
                details={"stage": "runtime_incarnation"},
            )
        inspect = self._run("runtime_inspect", container_id=container_id[-1])
        value = str(inspect.details.get("stdout") or "").strip()
        parts = value.split("|", 2)
        if not inspect.ok or len(parts) != 3 or not parts[0] or not parts[1] or not parts[2]:
            raise XrayAdapterError(
                inspect.error_code or "XRAY_RUNTIME_ID_UNAVAILABLE",
                "Could not read the running Xray process incarnation.",
                details={"stage": "runtime_incarnation"},
            )
        try:
            started_at = datetime.fromisoformat(parts[2].replace("Z", "+00:00"))
        except ValueError:
            started_at = None
        if parts[1] != "true" or started_at is None or started_at.year <= 1:
            raise XrayAdapterError(
                "XRAY_RUNTIME_NOT_RUNNING",
                "Xray container is not running with a valid process start time.",
                details={"stage": "runtime_incarnation"},
            )
        if not parts[0].startswith(container_id[-1]):
            raise XrayAdapterError(
                "XRAY_RUNTIME_ID_CHANGED_DURING_READ",
                "Xray container identity changed while reading its incarnation.",
                details={"stage": "runtime_incarnation"},
            )
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def get_runtime_config_sha256(self) -> str:
        """Hash mounted config bytes without relying on tools in the Xray image."""
        listed = self._run("runtime_container_id")
        ids = str(listed.details.get("stdout") or "").strip().splitlines()
        if not listed.ok or not ids:
            raise XrayAdapterError(
                listed.error_code or "XRAY_RUNTIME_ID_UNAVAILABLE",
                "Could not identify Xray runtime for mounted config readback.",
                details={"stage": "runtime_config_digest"},
            )
        result = self._run("runtime_config_archive", container_id=ids[-1])
        archive = result.details.get("archive_bytes")
        config_bytes: bytes | None = None
        if result.ok and isinstance(archive, bytes):
            try:
                with tarfile.open(fileobj=io.BytesIO(archive), mode="r:*") as tar:
                    members = tar.getmembers()
                    if len(members) == 1:
                        member = members[0]
                        if member.isfile() and member.name == "config.json" and 0 < member.size <= 4 * 1024 * 1024:
                            stream = tar.extractfile(member)
                            value = stream.read(4 * 1024 * 1024 + 1) if stream is not None else b""
                            if len(value) == member.size and len(value) <= 4 * 1024 * 1024:
                                config_bytes = value
            except (tarfile.TarError, OSError, EOFError):
                config_bytes = None
        if config_bytes is None:
            raise XrayAdapterError(
                result.error_code or "XRAY_RUNTIME_CONFIG_DIGEST_UNAVAILABLE",
                "Could not verify the mounted Xray configuration digest.",
                details={"stage": "runtime_config_digest"},
            )
        return hashlib.sha256(config_bytes).hexdigest()

    @xray_writer_guarded
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

    @xray_writer_guarded
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

    @xray_writer_guarded
    def reconcile_clients(
        self,
        *,
        desired_clients: list[dict[str, Any]],
        managed_email_prefixes: list[str] | None = None,
    ) -> XrayApplyResult:
        payload, inbound, clients = self._load_clients_and_config()
        self._remember_active_config()
        raw_clients = [
            dict(raw_client)
            for raw_client in list((inbound.get("settings") or {}).get("clients") or [])
            if isinstance(raw_client, dict)
        ]
        prefixes = tuple(str(prefix or "").strip().lower() for prefix in (managed_email_prefixes or []) if str(prefix or "").strip())
        desired_by_email = {
            str(item.get("email") or "").strip().lower(): item
            for item in desired_clients
            if str(item.get("email") or "").strip()
        }
        existing_by_email = {
            str(client.email or "").strip().lower(): client
            for client in clients
            if str(client.email or "").strip()
        }

        created: list[dict[str, Any]] = []
        deleted: list[dict[str, Any]] = []
        recreated: list[dict[str, Any]] = []
        next_raw_clients: list[dict[str, Any]] = []

        for raw_client in raw_clients:
            email = str(raw_client.get("email") or "").strip().lower()
            desired = desired_by_email.get(email)
            managed = bool(email and any(email.startswith(prefix) for prefix in prefixes))
            existing_uuid = str(raw_client.get("id") or "").strip()
            desired_uuid = str((desired or {}).get("client_uuid") or (desired or {}).get("client_id") or "").strip()

            if managed and desired is None:
                deleted.append(
                    {
                        "client_id": existing_uuid,
                        "client_uuid": existing_uuid,
                        "email": raw_client.get("email"),
                    }
                )
                continue

            if desired is not None and desired_uuid and existing_uuid != desired_uuid:
                recreated.append(
                    {
                        "email": raw_client.get("email"),
                        "old_client_uuid": existing_uuid,
                        "new_client_uuid": desired_uuid,
                    }
                )
                continue

            next_raw_clients.append(raw_client)

        next_by_email = {
            str(raw_client.get("email") or "").strip().lower(): raw_client
            for raw_client in next_raw_clients
            if str(raw_client.get("email") or "").strip()
        }
        for email, desired in desired_by_email.items():
            if email in next_by_email:
                raw_client = next_by_email[email]
                alias = str(desired.get("alias") or "").strip()
                if alias:
                    raw_client["fwrouterAlias"] = alias
                continue

            desired_uuid = str(desired.get("client_uuid") or desired.get("client_id") or uuid4()).strip()
            alias = str(desired.get("alias") or "").strip()
            raw_client = {
                "id": desired_uuid,
                "email": str(desired.get("email") or email),
            }
            if alias:
                raw_client["fwrouterAlias"] = alias
            next_raw_clients.append(raw_client)
            created.append(
                {
                    "client_id": desired_uuid,
                    "client_uuid": desired_uuid,
                    "email": str(desired.get("email") or email),
                }
            )

        inbound.setdefault("settings", {})["clients"] = next_raw_clients
        next_config_text = _json_dump(payload)
        if self._active_config_matches(next_config_text):
            return XrayApplyResult(
                ok=True,
                message="Xray clients already reconciled.",
                details={
                    "stage": "unchanged",
                    "config_changed": False,
                    "desired_clients_count": len(desired_by_email),
                    "created": [],
                    "deleted": [],
                    "recreated": [],
                    "reload": {"skipped": True, "reason": "config_unchanged"},
                },
            )

        candidate_path = self._persist_candidate(payload)
        validation = self.test_config(str(candidate_path))
        if not validation.ok:
            return XrayApplyResult(
                ok=False,
                message="Xray client reconciliation candidate failed validation.",
                error_code=validation.error_code or "XRAY_CLIENT_RECONCILE_TEST_FAILED",
                details={
                    "stage": "test_config",
                    "candidate_path": str(candidate_path),
                    "desired_clients_count": len(desired_by_email),
                    "created": created,
                    "deleted": deleted,
                    "recreated": recreated,
                    "validation": validation.details,
                },
            )

        self._write_active_config(payload)
        reload_result = self.reload()
        rollback = None
        if not reload_result.ok:
            rollback = self.restore_last_good_config()
        return XrayApplyResult(
            ok=reload_result.ok,
            message=(
                "Xray clients reconciled."
                if reload_result.ok
                else "Xray clients were saved, but runtime reload failed."
            ),
            error_code=None if reload_result.ok else reload_result.error_code or "XRAY_RELOAD_FAILED",
            details={
                "stage": "completed" if reload_result.ok else "reload",
                "candidate_path": str(candidate_path),
                "config_changed": True,
                "desired_clients_count": len(desired_by_email),
                "created": created,
                "deleted": deleted,
                "recreated": recreated,
                "reload": reload_result.details,
                "rollback": rollback.details if rollback is not None else None,
            },
        )

    def stage_subscription_generation(
        self,
        *,
        desired_clients: list[dict[str, Any]],
        managed_email_prefixes: list[str],
        bindings: list[dict[str, Any]],
        client_modes: list[dict[str, Any]],
        handoff_assignments: list[dict[str, Any]] | None = None,
        candidate_path: Path | None = None,
    ) -> XrayApplyResult:
        """Build and native-test the exact prospective Xray config without applying it."""
        payload, inbound, _ = self._load_clients_and_config()
        self._ensure_managed_inbound_tag(inbound)
        prefixes = tuple(str(prefix or "").strip().lower() for prefix in managed_email_prefixes if str(prefix or "").strip())
        desired_by_email = {
            str(item.get("email") or "").strip().lower(): item
            for item in desired_clients
            if str(item.get("email") or "").strip()
        }
        next_clients: list[dict[str, Any]] = []
        for raw in list((inbound.get("settings") or {}).get("clients") or []):
            if not isinstance(raw, dict):
                continue
            email = str(raw.get("email") or "").strip().lower()
            desired = desired_by_email.get(email)
            if email and any(email.startswith(prefix) for prefix in prefixes) and desired is None:
                continue
            updated = dict(raw)
            if desired is not None:
                expected_id = str(desired.get("client_uuid") or desired.get("client_id") or "").strip()
                if expected_id and str(raw.get("id") or "").strip() != expected_id:
                    continue
                alias = str(desired.get("alias") or "").strip()
                if alias:
                    updated["fwrouterAlias"] = alias
            next_clients.append(updated)
        have_by_email = {str(item.get("email") or "").strip().lower() for item in next_clients}
        for email, desired in desired_by_email.items():
            if email in have_by_email:
                continue
            client_uuid = str(desired.get("client_uuid") or desired.get("client_id") or "").strip()
            if not client_uuid:
                return XrayApplyResult(ok=False, message="Staged Xray client has no UUID.", error_code="XRAY_STAGE_CLIENT_UUID_MISSING")
            raw = {"id": client_uuid, "email": str(desired.get("email") or email)}
            alias = str(desired.get("alias") or "").strip()
            if alias:
                raw["fwrouterAlias"] = alias
            next_clients.append(raw)
        inbound.setdefault("settings", {})["clients"] = next_clients
        updated_clients, metadata_count = self._materialize_client_binding_metadata(
            raw_clients=next_clients,
            bindings=bindings,
        )
        inbound["settings"]["clients"] = updated_clients
        routing_count, egress = self._materialize_managed_egress(
            payload=payload,
            bindings=bindings,
            client_modes=client_modes,
            handoff_assignments=handoff_assignments,
        )
        resolved_path = candidate_path or self._candidate_path()
        _atomic_write_text(resolved_path, _json_dump(payload))
        staged_text = resolved_path.read_text(encoding="utf-8")
        staged_sha256 = hashlib.sha256(staged_text.encode("utf-8")).hexdigest()
        validation = self.test_config(str(resolved_path))
        if not validation.ok:
            return XrayApplyResult(
                ok=False,
                message="Staged Xray subscription generation failed native validation.",
                error_code=validation.error_code or "XRAY_GENERATION_CANDIDATE_INVALID",
                details={"stage": "native_validation", "validation": validation.details},
            )
        if hashlib.sha256(resolved_path.read_bytes()).hexdigest() != staged_sha256:
            return XrayApplyResult(
                ok=False,
                message="Staged Xray candidate changed during native validation.",
                error_code="XRAY_STAGE_CANDIDATE_CHANGED_DURING_VALIDATION",
            )
        return XrayApplyResult(
            ok=True,
            message="Staged Xray subscription generation passed native validation.",
            details={
                "stage": "validated",
                "candidate_path": str(resolved_path),
                "candidate_sha256": staged_sha256,
                "desired_clients_count": len(desired_by_email),
                "metadata_applied_count": metadata_count,
                "routing_applied_count": routing_count,
                "egress": egress,
                "expected_client_identities": sorted(
                    (str(item.get("id") or "").strip(), str(item.get("email") or "").strip())
                    for item in updated_clients
                    if str(item.get("id") or "").strip() and str(item.get("email") or "").strip()
                ),
            },
        )

    def apply_staged_subscription_generation(
        self,
        candidate_path: str | Path,
        *,
        expected_sha256: str,
    ) -> XrayApplyResult:
        """Apply bytes previously validated by `stage_subscription_generation`."""
        try:
            text = Path(candidate_path).read_text(encoding="utf-8")
            payload = json.loads(text)
        except Exception as exc:
            return XrayApplyResult(ok=False, message="Staged Xray candidate is unavailable.", error_code="XRAY_STAGE_CANDIDATE_UNREADABLE", details={"error": str(exc)})
        if not isinstance(payload, dict):
            return XrayApplyResult(ok=False, message="Staged Xray candidate is invalid.", error_code="XRAY_STAGE_CANDIDATE_INVALID")
        if hashlib.sha256(text.encode("utf-8")).hexdigest() != str(expected_sha256 or ""):
            return XrayApplyResult(ok=False, message="Staged Xray candidate changed after validation.", error_code="XRAY_STAGE_CANDIDATE_CHANGED_AFTER_VALIDATION")
        if self.config_path.exists() and hashlib.sha256(self.config_path.read_bytes()).hexdigest() == expected_sha256:
            return XrayApplyResult(
                ok=True,
                message="Staged Xray candidate already matches the active config.",
                details={"stage": "unchanged", "reload": {"skipped": True, "reason": "config_unchanged"}},
            )
        self._remember_active_config()
        _atomic_write_text(self.config_path, text)
        reload_result = self.reload()
        return XrayApplyResult(
            ok=reload_result.ok,
            message="Staged Xray generation applied." if reload_result.ok else "Staged Xray generation reload failed.",
            error_code=None if reload_result.ok else reload_result.error_code or "XRAY_RELOAD_FAILED",
            details={"stage": "applied" if reload_result.ok else "reload", "reload": reload_result.details},
        )

    @xray_writer_guarded
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

    @xray_writer_guarded
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

    @xray_writer_guarded
    def materialize_client_bindings(
        self,
        bindings: list[dict[str, Any]],
        *,
        client_modes: list[dict[str, Any]] | None = None,
        force_reload: bool = False,
    ) -> XrayApplyResult:
        client_modes = client_modes or []
        payload, inbound, _ = self._load_clients_and_config()
        self._remember_active_config()
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
            client_modes=client_modes,
        )
        applied_count = min(metadata_applied_count, routing_applied_count)

        next_config_text = _json_dump(payload)
        if not force_reload and self._active_config_matches(next_config_text):
            return XrayApplyResult(
                ok=True,
                message="Xray binding metadata and managed egress already materialized.",
                details={
                    "stage": "unchanged",
                    "config_changed": False,
                    "force_reload": False,
                    "bindings_count": len(bindings),
                    "client_modes_count": len(client_modes),
                    "metadata_applied_count": metadata_applied_count,
                    "routing_applied_count": routing_applied_count,
                    "applied_count": applied_count,
                    "egress": egress_details,
                    "reload": {"skipped": True, "reason": "config_unchanged"},
                },
            )

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
                    "client_modes_count": len(client_modes),
                    "metadata_applied_count": metadata_applied_count,
                    "routing_applied_count": routing_applied_count,
                    "applied_count": applied_count,
                    "egress": egress_details,
                    "validation": validation.details,
                },
            )

        self._write_active_config(payload)
        reload_result = self.reload()
        rollback = None
        if not reload_result.ok:
            rollback = self.restore_last_good_config()
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
                "config_changed": True,
                "force_reload": force_reload,
                "bindings_count": len(bindings),
                "client_modes_count": len(client_modes),
                "metadata_applied_count": metadata_applied_count,
                "routing_applied_count": routing_applied_count,
                "applied_count": applied_count,
                "egress": egress_details,
                "reload": reload_result.details,
                "rollback": rollback.details if rollback is not None else None,
            },
        )



DEFAULT_XRAY_ADAPTER: XrayAdapter = RealXrayAdapter()
