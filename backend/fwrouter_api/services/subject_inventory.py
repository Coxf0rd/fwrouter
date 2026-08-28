from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fwrouter_api.adapters.scripts import DEFAULT_SCRIPT_RUNNER, ScriptResult, ScriptRunnerError
from fwrouter_api.adapters.xray import DEFAULT_XRAY_ADAPTER
from fwrouter_api.core.config import get_settings
from fwrouter_api.db.connection import db_session
from fwrouter_api.services.external_ingress import external_ingress_clients_from_script_result
from fwrouter_api.services.logs import write_operational_log, write_technical_log
from fwrouter_api.services.subject_taxonomy import (
    EXPLICIT_EXTERNAL_CLIENT_SUBJECT_TYPE,
    EXTERNAL_NETWORK_CLIENT_SUBJECT_TYPE,
    EXTERNAL_INGRESS_SUBJECT_TYPES,
    external_ingress_contract,
)


CLIENT_SUBJECT_TYPES = {"lan", EXTERNAL_NETWORK_CLIENT_SUBJECT_TYPE, EXPLICIT_EXTERNAL_CLIENT_SUBJECT_TYPE}
SYSTEM_SUBJECT_TYPES = {"docker", "host", "fwrouter"}
DEFAULT_DESIRED_MODE_BY_TYPE = {
    "lan": "global",
    EXTERNAL_NETWORK_CLIENT_SUBJECT_TYPE: "global",
    EXPLICIT_EXTERNAL_CLIENT_SUBJECT_TYPE: "enabled",
    "docker": "direct",
    "host": "direct",
    "fwrouter": "direct",
}
DETAIL_TABLE_BY_TYPE = {
    "lan": "subject_lan",
    "docker": "subject_docker",
    "host": "subject_host",
    "fwrouter": "subject_fwrouter",
}
INACTIVE_RUNTIME_BY_TYPE = {
    "lan": "inactive",
    EXTERNAL_NETWORK_CLIENT_SUBJECT_TYPE: "inactive",
    EXPLICIT_EXTERNAL_CLIENT_SUBJECT_TYPE: "inactive",
    "docker": "missing",
    "host": "missing",
    "fwrouter": "missing",
}
SUBJECT_ROLE_BY_TYPE = {
    "lan": "lan_client",
    "tailscale": "external_network_source",
    "tailscale_node": "external_network_source",
    EXTERNAL_NETWORK_CLIENT_SUBJECT_TYPE: "external_network_source",
    "xray": "vless_client",
    EXPLICIT_EXTERNAL_CLIENT_SUBJECT_TYPE: "vless_client",
    "docker": "docker_runtime",
    "host": "host_runtime",
    "fwrouter": "router_core",
}


