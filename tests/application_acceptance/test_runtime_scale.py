from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import sys
from concurrent.futures import ThreadPoolExecutor
from collections import deque
from pathlib import Path
import threading
import time
import uuid
from typing import Any

import pytest

from .http_support import http_json


TOP_LEVEL_RULE_COUNT = 199_257
MIN_CONFIG_BYTES = 20_000_000
RSS_LIMIT_KIB = 1_572_864
STARTUP_HARD_LIMIT_SECONDS = 60.0
STARTUP_WARNING_SECONDS = 45.0
STARTUP_ENGINEERING_TARGET_SECONDS = 40.0
SCALE_PROFILE_COUNT = 7
SCALE_SERVERS_PER_PROFILE = 9
SCALE_INTERNAL_AUTO_CLIENTS = SCALE_SERVERS_PER_PROFILE + 1
SCALE_FIXED_EXTERNAL_CLIENTS = 2
SCALE_NATIVE_CLIENT_COUNT = (
    SCALE_PROFILE_COUNT * (SCALE_SERVERS_PER_PROFILE + 1)
    + SCALE_INTERNAL_AUTO_CLIENTS
    + SCALE_FIXED_EXTERNAL_CLIENTS
)
SCALE_SERVER_ADDRESS = "198.18.240.2"
SCALE_SERVER_PORT = 5303
SCALE_SERVER_UUID = "bcb65ef9-8d8c-4ba8-8f7f-1b57a6af9470"
SCALE_SERVER_PUBLIC_KEY = "hSDwCYkwp1R0i33ctD73Wg2_Og0mOBr066SpjqqbTmo"
SCALE_SERVER_SHORT_ID = "0123456789abcdef"
SCALE_SERVER_NAME = "reality.test"
API_WAVE = (
    "/health",
    "/selector/vpn-auto/state",
    "/mihomo",
    "/runtime",
    "/mihomo",
    "/selector/vpn-auto/state",
)


