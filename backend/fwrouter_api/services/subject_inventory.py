from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fwrouter_api.adapters.scripts import DEFAULT_SCRIPT_RUNNER, ScriptResult, ScriptRunnerError
from fwrouter_api.adapters.xray import DEFAULT_XRAY_ADAPTER
from fwrouter_api.db.connection import db_session
from fwrouter_api.services.logs import write_operational_log, write_technical_log


CLIENT_SUBJECT_TYPES = {"lan", "tailscale_node", "xray"}
SYSTEM_SUBJECT_TYPES = {"docker", "host", "fwrouter"}
DEFAULT_DESIRED_MODE_BY_TYPE = {
    "lan": "global",
    "tailscale_node": "global",
    "xray": "enabled",
    "docker": "direct",
    "host": "direct",
    "fwrouter": "direct",
}
DETAIL_TABLE_BY_TYPE = {
    "lan": "subject_lan",
    "tailscale_node": "subject_tailscale",
    "xray": "subject_xray",
    "docker": "subject_docker",
    "host": "subject_host",
    "fwrouter": "subject_fwrouter",
}
INACTIVE_RUNTIME_BY_TYPE = {
    "lan": "inactive",
    "tailscale_node": "inactive",
    "xray": "inactive",
    "docker": "missing",
    "host": "missing",
    "fwrouter": "missing",
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
            "source": "docker_ps",
            "status": item.get("State") or item.get("Status"),
            "collected_at": _utc_timestamp(),
        }
        detail = {
            "compose_project": project or None,
            "compose_service": service or None,
            "container_name": container_name or None,
            "container_id": container_id or None,
            "image_name": image_name or None,
            "ip_address": None,
            "network_name": None,
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


def _tailscale_peer_records(
    payload: dict[str, Any],
    *,
    include_all_peers: bool = False,
) -> list[SubjectInventoryRecord]:
    peers_value = payload.get("Peer") or payload.get("Peers") or {}
    peers: list[dict[str, Any]]
    if isinstance(peers_value, dict):
        peers = [item for item in peers_value.values() if isinstance(item, dict)]
    elif isinstance(peers_value, list):
        peers = [item for item in peers_value if isinstance(item, dict)]
    else:
        peers = []

    records: list[SubjectInventoryRecord] = []
    for item in peers:
        tail_addrs = item.get("TailscaleIPs") or item.get("Addresses") or []
        has_tailscale_ip = bool(isinstance(tail_addrs, list) and tail_addrs)
        routing_hint = bool(
            item.get("through_fwrouter")
            or item.get("fwrouter_routed")
            or item.get("routed_via_server")
            or item.get("UsesExitNode")
            or item.get("ExitNode")
            or item.get("UsesThisServerAsExit")
        )
        importable = routing_hint if not include_all_peers else (routing_hint or has_tailscale_ip)
        if not include_all_peers and not importable:
            continue

        node_id = str(item.get("ID") or item.get("NodeID") or item.get("IDShort") or "").strip()
        hostname = str(item.get("HostName") or item.get("DNSName") or item.get("Name") or "").strip()
        tailscale_ip = ""
        if isinstance(tail_addrs, list) and tail_addrs:
            tailscale_ip = str(tail_addrs[0]).strip()
        stable_key = node_id or tailscale_ip or hostname
        if not stable_key:
            continue

        subject_id = f"tailscale-node:{_safe_slug(stable_key)}"
        display_name = hostname or tailscale_ip or node_id
        records.append(
            SubjectInventoryRecord(
                subject_id=subject_id,
                subject_type="tailscale_node",
                stable_key=subject_id,
                display_name=display_name,
                desired_mode=DEFAULT_DESIRED_MODE_BY_TYPE["tailscale_node"],
                runtime_state="active" if bool(item.get("Online", False)) else "inactive",
                is_active=bool(item.get("Online", False)),
                alias=hostname or None,
                metadata={
                    "source": "tailscale_status",
                    "routing_hint": routing_hint,
                    "import_reason": "routing_hint" if routing_hint else "tailscale_ip",
                    "collected_at": _utc_timestamp(),
                },
                detail={
                    "node_id": node_id or None,
                    "tailscale_ip": tailscale_ip or None,
                    "hostname": hostname or None,
                    "user_name": item.get("User") or item.get("UserName"),
                    "online": 1 if bool(item.get("Online", False)) else 0,
                    "source_json": item,
                },
            )
        )
    return records


def _extract_tailscale_status_records(
    result: ScriptResult,
    *,
    include_all_peers: bool = False,
) -> tuple[list[SubjectInventoryRecord], dict[str, Any]]:
    try:
        payload = json.loads(result.stdout) if result.stdout.strip() else {}
    except json.JSONDecodeError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    records = _tailscale_peer_records(payload, include_all_peers=include_all_peers)
    return records, {
        "script_id": result.script_id,
        "peers_imported_count": len(records),
        "include_all_peers": include_all_peers,
    }


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


def _structured_tailscale_node_records(items: list[dict[str, Any]]) -> list[SubjectInventoryRecord]:
    records: list[SubjectInventoryRecord] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        node_id = str(item.get("node_id") or item.get("id") or "").strip()
        tailscale_ip = str(item.get("tailscale_ip") or item.get("ip") or "").strip()
        hostname = str(item.get("hostname") or item.get("display_name") or "").strip()
        stable = node_id or tailscale_ip or hostname
        if not stable:
            continue
        subject_id = f"tailscale-node:{_safe_slug(stable)}"
        records.append(
            SubjectInventoryRecord(
                subject_id=subject_id,
                subject_type="tailscale_node",
                stable_key=subject_id,
                display_name=hostname or tailscale_ip or node_id,
                desired_mode=DEFAULT_DESIRED_MODE_BY_TYPE["tailscale_node"],
                runtime_state="active" if bool(item.get("online", True)) else "inactive",
                is_active=bool(item.get("online", True)),
                alias=hostname or None,
                metadata={"source": "structured_input", "collected_at": _utc_timestamp()},
                detail={
                    "node_id": node_id or None,
                    "tailscale_ip": tailscale_ip or None,
                    "hostname": hostname or None,
                    "user_name": item.get("user_name"),
                    "online": 1 if bool(item.get("online", True)) else 0,
                    "source_json": item,
                },
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
        subject_id = f"host:{_safe_slug(stable)}"
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
                subject_type="xray",
                stable_key=subject_id,
                display_name=client.alias or client.email or stable_identity,
                desired_mode=DEFAULT_DESIRED_MODE_BY_TYPE["xray"],
                runtime_state="active" if client.enabled else "inactive",
                is_active=bool(client.enabled),
                alias=client.alias,
                metadata={"source": "xray_adapter", "collected_at": _utc_timestamp()},
                detail={
                    "client_id": client.client_id,
                    "client_uuid": client.client_uuid,
                    "email": client.email,
                    "subscription_path": f"/api/v2/xray/clients/{client.client_id}/subscription",
                    "last_subscription_at": None,
                    "enabled": 1 if client.enabled else 0,
                    "source_json": client.raw,
                },
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
                stable_key,
                display_name,
                alias,
                desired_mode,
                runtime_state,
                is_active,
                metadata_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, json(?))
            ON CONFLICT(subject_id) DO UPDATE SET
                subject_type = excluded.subject_type,
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
                record.stable_key,
                record.display_name,
                record.alias,
                record.desired_mode,
                record.runtime_state,
                1 if record.is_active else 0,
                _json_dumps(record.metadata),
            ),
        )

        table_name = DETAIL_TABLE_BY_TYPE[record.subject_type]
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