@dataclass(frozen=True)
class SubjectInventoryRecord:
    subject_id: str
    subject_type: str
    stable_key: str
    display_name: str
    desired_mode: str
    runtime_state: str
    is_active: bool
    alias: str | None
    metadata: dict[str, Any]
    detail: dict[str, Any]
    implementation_kind: str | None = None


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_dumps(value: dict[str, Any] | None) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _sql_value(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return value


def _subject_role(subject_type: str) -> str:
    return SUBJECT_ROLE_BY_TYPE.get(str(subject_type or ""), "unknown")


def _safe_slug(value: str) -> str:
    return "".join(character.lower() if character.isalnum() else "-" for character in value).strip("-")


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        parsed: dict[str, Any] = {}
        for chunk in value.split(","):
            item = chunk.strip()
            if not item or "=" not in item:
                continue
            try:
                key, raw_value = item.split("=", 1)
                parsed[key.strip()] = raw_value.strip()
            except ValueError:
                continue
        return parsed
    return {"value": value}


def _load_json_lines(stdout: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        raw = line.strip()
        if not raw:
            continue
        try:
            loaded = json.loads(raw)
            if isinstance(loaded, dict):
                items.append(loaded)
        except json.JSONDecodeError:
            continue
    return items


def _docker_subject_id(item: dict[str, Any]) -> str:
    labels = _as_dict(item.get("Labels") or item.get("Label") or {})
    project = str(
        labels.get("com.docker.compose.project")
        or item.get("Project")
        or item.get("ComposeProject")
        or ""
    ).strip()
    service = str(
        labels.get("com.docker.compose.service")
        or item.get("Service")
        or item.get("ComposeService")
        or ""
    ).strip()
    container_name = str(item.get("Names") or item.get("Name") or item.get("container_name") or "").strip()
    if project and service:
        return f"docker:{_safe_slug(project)}:{_safe_slug(service)}"
    stable = container_name or str(item.get("ID") or item.get("Id") or "container")
    return f"docker:{_safe_slug(stable)}"


def _extract_docker_records(result: ScriptResult) -> tuple[list[SubjectInventoryRecord], dict[str, Any]]:
    rows = _load_json_lines(result.stdout)
    records: list[SubjectInventoryRecord] = []

    for item in rows:
        if not isinstance(item, dict):
            continue
        labels = _as_dict(item.get("Labels") or item.get("Label") or {})
        project = str(labels.get("com.docker.compose.project") or item.get("Project") or "").strip()
        service = str(labels.get("com.docker.compose.service") or item.get("Service") or "").strip()
        container_name = str(item.get("Names") or item.get("Name") or "").strip()
        container_id = str(item.get("ID") or item.get("Id") or "").strip()
        image_name = str(item.get("Image") or item.get("ImageName") or "").strip()
        display_name = service or container_name or image_name or container_id or "docker-service"
        stable_key = _docker_subject_id(item)
        subject_id = stable_key
        metadata = {
            "source": result.script_id,
            "status": item.get("State") or item.get("Status"),
            "network_mode": item.get("NetworkMode"),
            "collected_at": _utc_timestamp(),
        }
        detail = {
            "compose_project": project or None,
            "compose_service": service or None,
            "container_name": container_name or None,
            "container_id": container_id or None,
            "image_name": image_name or None,
            "ip_address": item.get("IPAddress") or None,
            "network_name": item.get("NetworkName") or None,
            "source_json": item,
        }
        records.append(
            SubjectInventoryRecord(
                subject_id=subject_id,
                subject_type="docker",
                stable_key=stable_key,
                display_name=display_name,
                desired_mode=DEFAULT_DESIRED_MODE_BY_TYPE["docker"],
                runtime_state="running",
                is_active=True,
                alias=container_name or None,
                metadata=metadata,
                detail=detail,
            )
        )

    return records, {"script_id": result.script_id, "rows_count": len(rows)}


def _discover_lan_records() -> list[SubjectInventoryRecord]:
    """Parse dnsmasq.leases to discover LAN clients."""
    leases_path = Path("/var/lib/misc/dnsmasq.leases")
    if not leases_path.exists():
        return []

    records: list[SubjectInventoryRecord] = []
    try:
        content = leases_path.read_text(encoding="utf-8")
        for line in content.splitlines():
            parts = line.split()
            if len(parts) < 4:
                continue
            # expiry mac ip hostname client_id
            mac = parts[1].strip().lower()
            ip = parts[2].strip()
            hostname = parts[3].strip()
            
            if hostname == "*":
                hostname = ""

            stable = mac or ip or hostname
            if not stable:
                continue
            
            subject_id = f"lan:{_safe_slug(stable)}"
            records.append(
                SubjectInventoryRecord(
                    subject_id=subject_id,
                    subject_type="lan",
                    stable_key=subject_id,
                    display_name=hostname or ip or mac,
                    desired_mode=DEFAULT_DESIRED_MODE_BY_TYPE["lan"],
                    runtime_state="active",
                    is_active=True,
                    alias=hostname or None,
                    metadata={"source": "dnsmasq_leases", "collected_at": _utc_timestamp()},
                    detail={
                        "mac_address": mac or None,
                        "ip_address": ip or None,
                        "hostname": hostname or None,
                        "dhcp_hostname": hostname or None,
                        "source_json": {"line": line},
                    },
                )
            )
    except Exception:
        pass
    return records


def _structured_lan_records(items: list[dict[str, Any]]) -> list[SubjectInventoryRecord]:
    records: list[SubjectInventoryRecord] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        mac = str(item.get("mac_address") or item.get("mac") or "").strip().lower()
        ip = str(item.get("ip_address") or item.get("ip") or "").strip()
        hostname = str(item.get("hostname") or item.get("display_name") or ip or mac).strip()
        stable = mac or ip or hostname
        if not stable:
            continue
        subject_id = f"lan:{_safe_slug(stable)}"
        records.append(
            SubjectInventoryRecord(
                subject_id=subject_id,
                subject_type="lan",
                stable_key=subject_id,
                display_name=hostname or stable,
                desired_mode=DEFAULT_DESIRED_MODE_BY_TYPE["lan"],
                runtime_state="active",
                is_active=True,
                alias=hostname or None,
                metadata={"source": "structured_input", "collected_at": _utc_timestamp()},
                detail={
                    "mac_address": mac or None,
                    "ip_address": ip or None,
                    "hostname": hostname or None,
                    "dhcp_hostname": item.get("dhcp_hostname"),
                    "source_json": item,
                },
            )
        )
    return records


def _external_ingress_records(
    provider: str,
    items: list[dict[str, Any]],
    *,
    connection_id: str | None = None,
) -> list[SubjectInventoryRecord]:
    contract = external_ingress_contract(provider)
    if contract is None:
        return []

    subject_type = str(contract["subject_type"])
    implementation_kind = str(contract.get("implementation_kind") or provider)
    records: list[SubjectInventoryRecord] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        item_connection_id = str(item.get("connection_id") or connection_id or "").strip()
        subject_id_prefix = str(
            item.get("subject_id_prefix")
            or (f"{item_connection_id}:" if item_connection_id else contract.get("subject_id_prefix"))
            or f"{provider}:"
        )
        node_id = str(item.get("provider_node_id") or item.get("node_id") or item.get("id") or "").strip()
        provider_ip = str(item.get("ip_address") or item.get("tailscale_ip") or item.get("ip") or "").strip()
        hostname = str(item.get("hostname") or item.get("display_name") or item.get("name") or "").strip()
        stable = str(item.get("stable_key") or node_id or provider_ip or hostname).strip()
        if not stable:
            continue
        subject_id = f"{subject_id_prefix}{_safe_slug(stable)}"
        online = bool(item.get("online", True))
        records.append(
            SubjectInventoryRecord(
                subject_id=subject_id,
                subject_type=subject_type,
                stable_key=subject_id,
                display_name=hostname or provider_ip or node_id,
                desired_mode=DEFAULT_DESIRED_MODE_BY_TYPE[subject_type],
                runtime_state="active" if online else "inactive",
                is_active=online,
                alias=hostname or None,
                metadata={
                    "source": item.get("source") or provider,
                    "provider": provider,
                    "connection_id": item_connection_id or None,
                    "routing_hint": bool(item.get("routing_hint")),
                    "import_reason": item.get("import_reason"),
                    "collected_at": _utc_timestamp(),
                },
                detail={
                    "node_id": node_id or None,
                    "provider_node_id": node_id or None,
                    "tailscale_ip": provider_ip or None,
                    "ip_address": provider_ip or None,
                    "hostname": hostname or None,
                    "user_name": item.get("user_name"),
                    "online": online,
                    "source": item.get("source_json") or item,
                },
                implementation_kind=implementation_kind,
            )
        )
    return records


def _structured_host_records(items: list[dict[str, Any]]) -> list[SubjectInventoryRecord]:
    records: list[SubjectInventoryRecord] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        unit = str(item.get("systemd_unit") or "").strip()
        process_name = str(item.get("process_name") or item.get("display_name") or "").strip()
        stable = unit or process_name
        if not stable:
            continue
        subject_id = "host:ssh" if unit == "ssh.service" else f"host:{_safe_slug(stable)}"
        records.append(
            SubjectInventoryRecord(
                subject_id=subject_id,
                subject_type="host",
                stable_key=subject_id,
                display_name=process_name or unit,
                desired_mode=DEFAULT_DESIRED_MODE_BY_TYPE["host"],
                runtime_state=str(item.get("runtime_state") or "running"),
                is_active=bool(item.get("is_active", True)),
                alias=process_name or None,
                metadata={"source": "structured_input", "collected_at": _utc_timestamp()},
                detail={
                    "systemd_unit": unit or None,
                    "listen_proto": item.get("listen_proto"),
                    "listen_port": item.get("listen_port"),
                    "executable": item.get("executable"),
                    "process_name": process_name or None,
                    "source_json": item,
                },
            )
        )
    return records


def _extract_host_records(result: ScriptResult) -> tuple[list[SubjectInventoryRecord], dict[str, Any]]:
    try:
        payload = json.loads(result.stdout) if result.stdout.strip() else []
    except json.JSONDecodeError:
        payload = []
    items = payload if isinstance(payload, list) else [payload] if isinstance(payload, dict) else []
    records = _structured_host_records([item for item in items if isinstance(item, dict)])
    return records, {
        "source": "host_services",
        "services_count": len(records),
    }


def _xray_records() -> list[SubjectInventoryRecord]:
    records: list[SubjectInventoryRecord] = []
    try:
        clients = DEFAULT_XRAY_ADAPTER.list_clients()
    except Exception:
        clients = []

    for client in clients:
        stable_identity = client.client_uuid or client.client_id
        subject_id = f"xray:{_safe_slug(stable_identity)}"
        records.append(
            SubjectInventoryRecord(
                subject_id=subject_id,
                subject_type=EXPLICIT_EXTERNAL_CLIENT_SUBJECT_TYPE,
                stable_key=subject_id,
                display_name=client.alias or client.email or stable_identity,
                desired_mode=DEFAULT_DESIRED_MODE_BY_TYPE[EXPLICIT_EXTERNAL_CLIENT_SUBJECT_TYPE],
                runtime_state="active" if client.enabled else "inactive",
                is_active=bool(client.enabled),
                alias=client.alias,
                metadata={
                    "source": "xray_adapter",
                    "provider": "xray",
                    "collected_at": _utc_timestamp(),
                },
                detail={
                    "client_id": client.client_id,
                    "client_uuid": client.client_uuid,
                    "email": client.email,
                    "subscription_path": f"/api/v2/xray/clients/{client.client_id}/subscription",
                    "last_subscription_at": None,
                    "enabled": bool(client.enabled),
                    "source": client.raw,
                },
                implementation_kind="xray",
            )
        )
    return records


def _upsert_subject(record: SubjectInventoryRecord) -> None:
    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO subjects (
                subject_id,
                subject_type,
                subject_role,
                implementation_kind,
                stable_key,
                display_name,
                alias,
                desired_mode,
                runtime_state,
                is_active,
                metadata_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, json(?))
            ON CONFLICT(subject_id) DO UPDATE SET
                subject_type = excluded.subject_type,
                subject_role = excluded.subject_role,
                implementation_kind = excluded.implementation_kind,
                stable_key = excluded.stable_key,
                display_name = excluded.display_name,
                alias = COALESCE(subjects.alias, excluded.alias),
                runtime_state = excluded.runtime_state,
                is_active = excluded.is_active,
                is_deleted = 0,
                deleted_at = NULL,
                inactive_since = CASE WHEN excluded.is_active = 1 THEN NULL ELSE COALESCE(subjects.inactive_since, CURRENT_TIMESTAMP) END,
                last_seen_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP,
                metadata_json = excluded.metadata_json
            """,
            (
                record.subject_id,
                record.subject_type,
                _subject_role(record.subject_type),
                record.implementation_kind or record.subject_type,
                record.stable_key,
                record.display_name,
                record.alias,
                record.desired_mode,
                record.runtime_state,
                1 if record.is_active else 0,
                _json_dumps(
                    {
                        **record.metadata,
                        **(
                            {"detail": record.detail}
                            if record.subject_type not in DETAIL_TABLE_BY_TYPE
                            else {}
                        ),
                    }
                ),
            ),
        )

        table_name = DETAIL_TABLE_BY_TYPE.get(record.subject_type)
        if table_name is None:
            return
        detail = dict(record.detail)
        detail["subject_id"] = record.subject_id
        columns = ", ".join(detail.keys())
        placeholders = ", ".join("?" for _ in detail)
        updates = ", ".join(
            f"{key} = excluded.{key}" for key in detail.keys() if key != "subject_id"
        )
        connection.execute(
            f"""
            INSERT INTO {table_name} ({columns})
            VALUES ({placeholders})
            ON CONFLICT(subject_id) DO UPDATE SET
                {updates},
                updated_at = CURRENT_TIMESTAMP
            """,
            tuple(_sql_value(value) for value in detail.values()),
        )


def _mark_missing_subjects(
    subject_type: str,
    seen_subject_ids: set[str],
    *,
    connection_id: str | None = None,
) -> int:
    runtime_state = INACTIVE_RUNTIME_BY_TYPE[subject_type]
    scoped_connection_id = str(connection_id or "").strip()
    subject_types = {
        EXTERNAL_NETWORK_CLIENT_SUBJECT_TYPE: (
            EXTERNAL_NETWORK_CLIENT_SUBJECT_TYPE,
            "tailscale",
            "tailscale_node",
        ),
        EXPLICIT_EXTERNAL_CLIENT_SUBJECT_TYPE: (
            EXPLICIT_EXTERNAL_CLIENT_SUBJECT_TYPE,
            "xray",
        ),
    }.get(subject_type, (subject_type,))
    subject_type_placeholders = ", ".join("?" for _ in subject_types)
    with db_session() as connection:
        if seen_subject_ids:
            placeholders = ", ".join("?" for _ in seen_subject_ids)
            query = f"""
                UPDATE subjects
                SET
                    is_active = 0,
                    runtime_state = ?,
                    inactive_since = COALESCE(inactive_since, CURRENT_TIMESTAMP),
                    updated_at = CURRENT_TIMESTAMP
                WHERE subject_type IN ({subject_type_placeholders})
                  AND is_deleted = 0
                  AND subject_id != 'host:ssh'
                  AND subject_id NOT IN ({placeholders})
            """
            params: list[Any] = [
                runtime_state,
                *subject_types,
                *seen_subject_ids,
            ]
        else:
            query = f"""
                UPDATE subjects
                SET
                    is_active = 0,
                    runtime_state = ?,
                    inactive_since = COALESCE(inactive_since, CURRENT_TIMESTAMP),
                    updated_at = CURRENT_TIMESTAMP
                WHERE subject_type IN ({subject_type_placeholders})
                  AND is_deleted = 0
                  AND subject_id != 'host:ssh'
            """
            params = [runtime_state, *subject_types]

        if scoped_connection_id:
            query += " AND json_extract(metadata_json, '$.connection_id') = ?"
            params.append(scoped_connection_id)

        return connection.execute(query, tuple(params)).rowcount


def _tombstone_legacy_docker_subjects(records: list[SubjectInventoryRecord]) -> int:
    legacy_ids: set[str] = set()
    current_ids: set[str] = {record.subject_id for record in records}
    for record in records:
        project = str(record.detail.get("compose_project") or "").strip()
        service = str(record.detail.get("compose_service") or "").strip()
        if not project or not service:
            continue
        legacy_id = f"docker:{_safe_slug(project)}-{_safe_slug(service)}"
        if legacy_id != record.subject_id and legacy_id not in current_ids:
            legacy_ids.add(legacy_id)

    if not legacy_ids:
        return 0

    with db_session() as connection:
        placeholders = ", ".join("?" for _ in legacy_ids)
        return connection.execute(
            f"""
            UPDATE subjects
            SET
                is_deleted = 1,
                is_active = 0,
                runtime_state = 'missing',
                deleted_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE subject_type = 'docker'
              AND is_deleted = 0
              AND is_active = 0
              AND subject_id IN ({placeholders})
            """,
            tuple(legacy_ids),
        ).rowcount


def _tombstone_legacy_host_subjects(records: list[SubjectInventoryRecord]) -> int:
    subject_ids = {record.subject_id for record in records}
    if "host:ssh" not in subject_ids:
        return 0

    with db_session() as connection:
        return connection.execute(
            """
            UPDATE subjects
            SET
                is_deleted = 1,
                is_active = 0,
                runtime_state = 'missing',
                deleted_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE subject_type = 'host'
              AND subject_id = 'host:ssh-service'
              AND is_deleted = 0
              AND is_active = 0
            """
        ).rowcount


def _tombstone_missing_system_subjects(subject_type: str, grace_seconds: int) -> int:
    if subject_type not in {"docker", "host"}:
        return 0

    with db_session() as connection:
        return connection.execute(
            """
            UPDATE subjects
            SET
                is_deleted = 1,
                deleted_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            WHERE subject_type = ?
              AND is_deleted = 0
              AND is_active = 0
              AND runtime_state = 'missing'
              AND subject_id != 'host:ssh'
              AND inactive_since IS NOT NULL
              AND inactive_since <= datetime('now', ?)
            """,
            (subject_type, f"-{int(grace_seconds)} seconds"),
        ).rowcount


def _run_script(script_id: str, extra_args: list[str] | None = None) -> ScriptResult:
    return DEFAULT_SCRIPT_RUNNER.run(script_id, extra_args=extra_args)


def _external_ingress_connections_for_requested_providers(
    providers: set[str],
) -> tuple[list[dict[str, Any]], set[str]]:
    if not providers:
        return [], set()

    from fwrouter_api.services.external_connections_registry import list_external_connections

    connections: list[dict[str, Any]] = []
    covered: set[str] = set()
    for item in list_external_connections(enabled_only=True):
        if str(item.get("connection_type") or "") != "external_network_source":
            continue
        provider = str(item.get("runtime_type") or "").strip().lower()
        if provider not in providers:
            continue
        connections.append(item)
        covered.add(provider)
    connections.sort(key=lambda item: str(item.get("connection_id") or ""))
    return connections, providers - covered


def sync_subject_inventory(
    *,
    requested_by: str = "api",
    discover_docker: bool = True,
    discover_host: bool = False,
    discover_tailscale: bool = False,
    discover_external_ingress_providers: list[str] | None = None,
    discover_xray: bool = False,
    include_all_external_ingress_peers: bool = False,
    include_all_tailscale_peers: bool = False,
    lan_clients: list[dict[str, Any]] | None = None,
    tailscale_nodes: list[dict[str, Any]] | None = None,
    host_services: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    records_by_type: dict[str, list[SubjectInventoryRecord]] = {
        "lan": _structured_lan_records(lan_clients or []),
        "host": _structured_host_records(host_services or []),
        "docker": [],
        EXPLICIT_EXTERNAL_CLIENT_SUBJECT_TYPE: [],
    }
    for subject_type in EXTERNAL_INGRESS_SUBJECT_TYPES:
        records_by_type.setdefault(subject_type, [])
    sources: dict[str, Any] = {}
    warnings: list[dict[str, Any]] = []
    scoped_tailscale_nodes_count = 0
    if tailscale_nodes:
        scoped_tailscale_nodes = [
            item
            for item in tailscale_nodes
            if isinstance(item, dict) and str(item.get("connection_id") or "").strip()
        ]
        scoped_tailscale_nodes_count = len(scoped_tailscale_nodes)
        if scoped_tailscale_nodes:
            records_by_type[EXTERNAL_NETWORK_CLIENT_SUBJECT_TYPE].extend(
                _external_ingress_records("tailscale", scoped_tailscale_nodes)
            )
        if len(scoped_tailscale_nodes) != len(tailscale_nodes):
            warnings.append(
                {
                    "source": "tailscale_nodes",
                    "error_code": "EXTERNAL_INGRESS_CONNECTION_REQUIRED",
                    "message": "External ingress subjects require a registered connection_id.",
                }
            )

    # Auto-discovery for LAN from dnsmasq leases
    records_by_type["lan"].extend(_discover_lan_records())

    if discover_docker:
        try:
            try:
                docker_result = _run_script("docker_inventory")
            except ScriptRunnerError:
                docker_result = _run_script("docker_ps")
            if not docker_result.ok and docker_result.script_id == "docker_inventory":
                docker_result = _run_script("docker_ps")
            if docker_result.ok:
                docker_records, docker_source = _extract_docker_records(docker_result)
                records_by_type["docker"].extend(docker_records)
                sources["docker"] = docker_source
            else:
                warnings.append(
                    {
                        "source": "docker_ps",
                        "error_code": "DOCKER_PS_FAILED",
                        "message": docker_result.stderr.strip() or "docker_ps failed.",
                    }
                )
        except (ScriptRunnerError, json.JSONDecodeError) as exc:
            warnings.append(
                {
                    "source": "docker_ps",
                    "error_code": "DOCKER_DISCOVERY_ERROR",
                    "message": str(exc),
                }
            )

    if discover_host:
        try:
            host_result = _run_script("host_services")
            if host_result.ok:
                host_records, host_source = _extract_host_records(host_result)
                records_by_type["host"].extend(host_records)
                sources["host"] = host_source
            else:
                warnings.append(
                    {
                        "source": "host_services",
                        "error_code": "HOST_DISCOVERY_FAILED",
                        "message": host_result.stderr.strip() or "host_services failed.",
                    }
                )
        except (ScriptRunnerError, json.JSONDecodeError) as exc:
            warnings.append(
                {
                    "source": "host_services",
                    "error_code": "HOST_DISCOVERY_ERROR",
                    "message": str(exc),
                }
            )

    requested_external_ingress_providers = {
        str(provider).strip().lower()
        for provider in (discover_external_ingress_providers or [])
        if str(provider).strip()
    }
    if discover_tailscale:
        requested_external_ingress_providers.add("tailscale")
    include_all_provider_peers = include_all_external_ingress_peers or include_all_tailscale_peers
    external_ingress_connections, missing_provider_connections = (
        _external_ingress_connections_for_requested_providers(requested_external_ingress_providers)
    )

    for provider in sorted(missing_provider_connections):
        warnings.append(
            {
                "source": provider,
                "error_code": "EXTERNAL_INGRESS_CONNECTION_REQUIRED",
                "message": f"External ingress provider has no registered enabled connection: {provider}",
            }
        )

    refreshed_external_connections_by_type: dict[str, set[str]] = {}
    for source_connection in external_ingress_connections:
        connection_id = str(source_connection.get("connection_id") or "").strip()
        provider = str(source_connection.get("runtime_type") or "").strip().lower()
        contract = external_ingress_contract(provider)
        if contract is None:
            warnings.append(
                {
                    "source": connection_id or provider,
                    "error_code": "EXTERNAL_INGRESS_PROVIDER_UNKNOWN",
                    "message": f"External ingress provider is not registered: {provider}",
                }
            )
            continue
        subject_type = str(contract.get("subject_type") or "")
        collector_config = (
            source_connection.get("collector_config")
            if isinstance(source_connection.get("collector_config"), dict)
            else {}
        )
        contract_collector_config = (
            contract.get("collector_config") if isinstance(contract.get("collector_config"), dict) else {}
        )
        script_id = str(
            collector_config.get("script_id")
            or contract_collector_config.get("script_id")
            or ""
        ).strip()
        if not script_id:
            warnings.append(
                {
                    "source": connection_id or provider,
                    "error_code": "EXTERNAL_INGRESS_COLLECTOR_NOT_CONFIGURED",
                    "message": f"External ingress connection has no command probe script: {connection_id}",
                }
            )
            continue
        extra_args = collector_config.get("extra_args") if isinstance(collector_config.get("extra_args"), list) else []
        try:
            probe_result = _run_script(script_id, extra_args=[str(item) for item in extra_args])
            if probe_result.ok:
                provider_clients, provider_source = external_ingress_clients_from_script_result(
                    provider,
                    probe_result,
                    connection_id=connection_id,
                    include_all_peers=include_all_provider_peers,
                )
                provider_records = _external_ingress_records(
                    provider,
                    provider_clients,
                    connection_id=connection_id,
                )
                records_by_type.setdefault(subject_type, []).extend(provider_records)
                sources[connection_id] = {
                    **provider_source,
                    "provider": provider,
                    "connection_id": connection_id,
                }
                refreshed_external_connections_by_type.setdefault(subject_type, set()).add(connection_id)
            else:
                warnings.append(
                    {
                        "source": script_id,
                        "error_code": f"{provider.upper()}_STATUS_FAILED",
                        "message": probe_result.stderr.strip() or f"{script_id} failed.",
                    }
                )
        except (ScriptRunnerError, json.JSONDecodeError) as exc:
            warnings.append(
                {
                    "source": script_id,
                    "error_code": f"{provider.upper()}_DISCOVERY_ERROR",
                    "message": str(exc),
                }
            )

    if discover_xray:
        try:
            records_by_type[EXPLICIT_EXTERNAL_CLIENT_SUBJECT_TYPE].extend(_xray_records())
            sources["xray"] = {
                "adapter": "xray",
                "clients_count": len(records_by_type[EXPLICIT_EXTERNAL_CLIENT_SUBJECT_TYPE]),
            }
        except Exception as exc:
            warnings.append(
                {
                    "source": "xray_adapter",
                    "error_code": "XRAY_DISCOVERY_ERROR",
                    "message": str(exc),
                }
            )

    synced_counts: dict[str, int] = {}
    stale_counts: dict[str, int] = {}
    tombstoned_counts: dict[str, int] = {}
    seen_by_type: dict[str, set[str]] = {}
    refreshed_subject_types: set[str] = {"lan"}
    settings = get_settings()

    if sources.get("docker"):
        refreshed_subject_types.add("docker")
    if sources.get("host"):
        refreshed_subject_types.add("host")
    for subject_type, connection_ids in refreshed_external_connections_by_type.items():
        if connection_ids:
            refreshed_subject_types.add(subject_type)
    if sources.get("xray"):
        refreshed_subject_types.add(EXPLICIT_EXTERNAL_CLIENT_SUBJECT_TYPE)
    if scoped_tailscale_nodes_count:
        refreshed_subject_types.add(EXTERNAL_NETWORK_CLIENT_SUBJECT_TYPE)
    if host_services:
        refreshed_subject_types.add("host")

    for subject_type, records in records_by_type.items():
        seen_subject_ids: set[str] = set()
        seen_by_connection_id: dict[str, set[str]] = {}
        for record in records:
            _upsert_subject(record)
            seen_subject_ids.add(record.subject_id)
            record_connection_id = str(record.metadata.get("connection_id") or "").strip()
            if record_connection_id:
                seen_by_connection_id.setdefault(record_connection_id, set()).add(record.subject_id)
        if subject_type in refreshed_subject_types:
            scoped_connection_ids = refreshed_external_connections_by_type.get(subject_type) or set()
            if scoped_connection_ids:
                stale_counts[subject_type] = sum(
                    _mark_missing_subjects(
                        subject_type,
                        seen_by_connection_id.get(connection_id, set()),
                        connection_id=connection_id,
                    )
                    for connection_id in scoped_connection_ids
                )
            else:
                stale_counts[subject_type] = _mark_missing_subjects(subject_type, seen_subject_ids)
            if subject_type == "docker":
                legacy_tombstoned = _tombstone_legacy_docker_subjects(records)
                if legacy_tombstoned:
                    tombstoned_counts["docker_legacy"] = legacy_tombstoned
            if subject_type == "host":
                legacy_tombstoned = _tombstone_legacy_host_subjects(records)
                if legacy_tombstoned:
                    tombstoned_counts["host_legacy"] = legacy_tombstoned
            if settings.subject_inventory_tombstone_missing_system_subjects:
                tombstoned = _tombstone_missing_system_subjects(
                    subject_type,
                    settings.subject_inventory_missing_tombstone_grace_seconds,
                )
                if tombstoned:
                    tombstoned_counts[subject_type] = tombstoned
        synced_counts[subject_type] = len(records)
        seen_by_type[subject_type] = seen_subject_ids

    result = {
        "ok": not any(item["error_code"].endswith("_ERROR") for item in warnings if "error_code" in item),
        "requested_by": requested_by,
        "synced_counts": synced_counts,
        "stale_counts": stale_counts,
        "tombstoned_counts": tombstoned_counts,
        "sources": sources,
        "warnings": warnings,
        "external_ingress_policy": {
            "providers": sorted(requested_external_ingress_providers),
            "connections": [
                str(item.get("connection_id") or "")
                for item in external_ingress_connections
                if str(item.get("connection_id") or "")
            ],
            "include_all_peers": include_all_provider_peers,
            "note": (
                "Routed peers and online peers with usable provider IP identity are "
                "auto-imported by default. include_all_peers additionally keeps "
                "offline overlay-only peers with usable IP identity."
            ),
        },
    }

    if warnings:
        write_operational_log(
            event_type="subject_inventory_synced",
            level="warning",
            message="Subject inventory sync completed with warnings.",
            details=result,
            dedupe_key=json.dumps(
                {
                    "synced_counts": synced_counts,
                    "stale_counts": stale_counts,
                    "warnings_count": len(warnings),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            cooldown_seconds=1800,
        )
        write_technical_log(
            component="subject-inventory",
            level="warning",
            event_type="subject_inventory_sync_warning",
            message="Subject inventory sync completed with warnings.",
            details=result,
            dedupe_key=json.dumps(
                {
                    "synced_counts": synced_counts,
                    "stale_counts": stale_counts,
                    "warnings": warnings,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            cooldown_seconds=1800,
        )

    return result