def _write_scale_config(path: Path) -> dict[str, Any]:
    """Write a large, inert ruleset without retaining the whole document in memory."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    digest = hashlib.sha256()
    config_bytes = 0
    transparent_bytes = 0

    def write(stream, value: str, *, transparent: bool = False) -> None:
        nonlocal config_bytes, transparent_bytes
        encoded = value.encode("ascii")
        stream.write(encoded)
        digest.update(encoded)
        config_bytes += len(encoded)
        if transparent:
            transparent_bytes += len(encoded)

    with path.open("wb") as stream:
        for line in (
            "allow-lan: false\n",
            "bind-address: 127.0.0.1\n",
            "mode: rule\n",
            "log-level: warning\n",
            "external-controller: 127.0.0.1:5200\n",
            "mixed-port: 5201\n",
            "proxies: []\n",
            "proxy-groups:\n",
            "  - name: vpn-auto\n",
            "    type: select\n",
            "    proxies: [REJECT]\n",
            "  - name: vpn-global\n",
            "    type: select\n",
            "    proxies: [vpn-auto, REJECT]\n",
            "rules:\n",
        ):
            write(stream, line)
        def rule_sequence(indent: str, *, transparent: bool = False) -> None:
            write(stream, indent + "DOMAIN,protected.test,DIRECT\n", transparent=transparent)
            for index in range((TOP_LEVEL_RULE_COUNT - 3) // 2):
                domain = f"r{index:06d}.test"
                write(stream, f"{indent}DOMAIN-SUFFIX,{domain},vpn-global\n", transparent=transparent)
                write(stream, f"{indent}AND,((NETWORK,udp),(DOMAIN-SUFFIX,{domain})),REJECT\n", transparent=transparent)
            write(stream, indent + "MATCH,vpn-global\n", transparent=transparent)
            write(stream, indent + "NETWORK,udp,REJECT\n", transparent=transparent)

        rule_sequence("  - ")
        write(stream, "sub-rules:\n  fwrouter-transparent:\n")
        rule_sequence("    - ", transparent=True)
        transparent_count = TOP_LEVEL_RULE_COUNT
        write(stream, "  fwrouter-full-vpn:\n")
        write(stream, "    - MATCH,vpn-global\n")
        write(stream, "    - NETWORK,udp,REJECT\n")
        stream.flush()
        os.fsync(stream.fileno())
        os.fchmod(stream.fileno(), 0o600)

    return {
        "config_bytes": config_bytes,
        "config_sha256": digest.hexdigest(),
        "top_level_rule_count": TOP_LEVEL_RULE_COUNT,
        "transparent_subrule_count": transparent_count,
        "transparent_subrule_bytes": transparent_bytes,
        "full_vpn_fallback": ["MATCH,vpn-global", "NETWORK,udp,REJECT"],
        "selector_targets": {"vpn-auto": ["REJECT"], "vpn-global": ["vpn-auto", "REJECT"]},
    }


def _seed_scale_rules_source(state: Path) -> dict[str, Any]:
    """Seed the normal rules artifact/state so startup apply can regenerate scale."""
    from fwrouter_api.services import rules as rules_service
    from fwrouter_api.services.rules_state_files import write_active_rules_state
    from fwrouter_api.services.rules_state_store import (
        _default_rules_paths,
        _upsert_rules_state_record,
        get_rules_state,
    )

    paths = _default_rules_paths()
    expected_artifact = state / "generated" / "rules" / "effective-rules.json"
    if paths["effective_json_path"].resolve(strict=False) != expected_artifact.resolve(strict=False):
        raise AssertionError("scale rules artifact path escaped the owned test state")

    vpn_lines = [f"scale-{index:06d}.vpn.test" for index in range(99_627)]
    big_vpn_text = "\n".join(vpn_lines) + "\n"
    manual_text = "DIRECT direct.test\nDIRECT protected.test\n"
    manual_validation = rules_service.validate_manual_rules(manual_text)
    big_vpn_validation = rules_service.validate_value_list(
        big_vpn_text,
        action="VPN",
        source=rules_service.RULESET_BIG_VPN,
    )
    if not manual_validation.get("valid") or not big_vpn_validation.get("valid"):
        raise AssertionError({
            "manual_errors": manual_validation.get("errors"),
            "big_vpn_errors": big_vpn_validation.get("errors"),
        })
    if len(big_vpn_validation.get("rules") or []) != 99_627:
        raise AssertionError("scale source did not retain all unique VPN domain rules")

    effective_artifact = rules_service.build_effective_rules_artifact(
        manual_validation=manual_validation,
        selective_default="direct",
        big_vpn_validation=big_vpn_validation,
    )
    from fwrouter_api.services.dnsmasq import _iter_dnsmasq_probe_targets
    probe_targets = _iter_dnsmasq_probe_targets(effective_artifact["rules"])
    targets_by_action = {row["action"]: row["domain"] for row in probe_targets}
    vpn_probe = targets_by_action.get("VPN", "")
    if targets_by_action.get("DIRECT") != "direct.test" or not (vpn_probe == "vpn.test" or vpn_probe.endswith(".vpn.test")):
        raise AssertionError("scale DNS readiness targets are not served by the owned endpoints")
    rules_state = get_rules_state()
    for key, path in paths.items():
        if key in rules_state and key.endswith("_path"):
            rules_state[key] = str(path)
    rules_state.update({
        "selective_default": "direct",
        "status": "success",
        "last_success_at": rules_service._utc_now_iso(),
        "error_code": None,
        "error_message": None,
    })
    _upsert_rules_state_record(rules_state)
    metadata = rules_service._build_metadata_file(
        job_id="acceptance-runtime-scale",
        status="success",
        selective_default="direct",
        source_counts=dict(effective_artifact["source_counts"]),
        effective_counts=dict(effective_artifact["effective_counts"]),
    )
    write_active_rules_state(
        manual_active_text=manual_text,
        big_direct_text="",
        big_vpn_text=big_vpn_text,
        effective_artifact=effective_artifact,
        metadata=metadata,
    )

    return {
        "effective_rules_count": len(effective_artifact["rules"]),
        "vpn_domain_suffix_count": sum(
            1 for item in effective_artifact["rules"]
            if item.get("action") == "VPN" and item.get("kind") == "domain_suffix"
        ),
        "protected_direct_present": any(
            item.get("action") == "DIRECT" and item.get("protected") is True
            for item in effective_artifact["rules"]
        ),
        "artifact_path": str(expected_artifact),
        "artifact_bytes": expected_artifact.stat().st_size,
    }


def _scale_xray_config_with_managed_clients(config: dict[str, Any], clients: list[dict[str, Any]]) -> dict[str, Any]:
    """Keep exactly the scale-owned clients, excluding the shared upstream fixture."""
    result = json.loads(json.dumps(config))
    inbounds = result.get("inbounds")
    if not isinstance(inbounds, list):
        raise AssertionError("scale config has no inbound list")
    managed = [row for row in inbounds if row.get("tag") == "vless-ws"]
    auxiliary = [row for row in inbounds if row.get("tag") == "acceptance-provider-upstream"]
    if len(managed) != 1 or len(auxiliary) != 1:
        raise AssertionError("scale config does not match the shared inbound fixture")
    for row in inbounds:
        if row not in managed + auxiliary and (row.get("settings") or {}).get("clients"):
            raise AssertionError("unexpected client-bearing scale inbound")
    result["inbounds"] = [row for row in inbounds if row.get("tag") != "acceptance-provider-upstream"]
    managed[0]["settings"]["clients"] = json.loads(json.dumps(clients))
    result["routing"]["rules"] = [
        row for row in result["routing"]["rules"]
        if row.get("inboundTag") != ["acceptance-provider-upstream"]
    ]
    return result


def _seed_scale_xray_subjects(state: Path, xray_config: dict[str, Any]) -> dict[str, Any]:
    """Persist the synthetic native VLESS identities through the inventory row contract."""
    from fwrouter_api.db.connection import db_session
    from fwrouter_api.core.config import get_settings
    from fwrouter_api.services.subject_inventory import SubjectInventoryRecord, _upsert_subject

    if get_settings().paths.state_dir.resolve(strict=False) != state.resolve(strict=False):
        raise AssertionError("scale Xray projection DB escaped the owned test state")

    inbound = next(row for row in xray_config["inbounds"] if row.get("tag") == "vless-ws")
    clients = inbound["settings"]["clients"]
    for client in clients:
        client_id = str(client["id"])
        email = str(client["email"])
        subject_id = "xray:" + "".join(
            character.lower() if character.isalnum() else "-" for character in client_id
        ).strip("-")
        _upsert_subject(SubjectInventoryRecord(
            subject_id=subject_id,
            subject_type="explicit_external_client",
            stable_key=subject_id,
            display_name=email,
            desired_mode="enabled",
            runtime_state="active",
            is_active=True,
            alias=email,
            metadata={"source": "xray_adapter", "provider": "xray"},
            detail={
                "client_id": client_id,
                "client_uuid": client_id,
                "email": email,
                "subscription_path": f"/api/v2/xray/clients/{client_id}/subscription",
                "enabled": True,
                "source": {"id": client_id, "email": email, "level": 0},
            },
            implementation_kind="xray",
        ))

    with db_session() as connection:
        rows = connection.execute(
            """SELECT count(*) AS count FROM subjects
               WHERE implementation_kind = 'xray'
                 AND subject_type = 'explicit_external_client'
                 AND subject_role = 'vless_client'
                 AND is_active = 1 AND is_deleted = 0"""
        ).fetchone()
    count = int(rows["count"] if rows is not None else 0)
    if count != len(clients):
        raise AssertionError(f"expected {len(clients)} persisted active Xray clients, found {count}")
    return {"persisted_active_xray_clients": count}


def _seed_scale_subscription_profiles(state: Path) -> dict[str, Any]:
    """Seed owned profiles and supported native servers through source state."""
    from fwrouter_api.db.connection import db_session
    from fwrouter_api.core.config import get_settings
    from fwrouter_api.services.server_global_selection import VIRTUAL_XRAY_VPN_AUTO_SERVER_ID
    from fwrouter_api.services.subscription_profiles import (
        ensure_subscription_identity,
        list_desired_subscription_xray_clients,
    )
    from fwrouter_api.services.xray_subscription_service import (
        _vpn_auto_xray_client_email, _vpn_auto_servers_for_xray_subscription,
    )

    if get_settings().paths.state_dir.resolve(strict=False) != state.resolve(strict=False):
        raise AssertionError("scale profile DB escaped the owned test state")

    servers = [
        {
            "server_id": f"acceptance-scale-vless-{index:02d}",
            "runtime_name": f"acceptance-scale-vless-{index:02d}",
        }
        for index in range(1, SCALE_SERVERS_PER_PROFILE + 1)
    ]
    server_raw: dict[str, dict[str, Any]] = {}
    for server in servers:
        name = server["runtime_name"]
        server_raw[server["server_id"]] = {
            "name": name,
            "_fwrouter_runtime_name": name,
            "type": "vless",
            "server": SCALE_SERVER_ADDRESS,
            "port": SCALE_SERVER_PORT,
            "uuid": SCALE_SERVER_UUID,
            "network": "tcp",
            "udp": True,
            "tls": True,
            "servername": SCALE_SERVER_NAME,
            "client-fingerprint": "chrome",
            "reality-opts": {
                "public-key": SCALE_SERVER_PUBLIC_KEY,
                "short-id": SCALE_SERVER_SHORT_ID,
            },
        }

    with db_session() as connection:
        connection.execute(
            """UPDATE modules SET desired_state='enabled', lifecycle_mode='managed',
                   runtime_state='running', apply_state='clean',
                   status_text='Owned native Mihomo acceptance child'
               WHERE module_name='vpn'"""
        )
        for server in servers:
            server_id = server["server_id"]
            connection.execute(
                """INSERT INTO servers (
                       server_id, server_name, provider_name, raw_json, inventory_state,
                       first_seen_at, last_seen_at, updated_at
                   ) VALUES (?, ?, 'acceptance-scale', ?, 'active', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                   ON CONFLICT(server_id) DO UPDATE SET
                       server_name=excluded.server_name, provider_name=excluded.provider_name,
                       raw_json=excluded.raw_json, inventory_state='active', missing_since=NULL,
                       last_seen_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP""",
                (server_id, server["runtime_name"], json.dumps(server_raw[server_id], sort_keys=True)),
            )
            connection.execute(
                """INSERT INTO server_preferences (server_id, vpn_auto, vpn_auto_priority, vpn_auto_priority_origin)
                   VALUES (?, 1, 0, 'manual')
                   ON CONFLICT(server_id) DO UPDATE SET vpn_auto=1, vpn_auto_priority=0,
                       vpn_auto_priority_origin='manual', manually_deleted_at=NULL,
                       updated_at=CURRENT_TIMESTAMP""",
                (server_id,),
            )
            connection.execute("INSERT OR IGNORE INTO server_ping_state (server_id) VALUES (?)", (server_id,))
        connection.execute(
            """INSERT INTO routing_global_state (id, active_auto_server_id, server_mode)
               VALUES (1, ?, 'auto')
               ON CONFLICT(id) DO UPDATE SET active_auto_server_id=excluded.active_auto_server_id,
                   server_mode='auto', updated_at=CURRENT_TIMESTAMP""",
            (servers[0]["server_id"],),
        )
        connection.execute(
            """INSERT INTO subscription_state (
                   id, url, status, last_refresh_at, last_success_at,
                   server_inventory_updated_at, metadata_json, updated_at
               ) VALUES (1, 'https://acceptance.invalid/runtime-scale', 'success', CURRENT_TIMESTAMP,
                         CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, '{}', CURRENT_TIMESTAMP)
               ON CONFLICT(id) DO UPDATE SET url=excluded.url, status='success',
                   last_refresh_at=CURRENT_TIMESTAMP, last_success_at=CURRENT_TIMESTAMP,
                   server_inventory_updated_at=CURRENT_TIMESTAMP, error_code=NULL,
                   error_message=NULL, metadata_json='{}', updated_at=CURRENT_TIMESTAMP"""
        )

    profile_tokens: list[str] = []
    for index in range(1, SCALE_PROFILE_COUNT + 1):
        token = f"acceptance-scale-profile-{index:02d}"
        ensure_subscription_identity(token, display_name=f"Acceptance scale profile {index:02d}")
        profile_tokens.append(token)

    profile_nodes = list_desired_subscription_xray_clients()
    expected_profile_nodes = SCALE_PROFILE_COUNT * (SCALE_SERVERS_PER_PROFILE + 1)
    if len(profile_nodes) != expected_profile_nodes:
        raise AssertionError(f"expected {expected_profile_nodes} desired profile nodes, found {len(profile_nodes)}")
    if {str(node.get("server_id") or "") for node in profile_nodes} != {
        VIRTUAL_XRAY_VPN_AUTO_SERVER_ID, *(server["server_id"] for server in servers)
    }:
        raise AssertionError("desired profile nodes do not match the owned virtual and native server set")

    profile_clients = [
        {"id": str(node["client_uuid"]), "email": str(node["client_email"]), "level": 0}
        for node in profile_nodes
    ]
    internal_servers = _vpn_auto_servers_for_xray_subscription()
    if {row["server_id"] for row in internal_servers} != {
        VIRTUAL_XRAY_VPN_AUTO_SERVER_ID, *(server["server_id"] for server in servers)
    }:
        raise AssertionError("internal auto inventory is not the owned real and virtual server set")
    internal_auto_clients = []
    for server in internal_servers:
        server_id = server["server_id"]
        internal_auto_clients.append({
            "id": str(uuid.uuid5(uuid.NAMESPACE_DNS, f"acceptance-scale-vpn-auto:{server_id}")),
            "email": _vpn_auto_xray_client_email(server_id),
            "level": 0,
        })
    fixed_external_clients = [
        {
            "id": str(uuid.uuid5(uuid.NAMESPACE_DNS, f"acceptance-scale-fixed-external:{index}")),
            "email": f"acceptance-scale-fixed-{index:02d}@acceptance.invalid",
            "level": 0,
        }
        for index in range(1, SCALE_FIXED_EXTERNAL_CLIENTS + 1)
    ]
    clients = [*profile_clients, *internal_auto_clients, *fixed_external_clients]
    if len(clients) != SCALE_NATIVE_CLIENT_COUNT:
        raise AssertionError("owned scale identity count does not match fixture contract")

    return {
        "clients": clients,
        "profile_nodes": profile_nodes,
        "profile_tokens": profile_tokens,
        "servers": servers,
        "expected_counts": {
            "profile_accounts": SCALE_PROFILE_COUNT,
            "profile_nodes": len(profile_nodes),
            "internal_vpn_auto_clients": len(internal_auto_clients),
            "fixed_external_clients": len(fixed_external_clients),
            "native_clients": len(clients),
        },
    }


def _inspect_generated_scale_config(path: Path) -> dict[str, Any]:
    """Inspect the generated YAML with bounded memory after real startup apply."""
    top_level = False
    top_level_rule_count = 0
    vpn_suffix_count = 0
    udp_guard_count = 0
    protected_direct_present = False
    with path.open("r", encoding="ascii") as stream:
        for line in stream:
            if line == "rules:\n":
                top_level = True
                continue
            if top_level and line.startswith("sub-rules:"):
                top_level = False
                continue
            if not top_level or not line.startswith("- "):
                continue
            rule = line[2:].rstrip("\r\n")
            top_level_rule_count += 1
            if (rule.startswith("DOMAIN-SUFFIX,scale-") or rule.startswith("DOMAIN-SUFFIX,vpn.test,")) and rule.endswith(",vpn-global"):
                vpn_suffix_count += 1
            if (rule.startswith("AND,((NETWORK,udp),(DOMAIN-SUFFIX,scale-") or rule.startswith("AND,((NETWORK,udp),(DOMAIN-SUFFIX,vpn.test)")) and rule.endswith(")),REJECT"):
                udp_guard_count += 1
            if rule == "DOMAIN,protected.test,DIRECT":
                protected_direct_present = True
    return {
        "config_bytes": path.stat().st_size,
        "top_level_rule_count": top_level_rule_count,
        "vpn_domain_suffix_count": vpn_suffix_count,
        "udp_guard_count": udp_guard_count,
        "protected_direct_present": protected_direct_present,
    }


def _read_rss_kib(pid: int, *, field: str = "VmRSS") -> int | None:
    try:
        for line in Path(f"/proc/{pid}/status").read_text(encoding="ascii").splitlines():
            if line.startswith(field + ":"):
                return int(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None


def _read_process_cpu_seconds(pid: int) -> float | None:
    """Return cumulative user+system CPU time for the owned API process."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
        fields = raw[raw.rfind(")") + 2:].split()
        ticks = int(fields[11]) + int(fields[12])
        return round(ticks / os.sysconf("SC_CLK_TCK"), 4)
    except (OSError, ValueError, IndexError):
        return None


def _read_worker_resource_snapshot(worker) -> dict[str, Any]:
    """Capture bounded API-process and cgroup memory evidence around scale waves."""
    pid = int(getattr(worker, "pid", 0) or 0)
    snapshot: dict[str, Any] = {
        "worker_pid": pid or None,
        "worker_returncode": worker.poll(),
        "process_rss_kib": _read_rss_kib(pid) if pid else None,
        "process_hwm_kib": _read_rss_kib(pid, field="VmHWM") if pid else None,
        "process_cpu_seconds": _read_process_cpu_seconds(pid) if pid else None,
        "process_threads": None,
        "cgroup": None,
    }
    try:
        status = Path(f"/proc/{pid}/status").read_text(encoding="ascii")
        for line in status.splitlines():
            if line.startswith("Threads:"):
                snapshot["process_threads"] = int(line.split()[1])
                break
    except (OSError, ValueError, IndexError):
        pass

    # The test worker and this pytest process share the acceptance container's
    # cgroup. Read only fixed scalar controller files; never recurse or dump raw
    # cgroup paths/process contents into the receipt.
    try:
        cgroup_pid = pid if Path(f"/proc/{pid}/cgroup").exists() else os.getpid()
        cgroup_lines = Path(f"/proc/{cgroup_pid}/cgroup").read_text(encoding="ascii").splitlines()
        relative = next(line.split(":", 2)[2] for line in cgroup_lines if line.startswith("0::"))
        cgroup_dir = (Path("/sys/fs/cgroup") / relative.lstrip("/")).resolve()
        root = Path("/sys/fs/cgroup").resolve()
        if cgroup_dir == root or root in cgroup_dir.parents:
            values: dict[str, Any] = {}
            for name in ("memory.current", "memory.peak", "memory.max"):
                try:
                    raw = (cgroup_dir / name).read_text(encoding="ascii").strip()
                    values[name.removeprefix("memory.")] = int(raw) if raw.isdigit() else raw[:32]
                except OSError:
                    values[name.removeprefix("memory.")] = None
            try:
                events: dict[str, int] = {}
                for line in (cgroup_dir / "memory.events").read_text(encoding="ascii").splitlines():
                    fields = line.split()
                    if len(fields) == 2 and fields[0] in {"low", "high", "max", "oom", "oom_kill", "oom_group_kill"}:
                        events[fields[0]] = int(fields[1])
                values["events"] = events
            except (OSError, ValueError, IndexError):
                values["events"] = None
            snapshot["cgroup"] = values
    except (OSError, StopIteration, ValueError):
        pass
    return snapshot


_STARTUP_CGROUP_MEMORY_EVENTS = {"low", "high", "max", "oom", "oom_kill", "oom_group_kill"}
_STARTUP_CGROUP_CPU_FIELDS = {"usage_usec", "user_usec", "system_usec", "nr_periods", "nr_throttled", "throttled_usec"}
_STARTUP_CGROUP_IO_FIELDS = {"rbytes", "wbytes", "rios", "wios", "dbytes", "dios"}
_STARTUP_CGROUP_PRESSURE_FILES = {"cpu.pressure", "memory.pressure", "io.pressure"}
_STARTUP_PROC_STATUS_FIELDS = {"VmRSS", "VmHWM", "Threads"}
_STARTUP_PROC_IO_FIELDS = {"rchar", "wchar", "read_bytes", "write_bytes", "syscr", "syscw"}
_STARTUP_RESOURCE_SAMPLE_INTERVAL_SECONDS = 1.0
_STARTUP_RESOURCE_SAMPLE_CAPACITY = 64


def _read_bounded_ascii(path: Path, limit: int) -> str | None:
    try:
        with path.open("rb") as stream:
            raw = stream.read(limit + 1)
        if len(raw) > limit:
            return None
        return raw.decode("ascii").strip()
    except (OSError, UnicodeDecodeError):
        return None


def _parse_startup_keyed_counters(raw: str | None, allowed: set[str]) -> dict[str, int] | None:
    if raw is None:
        return None
    result: dict[str, int] = {}
    for line in raw.splitlines():
        fields = line.split()
        key = fields[0].rstrip(":") if fields else ""
        if len(fields) != 2 or key not in allowed or not fields[1].isascii() or not fields[1].isdigit():
            continue
        result[key] = int(fields[1])
    return result or None


def _read_startup_worker_resource_snapshot(
    pid: int, *, proc_root: Path = Path("/proc"), cgroup_root: Path = Path("/sys/fs/cgroup"),
) -> dict[str, Any]:
    """Read bounded scalar metrics for the owned API PID and its visible cgroup only."""
    if type(pid) is not int or pid <= 1:
        raise ValueError("startup resource snapshot requires an owned positive PID")
    result: dict[str, Any] = {
        "process_cpu_seconds": None, "process_rss_kib": None, "process_hwm_kib": None,
        "process_threads": None, "minor_faults": None, "major_faults": None,
        "process_io_bytes_read": None, "process_io_bytes_written": None,
        "main_thread_sched_scope": "pid_leader_thread_only",
        "main_thread_sched_runtime_ns": None, "main_thread_sched_wait_ns": None,
        "main_thread_sched_runqueue_count": None,
        "cgroup_scope": "owned_acceptance_container", "cgroup": None,
    }
    process_root = proc_root / str(pid)
    stat_raw = _read_bounded_ascii(process_root / "stat", 8192)
    if stat_raw is not None:
        try:
            fields = stat_raw[stat_raw.rfind(")") + 2:].split()
            ticks = int(fields[11]) + int(fields[12])
            result["process_cpu_seconds"] = round(ticks / os.sysconf("SC_CLK_TCK"), 4)
            result["minor_faults"] = int(fields[7])
            result["major_faults"] = int(fields[9])
        except (ValueError, IndexError, OSError):
            pass
    status_raw = _read_bounded_ascii(process_root / "status", 16384)
    if status_raw is not None:
        for line in status_raw.splitlines():
            fields = line.split()
            if len(fields) >= 2 and fields[0].rstrip(":") in _STARTUP_PROC_STATUS_FIELDS:
                key = fields[0].rstrip(":")
                if ((key in {"VmRSS", "VmHWM"} and (len(fields) != 3 or fields[2] != "kB"))
                        or (key == "Threads" and len(fields) != 2)):
                    continue
                try:
                    value = int(fields[1])
                except ValueError:
                    continue
                result[{"VmRSS": "process_rss_kib", "VmHWM": "process_hwm_kib", "Threads": "process_threads"}[key]] = value
    io_values = _parse_startup_keyed_counters(_read_bounded_ascii(process_root / "io", 4096), _STARTUP_PROC_IO_FIELDS)
    if io_values is not None:
        result["process_io_bytes_read"] = io_values.get("read_bytes")
        result["process_io_bytes_written"] = io_values.get("write_bytes")
    # /proc/PID/schedstat is the PID leader task, not all threads. Kernel
    # schedstats may be disabled, so zero values do not prove zero scheduling wait.
    sched_raw = _read_bounded_ascii(process_root / "schedstat", 256)
    if sched_raw is not None:
        try:
            runtime_ns, wait_ns, runqueue_count = (int(value) for value in sched_raw.split()[:3])
            if min(runtime_ns, wait_ns, runqueue_count) >= 0:
                result["main_thread_sched_runtime_ns"] = runtime_ns
                result["main_thread_sched_wait_ns"] = wait_ns
                result["main_thread_sched_runqueue_count"] = runqueue_count
        except (ValueError, TypeError):
            pass

    try:
        cgroup_raw = _read_bounded_ascii(process_root / "cgroup", 4096)
        unified = next((line.split(":", 2)[2] for line in (cgroup_raw or "").splitlines()
                        if line.startswith("0::")), None)
        if unified is None or ".." in Path(unified).parts:
            return result
        root = cgroup_root.resolve()
        directory = (root / unified.lstrip("/")).resolve()
        if directory != root and root not in directory.parents:
            return result
    except (OSError, ValueError, StopIteration):
        return result

    cgroup: dict[str, Any] = {
        "cpu": None, "cpu_max": None, "memory": None, "memory_events": None,
        "psi_total_us": None, "io_bytes_read": None, "io_bytes_written": None,
        "io_read_ops": None, "io_write_ops": None,
    }
    cgroup["cpu"] = _parse_startup_keyed_counters(
        _read_bounded_ascii(directory / "cpu.stat", 4096), _STARTUP_CGROUP_CPU_FIELDS,
    )
    if isinstance(cgroup["cpu"], dict) and "throttled_usec" in cgroup["cpu"]:
        cgroup["cpu"]["throttled_seconds"] = round(cgroup["cpu"]["throttled_usec"] / 1_000_000, 6)
    cpu_max = _read_bounded_ascii(directory / "cpu.max", 128)
    if cpu_max is not None:
        fields = cpu_max.split()
        try:
            if len(fields) == 2 and fields[1].isdigit() and (fields[0] == "max" or fields[0].isdigit()):
                cgroup["cpu_max"] = {"quota_us": None if fields[0] == "max" else int(fields[0]),
                                     "period_us": int(fields[1])}
        except ValueError:
            pass
    memory: dict[str, int | str | None] = {}
    for filename, key in (("memory.current", "current_bytes"), ("memory.peak", "peak_bytes"),
                          ("memory.max", "max_bytes")):
        raw = _read_bounded_ascii(directory / filename, 128)
        if raw is None:
            memory[key] = None
        elif raw == "max":
            memory[key] = "max"
        elif raw.isascii() and raw.isdigit():
            memory[key] = int(raw)
        else:
            memory[key] = None
    cgroup["memory"] = memory
    cgroup["memory_events"] = _parse_startup_keyed_counters(
        _read_bounded_ascii(directory / "memory.events", 2048), _STARTUP_CGROUP_MEMORY_EVENTS,
    )

    psi: dict[str, int] = {}
    for filename in _STARTUP_CGROUP_PRESSURE_FILES:
        raw = _read_bounded_ascii(directory / filename, 4096)
        if raw is None:
            continue
        for line in raw.splitlines():
            fields = line.split()
            if not fields or fields[0] not in {"some", "full"}:
                continue
            for token in fields[1:]:
                key, separator, value = token.partition("=")
                if key == "total" and separator and value.isascii() and value.isdigit():
                    psi[f"{filename.split('.', 1)[0]}_{fields[0]}_total_us"] = int(value)
    cgroup["psi_total_us"] = psi or None

    io_raw = _read_bounded_ascii(directory / "io.stat", 65536)
    io_totals: dict[str, int | None] = {"rbytes": None, "wbytes": None, "rios": None, "wios": None}
    if io_raw is not None:
        for line in io_raw.splitlines():
            fields = line.split()
            if not fields or not re.fullmatch(r"[0-9]+:[0-9]+", fields[0]):
                continue
            for token in fields[1:]:
                key, separator, value = token.partition("=")
                if key in _STARTUP_CGROUP_IO_FIELDS and separator and value.isascii() and value.isdigit():
                    if key in io_totals:
                        io_totals[key] = (io_totals[key] or 0) + int(value)
    if any(value is not None for value in io_totals.values()):
        cgroup["io_bytes_read"] = io_totals["rbytes"]
        cgroup["io_bytes_written"] = io_totals["wbytes"]
        cgroup["io_read_ops"] = io_totals["rios"]
        cgroup["io_write_ops"] = io_totals["wios"]
    result["cgroup"] = cgroup
    return result


class _RssSampler:
    def __init__(self, pid: int) -> None:
        self.pid = pid
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._sample, name="scale-rss-sampler", daemon=True)
        self.samples: list[int] = []
        self.high_water_samples: list[int] = []
        self.cpu_seconds_samples: list[float] = []
        self._startup_started = 0.0
        self._startup_finished: float | None = None
        self._startup_freeze_trigger: str | None = None
        self._startup_active = True
        self._startup_next_sample = 0.0
        self._startup_lock = threading.Lock()
        self._startup_samples: deque[dict[str, Any]] = deque(maxlen=_STARTUP_RESOURCE_SAMPLE_CAPACITY)
        self._startup_probe_cost = {"sample_count": 0, "wall_seconds_sum": 0.0,
                                    "wall_seconds_max": 0.0, "thread_cpu_seconds_sum": 0.0,
                                    "thread_cpu_seconds_max": 0.0}

    def _sample(self) -> None:
        while not self._stop.is_set():
            self._sample_once()
            self._stop.wait(0.05)

    def start(self) -> None:
        self._startup_started = time.monotonic()
        self._startup_next_sample = self._startup_started
        self._sample_once()
        self._thread.start()

    def _sample_once(self) -> None:
        value = _read_rss_kib(self.pid)
        if value is not None:
            self.samples.append(value)
        high_water = _read_rss_kib(self.pid, field="VmHWM")
        if high_water is not None:
            self.high_water_samples.append(high_water)
        cpu_seconds = _read_process_cpu_seconds(self.pid)
        if cpu_seconds is not None:
            self.cpu_seconds_samples.append(cpu_seconds)
        self._sample_startup_resources_if_due()

    def _sample_startup_resources_if_due(self, *, force: bool = False) -> None:
        now = time.monotonic()
        with self._startup_lock:
            if not self._startup_active or (not force and now < self._startup_next_sample):
                return
            self._startup_next_sample = now + _STARTUP_RESOURCE_SAMPLE_INTERVAL_SECONDS
            started_wall = now
            started_cpu = time.thread_time()
            try:
                snapshot = _read_startup_worker_resource_snapshot(self.pid)
            except Exception as exc:
                snapshot = {
                    "process_cpu_seconds": None, "process_rss_kib": None, "process_hwm_kib": None,
                    "process_threads": None, "minor_faults": None, "major_faults": None,
                    "process_io_bytes_read": None, "process_io_bytes_written": None,
                    "main_thread_sched_scope": "pid_leader_thread_only",
                    "main_thread_sched_runtime_ns": None, "main_thread_sched_wait_ns": None,
                    "main_thread_sched_runqueue_count": None, "cgroup_scope": "owned_acceptance_container",
                    "cgroup": None, "snapshot_error_type": type(exc).__name__[:64],
                }
            finished_wall = time.monotonic()
            finished_cpu = time.thread_time()
            wall_cost = max(0.0, finished_wall - started_wall)
            cpu_cost = max(0.0, finished_cpu - started_cpu)
            snapshot["elapsed_seconds"] = round(max(0.0, finished_wall - self._startup_started), 4)
            snapshot["probe_wall_seconds"] = round(wall_cost, 6)
            snapshot["probe_thread_cpu_seconds"] = round(cpu_cost, 6)
            self._startup_samples.append(snapshot)
            costs = self._startup_probe_cost
            costs["sample_count"] += 1
            costs["wall_seconds_sum"] += wall_cost
            costs["wall_seconds_max"] = max(costs["wall_seconds_max"], wall_cost)
            costs["thread_cpu_seconds_sum"] += cpu_cost
            costs["thread_cpu_seconds_max"] = max(costs["thread_cpu_seconds_max"], cpu_cost)

    def freeze_startup_resources(self, *, trigger: str) -> None:
        self._sample_startup_resources_if_due(force=True)
        with self._startup_lock:
            self._startup_active = False
            self._startup_finished = time.monotonic()
            self._startup_freeze_trigger = trigger if trigger in {"start_worker_returned", "start_worker_raised"} else "unknown"

    def startup_resource_observations(self) -> dict[str, Any]:
        with self._startup_lock:
            samples = list(self._startup_samples)
            costs = dict(self._startup_probe_cost)
            started = self._startup_started
            finished = self._startup_finished
            trigger = self._startup_freeze_trigger
            active = self._startup_active
        if started <= 0:
            return {"status": "not_started", "scope": "owned_acceptance_container",
                    "samples": [], "probe_read_cost": {"sample_count": 0}}
        if costs["sample_count"]:
            for key in ("wall_seconds_sum", "wall_seconds_max", "thread_cpu_seconds_sum", "thread_cpu_seconds_max"):
                costs[key] = round(float(costs[key]), 6)
        return {
            "status": "sampling" if active else "frozen",
            "scope": "owned_acceptance_container",
            "sample_interval_seconds": _STARTUP_RESOURCE_SAMPLE_INTERVAL_SECONDS,
            "sample_capacity": _STARTUP_RESOURCE_SAMPLE_CAPACITY,
            "freeze_trigger": trigger,
            "sample_count_retained": len(samples),
            "observation_window_elapsed_seconds": round(
                max(0.0, (finished if finished is not None else time.monotonic()) - started), 4,
            ),
            "samples": samples,
            "probe_read_cost": costs,
        }

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)
        self._sample_once()
        if self._thread.is_alive():
            raise AssertionError("owned API RSS sampler did not stop")

    @property
    def peak_kib(self) -> int | None:
        return max(self.samples) if self.samples else None

    @property
    def process_cpu_seconds(self) -> float | None:
        return max(self.cpu_seconds_samples) if self.cpu_seconds_samples else None