def _mark_missing_subjects(subject_type: str, seen_subject_ids: set[str]) -> int:
    runtime_state = INACTIVE_RUNTIME_BY_TYPE[subject_type]
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
                WHERE subject_type IN (?, ?)
                  AND is_deleted = 0
                  AND subject_id NOT IN ({placeholders})
            """
            params: list[Any] = [runtime_state, subject_type, "tailscale" if subject_type == "tailscale_node" else subject_type, *seen_subject_ids]
        else:
            query = """
                UPDATE subjects
                SET
                    is_active = 0,
                    runtime_state = ?,
                    inactive_since = COALESCE(inactive_since, CURRENT_TIMESTAMP),
                    updated_at = CURRENT_TIMESTAMP
                WHERE subject_type IN (?, ?)
                  AND is_deleted = 0
            """
            params = [runtime_state, subject_type, "tailscale" if subject_type == "tailscale_node" else subject_type]

        return connection.execute(query, tuple(params)).rowcount


def _run_script(script_id: str, extra_args: list[str] | None = None) -> ScriptResult:
    return DEFAULT_SCRIPT_RUNNER.run(script_id, extra_args=extra_args)


def sync_subject_inventory(
    *,
    requested_by: str = "api",
    discover_docker: bool = True,
    discover_host: bool = False,
    discover_tailscale: bool = False,
    discover_xray: bool = False,
    include_all_tailscale_peers: bool = False,
    lan_clients: list[dict[str, Any]] | None = None,
    tailscale_nodes: list[dict[str, Any]] | None = None,
    host_services: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    records_by_type: dict[str, list[SubjectInventoryRecord]] = {
        "lan": _structured_lan_records(lan_clients or []),
        "tailscale_node": _structured_tailscale_node_records(tailscale_nodes or []),
        "host": _structured_host_records(host_services or []),
        "docker": [],
        "xray": [],
    }
    
    # Auto-discovery for LAN from dnsmasq leases
    records_by_type["lan"].extend(_discover_lan_records())

    sources: dict[str, Any] = {}
    warnings: list[dict[str, Any]] = []

    if discover_docker:
        try:
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

    if discover_tailscale:
        try:
            tailscale_result = _run_script("tailscale_status")
            if tailscale_result.ok:
                tailscale_records, tailscale_source = _extract_tailscale_status_records(
                    tailscale_result,
                    include_all_peers=include_all_tailscale_peers,
                )
                records_by_type["tailscale_node"].extend(tailscale_records)
                sources["tailscale"] = tailscale_source
            else:
                warnings.append(
                    {
                        "source": "tailscale_status",
                        "error_code": "TAILSCALE_STATUS_FAILED",
                        "message": tailscale_result.stderr.strip() or "tailscale_status failed.",
                    }
                )
        except (ScriptRunnerError, json.JSONDecodeError) as exc:
            warnings.append(
                {
                    "source": "tailscale_status",
                    "error_code": "TAILSCALE_DISCOVERY_ERROR",
                    "message": str(exc),
                }
            )

    if discover_xray:
        try:
            records_by_type["xray"].extend(_xray_records())
            sources["xray"] = {
                "adapter": "xray",
                "clients_count": len(records_by_type["xray"]),
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
    seen_by_type: dict[str, set[str]] = {}
    managed_subject_types: set[str] = {"lan"}

    if discover_docker:
        managed_subject_types.add("docker")
    if discover_host:
        managed_subject_types.add("host")
    if discover_tailscale:
        managed_subject_types.add("tailscale_node")
    if discover_xray:
        managed_subject_types.add("xray")
    if tailscale_nodes:
        managed_subject_types.add("tailscale_node")
    if host_services:
        managed_subject_types.add("host")

    for subject_type, records in records_by_type.items():
        seen_subject_ids: set[str] = set()
        for record in records:
            _upsert_subject(record)
            seen_subject_ids.add(record.subject_id)
        if subject_type in managed_subject_types:
            stale_counts[subject_type] = _mark_missing_subjects(subject_type, seen_subject_ids)
        synced_counts[subject_type] = len(records)
        seen_by_type[subject_type] = seen_subject_ids

    result = {
        "ok": not any(item["error_code"].endswith("_ERROR") for item in warnings if "error_code" in item),
        "requested_by": requested_by,
        "synced_counts": synced_counts,
        "stale_counts": stale_counts,
        "sources": sources,
        "warnings": warnings,
        "tailscale_policy": {
            "client_subject_type": "tailscale_node",
            "module_concept": "tailscale",
            "include_all_tailscale_peers": include_all_tailscale_peers,
        "note": (
                "Only routed peers are auto-imported as tailscale_node by default. "
                "include_all_tailscale_peers=true additionally keeps overlay-only peers with usable IP identity."
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