def _stop_owned_worker(worker, *, timeout_seconds: float = 8.0) -> dict[str, Any]:
    started = time.monotonic()
    if worker.poll() is None and not getattr(worker, "_acceptance_stop_requested", False):
        worker._acceptance_stop_requested = True
        worker.terminate()
    try:
        return_code = worker.wait(timeout=timeout_seconds)
    except Exception as exc:
        return {"elapsed_seconds": round(time.monotonic() - started, 4), "return_code": None,
                "graceful": False, "error_type": type(exc).__name__}
    elapsed = time.monotonic() - started
    log_path = getattr(worker, "_acceptance_log_path", None)
    cleanup_observed = False
    if log_path:
        with Path(log_path).open("rb") as stream:
            stream.seek(max(0, Path(log_path).stat().st_size - 4096))
            cleanup_observed = b"FWROUTER_ACCEPTANCE_LIFESPAN_SHUTDOWN_COMPLETE" in stream.read(4096)
    return {"elapsed_seconds": round(elapsed, 4), "return_code": return_code,
            "application_cleanup_observed": cleanup_observed,
            "graceful": cleanup_observed and return_code in {0, -signal.SIGTERM} and elapsed < timeout_seconds}


def _timed_get(api: str, route: str) -> dict[str, Any]:
    started = time.monotonic()
    code, payload = http_json(api + route, timeout=30.0)
    elapsed = time.monotonic() - started
    result: dict[str, Any] = {
        "route": route,
        "status_code": code,
        "ok": bool(payload.get("ok")),
        "elapsed_seconds": round(elapsed, 4),
    }
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    if route == "/mihomo":
        mihomo = data.get("mihomo") if isinstance(data.get("mihomo"), dict) else {}
        details = mihomo.get("details") if isinstance(mihomo.get("details"), dict) else {}
        proof = details.get("vpn_udp_policy") if isinstance(details.get("vpn_udp_policy"), dict) else {}
        result["native_proof"] = {
            key: proof.get(key)
            for key in (
                "native_top_level_rules_match", "guarded_rules_match", "wiring_proven",
                "reconcile_receipt_matches", "policy_installed",
            )
        }
    elif route == "/selector/vpn-auto/state":
        selector = data.get("vpn_auto") if isinstance(data.get("vpn_auto"), dict) else {}
        result["selector_state"] = {
            "problem_code": selector.get("problem_code"),
            "active_auto_server_id": selector.get("active_auto_server_id"),
            "enabled_candidates_count": selector.get("enabled_candidates_count"),
            "runtime_vpn_auto_targets": selector.get("runtime_vpn_auto_targets"),
        }
    elif route == "/runtime":
        runtime = data.get("runtime") if isinstance(data.get("runtime"), dict) else {}
        result["runtime_state"] = runtime.get("runtime_state")
    return result


def _run_six_client_wave(api: str) -> list[dict[str, Any]]:
    with ThreadPoolExecutor(max_workers=6, thread_name_prefix="runtime-scale-client") as pool:
        futures = [pool.submit(_timed_get, api, route) for route in API_WAVE]
        return [future.result() for future in futures]


def _write_metrics(payload: dict[str, Any]) -> None:
    receipt_dir = Path("/tmp/fwrouter-receipts")
    receipt_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if receipt_dir.is_symlink() or not receipt_dir.is_dir() or receipt_dir.stat().st_uid != os.getuid():
        raise AssertionError("runtime scale metrics directory is not an owned directory")
    target = receipt_dir / "runtime-scale-metrics.json"
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    os.replace(temporary, target)


@pytest.mark.packet
def test_large_mihomo_health_readback_stays_bounded_under_concurrent_api_load(acceptance_stack) -> None:
    stack = acceptance_stack
    native = stack["native"]
    config_path = Path(stack["state"]) / "generated" / "mihomo" / "config.yaml"
    metrics: dict[str, Any] = {
        "schema": "fwrouter-runtime-scale-metrics/v1",
        "scope": "owned-native-mihomo-and-api-worker",
        "status": "started",
        "limitations": [
            "isolated kernel dataplane only; no host systemd/reboot claim",
            "periodic RSS begins at owned process spawn; Linux VmHWM retains prior process peaks",
            "native /rules request counters are unavailable in the current worker harness",
            "native UDP policy proof confirms applied config, not remote VPN egress availability",
            "startup resource snapshots are owned-PID and visible acceptance-container cgroup observations; PSI total values are microseconds",
            "schedstat values describe only the PID leader thread; zero is not proof that scheduling wait was absent",
        ],
        "native_rules_request_count": None,
        "rss_scope": "owned API VmRSS plus process-lifetime Linux VmHWM; not aggregate/native memory",
        "cpu_scope": "cumulative user+system CPU ticks read from /proc for the owned API worker; sampled while alive",
        "startup_resource_observations": {"status": "not_started", "scope": "owned_acceptance_container",
                                          "samples": [], "probe_read_cost": {"sample_count": 0}},
        "startup_limits_seconds": {
            "hard_deadline": STARTUP_HARD_LIMIT_SECONDS,
            "warning_seconds": STARTUP_WARNING_SECONDS,
            "engineering_target": STARTUP_ENGINEERING_TARGET_SECONDS,
        },
    }
    sampler: _RssSampler | None = None
    active_worker = stack["worker"]
    worker_was_stopped = False
    try:
        initial_stop = _stop_owned_worker(active_worker)
        worker_was_stopped = initial_stop["graceful"]
        metrics["initial_worker_shutdown"] = initial_stop
        assert worker_was_stopped and initial_stop["elapsed_seconds"] < 8.0, initial_stop

        metrics["config"] = _write_scale_config(config_path)
        assert metrics["config"]["config_bytes"] >= MIN_CONFIG_BYTES
        metrics["rules_source"] = _seed_scale_rules_source(Path(stack["state"]))
        assert metrics["rules_source"]["vpn_domain_suffix_count"] == 99_627
        assert metrics["rules_source"]["protected_direct_present"] is True
        profile_state = _seed_scale_subscription_profiles(Path(stack["state"]))
        metrics["subscription_source"] = profile_state["expected_counts"]
        assert profile_state["expected_counts"] == {
            "profile_accounts": 7,
            "profile_nodes": 70,
            "internal_vpn_auto_clients": 10,
            "fixed_external_clients": 2,
            "native_clients": 82,
        }
        xray_path = Path(stack["state"]) / "xray" / "config.json"
        xray_config = _scale_xray_config_with_managed_clients(
            json.loads(xray_path.read_text()), profile_state["clients"],
        )
        xray_path.write_text(json.dumps(xray_config))
        metrics["xray_source"] = _seed_scale_xray_subjects(Path(stack["state"]), xray_config)
        native.restart_xray_child()
        from .xray_support import loaded_identities
        native_identities_before = loaded_identities(native)
        assert len(native_identities_before) == SCALE_NATIVE_CLIENT_COUNT
        assert len(set(native_identities_before)) == SCALE_NATIVE_CLIENT_COUNT
        metrics["synthetic_xray_loaded_clients"] = SCALE_NATIVE_CLIENT_COUNT
        metrics["subscription_profile_accounts"] = SCALE_PROFILE_COUNT
        metrics["subscription_profile_nodes"] = SCALE_PROFILE_COUNT * (SCALE_SERVERS_PER_PROFILE + 1)
        metrics["internal_vpn_auto_clients"] = SCALE_INTERNAL_AUTO_CLIENTS
        metrics["fixed_external_clients"] = SCALE_FIXED_EXTERNAL_CLIENTS
        native.restart_mihomo_child()

        def observe_spawn(worker) -> None:
            nonlocal active_worker, sampler
            active_worker = worker
            sampler = _RssSampler(worker.pid)
            sampler.start()

        start = time.monotonic()
        startup_freeze_trigger = "start_worker_raised"
        try:
            active_worker, api = stack["start_worker"](
                startup_tasks=True,
                startup_recovery=True,
                on_spawn=observe_spawn,
            )
            startup_freeze_trigger = "start_worker_returned"
            metrics["startup_ready"] = True
            metrics["startup_deadline_seconds"] = STARTUP_HARD_LIMIT_SECONDS
            metrics["startup_readiness_probe"] = getattr(active_worker, "_acceptance_readiness_probe", None)
            metrics["startup_worker_pid"] = active_worker.pid
            metrics["startup_slow_warning"] = getattr(active_worker, "_acceptance_startup_warning", None)
            metrics["startup_wall_seconds"] = metrics["startup_readiness_probe"]["elapsed_seconds"]
            metrics["rss_hwm_kib_at_readiness"] = _read_rss_kib(active_worker.pid, field="VmHWM")
            metrics["rss_kib_at_readiness"] = _read_rss_kib(active_worker.pid, field="VmRSS")
            metrics["api_process_cpu_seconds_at_readiness"] = _read_process_cpu_seconds(active_worker.pid)
        except BaseException:
            failures = stack.get("startup_failures") or []
            if failures:
                startup_failure = failures[-1]
                metrics["startup_readiness_failure"] = startup_failure
                metrics["startup_wall_seconds"] = startup_failure.get("readiness_elapsed_seconds")
                metrics["startup_deadline_seconds"] = startup_failure.get("readiness_deadline_seconds")
                metrics["startup_ready"] = False
                metrics["startup_worker_pid"] = startup_failure.get("worker_pid")
                metrics["startup_slow_warning"] = startup_failure.get("slow_startup_warning")
                metrics["rss_hwm_kib_at_readiness_deadline"] = startup_failure.get("rss_hwm_kib_at_deadline")
                metrics["api_process_cpu_seconds_at_readiness_deadline"] = startup_failure.get(
                    "process_cpu_seconds_at_deadline"
                )
                metrics["failure_shutdown_acceptance"] = "NOT RUN — worker never reached readiness or steady load"
                metrics["final_worker_shutdown"] = {
                    "status": "NOT RUN",
                    "reason": "post-readiness bounded shutdown acceptance was not reached",
                }
            raise
        finally:
            if sampler is not None:
                sampler.freeze_startup_resources(trigger=startup_freeze_trigger)
                resource_observations = sampler.startup_resource_observations()
                resource_observations["readiness_elapsed_seconds"] = metrics.get("startup_wall_seconds")
                metrics["startup_resource_observations"] = resource_observations
            if metrics.get("startup_wall_seconds") is None:
                metrics["startup_wall_seconds"] = round(time.monotonic() - start, 4)
        stack["worker"] = active_worker
        stack["api"] = api

        bootstrap_code, bootstrap_payload = http_json(api + "/__acceptance/bootstrap/report")
        observation = (
            bootstrap_payload.get("observation")
            if isinstance(bootstrap_payload.get("observation"), dict)
            else bootstrap_payload.get("data")
            if isinstance(bootstrap_payload.get("data"), dict)
            else bootstrap_payload
        )
        metrics["bootstrap_observation"] = observation
        metrics["startup_engineering_margin_seconds"] = round(
            STARTUP_ENGINEERING_TARGET_SECONDS - metrics["startup_wall_seconds"], 4,
        )
        metrics["startup_engineering_target_met"] = (
            metrics["startup_wall_seconds"] < STARTUP_ENGINEERING_TARGET_SECONDS
        )
        readiness_probe = metrics["startup_readiness_probe"]
        assert (
            isinstance(readiness_probe, dict)
            and readiness_probe.get("path") == "/health"
            and readiness_probe.get("status_code") == 200
            and readiness_probe.get("healthy") is True
            and isinstance(readiness_probe.get("elapsed_seconds"), (int, float))
        ), readiness_probe
        assert bootstrap_code == 200, bootstrap_payload
        assert observation.get("schema") == "fwrouter-runtime-bootstrap-observation/v1", observation
        assert observation.get("worker_pid") == active_worker.pid, observation
        assert observation.get("recovery_enabled") is True, observation
        assert observation.get("foundation_ok") is True, observation
        assert metrics["startup_wall_seconds"] <= STARTUP_HARD_LIMIT_SECONDS, {
            "startup_wall_seconds": metrics["startup_wall_seconds"],
            "hard_limit_seconds": STARTUP_HARD_LIMIT_SECONDS,
        }
        # Readiness is the bounded /health lifecycle check. Keep the richer
        # public Xray projection as a separate, measured functional request so
        # a timeout here cannot create overlapping startup-poll requests.
        xray_probe_started = time.monotonic()
        try:
            xray_code, xray_payload = http_json(api + "/xray", timeout=20.0)
        except (OSError, ValueError, TimeoutError) as exc:
            metrics["xray_public_readback"] = {
                "status_code": None,
                "error_type": type(exc).__name__,
                "elapsed_seconds": round(time.monotonic() - xray_probe_started, 4),
            }
            raise
        xray_elapsed = round(time.monotonic() - xray_probe_started, 4)
        xray_data = xray_payload.get("data") if isinstance(xray_payload.get("data"), dict) else {}
        xray_status = xray_data.get("xray") if isinstance(xray_data.get("xray"), dict) else {}
        xray_details = xray_status.get("details") if isinstance(xray_status.get("details"), dict) else {}
        loaded_after_readback = loaded_identities(native)
        metrics["xray_public_readback"] = {
            "status_code": xray_code,
            "ok": xray_payload.get("ok") is True,
            "elapsed_seconds": xray_elapsed,
            "runtime_state": xray_status.get("runtime_state"),
            "configured_client_count": xray_details.get("clients_count"),
            "native_loaded_client_count": len(loaded_after_readback),
            "native_loaded_client_count_unique": len(set(loaded_after_readback)),
        }
        assert xray_code == 200 and xray_payload.get("ok") is True, metrics["xray_public_readback"]
        assert xray_elapsed < 20.0, metrics["xray_public_readback"]
        assert xray_status.get("runtime_state") == "running", metrics["xray_public_readback"]
        assert xray_details.get("clients_count") == SCALE_NATIVE_CLIENT_COUNT, metrics["xray_public_readback"]
        assert len(loaded_after_readback) == SCALE_NATIVE_CLIENT_COUNT, metrics["xray_public_readback"]
        assert len(set(loaded_after_readback)) == SCALE_NATIVE_CLIENT_COUNT, metrics["xray_public_readback"]
        assert loaded_after_readback == native_identities_before, metrics["xray_public_readback"]
        live_recovery = observation.get("live_recovery")
        assert isinstance(live_recovery, dict), observation
        live_routing = live_recovery.get("live_routing")
        selector_recovery = live_recovery.get("selector")
        assert isinstance(live_routing, dict) and live_routing.get("recovery_required") is True, live_routing
        assert live_routing.get("recovered") is True, live_routing
        apply_result = live_routing.get("recovery")
        assert isinstance(apply_result, dict) and apply_result.get("ok") is True, live_routing
        assert isinstance(selector_recovery, dict) and selector_recovery.get("restored") is True, selector_recovery
        apply_reconcile = observation.get("apply_reconcile")
        assert isinstance(apply_reconcile, dict), observation
        for phase_name in (
            "intended_routing", "scoped_subject_routing", "xray_subscription_profiles", "dnsmasq_reconcile",
        ):
            phase = apply_reconcile.get(phase_name)
            assert isinstance(phase, dict), {"phase": phase_name, "observation": apply_reconcile}
        intended_routing = apply_reconcile["intended_routing"]
        assert intended_routing.get("mode_matches") is True, intended_routing
        assert intended_routing.get("reapply_required") is False, intended_routing
        scoped_routing = apply_reconcile["scoped_subject_routing"]
        assert scoped_routing.get("classify_probe_ok") is True, scoped_routing
        assert scoped_routing.get("persisted_scoped_subjects_count") == 0, scoped_routing
        assert scoped_routing.get("missing_scoped_subjects_count") == 0, scoped_routing
        assert scoped_routing.get("reapply_required") is False, scoped_routing
        xray_reconcile = apply_reconcile["xray_subscription_profiles"]
        assert xray_reconcile.get("ok") is True, xray_reconcile
        assert xray_reconcile.get("status") == "success", xray_reconcile
        assert xray_reconcile.get("inventory_authority") == "persisted_success", xray_reconcile
        staged_receipt = xray_reconcile.get("staged_publication_receipt")
        generation_apply = xray_reconcile.get("generation_apply")
        final_mihomo = generation_apply.get("final_mihomo") if isinstance(generation_apply, dict) else None
        final_mihomo_reconcile = xray_reconcile.get("final_mihomo_reconcile")
        promotion = xray_reconcile.get("public_profile_promote")
        assert (isinstance(staged_receipt, dict)
                and type(staged_receipt.get("schema")) is int
                and staged_receipt.get("schema") == 1
                and staged_receipt.get("staged_native_publication_complete") is True), xray_reconcile
        assert isinstance(generation_apply, dict) and type(generation_apply.get("public_snapshots_changed")) is bool, xray_reconcile
        assert isinstance(final_mihomo, dict) and final_mihomo.get("ok") is True, xray_reconcile
        assert (isinstance(final_mihomo_reconcile, dict)
                and final_mihomo_reconcile.get("ok") is True
                and final_mihomo_reconcile.get("skipped") is True
                and final_mihomo_reconcile.get("reason") == "staged_publication_native_proof_complete"), xray_reconcile
        assert isinstance(promotion, dict) and all(
            type(promotion.get(key)) is int and promotion[key] >= 0
            for key in ("profiles_count", "nodes_count")
        ), xray_reconcile
        metrics["startup_staged_publication_proof"] = {
            "native_publication_complete": True,
            "final_mihomo_apply_ok": True,
            "duplicate_mihomo_reconcile_skipped": True,
            "public_snapshots_changed": generation_apply["public_snapshots_changed"],
            "promoted_profiles_count": promotion["profiles_count"],
            "promoted_nodes_count": promotion["nodes_count"],
        }
        from .xray_support import assert_applied_bindings, read_subscription_rows

        expected_binding_pairs = {
            (str(client["id"]), str(client["email"]))
            for client in profile_state["clients"]
        }
        binding_state = assert_applied_bindings(Path(stack["state"]), expected_binding_pairs)
        actual_binding_pairs = {
            (str(row.get("client_uuid") or row.get("client_id") or ""), str(row.get("client_email") or ""))
            for row in binding_state["bindings"] if row.get("status") == "applied"
        }
        unexpected_rows = [row for row in binding_state["bindings"] if (
            str(row.get("client_uuid") or row.get("client_id") or ""), str(row.get("client_email") or "")
        ) not in expected_binding_pairs]
        from fwrouter_api.db.connection import db_session
        unexpected_origins = []
        with db_session() as connection:
            for row in unexpected_rows[:4]:
                origin = connection.execute(
                    "SELECT subject_type, subject_role, is_active, is_deleted, desired_mode, metadata_json "
                    "FROM subjects WHERE subject_id=?", (row.get("subject_id"),),
                ).fetchone()
                detail = json.loads(origin["metadata_json"]) if origin is not None else {}
                email = str(row.get("client_email") or "")
                unexpected_origins.append({
                    "subject_type": origin["subject_type"] if origin else None,
                    "subject_role": origin["subject_role"] if origin else None,
                    "is_active": origin["is_active"] if origin else None,
                    "is_deleted": origin["is_deleted"] if origin else None,
                    "desired_mode": origin["desired_mode"] if origin else None,
                    "origin_source": detail.get("source"),
                    "is_auxiliary_upstream_email": email == "acceptance-provider-upstream",
                    "is_internal_auto_email": email.startswith("vpn-auto-"),
                    "is_subscription_email": email.startswith("sub-"),
                    "is_fixed_scale_email": email.startswith("acceptance-scale-fixed-"),
                    "has_selected_server": bool(row.get("selected_server_id")),
                })
        metrics["bindings_identity_parity"] = {
            "row_count": len(binding_state["bindings"]), "unique_count": len(actual_binding_pairs),
            "missing_count": len(expected_binding_pairs - actual_binding_pairs),
            "unexpected_count": len(actual_binding_pairs - expected_binding_pairs),
            "unexpected_origins": unexpected_origins,
            "native_identities_unchanged": loaded_identities(native) == native_identities_before,
        }
        assert binding_state.get("applied_count") == len(expected_binding_pairs), metrics["bindings_identity_parity"]
        assert actual_binding_pairs == expected_binding_pairs, metrics["bindings_identity_parity"]
        metrics["applied_profile_bindings"] = len(expected_binding_pairs)

        nodes_by_token: dict[str, set[tuple[str, str]]] = {token: set() for token in profile_state["profile_tokens"]}
        for node in profile_state["profile_nodes"]:
            nodes_by_token[str(node["subscription_token"])].add(
                (str(node["client_uuid"]), str(node["client_email"]))
            )
        for token, expected_nodes in nodes_by_token.items():
            persisted = read_subscription_rows(Path(stack["state"]), token)
            assert len(persisted["accounts"]) == 1 and persisted["accounts"][0]["enabled"] == 1
            assert len(persisted["clients"]) == 1 and persisted["clients"][0]["enabled"] == 1
            assert len(persisted["snapshots"]) == 1, {"snapshot_count": len(persisted["snapshots"])}
            snapshot = persisted["snapshots"][0]
            assert snapshot.get("runtime_verified_at"), {"runtime_verified": False}
            snapshot_nodes = json.loads(snapshot["nodes_json"])
            actual_nodes = {
                (str(node.get("client_uuid") or ""), str(node.get("client_email") or ""))
                for node in snapshot_nodes
            }
            assert actual_nodes == expected_nodes, {
                "snapshot_node_count": len(actual_nodes),
                "expected_node_count": len(expected_nodes),
            }
        metrics["runtime_verified_profile_snapshots"] = len(nodes_by_token)
        dnsmasq_reconcile = apply_reconcile["dnsmasq_reconcile"]
        assert dnsmasq_reconcile.get("ok") is True, dnsmasq_reconcile
        dns_fixture = observation.get("dns_fixture")
        assert isinstance(dns_fixture, dict) and all(dns_fixture.get(key) is True for key in (
            "configured", "daemon_running", "daemon_uid_matches",
        )), dns_fixture

        generated_config = _inspect_generated_scale_config(config_path)
        metrics["generated_config_after_recovery"] = generated_config
        assert generated_config["config_bytes"] >= MIN_CONFIG_BYTES, generated_config
        assert generated_config["top_level_rule_count"] >= TOP_LEVEL_RULE_COUNT, generated_config
        assert generated_config["vpn_domain_suffix_count"] == 99_627, generated_config
        assert generated_config["udp_guard_count"] == 99_627, generated_config
        assert generated_config["protected_direct_present"] is True, generated_config

        # The new transport fixture must carry a real request, not merely
        # accept config validation and listen on a socket. Native delay uses
        # the named VLESS proxy; the endpoint must observe Xray's owned source.
        from urllib.parse import quote, urlencode
        observation_url = "http://198.18.240.2:8082/observations"
        before_code, before_endpoint = http_json(observation_url, timeout=3.0)
        assert before_code == 200
        target = profile_state["servers"][0]["runtime_name"]
        delay_url = "http://127.0.0.1:5200/proxies/" + quote(target, safe="") + "/delay?" + urlencode({
            "url": "http://203.0.113.53:9080/probe", "timeout": 5000,
        })
        delay_code, delay_payload = http_json(delay_url, timeout=8.0)
        after_code, after_endpoint = http_json(observation_url, timeout=3.0)
        delay_ms = delay_payload.get("delay")
        endpoint_delta = ((after_endpoint.get("counts") or {}).get("http", 0)
                          - (before_endpoint.get("counts") or {}).get("http", 0))
        metrics["native_reality_tcp_probe"] = {
            "status_code": delay_code, "delay_ms": delay_ms if type(delay_ms) is int else None,
            "endpoint_http_delta": endpoint_delta,
            "endpoint_vpn_source_matches": (after_endpoint.get("last_peers") or {}).get("http") == "203.0.113.53",
        }
        assert delay_code == 200 and type(delay_ms) is int and delay_ms >= 0, metrics["native_reality_tcp_probe"]
        assert after_code == 200 and endpoint_delta == 1, metrics["native_reality_tcp_probe"]
        assert metrics["native_reality_tcp_probe"]["endpoint_vpn_source_matches"] is True, metrics["native_reality_tcp_probe"]

        assert sampler is not None
        metrics["rss_hwm_kib_after_native_probe"] = _read_rss_kib(active_worker.pid, field="VmHWM")
        metrics["api_process_cpu_seconds_after_native_probe"] = _read_process_cpu_seconds(active_worker.pid)
        waves: list[list[dict[str, Any]]] = []
        metrics["waves"] = waves
        metrics["wave_resource_observations"] = []
        for wave_number in range(4):
            if wave_number:
                # Let the shared read-only health cache expire between waves so
                # each wave includes one fresh large native readback.
                threading.Event().wait(2.1)
            wave_observation: dict[str, Any] = {
                "wave": wave_number + 1,
                "before": _read_worker_resource_snapshot(active_worker),
            }
            metrics["wave_resource_observations"].append(wave_observation)
            try:
                wave = _run_six_client_wave(api)
            except BaseException as exc:
                wave_observation["failure"] = {
                    "type": type(exc).__name__,
                }
                wave_observation["after"] = _read_worker_resource_snapshot(active_worker)
                raise
            wave_observation["after"] = _read_worker_resource_snapshot(active_worker)
            waves.append(wave)
            for result in wave:
                assert result["status_code"] == 200 and result["ok"] is True, result
                route = result["route"]
                bound = 5.0 if route == "/health" else 15.0 if route in {"/mihomo", "/selector/vpn-auto/state"} else 20.0
                assert result["elapsed_seconds"] < bound, result
                if route == "/mihomo":
                    proof = result.get("native_proof") or {}
                    assert proof.get("native_top_level_rules_match") is True, proof
                    assert proof.get("guarded_rules_match") is True, proof
                    assert proof.get("wiring_proven") is True, proof
                    assert proof.get("reconcile_receipt_matches") is True, proof
                    assert proof.get("policy_installed") is True, proof
                elif route == "/selector/vpn-auto/state":
                    state = result.get("selector_state") or {}
                    expected_ids = {server["server_id"] for server in profile_state["servers"]}
                    expected_targets = {server["runtime_name"] for server in profile_state["servers"]}
                    assert state.get("active_auto_server_id") in expected_ids, state
                    assert state.get("enabled_candidates_count") == SCALE_SERVERS_PER_PROFILE, state
                    assert expected_targets.issubset(set(state.get("runtime_vpn_auto_targets") or [])), state
                    selected_name = next(
                        server["runtime_name"] for server in profile_state["servers"]
                        if server["server_id"] == state.get("active_auto_server_id")
                    )
                    assert selected_name in set(state.get("runtime_vpn_auto_targets") or []), state

        metrics["waves"] = waves
        metrics["rss_peak_kib_sampled_process_lifetime"] = sampler.peak_kib
        lifetime_peak = max(sampler.high_water_samples) if sampler.high_water_samples else None
        metrics["rss_hwm_kib_process_lifetime"] = lifetime_peak
        metrics["api_process_cpu_seconds_sampled"] = sampler.process_cpu_seconds
        assert lifetime_peak is not None and lifetime_peak < RSS_LIMIT_KIB, {"rss_hwm_kib": lifetime_peak}
        assert sampler.peak_kib is not None and sampler.peak_kib < RSS_LIMIT_KIB, {
            "rss_peak_kib_sampled_process_lifetime": sampler.peak_kib,
            "limit_kib": RSS_LIMIT_KIB,
        }

        from .http_support import http_json as _http_json

        auto_code, auto_state = _http_json("http://127.0.0.1:5200/proxies/vpn-auto", timeout=3.0)
        expected_selected = profile_state["servers"][0]["runtime_name"]
        expected_targets = {server["runtime_name"] for server in profile_state["servers"]}
        assert auto_code == 200 and auto_state.get("now") in expected_targets, {
            "status_code": auto_code,
            "selected_member": auto_state.get("now") in expected_targets,
        }
        assert expected_targets.issubset(set(auto_state.get("all") or [])), {
            "expected_target_count": len(expected_targets),
            "actual_target_count": len(auto_state.get("all") or []),
        }
        assert auto_state.get("now") == expected_selected, {"selected_member_matches_persisted": False}
        assert loaded_identities(native) == native_identities_before
        metrics["synthetic_xray_identities_preserved"] = SCALE_NATIVE_CLIENT_COUNT
        metrics["native_selector"] = {
            "status_code": auto_code,
            "selected_owned_member": True,
            "owned_member_count": len(expected_targets),
        }
        metrics["status"] = "passed"
    except BaseException as exc:
        metrics["status"] = "failed"
        metrics["failure_type"] = type(exc).__name__
        raise
    finally:
        if sampler is not None:
            sampler.stop()
            resource_observations = sampler.startup_resource_observations()
            resource_observations["readiness_elapsed_seconds"] = metrics.get("startup_wall_seconds")
            metrics["startup_resource_observations"] = resource_observations
            metrics["rss_peak_kib_sampled_process_lifetime"] = sampler.peak_kib
            metrics["rss_hwm_kib_process_lifetime"] = max(sampler.high_water_samples) if sampler.high_water_samples else None
            metrics["api_process_cpu_seconds_sampled"] = sampler.process_cpu_seconds
        if active_worker.poll() is None:
            shutdown = _stop_owned_worker(active_worker)
            metrics["final_worker_shutdown"] = shutdown
            if not shutdown.get("graceful"):
                metrics["status"] = "failed"
                _write_metrics(metrics)
                if sys.exc_info()[0] is None:
                    raise AssertionError(f"restarted API worker failed graceful SIGTERM: {shutdown}")
                # Preserve the original startup/assertion error; metrics retain
                # the independent shutdown failure for the same execution.
        elif metrics.get("failure_shutdown_acceptance") is None:
            metrics["final_worker_shutdown"] = {
                "status": "NOT RUN",
                "reason": "owned API worker already exited before post-load shutdown gate",
                "return_code": active_worker.poll(),
            }
        _write_metrics(metrics)
