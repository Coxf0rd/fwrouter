from __future__ import annotations

import ipaddress
import ctypes
import hashlib
import json
import os
import re
import selectors
import socket
import sqlite3
import stat
import struct
import subprocess
import time
from datetime import datetime, timedelta, timezone
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
import yaml

from .http_support import http_json
from .joined_support import await_core_job
from .native_runner import _redact_diagnostic
from .synthetic_subscription import SyntheticSubscriptionSource
from .test_core_provider_mihomo import _configure_provider, _enable_owned_mihomo_runtime_state, _seed_source
from .xray_support import (
    assert_mihomo_launch_matches_active,
    assert_native_matches_active,
    await_job,
    enable_vpn_from_synthetic_subscription,
    loaded_identities,
    native_config_bytes,
)


CLIENT_IP = "10.240.0.2"
ROUTER_WAN_IP = "198.18.240.1"
ENDPOINT_IP = "198.18.240.2"
ENDPOINT_SELECTOR_PORT = 5302
SELECTOR_XRAY_ID = "e27bc911-23fb-4d12-8d51-bb1b9cb2cd08"
SERVICE_VIP = "203.0.113.53"
DNS_VPN_SERVICE_VIP = "198.51.100.77"
PROTECTED_VIP = "10.250.240.2"
CLIENT_CONTROL = "http://10.240.0.2:8081"
ENDPOINT_CONTROL = "http://198.18.240.2:8082"
CAPTURE_ROOT = Path("/tmp/fwrouter-packet-evidence")
TCP_CAPTURE_PATH = CAPTURE_ROOT / "tcp.pcap"
UDP_CAPTURE_PATH = CAPTURE_ROOT / "udp.pcap"
CAPTURE_SUMMARY_PATH = CAPTURE_ROOT / "appboundedpacket-summary.json"
CAPTURE_LIMIT = 2048
TCP_SNAPLEN = 54
UDP_SNAPLEN = 42
NEGATIVE_CAPTURE_DRAIN_SECONDS = 1.0
CORE_NFT_TABLE = "fwrouter_v2"
ROUTER_GUARD_TABLE = "fwrouter_packet_guard"
MAX_CORE_NFT_JSON = 1024 * 1024
MAX_PHASE_DIAGNOSTIC_BYTES = 4096
_DIAGNOSTIC_URL = re.compile(r"(?i)\b[a-z][a-z0-9+.-]{1,15}://[^\s\"'<>]+")


def _xray_bindings_startup_projection(payload: Any) -> tuple[dict[str, Any], list[tuple[str, datetime]]]:
    """Preserve the complete bindings payload except its two refreshed UTC timestamps."""
    if not isinstance(payload, dict) or payload.get("bindings_version") != 1:
        raise AssertionError("Xray bindings payload has an unsupported schema.")

    def parse_utc_timestamp(value: Any, field: str) -> datetime:
        if not isinstance(value, str) or "T" not in value:
            raise AssertionError(f"Xray bindings timestamp is missing or malformed: {field}.")
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise AssertionError(f"Xray bindings timestamp is malformed: {field}.") from exc
        if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
            raise AssertionError(f"Xray bindings timestamp is not UTC: {field}.")
        return parsed

    bindings = payload.get("bindings")
    handoffs = payload.get("handoff_listeners")
    client_modes = payload.get("client_modes")
    if (not isinstance(bindings, list) or not all(isinstance(row, dict) for row in bindings)
            or not isinstance(handoffs, list) or not all(isinstance(row, dict) for row in handoffs)
            or not isinstance(client_modes, list) or not all(isinstance(row, dict) for row in client_modes)):
        raise AssertionError("Xray bindings payload has an invalid collection shape.")
    expected_counts = {
        "bindings_count": len(bindings),
        "applied_count": sum(row.get("status") == "applied" for row in bindings),
        "handoff_count": len(handoffs),
        "client_modes_count": len(client_modes),
    }
    if any(type(payload.get(key)) is not int or payload.get(key) != value
           for key, value in expected_counts.items()):
        raise AssertionError("Xray bindings payload counters do not match its contents.")

    generated_at = parse_utc_timestamp(payload.get("generated_at"), "generated_at")
    timestamps = [("generated_at", generated_at)]
    normalized_bindings = []
    for index, binding in enumerate(bindings):
        field = f"bindings[{index}].applied_at"
        applied_at = parse_utc_timestamp(binding.get("applied_at"), field)
        if applied_at > generated_at:
            raise AssertionError(f"Xray bindings timestamp is later than generated_at: {field}.")
        timestamps.append((field, applied_at))
        normalized_bindings.append({key: value for key, value in binding.items() if key != "applied_at"})

    normalized = {
        key: value for key, value in payload.items() if key not in {"generated_at", "bindings"}
    }
    normalized["bindings"] = normalized_bindings
    return normalized, timestamps


def _assert_xray_bindings_startup_equivalent(before: Any, after: Any) -> None:
    before_semantics, before_timestamps = _xray_bindings_startup_projection(before)
    after_semantics, after_timestamps = _xray_bindings_startup_projection(after)
    if before_semantics != after_semantics:
        raise AssertionError("Xray bindings semantic payload changed across startup.")
    if [field for field, _ in before_timestamps] != [field for field, _ in after_timestamps]:
        raise AssertionError("Xray bindings timestamp fields changed across startup.")
    if any(after_time < before_time for (_, before_time), (_, after_time)
           in zip(before_timestamps, after_timestamps, strict=True)):
        raise AssertionError("Xray bindings timestamps moved backwards across startup.")


def _subscription_profile_snapshot_projection(
    rows: Any,
) -> tuple[list[dict[str, Any]], list[tuple[str, datetime]]]:
    """Keep complete profile rows except their two SQLite UTC verification timestamps."""
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise AssertionError("Subscription profile snapshot rows have an invalid shape.")

    def parse_sqlite_utc_timestamp(value: Any, field: str) -> datetime:
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", value):
            raise AssertionError(f"Subscription profile timestamp is missing or malformed: {field}.")
        try:
            return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except ValueError as exc:
            raise AssertionError(f"Subscription profile timestamp is malformed: {field}.") from exc

    normalized_rows = []
    timestamps = []
    for index, row in enumerate(rows):
        if not isinstance(row.get("token"), str) or not isinstance(row.get("nodes_json"), str):
            raise AssertionError(f"Subscription profile snapshot identity is invalid at row {index}.")
        try:
            nodes = json.loads(row["nodes_json"])
        except json.JSONDecodeError as exc:
            raise AssertionError(f"Subscription profile snapshot nodes are invalid at row {index}.") from exc
        if not isinstance(nodes, list) or not all(isinstance(node, dict) for node in nodes):
            raise AssertionError(f"Subscription profile snapshot nodes have an invalid shape at row {index}.")
        verified_at = parse_sqlite_utc_timestamp(row.get("runtime_verified_at"), f"rows[{index}].runtime_verified_at")
        updated_at = parse_sqlite_utc_timestamp(row.get("updated_at"), f"rows[{index}].updated_at")
        timestamps.extend((
            (f"rows[{index}].runtime_verified_at", verified_at),
            (f"rows[{index}].updated_at", updated_at),
        ))
        normalized_rows.append({
            key: value for key, value in row.items() if key not in {"runtime_verified_at", "updated_at"}
        })
    return normalized_rows, timestamps


def _assert_subscription_profile_startup_equivalent(before: Any, after: Any) -> None:
    before_semantics, before_timestamps = _subscription_profile_snapshot_projection(before)
    after_semantics, after_timestamps = _subscription_profile_snapshot_projection(after)
    if before_semantics != after_semantics:
        raise AssertionError("Subscription profile snapshot rows changed across startup.")
    if [field for field, _ in before_timestamps] != [field for field, _ in after_timestamps]:
        raise AssertionError("Subscription profile snapshot timestamp fields changed across startup.")
    if any(after_time < before_time for (_, before_time), (_, after_time)
           in zip(before_timestamps, after_timestamps, strict=True)):
        raise AssertionError("Subscription profile snapshot timestamps moved backwards across startup.")


def _assert_subject_overrides_startup_equivalent(
    before_server: Any,
    after_server: Any,
    before_user: Any,
    after_user: Any,
    *,
    allow_first_startup_owner: bool,
) -> None:
    if before_user != after_user:
        raise AssertionError("User subject overrides changed across startup.")
    if not isinstance(before_server, list) or not isinstance(after_server, list):
        raise AssertionError("Server subject overrides have an invalid shape.")
    if len(before_server) != len(after_server):
        raise AssertionError("Server subject override rows changed across startup.")

    def parse_timestamp(value: Any, field: str) -> datetime:
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", value):
            raise AssertionError(f"Server subject override timestamp is malformed: {field}.")
        try:
            return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except ValueError as exc:
            raise AssertionError(f"Server subject override timestamp is malformed: {field}.") from exc

    before_by_id = {row.get("subject_id"): row for row in before_server if isinstance(row, dict)}
    after_by_id = {row.get("subject_id"): row for row in after_server if isinstance(row, dict)}
    if (len(before_by_id) != len(before_server) or len(after_by_id) != len(after_server)
            or set(before_by_id) != set(after_by_id)):
        raise AssertionError("Server subject override identities changed across startup.")
    for subject_id, before_row in before_by_id.items():
        after_row = after_by_id[subject_id]
        if set(before_row) != set(after_row):
            raise AssertionError("Server subject override fields changed across startup.")
        for field in before_row:
            before_value = before_row[field]
            after_value = after_row[field]
            if field in {"requested_by", "updated_by"} and allow_first_startup_owner:
                if (after_value != before_value
                        and after_value != "startup-xray-vpn-auto-reconcile"):
                    raise AssertionError(f"Server subject override owner changed unexpectedly: {field}.")
            elif field == "updated_at" and allow_first_startup_owner:
                before_time = parse_timestamp(before_value, f"{subject_id}.before.updated_at")
                after_time = parse_timestamp(after_value, f"{subject_id}.after.updated_at")
                if after_time < before_time:
                    raise AssertionError("Server subject override timestamp moved backwards across startup.")
            elif before_value != after_value:
                raise AssertionError(f"Server subject override semantic field changed across startup: {field}.")
        changed_owner = any(
            field in before_row and before_row[field] != after_row[field]
            for field in ("requested_by", "updated_by")
        )
        if (allow_first_startup_owner and before_row.get("updated_at") != after_row.get("updated_at")
                and not changed_owner):
            raise AssertionError("Server subject override timestamp changed without an owner update.")


def _assert_subject_inventory_startup_equivalent(
    before_rows: Any,
    after_rows: Any,
    *,
    allowed_added_ids: set[str] | frozenset[str] = frozenset(),
) -> None:
    """Allow only the observation timestamps refreshed by explicit Xray inventory."""
    if (not isinstance(before_rows, (list, tuple)) or not isinstance(after_rows, (list, tuple))
            or not all(isinstance(row, dict) for row in before_rows)
            or not all(isinstance(row, dict) for row in after_rows)):
        raise AssertionError("Subject inventory rows have an invalid shape.")
    before_by_id = {row.get("subject_id"): dict(row) for row in before_rows}
    after_by_id = {row.get("subject_id"): dict(row) for row in after_rows}
    if (len(before_by_id) != len(before_rows) or len(after_by_id) != len(after_rows)
            or None in before_by_id or None in after_by_id):
        raise AssertionError("Subject inventory identities are invalid across startup.")
    if not isinstance(allowed_added_ids, (set, frozenset)) or not all(
        isinstance(subject_id, str) for subject_id in allowed_added_ids
    ):
        raise AssertionError("Allowed bootstrap subject additions have an invalid shape.")
    if not allowed_added_ids <= {"fwrouter:global", "host:ssh"}:
        raise AssertionError("Only documented built-in subjects may be added during startup.")
    added_ids = after_by_id.keys() - before_by_id.keys()
    removed_ids = before_by_id.keys() - after_by_id.keys()
    if removed_ids or added_ids != allowed_added_ids:
        raise AssertionError("Subject inventory identities changed outside allowed bootstrap additions.")

    def parse_sqlite_utc(value: Any, field: str) -> datetime:
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", value):
            raise AssertionError(f"Subject inventory timestamp is missing or malformed: {field}.")
        try:
            return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except ValueError as exc:
            raise AssertionError(f"Subject inventory timestamp is malformed: {field}.") from exc

    def parse_metadata_utc(value: Any, field: str) -> datetime:
        if not isinstance(value, str) or "T" not in value:
            raise AssertionError(f"Subject inventory timestamp is missing or malformed: {field}.")
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise AssertionError(f"Subject inventory timestamp is malformed: {field}.") from exc
        if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
            raise AssertionError(f"Subject inventory timestamp is not UTC: {field}.")
        return parsed

    for subject_id, before in before_by_id.items():
        after = after_by_id[subject_id]
        if before.keys() != after.keys():
            raise AssertionError(f"Subject inventory columns changed: {subject_id}.")
        is_xray = (
            before.get("subject_type") == "explicit_external_client"
            and before.get("implementation_kind") == "xray"
        )
        if not is_xray:
            if before != after:
                raise AssertionError(f"Non-Xray subject changed across startup: {subject_id}.")
            continue

        try:
            before_metadata = json.loads(before.get("metadata_json"))
            after_metadata = json.loads(after.get("metadata_json"))
        except (TypeError, json.JSONDecodeError) as exc:
            raise AssertionError(f"Xray subject metadata is not valid JSON: {subject_id}.") from exc
        if not isinstance(before_metadata, dict) or not isinstance(after_metadata, dict):
            raise AssertionError(f"Xray subject metadata is not an object: {subject_id}.")

        before_updated = parse_sqlite_utc(before.get("updated_at"), f"{subject_id}.updated_at")
        after_updated = parse_sqlite_utc(after.get("updated_at"), f"{subject_id}.updated_at")
        before_collected = parse_metadata_utc(
            before_metadata.get("collected_at"), f"{subject_id}.metadata.collected_at",
        )
        after_collected = parse_metadata_utc(
            after_metadata.get("collected_at"), f"{subject_id}.metadata.collected_at",
        )
        if after_updated < before_updated or after_collected < before_collected:
            raise AssertionError(f"Xray subject observation timestamp moved backwards: {subject_id}.")

        before_metadata.pop("collected_at")
        after_metadata.pop("collected_at")
        before_semantic = {key: value for key, value in before.items() if key not in {"updated_at", "metadata_json"}}
        after_semantic = {key: value for key, value in after.items() if key not in {"updated_at", "metadata_json"}}
        if before_semantic != after_semantic or before_metadata != after_metadata:
            raise AssertionError(f"Xray subject changed outside observation timestamps: {subject_id}.")


def _rules_apply_failure_diagnostic(
    response: dict[str, Any],
    status: int,
    *,
    protected: tuple[str, ...],
    phase: str,
    terminal_job: dict[str, Any] | None = None,
) -> dict[str, Any]:
    data = response.get("data") if isinstance(response.get("data"), dict) else {}
    response_job = data.get("job") if isinstance(data.get("job"), dict) else {}
    job = terminal_job if isinstance(terminal_job, dict) else response_job
    error = response.get("error") if isinstance(response.get("error"), dict) else {}
    if not error and isinstance(data.get("error"), dict):
        error = data["error"]
    raw_message = error.get("message") or job.get("error_message") or ""
    safe_message = _redact_diagnostic(str(raw_message), limit=512)
    safe_message = _DIAGNOSTIC_URL.sub("[URL]", safe_message)
    for value in protected:
        if value:
            for protected_value in (value, *value.splitlines()):
                safe_message = safe_message.replace(protected_value, "[REDACTED]")
    safe_message = "".join(character if character.isprintable() or character in "\n\t" else " "
                             for character in safe_message)
    exception_class = error.get("exception_class") or error.get("error_class") \
        or job.get("exception_class") or job.get("error_class")
    if not isinstance(exception_class, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", exception_class):
        exception_class = None
    def safe_token(value: Any) -> str | None:
        return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,96}", value) else None

    return {
        "http_status": status,
        "failure_phase": phase,
        "error_code": safe_token(error.get("code")),
        "job_type": safe_token(job.get("job_type")),
        "job_status": safe_token(job.get("status")),
        "job_error_code": safe_token(job.get("error_code")),
        "exception_class": exception_class,
        "error_message": safe_message,
    }


def _serialize_rules_apply_failure(summary: dict[str, Any]) -> str:
    encoded = json.dumps(summary, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > 2048:
        summary = {**summary, "error_message": _redact_diagnostic(summary.get("error_message", ""), limit=128)}
        encoded = json.dumps(summary, sort_keys=True, separators=(",", ":"))
    return encoded


def _record_dns53_probe_failure(record_property: Any, receipt: Any) -> bool:
    if not callable(record_property) or not isinstance(receipt, dict):
        return False
    if set(receipt) != {"schema", "query_label", "transport", "error_code"}:
        return False
    if any(not isinstance(value, str) for value in receipt.values()):
        return False
    if (receipt.get("schema") != "fwrouter-dns53-probe-failure/v1"
            or receipt.get("query_label") not in {"vpn", "direct"}
            or receipt.get("transport") not in {"udp", "tcp"}
            or receipt.get("error_code") not in {"timeout", "os_error", "response_mismatch", "fixture_error"}):
        return False
    encoded = json.dumps(receipt, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode("ascii")) > 256:
        return False
    record_property("dns53_probe_failure", encoded)
    return True


def _record_dns53_flow_failure(record_property: Any, receipt: Any) -> bool:
    if not callable(record_property) or not isinstance(receipt, dict):
        return False
    required = {"schema", "query_label", "stage", "error_code"}
    if not required <= set(receipt) or set(receipt) - required - {"peer_class"}:
        return False
    if any(not isinstance(value, str) for value in receipt.values()):
        return False
    if (receipt.get("schema") != "fwrouter-dns53-flow-failure/v1"
            or receipt.get("query_label") not in {"vpn", "direct"}
            or receipt.get("stage") not in {"dns_resolution", "tcp_connect", "tcp_receive", "tcp_shape",
                                              "tcp_peer", "udp_receive", "udp_shape"}
            or receipt.get("error_code") not in {"timeout", "os_error", "fixture_error", "response_mismatch"}):
        return False
    if "peer_class" in receipt and (receipt.get("stage") != "tcp_peer"
            or receipt["peer_class"] not in {"allowed", "endpoint_wan", "router_wan", "lan_client", "other", "missing"}):
        return False
    encoded = json.dumps(receipt, sort_keys=True, separators=(",", ":"))
    if len(encoded.encode("ascii")) > 256:
        return False
    record_property("dns53_flow_failure", encoded)
    return True


def _packet_selectable_vless_profile() -> dict[str, Any]:
    """Build one synthetic subscription target backed by the owned packet Xray inbound."""
    return {
        "remarks": "acceptance-owned-packet-selector-profile",
        "outbounds": [
            {
                "tag": "packet-selector-target",
                "protocol": "vless",
                "settings": {"vnext": [{
                    "address": ENDPOINT_IP,
                    "port": ENDPOINT_SELECTOR_PORT,
                    "users": [{"id": SELECTOR_XRAY_ID, "encryption": "none"}],
                }]},
                "streamSettings": {"network": "tcp", "security": "none"},
            },
            {"tag": "direct", "protocol": "freedom"},
            {"tag": "reject", "protocol": "blackhole"},
            {"tag": "dns", "protocol": "dns"},
        ],
        "routing": {"rules": [{"type": "field", "outboundTag": "direct"}]},
    }


@pytest.mark.packet
def test_verified_legacy_admission_survives_normal_startup_without_reapply_or_refresh(acceptance_stack, record_property):
    """Normal packet-profile startup preserves an explicitly admitted selective/VPN legacy inventory."""
    _run_verified_legacy_startup_lifecycle(acceptance_stack, fixed_target=False, record_property=record_property)


@pytest.mark.packet
def test_fixed_legacy_admission_survives_two_normal_startups(acceptance_stack, record_property):
    """An off-list active fixed target survives owned apply, strict admission and two startups."""
    _run_verified_legacy_startup_lifecycle(acceptance_stack, fixed_target=True, record_property=record_property)


def _run_verified_legacy_startup_lifecycle(stack, *, fixed_target: bool, record_property):
    state, native, source_bridge = stack["state"], stack["native"], stack["provider_bridge"]
    api = stack["api"]
    assert stack["profile"].get("profile") == "hosted-kernel-packet"
    endpoint_ready = _role_json(f"{ENDPOINT_CONTROL}/ready")
    endpoint_sockets = endpoint_ready.get("sockets")
    endpoint_xray = endpoint_ready.get("xray")
    assert (endpoint_ready.get("ready") is True and endpoint_ready.get("role") == "endpoint"
            and isinstance(endpoint_sockets, dict) and endpoint_sockets.get("xray_tcp_5302") is True
            and isinstance(endpoint_xray, dict) and endpoint_xray.get("running") is True
            and endpoint_xray.get("binary_sha256") == stack["profile"].get("xray", {}).get("sha256")), {
        "endpoint_ready": endpoint_ready.get("ready") is True,
        "endpoint_role": endpoint_ready.get("role"),
        "selector_listener_ready": isinstance(endpoint_sockets, dict)
        and endpoint_sockets.get("xray_tcp_5302") is True,
        "xray_binary_matches_profile": isinstance(endpoint_xray, dict)
        and endpoint_xray.get("binary_sha256") == stack["profile"].get("xray", {}).get("sha256"),
    }
    provider_calls_before_fixture = source_bridge.snapshot_calls()
    source = SyntheticSubscriptionSource(_packet_selectable_vless_profile())

    def native_core_table_semantics() -> tuple[str, int]:
        from fwrouter_api.services.native_proof import canonical_nft_table, canonical_nft_table_sha256

        try:
            result = subprocess.run(
                ("/usr/sbin/nft", "-j", "-s", "list", "table", "inet", CORE_NFT_TABLE),
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=4, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AssertionError("bounded canonical Core nft readback did not complete") from exc
        assert (result.returncode == 0 and len(result.stdout) <= MAX_CORE_NFT_JSON
                and len(result.stderr) <= 4096), "bounded canonical Core nft readback failed"
        try:
            payload = json.loads(result.stdout)
            canonical = canonical_nft_table(payload)
        except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise AssertionError("canonical Core nft readback was invalid or unsupported") from exc
        counters = canonical.get("counters")
        assert isinstance(counters, list) and counters, "owned Core table has no canonical named counters"
        return canonical_nft_table_sha256(payload), len(counters)

    def post_apply(payload: dict[str, Any]) -> dict[str, Any]:
        status, result = http_json(f"{api}/routing/global", method="POST", payload=payload, timeout=45)
        assert status == 200 and result.get("ok") is True, result
        job = await_job(api, result)
        assert job.get("status") == "success", job
        return job

    def mutation_apply_id(job: dict[str, Any]) -> str:
        result = job.get("result") if isinstance(job.get("result"), dict) else {}
        mutation = result.get("mutation") if isinstance(result.get("mutation"), dict) else {}
        apply_id = mutation.get("apply_id")
        assert (job.get("status") == "success" and result.get("job_status") == "success"
                and mutation.get("ok") is True and isinstance(apply_id, str) and apply_id), job
        return apply_id

    def native_fingerprint() -> dict[str, Any]:
        xray_bytes, _launch = native_config_bytes(native)
        loaded = loaded_identities(native)
        active_mihomo = state / "generated" / "mihomo" / "config.yaml"
        bindings_path = state / "xray" / "fwrouter-bindings.json"
        return {
            "xray_active": hashlib.sha256((state / "xray" / "config.json").read_bytes()).hexdigest(),
            "xray_mounted": hashlib.sha256(xray_bytes).hexdigest(),
            "xray_loaded": hashlib.sha256(json.dumps(loaded, separators=(",", ":")).encode()).hexdigest(),
            "xray_loaded_identity_count": len(loaded),
            "xray_bindings": hashlib.sha256(bindings_path.read_bytes()).hexdigest(),
            "xray_last_good": _packet_tree_fingerprint(state / "last-good" / "xray"),
            "mihomo_active": hashlib.sha256(active_mihomo.read_bytes()).hexdigest(),
            "mihomo_generated": _packet_tree_fingerprint(
                state / "generated" / "mihomo", excluded={"reconcile-state.json"},
            ),
            "mihomo_last_good": _packet_tree_fingerprint(state / "last-good" / "mihomo"),
            "xray_incarnation": (native.process.pid, native.started_at),
            "mihomo_incarnation": (native.mihomo_process.pid, native.mihomo_started_at),
            "xray_reload_calls": native.reload_calls,
        }

    def startup_native_snapshot() -> tuple[dict[str, Any], dict[str, Any]]:
        bindings_path = state / "xray" / "fwrouter-bindings.json"
        bindings_bytes_before = bindings_path.read_bytes()
        try:
            bindings_payload = json.loads(bindings_bytes_before)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise AssertionError("Xray bindings payload was not valid JSON at startup boundary.") from exc
        semantics, _timestamps = _xray_bindings_startup_projection(bindings_payload)
        fingerprint = native_fingerprint()
        if bindings_path.read_bytes() != bindings_bytes_before:
            raise AssertionError("Xray bindings payload changed while capturing startup fingerprint.")
        fingerprint["xray_bindings"] = hashlib.sha256(json.dumps(
            semantics, sort_keys=True, separators=(",", ":"),
        ).encode()).hexdigest()
        return fingerprint, bindings_payload

    def durable_fingerprint(core_apply_id: str, *, identities_only: bool = False) -> dict[str, Any]:
        connection = sqlite3.connect(f"file:{state / 'fwrouter.db'}?mode=ro", uri=True, timeout=5)
        try:
            values = {
                table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY 1, 2")]
                for table in (
                    "subscription_node_identities", "subscription_node_aliases",
                    "subscription_node_credentials", "subscription_node_credential_history",
                    "subscription_node_identity_transitions",
                )
            }
            values["core"] = {
                "revision": connection.execute(
                    "SELECT value_json FROM settings WHERE key='core.intent_revision'",
                ).fetchone(),
                "module": connection.execute(
                    "SELECT desired_state, lifecycle_mode, runtime_state, apply_state "
                    "FROM modules WHERE module_name='core'",
                ).fetchone(),
                "receipt": connection.execute(
                    "SELECT apply_id, status, core_intent_revision FROM apply_versions WHERE apply_id=?",
                    (core_apply_id,),
                ).fetchone(),
            }
            if identities_only:
                values.pop("core")
            return {
                "sha256": hashlib.sha256(json.dumps(values, sort_keys=True, default=list).encode()).hexdigest(),
                "identity_rows": len(values["subscription_node_identities"]),
                "credential_rows": len(values["subscription_node_credentials"]),
                "credential_history_rows": len(values["subscription_node_credential_history"]),
            }
        finally:
            connection.close()

    def subscription_profile_snapshot_rows() -> list[dict[str, Any]]:
        connection = sqlite3.connect(f"file:{state / 'fwrouter.db'}?mode=ro", uri=True, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM subscription_profile_snapshots ORDER BY token",
            ).fetchall()]
        finally:
            connection.close()

    def subject_override_rows() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        connection = sqlite3.connect(f"file:{state / 'fwrouter.db'}?mode=ro", uri=True, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            server_rows = [dict(row) for row in connection.execute(
                "SELECT * FROM subject_server_overrides ORDER BY subject_id",
            ).fetchall()]
            user_rows = [dict(row) for row in connection.execute(
                "SELECT * FROM subject_user_overrides ORDER BY subject_id",
            ).fetchall()]
            return server_rows, user_rows
        finally:
            connection.close()

    def subject_bootstrap_fingerprint() -> dict[str, tuple[tuple[Any, ...], ...]]:
        connection = sqlite3.connect(f"file:{state / 'fwrouter.db'}?mode=ro", uri=True, timeout=5)
        try:
            return {
                table: tuple(tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY 1"))
                for table in ("subjects", "subject_fwrouter", "subject_host")
            }
        finally:
            connection.close()

    def subject_inventory_rows() -> list[dict[str, Any]]:
        connection = sqlite3.connect(f"file:{state / 'fwrouter.db'}?mode=ro", uri=True, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            return [dict(row) for row in connection.execute("SELECT * FROM subjects ORDER BY subject_id")]
        finally:
            connection.close()

    def assert_builtin_bootstrap_rows(
        before: dict[str, tuple[tuple[Any, ...], ...]],
        after: dict[str, tuple[tuple[Any, ...], ...]],
    ) -> None:
        # These are the two documented rows owned by ensure_builtin_system_subjects.
        builtin_ids = {"fwrouter:global", "host:ssh"}
        before_subjects = {row[0]: row for row in before["subjects"]}
        after_subjects = {row[0]: row for row in after["subjects"]}
        assert set(after_subjects) == set(before_subjects) | builtin_ids, {
            "before_ids": sorted(before_subjects), "after_ids": sorted(after_subjects),
        }
        stable_subjects = {
            key: row for key, row in before_subjects.items()
            if not (row[1] == "explicit_external_client" and row[3] == "xray")
        }
        assert all(after_subjects[key] == row for key, row in stable_subjects.items()), {
            key: (row, after_subjects.get(key)) for key, row in before_subjects.items()
            if key in stable_subjects and after_subjects.get(key) != row
        }
        for subject_id, subject_type, role, display_name in (
            ("fwrouter:global", "fwrouter", "router_core", "FWRouter global traffic"),
            ("host:ssh", "host", "host_runtime", "SSH daemon"),
        ):
            row = after_subjects[subject_id]
            assert (row[1], row[2], row[3], row[4], row[5], row[7], row[8], row[9], row[10], row[11], row[12]) == (
                subject_type, role, subject_type, subject_id, display_name,
                "direct", "direct", "clean", "running", 1, 0,
            ), {"subject_id": subject_id, "row": row}
            expected_metadata = (
                {"source": "system_subjects", "component_name": "global"}
                if subject_id == "fwrouter:global" else {
                    "source": "system_subjects", "systemd_unit": "ssh.service",
                    "listen_proto": "tcp", "listen_port": 22, "kind": "management",
                }
            )
            assert json.loads(row[18]) == expected_metadata, {"subject_id": subject_id, "metadata": row[18]}
        for table in ("subject_fwrouter", "subject_host"):
            before_rows = {row[0]: row for row in before[table]}
            after_rows = {row[0]: row for row in after[table]}
            assert set(after_rows) == set(before_rows) | builtin_ids.intersection(
                {"fwrouter:global" if table == "subject_fwrouter" else "host:ssh"},
            ), {"table": table, "before_ids": sorted(before_rows), "after_ids": sorted(after_rows)}
            assert all(after_rows[key] == row for key, row in before_rows.items()), table
        global_detail = {row[0]: row for row in after["subject_fwrouter"]}["fwrouter:global"]
        host_detail = {row[0]: row for row in after["subject_host"]}["host:ssh"]
        assert (global_detail[1], json.loads(global_detail[2])) == (
            "global", {"source": "system_subjects"},
        ), global_detail
        assert (host_detail[1], host_detail[2], host_detail[3], host_detail[4], host_detail[5],
                json.loads(host_detail[6])) == (
            "ssh.service", "tcp", 22, "sshd", "SSH daemon",
            {"source": "system_subjects", "kind": "management"},
        ), host_detail

    def generation(api_url: str) -> dict[str, Any]:
        status, result = http_json(f"{api_url}/__acceptance/subscription/native-generation")
        assert status == 200 and result.get("ok") is True, result
        value = result.get("state")
        assert isinstance(value, dict), result
        return value

    def assert_owned_dns_daemon(api_url: str) -> None:
        status_code, status = http_json(f"{api_url}/__acceptance/dns53/status")
        assert status_code == 200, {"status": status_code}
        for field in (
            "configured", "daemon_running", "daemon_uid_matches", "native_process_arguments_match",
            "dns_capture_ok", "rescue0_allowlisted", "rescue0_role_excluded_from_discovery",
            "dhcp_forces_router_dns", "dhcp_excludes_rescue_vip", "local_hosts_exclude_rescue_vip",
            "dns_capture_excludes_rescue0", "router_lan_hostname_query_ok",
        ):
            assert status.get(field) is True, {"field": field, "status": status}

    def protected_direct_rows() -> list[str]:
        rules_path = state / "generated" / "rules" / "effective-rules.json"
        artifact = json.loads(rules_path.read_text(encoding="utf-8"))
        rules = artifact.get("rules") if isinstance(artifact, dict) else None
        assert isinstance(rules, list), {"effective_rules_shape": type(rules).__name__}
        return sorted(
            json.dumps(row, sort_keys=True, separators=(",", ":"))
            for row in rules
            if isinstance(row, dict) and row.get("source") == "protected" and row.get("action") == "DIRECT"
        )

    def _nft_set_text_contains_ipv4(nft_text: str, set_name: str, address: str) -> bool:
        set_match = re.search(
            rf"(?ms)^\s*set\s+{re.escape(set_name)}\s*\{{(?P<body>.*?)^\s*\}}",
            nft_text,
        )
        values: list[str] = []
        if set_match is not None:
            elements_match = re.search(r"elements\s*=\s*\{([^}]*)\}", set_match.group("body"))
            if elements_match is not None:
                values.extend(elements_match.group(1).split(","))
        values.extend(re.findall(
            rf"(?m)^\s*add element inet {re.escape(CORE_NFT_TABLE)} {re.escape(set_name)} "
            r"\{([^}]*)\}\s*$",
            nft_text,
        ))
        target = ipaddress.ip_address(address)
        for raw_value in values:
            value = raw_value.strip()
            if not value:
                continue
            try:
                if target in ipaddress.ip_network(value, strict=False):
                    return True
            except ValueError:
                continue
        return False

    def native_global_rule_set_membership() -> dict[str, bool]:
        try:
            result = subprocess.run(
                ("/usr/sbin/nft", "-j", "list", "table", "inet", CORE_NFT_TABLE),
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=3, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AssertionError("global ruleset native readback did not complete within its bound") from exc
        if (result.returncode or len(result.stdout) > MAX_CORE_NFT_JSON
                or len(result.stderr) > 4096):
            raise AssertionError("global ruleset native readback failed or exceeded its fixed byte bound")
        try:
            nftables = json.loads(result.stdout).get("nftables")
        except (UnicodeError, json.JSONDecodeError, AttributeError) as exc:
            raise AssertionError("global ruleset native readback was not valid nft JSON") from exc
        if not isinstance(nftables, list):
            raise AssertionError("global ruleset native readback has an invalid nftables shape")

        sets: dict[str, list[Any]] = {}
        for item in nftables:
            entry = item.get("set") if isinstance(item, dict) else None
            if (not isinstance(entry, dict) or entry.get("family") != "inet"
                    or entry.get("table") != CORE_NFT_TABLE
                    or entry.get("name") not in {"vpn_ipv4", "direct_ipv4", "protected_ipv4"}):
                continue
            name = str(entry["name"])
            assert name not in sets, f"duplicate native FWRouter set readback: {name}"
            elements = entry.get("elem")
            assert isinstance(elements, list), f"native FWRouter set has invalid elements: {name}"
            sets[name] = elements

        def contains(name: str, address: str) -> bool:
            target = ipaddress.ip_address(address)
            for raw_element in sets.get(name, []):
                element = raw_element
                if isinstance(element, dict) and isinstance(element.get("elem"), (dict, str)):
                    element = element["elem"]
                if isinstance(element, dict) and isinstance(element.get("prefix"), dict):
                    prefix = element["prefix"]
                    value = prefix.get("addr")
                    length = prefix.get("len")
                    if isinstance(value, str) and type(length) is int:
                        element = f"{value}/{length}"
                if not isinstance(element, str):
                    continue
                try:
                    if target in ipaddress.ip_network(element, strict=False):
                        return True
                except ValueError:
                    continue
            return False

        assert set(sets) == {"vpn_ipv4", "direct_ipv4", "protected_ipv4"}, sorted(sets)
        protected_readback = _core_protected_exception_readback()
        return {
            "manual_vpn_ipv4": contains("vpn_ipv4", "198.51.100.77"),
            "manual_direct_ipv4": contains("direct_ipv4", "203.0.113.53"),
            "protected_vip_ipv4": contains("protected_ipv4", PROTECTED_VIP),
            "protected_classify_rule": protected_readback.get("classifier_bypass_installed") is True,
        }

    def global_rules_source_fingerprint(
        expected_apply_id: str,
        *,
        previous_applied_mode: str,
    ) -> dict[str, Any]:
        rules_path = state / "generated" / "rules" / "effective-rules.json"
        assert rules_path.stat().st_size <= MAX_CORE_NFT_JSON, "canonical effective rules exceeded its byte bound"
        rules_bytes = rules_path.read_bytes()
        try:
            artifact = json.loads(rules_bytes)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise AssertionError("canonical effective rules were not valid JSON") from exc
        rows = artifact.get("rules") if isinstance(artifact, dict) else None
        assert isinstance(rows, list), {"effective_rules_shape": type(rows).__name__}
        manual_pairs = sorted(
            (row.get("action"), row.get("value"))
            for row in rows
            if isinstance(row, dict) and row.get("source") == "manual"
        )
        assert artifact.get("selective_default") == "vpn", artifact
        assert manual_pairs == [
            ("DIRECT", "203.0.113.53"), ("VPN", "198.51.100.77"),
        ], manual_pairs
        protected = protected_direct_rows()
        assert protected, "canonical rules lost protected DIRECT rows"

        dataplane_dir = state / "generated" / "dataplane"
        manifest_paths = {
            "candidate": dataplane_dir / "candidate-manifest.json",
            "applied": dataplane_dir / "applied-manifest.json",
        }
        for label, path in manifest_paths.items():
            value = json.loads(path.read_text(encoding="utf-8"))
            routing_state = value.get("routing_global_state")
            assert value.get("plan_id") == expected_apply_id, {"manifest": label, "plan_id": value.get("plan_id")}
            assert (isinstance(routing_state, dict)
                    and routing_state.get("desired_mode") == "selective"
                    and routing_state.get("applied_mode") == previous_applied_mode
                    and routing_state.get("selective_default") == "vpn"), {
                "manifest": label, "routing": routing_state,
            }

        connection = sqlite3.connect(f"file:{state / 'fwrouter.db'}?mode=ro", uri=True, timeout=5)
        try:
            receipt = connection.execute(
                "SELECT status, core_intent_revision FROM apply_versions WHERE apply_id=?",
                (expected_apply_id,),
            ).fetchone()
            revision_row = connection.execute(
                "SELECT value_json FROM settings WHERE key='core.intent_revision'",
            ).fetchone()
        finally:
            connection.close()
        assert receipt and receipt[0] == "applied" and revision_row, {
            "apply_id": expected_apply_id, "receipt": receipt,
        }
        assert receipt[1] == int(json.loads(revision_row[0])), {
            "apply_id": expected_apply_id, "receipt_revision": receipt[1],
            "current_revision": revision_row[0],
        }

        nft_paths = {
            "candidate": dataplane_dir / "candidate.nft",
            "applied": dataplane_dir / "applied.nft",
            "last_good": state / "last-good" / "dataplane" / "last-good.nft",
        }
        nft_hashes: dict[str, str] = {}
        for label, path in nft_paths.items():
            assert path.stat().st_size <= MAX_CORE_NFT_JSON, {
                "nft_artifact": label, "bytes": path.stat().st_size,
            }
            content = path.read_bytes()
            try:
                nft_text = content.decode("utf-8")
            except UnicodeError as exc:
                raise AssertionError(f"rendered {label} nft artifact was not UTF-8") from exc
            assert _nft_set_text_contains_ipv4(nft_text, "vpn_ipv4", "198.51.100.77"), label
            assert _nft_set_text_contains_ipv4(nft_text, "direct_ipv4", "203.0.113.53"), label
            nft_hashes[label] = hashlib.sha256(content).hexdigest()
        assert nft_hashes["candidate"] == nft_hashes["applied"] == nft_hashes["last_good"], nft_hashes
        native_membership = native_global_rule_set_membership()
        assert all(native_membership.values()), native_membership
        return {
            "effective_rules_sha256": hashlib.sha256(rules_bytes).hexdigest(),
            "protected_direct_sha256": hashlib.sha256("\n".join(protected).encode("utf-8")).hexdigest(),
            "manual_rule_pairs": manual_pairs,
            "nft_sha256": nft_hashes,
            "native_membership": native_membership,
        }

    try:
        # Import under the established fixed-without-target state to avoid
        # probing before the managed VPN owner has materialized the native
        # inventory. The single synthetic endpoint below is an owned packet
        # fixture listener, not an external provider or arbitrary target.
        post_apply({"mode": "direct", "requested_by": "hosted-acceptance", "run_now": True})
        post_apply({"server_mode": "fixed", "requested_by": "hosted-acceptance", "run_now": True})
        routing_code, routing_response = http_json(f"{api}/routing/global")
        initial_routing = ((routing_response.get("data") or {}).get("routing") or {})
        assert routing_code == 200 and routing_response.get("ok") is True, routing_response
        assert (initial_routing.get("desired_mode"), initial_routing.get("applied_mode"),
                initial_routing.get("server_mode"), initial_routing.get("desired_fixed_server_id"),
                initial_routing.get("applied_fixed_server_id")) == ("direct", "direct", "fixed", None, None), initial_routing

        code, imported = http_json(
            f"{api}/subscription", method="POST",
            payload={"urls": [source.url], "requested_by": "hosted-acceptance-synthetic"}, timeout=90,
        )
        assert code == 200 and imported.get("ok") is True, imported
        assert source.snapshot_requests() == 2, {
            "import_owner_synthetic_source_requests": source.snapshot_requests(),
        }
        assert source_bridge.snapshot_calls() == provider_calls_before_fixture, {
            "provider_bridge_calls_after_import": True,
        }
        imported_data = imported.get("data") if isinstance(imported.get("data"), dict) else {}
        auto_select = imported_data.get("auto_select")
        assert isinstance(auto_select, dict) and auto_select.get("status") == "skipped_no_fixed_target", imported
        # Resolve the imported candidate read-only, then establish eligibility
        # and selection exclusively through public owner APIs.
        connection = sqlite3.connect(f"file:{state / 'fwrouter.db'}?mode=ro", uri=True, timeout=5)
        try:
            connection.execute("PRAGMA query_only=ON")
            member_rows = connection.execute(
                "SELECT logical_server_id, member_config_json FROM logical_server_members "
                "WHERE is_active=1 AND json_extract(member_config_json, '$.server')=? "
                "AND json_extract(member_config_json, '$.port')=? LIMIT 2",
                (ENDPOINT_IP, ENDPOINT_SELECTOR_PORT),
            ).fetchall()
        finally:
            connection.close()
        packet_target_ids = []
        for logical_server_id, member_config_json in member_rows:
            try:
                member_config = json.loads(member_config_json)
            except (TypeError, json.JSONDecodeError):
                continue
            if (isinstance(member_config, dict)
                    and member_config.get("server") == ENDPOINT_IP
                    and member_config.get("port") == ENDPOINT_SELECTOR_PORT):
                packet_target_ids.append(str(logical_server_id))
        assert len(packet_target_ids) == 1, {
            "owned_packet_target_count": len(packet_target_ids),
            "owned_endpoint": ENDPOINT_IP,
            "owned_port": ENDPOINT_SELECTOR_PORT,
        }
        packet_target_id = packet_target_ids[0]

        if fixed_target:
            # Use the supported manual-only policy before owner generation.
            # Priority -1 excludes auto selection and Xray auto-client pools;
            # retaining the flag prevents refresh's empty-pool initial seeding.
            # Changing eligibility after generation would be actual drift.
            preference_code, preference = http_json(
                f"{api}/servers/{packet_target_id}/preferences", method="PATCH",
                payload={"vpn_auto": True, "vpn_auto_priority": -1, "global_list": False,
                         "reconcile_mihomo": True, "requested_by": "hosted-fixed-owner-fixture"},
            )
            assert preference_code == 200 and preference.get("ok") is True, preference
        enable_vpn_from_synthetic_subscription(api, state, source.url)
        assert source.snapshot_requests() == 4, {
            "managed_vpn_owner_additional_synthetic_source_requests": source.snapshot_requests(),
        }
        assert source_bridge.snapshot_calls() == provider_calls_before_fixture, {
            "provider_bridge_calls_after_managed_vpn_enable": True,
        }
        verified = generation(api)
        assert verified.get("status") == "verified", verified
        assert verified.get("candidate_digest") == verified.get("verified_digest"), verified
        assert_native_matches_active(native, state / "xray" / "config.json")
        assert_mihomo_launch_matches_active(native, state / "generated" / "mihomo" / "config.yaml")
        bindings_path = state / "xray" / "fwrouter-bindings.json"
        assert bindings_path.is_file(), "managed synthetic Xray enable did not materialize runtime bindings"

        if fixed_target:
            # Explicit synthetic operator intent; fixed apply must never enable
            # a disabled Core on its own. The unified owner completes pending.
            core_code, core_request = http_json(
                f"{api}/modules/core/desired-state", method="POST",
                payload={"desired_state": "enabled", "requested_by": "hosted-fixed-owner-fixture", "run_now": False},
            )
            assert core_code == 200 and core_request.get("ok") is True, core_request
            assert core_request.get("data", {}).get("module", {}).get("desired_state") == "enabled"
        before_fixed_transition_native = native_fingerprint() if fixed_target else None
        before_fixed_transition_durable = durable_fingerprint("", identities_only=True) if fixed_target else None

        def packet_selection_snapshot(api_url: str) -> dict[str, Any]:
            status_code, response = http_json(f"{api_url}/selector/vpn-auto/state")
            data = response.get("data") if isinstance(response, dict) else None
            selector_state = data.get("vpn_auto") if isinstance(data, dict) else None
            selector_state = selector_state if isinstance(selector_state, dict) else {}
            runtime_selectors = selector_state.get("selector_runtime")
            runtime_selectors = runtime_selectors if isinstance(runtime_selectors, dict) else {}
            fence = _read_packet_selection_fence()
            snapshot = {
                "server_mode": selector_state.get("server_mode"),
                "active_auto_server_id": selector_state.get("active_auto_server_id"),
                "active_auto_target_valid": selector_state.get("active_auto_target_valid"),
                "runtime_vpn_auto_contains_target": packet_runtime_name
                in selector_state.get("runtime_vpn_auto_targets", []),
                "vpn_auto_now": runtime_selectors.get("vpn_auto_now"),
                "vpn_global_now": runtime_selectors.get("vpn_global_now"),
                "selection_revision": fence.get("revision"),
                "fenced_active_server_id": fence.get("active_server_id"),
                "fenced_provenance_server_id": fence.get("provenance_server_id"),
            }
            if fixed_target:
                routing_status, routing_response = http_json(f"{api_url}/routing/global")
                fixed_routing = routing_response.get("data", {}).get("routing", {})
                native_status, native_global = http_json("http://127.0.0.1:5200/proxies/vpn-global")
                assert (status_code == routing_status == native_status == 200
                        and response.get("ok") is True and routing_response.get("ok") is True
                        and snapshot.get("server_mode") == "fixed"
                        and fixed_routing.get("desired_fixed_server_id") == packet_target_id
                        and fixed_routing.get("applied_fixed_server_id") == packet_target_id
                        and fixed_routing.get("apply_state") == "clean"
                        and snapshot.get("vpn_global_now") == packet_runtime_name
                        and native_global.get("now") == packet_runtime_name
                        and packet_runtime_name in native_global.get("all", [])
                        and snapshot.get("runtime_vpn_auto_contains_target") is False
                        and type(snapshot.get("selection_revision")) is int), {
                    "routing": fixed_routing, "selection": snapshot, "native_global": native_global,
                }
                snapshot["fixed_target_id"] = fixed_routing["desired_fixed_server_id"]
                snapshot["fixed_until"] = fixed_routing.get("fixed_server_until")
                return snapshot
            assert (status_code == 200 and response.get("ok") is True
                    and snapshot.get("server_mode") == "auto"
                    and snapshot.get("active_auto_server_id") == packet_target_id
                    and snapshot.get("active_auto_target_valid") is True
                    and snapshot.get("runtime_vpn_auto_contains_target") is True
                    and snapshot.get("vpn_auto_now") == packet_runtime_name
                    and snapshot.get("vpn_global_now") == "vpn-auto"
                    and snapshot.get("fenced_active_server_id") == packet_target_id
                    and snapshot.get("fenced_provenance_server_id") == packet_target_id
                    and type(snapshot.get("selection_revision")) is int), {
                "status": status_code,
                "selection": snapshot,
            }
            return snapshot

        from fwrouter_api.services.logical_topology import get_logical_runtime_name
        packet_runtime_name = get_logical_runtime_name(packet_target_id)
        if fixed_target:
            connection = sqlite3.connect(f"file:{state / 'fwrouter.db'}?mode=ro", uri=True, timeout=5)
            try:
                assert connection.execute(
                    "SELECT vpn_auto, vpn_auto_priority, global_list FROM server_preferences WHERE server_id=?",
                    (packet_target_id,),
                ).fetchone() == (1, -1, 0), "refresh changed manual-only fixed intent"
            finally:
                connection.close()
            fixed_code, fixed_response = http_json(
                f"{api}/routing/global/fixed-server", method="POST",
                payload={"server_id": packet_target_id, "confirm_switch": True,
                         "post_check": True, "requested_by": "hosted-fixed-owner-fixture"}, timeout=90,
            )
            assert fixed_code == 200 and fixed_response.get("ok") is True, fixed_response
            fixed_job = await_job(api, fixed_response)
            if fixed_job.get("status") != "success":
                # Keep the complete failure phase chain, never configs, IDs or
                # credentials. This is emitted only for this synthetic job.
                observations = []
                allowed = {"ok", "stage", "fixed_selection_stage", "status", "error_code", "reason",
                           "reconcile_reason", "reconcile_action", "last_good_retained", "checkpoint_retained",
                           "config_membership_ok", "native_membership_ok",
                           "logical_group_native_membership_ok", "selected_fixed_target",
                           "vpn_global_matches_target"}

                safe_values = {
                    'owner_fence_before_selection',
                    'membership_before_selection',
                    'selected_proof_before_selection',
                    'selected_proof_after_selection',
                    'owner_fence_after_selection',
                    'CORE_INTENT_STALE',
                    'CORE_NATIVE_PROOF_UNVERIFIED',
                    'CORE_NOT_ENABLED_MANAGED',
                    'FIXED_XRAY_GENERATION_RECEIPT_INVALID',
                    'FIXED_XRAY_MIHOMO_PROOF_FAILED',
                    'FIXED_XRAY_ROUTING_UNAVAILABLE',
                    'FIXED_XRAY_WORKFLOW_CAS_FAILED',
                    'FIXED_XRAY_WORKFLOW_FENCE_CHANGED',
                    'GLOBAL_FIXED_SERVER_FINAL_PROOF_FAILED',
                    'GLOBAL_FIXED_SERVER_WORKFLOW_STALE',
                    'GLOBAL_FIXED_WORKFLOW_INVALID',
                    'MIHOMO_GENERATION_CHECKPOINT_UNAVAILABLE',
                    'MIHOMO_GENERATION_READBACK_UNVERIFIED',
                    'MIHOMO_GENERATION_RECEIPT_PUBLICATION_FAILED',
                    'MIHOMO_GENERATION_RESERVATION_FAILED',
                    'MIHOMO_GENERATION_RESERVATION_INVALID',
                    'MIHOMO_GENERATION_RESERVATION_STALE',
                    'XRAY_NATIVE_PROOF_UNVERIFIED',
                    'XRAY_RECONCILE_FAILED',
                    'checkpoint_operation_invalid',
                    'checkpoint_source_changed',
                    'fixed_workflow_remains_reserved',
                    'fixed_workflow_resume_after_mihomo_commit',
                    'generation_receipt_publication',
                    'generation_reservation',
                    'generation_resume_readback',
                    'generation_resume_receipt',
                    'generation_resume_reservation',
                    'MIHOMO_GENERATION_SELECTOR_PROOF_FAILED',
                    'fixed_target_native_membership_unverified',
                    'fixed_intent_superseded',
                    'fixed_target_native_membership_unverified',
                    'fixed_target_config_membership_mismatch',
                    'fixed_target_mapping_unresolved',
                    'fixed_target_missing',
                    'active_config_unavailable',
                    'mihomo_health_unavailable',
                    'GLOBAL_AUTO_FENCE_UNAVAILABLE',
                    'GLOBAL_AUTO_INTENT_FAILED',
                    'GLOBAL_AUTO_NATIVE_PROOF_FAILED',
                    'GLOBAL_AUTO_STALE',
                    'GLOBAL_FIXED_SERVER_FENCE_UNAVAILABLE',
                    'GLOBAL_FIXED_SERVER_MATERIALIZATION_UNVERIFIED',
                    'GLOBAL_FIXED_SERVER_NATIVE_PROOF_FAILED',
                    'GLOBAL_FIXED_SERVER_PRE_CHECK_FAILED',
                    'GLOBAL_FIXED_SERVER_STALE',
                    'MIHOMO_CANDIDATE_CHANGED_DURING_PROMOTE',
                    'MIHOMO_CANDIDATE_COPY_MISMATCH',
                    'MIHOMO_CANDIDATE_INVALID',
                    'MIHOMO_CANDIDATE_MISSING',
                    'MIHOMO_CANDIDATE_PROVENANCE_UNVERIFIED',
                    'MIHOMO_CANDIDATE_READ_FAILED',
                    'MIHOMO_CONFIG_VALIDATION_FAILED',
                    'MIHOMO_CONTROLLER_NOT_READY',
                    'MIHOMO_FINAL_READBACK_FAILED',
                    'MIHOMO_GENERATION_RECEIPT_INVALID',
                    'MIHOMO_GENERATION_STALE',
                    'MIHOMO_GENERATION_STALE_AFTER_RECEIPT',
                    'MIHOMO_GENERATION_STALE_BEFORE_PUBLICATION',
                    'MIHOMO_GENERATION_VERIFICATION_FENCE_REQUIRED',
                    'MIHOMO_INPUT_REVALIDATION_FAILED',
                    'MIHOMO_RECONCILE_FAILED',
                    'MIHOMO_RUNTIME_IDENTITY_UNAVAILABLE',
                    'SERVER_MANUALLY_DELETED',
                    'SERVER_NOT_FOUND_OR_INACTIVE',
                    'SERVER_NOT_USER_SELECTABLE',
                    'SUBSCRIPTION_INVENTORY_GENERATION_UNVERIFIED',
                    'VPN_AUTO_SELECTION_FENCE_INVALID',
                    'VPN_AUTO_SELECTION_FENCE_REQUIRED',
                    'VPN_AUTO_SELECTION_REVISION_CONFLICT',
                    'active_config_changed',
                    'active_config_unavailable',
                    'active_runtime_already_matches_routing',
                    'applied',
                    'candidate_provenance',
                    'checkpoint_write_failed',
                    'clean',
                    'config_validation',
                    'failed',
                    'fixed_intent_revalidation',
                    'fixed_intent_superseded',
                    'fixed_target_config_membership_mismatch',
                    'fixed_target_mapping_unresolved',
                    'fixed_target_missing',
                    'fwrouter_metadata_mismatch',
                    'generation_fence',
                    'generation_fence_conflict',
                    'generation_publication_revalidation',
                    'generation_revalidation',
                    'generation_snapshot',
                    'global_direct_does_not_change_mihomo_config',
                    'input_fingerprint_unavailable',
                    'input_fingerprint_unchanged',
                    'inventory_generation_admission',
                    'inventory_generation_publication_revalidation',
                    'inventory_generation_stale',
                    'inventory_generation_unverified',
                    'managed_runtime_required',
                    'mihomo_health_failed',
                    'mihomo_health_unavailable',
                    'native_policy_or_fence_unconfirmed',
                    'native_udp_policy_readback',
                    'native_udp_policy_receipt',
                    'no_active_config',
                    'none',
                    'pending',
                    'previous_config_not_failclosed_for_vpn_udp',
                    'receipt_fence_mismatch',
                    'restart',
                    'restore_hash_mismatch',
                    'restore_restart_failed',
                    'restore_write_failed',
                    'rollback_context_missing_or_mismatched',
                    'running',
                    'selection_or_runtime_superseded',
                    'selection_revision_changed',
                    'stale_generation_candidate',
                    'structural_change',
                    'success',
                    'unchanged',
                    'unchanged_config',
                    'validation_failed',
                    'verification',
                    'verification_callback_not_requested',
                    'verified_publication_requires_full_reconcile',
                    'vpn_selector_config_mismatch',
                }

                def collect_failure(value, path="job", depth=0):
                    if not isinstance(value, dict) or depth > 8 or len(observations) >= 32:
                        return
                    for key, item in value.items():
                        if not isinstance(key, str) or not re.fullmatch(r"[a-z_]{1,48}", key):
                            continue
                        label = path + "." + key
                        if key in allowed and (type(item) is bool or item is None
                                               or isinstance(item, str) and item in safe_values):
                            observations.append((label, item))
                        elif isinstance(item, dict):
                            collect_failure(item, label, depth + 1)
                        if len(observations) >= 32:
                            break

                collect_failure(fixed_job)
                encoded = json.dumps(observations, separators=(",", ":"))
                while len(encoded.encode("ascii")) > MAX_PHASE_DIAGNOSTIC_BYTES:
                    observations.pop()
                    encoded = json.dumps(observations, separators=(",", ":"))
                record_property("fixed_generation_failure", encoded)
            assert fixed_job.get("status") == "success", fixed_job
            fixed_result = fixed_job.get("result") if isinstance(fixed_job.get("result"), dict) else {}
            fixed_completion = fixed_result.get("global_fixed_server")
            assert isinstance(fixed_completion, dict) and fixed_completion.get("ok") is True
            unified_proof = fixed_completion.get("unified_fixed_proof")
            assert isinstance(unified_proof, dict), "fixed SUCCESS lacks unified owner proof"
            assert unified_proof.get("mihomo_target") == packet_runtime_name
            assert unified_proof.get("xray_state") == "verified"
            assert type(unified_proof.get("pending_revision")) is int
            assert type(unified_proof.get("selection_revision")) is int
            for proof_key in ("core_manifest_hash", "mihomo_config_sha256",
                              "xray_config_sha256", "xray_artifact_sha256"):
                assert isinstance(unified_proof.get(proof_key), str) and re.fullmatch(
                    r"[0-9a-f]{64}", unified_proof[proof_key],
                ), {"missing_or_invalid_unified_proof_field": proof_key}
            assert all(isinstance(unified_proof.get(key), str) and unified_proof[key] for key in (
                "core_apply_id", "mihomo_runtime_incarnation", "xray_runtime_incarnation",
            )), "fixed SUCCESS lacks native incarnation/Core apply ownership"
            routing_code, routing_response = http_json(f"{api}/routing/global")
            routing_projection = routing_response.get("data", {}).get("routing", {})
            assert (routing_code == 200 and routing_response.get("ok") is True
                    and routing_projection.get("desired_mode") == "direct"
                    and routing_projection.get("applied_mode") == "direct"), routing_response
        else:
            preference_code, preference = http_json(
                f"{api}/servers/{packet_target_id}/preferences", method="PATCH",
                payload={"vpn_auto": True, "vpn_auto_priority": 1, "reconcile_mihomo": True,
                         "requested_by": "hosted-acceptance-packet-legacy-fixture"},
            )
            preference_data = preference.get("data") if isinstance(preference, dict) else None
            server_preferences = (preference_data.get("server_preferences")
                                  if isinstance(preference_data, dict) else None)
            preference_server = server_preferences.get("server") if isinstance(server_preferences, dict) else None
            preferences = preference_server.get("preferences") if isinstance(preference_server, dict) else None
            preferences = preferences if isinstance(preferences, dict) else {}
            assert (preference_code == 200 and preference.get("ok") is True
                    and preferences.get("vpn_auto") is True
                    and preferences.get("vpn_auto_priority") == 1), {
                "preference_status": preference_code,
                "vpn_auto_enabled": preferences.get("vpn_auto") is True,
                "priority": preferences.get("vpn_auto_priority"),
            }
            selector_code, selector_response = http_json(
                f"{api}/selector/vpn-auto/switch", method="POST",
                payload={"confirm_switch": True, "exclude_active": False, "update_ping_state": False,
                         "limit": 20, "timeout_ms": 1000,
                         "reason": "hosted_packet_legacy_selectable_fixture"}, timeout=90,
            )
            selector_data = selector_response.get("data") if isinstance(selector_response, dict) else None
            selector_result = selector_data.get("selector") if isinstance(selector_data, dict) else None
            selector_result = selector_result if isinstance(selector_result, dict) else {}
            assert (selector_code == 200 and selector_response.get("ok") is True
                    and selector_result.get("ok") is True and selector_result.get("selected_server_id") == packet_target_id
                    and selector_result.get("active_after") == packet_target_id
                    and selector_result.get("selection_outcome") in {"selected", "noop"}), {
                "selector_status": selector_code,
                "selected_owned_packet_target": selector_result.get("selected_server_id") == packet_target_id,
                "active_owned_packet_target": selector_result.get("active_after") == packet_target_id,
                "selection_outcome": selector_result.get("selection_outcome"),
            }
            post_apply({"server_mode": "auto", "requested_by": "hosted-acceptance", "run_now": True})
            routing_code, routing_response = http_json(f"{api}/routing/global")
            routing_data = routing_response.get("data") if isinstance(routing_response, dict) else None
            routing_projection = routing_data.get("routing") if isinstance(routing_data, dict) else None
            routing_projection = routing_projection if isinstance(routing_projection, dict) else {}
            assert (routing_code == 200 and routing_response.get("ok") is True
                    and routing_projection.get("server_mode") == "auto"
                    and routing_projection.get("desired_mode") == "direct"
                    and routing_projection.get("applied_mode") == "direct"), routing_response
            selector_runtime_code, selector_runtime = http_json("http://127.0.0.1:5200/proxies/vpn-auto")
            from fwrouter_api.services.logical_topology import get_logical_runtime_name
            packet_runtime_name = get_logical_runtime_name(packet_target_id)
            assert (selector_runtime_code == 200 and selector_runtime.get("now") == packet_runtime_name), {
                "native_selector_status": selector_runtime_code,
                "native_target_matches_owned_packet_candidate": (
                    isinstance(selector_runtime, dict) and selector_runtime.get("now") == packet_runtime_name
                ),
            }
        packet_selection_baseline = packet_selection_snapshot(api)
        packet_config = yaml.safe_load((state / "generated" / "mihomo" / "config.yaml").read_bytes())
        packet_proxies = packet_config.get("proxies") if isinstance(packet_config, dict) else None
        packet_proxy = ([row for row in packet_proxies if isinstance(row, dict)
                         and row.get("type") == "vless" and row.get("server") == ENDPOINT_IP
                         and row.get("port") == ENDPOINT_SELECTOR_PORT]
                        if isinstance(packet_proxies, list) else [])
        packet_groups = packet_config.get("proxy-groups") if isinstance(packet_config, dict) else None
        packet_group = ([row for row in packet_groups if isinstance(row, dict)
                         and row.get("name") == packet_runtime_name]
                        if isinstance(packet_groups, list) else [])
        group_members = packet_group[0].get("proxies") if len(packet_group) == 1 else None
        assert (len(packet_proxy) == 1 and len(packet_group) == 1
                and isinstance(group_members, list)
                and group_members == [packet_proxy[0].get("name")]), {
            "owned_packet_proxy_count": len(packet_proxy),
            "owned_logical_group_count": len(packet_group),
            "logical_group_contains_only_owned_member": bool(
                len(packet_proxy) == 1 and isinstance(group_members, list)
                and group_members == [packet_proxy[0].get("name")]
            ),
        }
        vpn_auto_group = ([row for row in packet_groups if isinstance(row, dict)
                           and row.get("name") == "vpn-auto"] if isinstance(packet_groups, list) else [])
        vpn_auto_members = vpn_auto_group[0].get("proxies") if len(vpn_auto_group) == 1 else None
        assert (isinstance(vpn_auto_members, list)
                and (packet_runtime_name in vpn_auto_members) is (not fixed_target)), {
            "vpn_auto_group_contains_owned_logical_target": (
                isinstance(vpn_auto_members, list) and packet_runtime_name in vpn_auto_members
            ),
        }
        assert_native_matches_active(native, state / "xray" / "config.json")
        assert_mihomo_launch_matches_active(native, state / "generated" / "mihomo" / "config.yaml")
        assert source_bridge.snapshot_calls() == provider_calls_before_fixture, {
            "provider_bridge_calls_changed_during_fixture_setup": True,
        }
        # The adapter uses two bounded request profiles for each owner refresh;
        # the import and managed-VPN enable account for all four loopback GETs.
        assert source.snapshot_requests() == 4, {
            "synthetic_source_requests_during_fixture_setup": source.snapshot_requests(),
        }

        if fixed_target:
            after_fixed_transition_native = native_fingerprint()
            # Routing-sensitive handoff/config may legitimately change during
            # the staged generation owner. Identities and durable credentials
            # must not. Admission/startups below retain exact native fingerprints.
            fixed_protected_fields = {"xray_loaded", "xray_loaded_identity_count"}
            assert all(after_fixed_transition_native[key] == before_fixed_transition_native[key]
                       for key in fixed_protected_fields), {
                "fixed_apply_changed_xray_fields": sorted(
                    key for key in fixed_protected_fields
                    if after_fixed_transition_native[key] != before_fixed_transition_native[key]
                ),
            }
            assert durable_fingerprint("", identities_only=True) == before_fixed_transition_durable, {
                "contract": "unified fixed generation preserves all identity and credential mappings/history",
            }
            connection = sqlite3.connect(f"file:{state / 'fwrouter.db'}?mode=ro", uri=True, timeout=5)
            try:
                assert connection.execute(
                    "SELECT desired_state, apply_state FROM modules WHERE module_name='core'",
                ).fetchone() == ("enabled", "clean"), "unified fixed job did not finish Core apply"
                pending_row = connection.execute(
                    "SELECT value_json FROM settings WHERE key='xray.vpn_auto_pending'",
                ).fetchone()
                assert pending_row, "unified fixed completion lost its durable pending record"
                pending_completion = json.loads(pending_row[0])
                assert pending_completion.get("pending") is False
                assert pending_completion.get("applied_revision") == unified_proof["pending_revision"]
                assert pending_completion.get("revision") == unified_proof["pending_revision"]
            finally:
                connection.close()

        # Manual rules apply performs the real DNSmasq reconciliation as part
        # of finalization. Prepare the already-qualified packet fixture first:
        # it redirects generated files into this suite's owned directory and
        # supplies the real pinned daemon while leaving the read-only root intact.
        dns_fixture_code, dns_fixture = http_json(f"{api}/__acceptance/dns53/configure", method="POST")
        assert (dns_fixture_code == 200 and dns_fixture.get("ok") is True
                and dns_fixture.get("fixture_configured") is True
                and dns_fixture.get("daemon_running") is False
                and dns_fixture.get("nftset_support") is True
                and isinstance(dns_fixture.get("dnsmasq_version"), str)
                and dns_fixture["dnsmasq_version"].startswith("Dnsmasq version 2.90")), dns_fixture

        # Capture Xray state only after the real managed lifecycle established
        # its bindings. Seed the rules artifact through its owner; these
        # documentation-range destinations do not prove external VPN access.
        synthetic_rules = "VPN 198.51.100.77\nDIRECT 203.0.113.53"
        native_before_rules_fixture = native_fingerprint()
        rules_code, rules_saved = http_json(
            f"{api}/rules/manual", method="POST", payload={"text": synthetic_rules},
        )
        assert rules_code == 200 and rules_saved.get("ok") is True, rules_saved
        rules_apply_code, rules_apply = http_json(
            f"{api}/rules/manual/apply", method="POST",
            payload={"requested_by": "hosted-acceptance-selective-fixture", "run_now": True}, timeout=90,
        )
        if rules_apply_code != 200 or rules_apply.get("ok") is not True:
            failure = _rules_apply_failure_diagnostic(
                rules_apply, rules_apply_code, protected=(synthetic_rules, source.url), phase="api_response",
            )
            raise AssertionError(_serialize_rules_apply_failure(failure))
        try:
            rules_job = await_core_job(api, rules_apply, timeout_seconds=90)
        except AssertionError:
            failure = _rules_apply_failure_diagnostic(
                rules_apply, rules_apply_code, protected=(synthetic_rules, source.url), phase="job_wait",
            )
            raise AssertionError(_serialize_rules_apply_failure(failure)) from None
        if not isinstance(rules_job, dict) or rules_job.get("status") != "success":
            failure = _rules_apply_failure_diagnostic(
                rules_apply, rules_apply_code, protected=(synthetic_rules, source.url),
                phase="terminal_job", terminal_job=rules_job if isinstance(rules_job, dict) else None,
            )
            raise AssertionError(_serialize_rules_apply_failure(failure))
        named_counter_semantics_before, named_counter_count_before = native_core_table_semantics()
        named_counter_semantics_after, named_counter_count_after = native_core_table_semantics()
        assert (named_counter_count_before > 0 and named_counter_count_after > 0
                and named_counter_semantics_before == named_counter_semantics_after), {
            "named_counter_count_before": named_counter_count_before,
            "named_counter_count_after": named_counter_count_after,
            "contract": "canonical Core digest ignores only volatile named-counter values",
        }
        rules_artifact = json.loads((state / "generated" / "rules" / "effective-rules.json").read_text(
            encoding="utf-8",
        ))
        effective_rules = rules_artifact.get("rules") if isinstance(rules_artifact, dict) else None
        assert isinstance(effective_rules, list) and effective_rules, rules_artifact
        assert any(item.get("action") == "VPN" and item.get("value") == "198.51.100.77"
                   for item in effective_rules if isinstance(item, dict)), rules_artifact
        assert any(item.get("action") == "DIRECT" and item.get("value") == "203.0.113.53"
                   for item in effective_rules if isinstance(item, dict)), rules_artifact
        assert any(item.get("source") == "protected" and item.get("action") == "DIRECT"
                   for item in effective_rules if isinstance(item, dict)), rules_artifact
        protected_direct_baseline = protected_direct_rows()
        assert protected_direct_baseline, "rules owner did not emit canonical protected DIRECT rows"
        assert_owned_dns_daemon(api)
        native_after_rules_fixture = native_fingerprint()
        xray_native_fields = {
            "xray_active", "xray_mounted", "xray_loaded", "xray_bindings", "xray_last_good",
            "xray_incarnation", "xray_reload_calls",
        }
        assert all(native_after_rules_fixture.get(key) == native_before_rules_fixture.get(key)
                   for key in xray_native_fields), {
            key: (native_before_rules_fixture.get(key), native_after_rules_fixture.get(key))
            for key in sorted(xray_native_fields)
            if native_after_rules_fixture.get(key) != native_before_rules_fixture.get(key)
        }
        assert_mihomo_launch_matches_active(native, state / "generated" / "mihomo" / "config.yaml")

        native_before_selective_default = native_fingerprint()
        post_apply({"selective_default": "vpn", "requested_by": "hosted-acceptance", "run_now": True})
        assert native_fingerprint() == native_before_selective_default, {
            "before": native_before_selective_default,
            "after": native_fingerprint(),
            "contract": "global direct selective-default update leaves runtime unchanged",
        }
        native_before_selective_mode = native_fingerprint()
        direct_mihomo_config = yaml.safe_load(
            (state / "generated" / "mihomo" / "config.yaml").read_text(encoding="utf-8"),
        )
        direct_subrules = direct_mihomo_config.get("sub-rules") if isinstance(direct_mihomo_config, dict) else None
        direct_transparent_rules = direct_subrules.get("fwrouter-transparent") if isinstance(direct_subrules, dict) else None
        assert isinstance(direct_transparent_rules, list) and direct_transparent_rules[-1] == "MATCH,DIRECT", {
            "transparent_fallback": (
                direct_transparent_rules[-1]
                if isinstance(direct_transparent_rules, list) and direct_transparent_rules else None
            ),
        }
        mihomo_proxy_surface_before = hashlib.sha256(json.dumps(
            {
                "proxies": direct_mihomo_config.get("proxies"),
                "proxy-groups": direct_mihomo_config.get("proxy-groups"),
            }, sort_keys=True, separators=(",", ":"),
        ).encode()).hexdigest()
        packet_selection_before_selective_mode = packet_selection_snapshot(api)
        assert packet_selection_before_selective_mode == packet_selection_baseline, {
            "initial_selector_state_changed_before_selective_mode": True,
            "initial": packet_selection_baseline,
            "before_selective_mode": packet_selection_before_selective_mode,
        }
        selective_mode_job = post_apply(
            {"mode": "selective", "requested_by": "hosted-acceptance", "run_now": True},
        )
        packet_selective_selection_baseline = packet_selection_snapshot(api)
        selection_fields = set(packet_selection_before_selective_mode) - {"selection_revision"}
        assert (packet_selective_selection_baseline["selection_revision"]
                == packet_selection_before_selective_mode["selection_revision"] + 1
                and all(packet_selective_selection_baseline[key]
                        == packet_selection_before_selective_mode[key] for key in selection_fields)), {
            "selector_identity_or_provenance_changed_during_selective_apply": {
                key: (packet_selection_before_selective_mode.get(key),
                      packet_selective_selection_baseline.get(key))
                for key in sorted(selection_fields)
                if packet_selective_selection_baseline.get(key) != packet_selection_before_selective_mode.get(key)
            },
            "selection_revision_before": packet_selection_before_selective_mode["selection_revision"],
            "selection_revision_after": packet_selective_selection_baseline["selection_revision"],
            "expected_revision_increment": 1,
        }
        assert protected_direct_rows() == protected_direct_baseline
        native_after_selective_apply = native_fingerprint()
        assert all(native_after_selective_apply.get(key) == native_before_selective_mode.get(key)
                   for key in xray_native_fields), {
            key: (native_before_selective_mode.get(key), native_after_selective_apply.get(key))
            for key in sorted(xray_native_fields)
            if native_after_selective_apply.get(key) != native_before_selective_mode.get(key)
        }
        selective_config_path = state / "generated" / "mihomo" / "config.yaml"
        assert_mihomo_launch_matches_active(native, selective_config_path)
        selective_mihomo_config = yaml.safe_load(selective_config_path.read_text(encoding="utf-8"))
        selective_subrules = selective_mihomo_config.get("sub-rules") if isinstance(selective_mihomo_config, dict) else None
        transparent_rules = selective_subrules.get("fwrouter-transparent") if isinstance(selective_subrules, dict) else None
        mihomo_proxy_surface_after = hashlib.sha256(json.dumps(
            {
                "proxies": selective_mihomo_config.get("proxies"),
                "proxy-groups": selective_mihomo_config.get("proxy-groups"),
            }, sort_keys=True, separators=(",", ":"),
        ).encode()).hexdigest()
        assert mihomo_proxy_surface_after == mihomo_proxy_surface_before, {
            "proxies_proxy_groups_sha256_before": mihomo_proxy_surface_before,
            "proxies_proxy_groups_sha256_after": mihomo_proxy_surface_after,
        }
        assert isinstance(transparent_rules, list) and transparent_rules[-2:] == [
            "MATCH,vpn-global", "NETWORK,udp,REJECT",
        ], {
            "transparent_policy_tail": transparent_rules[-2:] if isinstance(transparent_rules, list) else None,
        }
        selective_fwrouter_metadata = selective_mihomo_config.get("fwrouter") if isinstance(selective_mihomo_config, dict) else None
        assert isinstance(selective_fwrouter_metadata, dict)
        assert selective_fwrouter_metadata.get("transparent_final_match_rule") == "MATCH,vpn-global", {
            "transparent_final_match_rule": selective_fwrouter_metadata.get("transparent_final_match_rule"),
            "selective_default": selective_fwrouter_metadata.get("resolved_selective_default"),
        }
        status, routing_result = http_json(f"{api}/routing/global")
        routing = routing_result.get("data", {}).get("routing", {})
        enforcement = routing_result.get("data", {}).get("runtime_enforcement", {})
        assert status == 200 and routing_result.get("ok") is True
        assert (routing.get("desired_mode"), routing.get("applied_mode"), routing.get("selective_default")) == (
            "selective", "selective", "vpn",
        ), routing
        assert enforcement.get("traffic_enforcement_guaranteed") is True, enforcement
        assert enforcement.get("enforcement_level") == "global_selective_enforced", enforcement
        assert enforcement.get("active_mode_matches_intent") is True, enforcement
        assert enforcement.get("selective_vpn_policy_ready") is True, enforcement
        assert enforcement.get("vpn_egress_ready") is True, enforcement
        missing_requirements = enforcement.get("missing_runtime_requirements")
        assert isinstance(missing_requirements, list) and missing_requirements == [], enforcement
        assert enforcement.get("supported_modes", {}).get("vpn") is True, enforcement
        runtime_code, runtime_response = http_json(f"{api}/runtime")
        runtime = ((runtime_response.get("data") or {}).get("runtime") or {})
        dataplane = runtime.get("dataplane") if isinstance(runtime.get("dataplane"), dict) else {}
        live_payload = dataplane.get("details") if isinstance(dataplane.get("details"), dict) else {}
        live_chains = live_payload.get("required_chains") if isinstance(live_payload.get("required_chains"), dict) else {}
        assert runtime_code == 200 and runtime_response.get("ok") is True, runtime_response
        assert live_payload.get("ok") is True and live_payload.get("table_exists") is True, live_payload
        # These host-script flags prove owned policy-routing readiness; they do not
        # prove an external client transport or VPN endpoint connectivity.
        assert live_payload.get("vpn_policy_required") is True, live_payload
        assert live_payload.get("vpn_contract_ready") is True, live_payload
        assert live_payload.get("vpn_external_path_verified") is True, live_payload
        assert live_chains and all(value is True for value in live_chains.values()), live_chains
        mihomo_code, mihomo_response = http_json(f"{api}/mihomo")
        mihomo = (((mihomo_response.get("data") or {}).get("mihomo"))
                  if isinstance(mihomo_response.get("data"), dict) else {})
        mihomo_details = mihomo.get("details") if isinstance(mihomo, dict) else {}
        vpn_policy = mihomo_details.get("vpn_udp_policy") if isinstance(mihomo_details, dict) else {}
        selectors = mihomo_details.get("selectors") if isinstance(mihomo_details, dict) else {}
        assert mihomo_code == 200 and mihomo_response.get("ok") is True, mihomo_response
        assert isinstance(vpn_policy, dict) and all(vpn_policy.get(key) is True for key in (
            "policy_installed", "wiring_proven", "reconcile_receipt_matches",
            "native_top_level_rules_match", "selector_membership_matches", "guarded_rules_match",
        )), vpn_policy
        assert isinstance(selectors, dict), mihomo_details
        vpn_auto_targets = selectors.get("vpn_auto_targets")
        vpn_global_targets = selectors.get("vpn_global_targets")
        assert isinstance(vpn_auto_targets, list) and vpn_auto_targets, selectors
        assert isinstance(vpn_global_targets, list) and vpn_global_targets, selectors
        assert "REJECT" in [str(item).upper() for item in vpn_auto_targets], selectors
        assert "REJECT" in [str(item).upper() for item in vpn_global_targets], selectors
        assert "DIRECT" not in [str(item).upper() for item in vpn_auto_targets], selectors
        assert "DIRECT" not in [str(item).upper() for item in vpn_global_targets], selectors
        assert selectors.get("vpn_global_now") == (packet_runtime_name if fixed_target else "vpn-auto"), selectors
        applied_manifest = json.loads(
            (state / "generated" / "dataplane" / "applied-manifest.json").read_text(encoding="utf-8"),
        )
        applied_routing = applied_manifest.get("routing_global_state")
        selective_apply_id = mutation_apply_id(selective_mode_job)
        assert applied_manifest.get("plan_id") == selective_apply_id, applied_manifest
        assert (isinstance(applied_routing, dict)
                and applied_routing.get("desired_mode") == "selective"
                and applied_routing.get("applied_mode") == initial_routing.get("applied_mode")
                and applied_routing.get("selective_default") == "vpn"), applied_manifest
        rules_source_baseline = global_rules_source_fingerprint(
            selective_apply_id,
            previous_applied_mode=str(initial_routing.get("applied_mode")),
        )

        connection = sqlite3.connect(state / "fwrouter.db", timeout=5)
        try:
            assert connection.execute(
                "DELETE FROM settings WHERE key='subscriptions.native_generation.v1'",
            ).rowcount == 1
            connection.commit()
        finally:
            connection.close()
        digest_code, digest_result = http_json(
            f"{api}/__acceptance/subscription/legacy-inventory-digest",
        )
        expected_digest = digest_result.get("digest")
        assert digest_code == 200 and digest_result.get("ok") is True
        assert isinstance(expected_digest, str) and re.fullmatch(r"[0-9a-f]{64}", expected_digest)
        disabled_code, disabled = http_json(
            f"{api}/modules/core/desired-state", method="POST",
            payload={"desired_state": "disabled", "requested_by": "hosted-acceptance", "run_now": False},
        )
        enabled_code, enabled = http_json(
            f"{api}/modules/core/desired-state", method="POST",
            payload={"desired_state": "enabled", "requested_by": "hosted-acceptance", "run_now": False},
        )
        assert disabled_code == enabled_code == 200 and disabled.get("ok") and enabled.get("ok")
        pending = enabled.get("data", {}).get("module", {})
        assert pending.get("desired_state") == "enabled" and pending.get("apply_state") == "pending", pending
        core_apply_job = post_apply(
            {"mode": "selective", "requested_by": "hosted-acceptance", "run_now": True},
        )
        core_apply_id = mutation_apply_id(core_apply_job)
        assert global_rules_source_fingerprint(
            core_apply_id,
            previous_applied_mode="selective",
        ) == rules_source_baseline
        assert packet_selection_snapshot(api) == packet_selective_selection_baseline
        assert protected_direct_rows() == protected_direct_baseline
        assert native_fingerprint() == native_after_selective_apply, {
            "before_core_enable_apply": native_after_selective_apply,
            "after_core_enable_apply": native_fingerprint(),
            "contract": "same-mode Core apply preserves Xray and Mihomo native state",
        }
        connection = sqlite3.connect(f"file:{state / 'fwrouter.db'}?mode=ro", uri=True, timeout=5)
        try:
            core_revision_row = connection.execute(
                "SELECT value_json FROM settings WHERE key='core.intent_revision'",
            ).fetchone()
            core_module_row = connection.execute(
                "SELECT desired_state, apply_state FROM modules WHERE module_name='core'",
            ).fetchone()
            core_receipt_row = connection.execute(
                "SELECT apply_id, status, core_intent_revision FROM apply_versions WHERE apply_id=?",
                (core_apply_id,),
            ).fetchone()
        finally:
            connection.close()
        assert core_revision_row and core_module_row == ("enabled", "clean") and core_receipt_row
        assert (core_receipt_row[0] == core_apply_id and core_receipt_row[1] == "applied"
                and core_receipt_row[2] == int(json.loads(core_revision_row[0]))), {
            "module": core_module_row, "receipt": core_receipt_row,
            "intent_revision": core_revision_row,
        }
        core_applied_manifest = json.loads(
            (state / "generated" / "dataplane" / "applied-manifest.json").read_text(encoding="utf-8"),
        )
        assert core_applied_manifest.get("plan_id") == core_apply_id, core_applied_manifest
        core_native_receipt = json.loads(
            (state / "generated" / "dataplane" / "applied-native-proof.json").read_text(encoding="utf-8"),
        )
        current_native_semantics, current_native_counter_count = native_core_table_semantics()
        assert (core_native_receipt.get("apply_id") == core_apply_id
                and core_native_receipt.get("native_semantics_sha256") == current_native_semantics
                and current_native_counter_count > 0), {
            "receipt_matches_current_named_counter_semantics": (
                core_native_receipt.get("native_semantics_sha256") == current_native_semantics
            ),
            "receipt_apply_matches_owner": core_native_receipt.get("apply_id") == core_apply_id,
            "named_counter_count": current_native_counter_count,
        }
        assert global_rules_source_fingerprint(
            core_apply_id,
            previous_applied_mode="selective",
        ) == rules_source_baseline

        native_before_admission = native_fingerprint()
        calls_before_admission = source_bridge.snapshot_calls()
        requests_before_admission = source.snapshot_requests()
        admission_code, admission = http_json(
            f"{api}/__acceptance/subscription/admit-legacy-inventory", method="POST",
            payload={"expected_digest": expected_digest}, timeout=90,
        )
        assert admission_code == 200 and admission.get("ok") is True, admission
        assert (admission.get("status") == "verified" and admission.get("digest") == expected_digest
                and admission.get("proof_receipt_written") is True), admission
        assert admission.get("mutation_attempts") == {
            "xray_apply_or_reload": 0, "mihomo_apply_or_reload": 0,
            "core_dataplane_apply_or_rollback": 0, "unexpected_http_request": 0,
        }, admission
        assert generation(api).get("status") == "verified"
        assert source_bridge.snapshot_calls() == calls_before_admission
        assert source.snapshot_requests() == requests_before_admission
        assert native_fingerprint() == native_before_admission, {
            "contract": "proof-only legacy admission does not change native state or binding bytes",
        }
        assert (state / "xray" / ".generation" / "generation-checkpoint.json").exists() is False
        receipt_path = state / "generated" / "mihomo" / "reconcile-state.json"

        def mihomo_receipt_semantics() -> dict[str, Any]:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            incarnation = native.rpc("mihomo_incarnation", {}).get("details", {}).get("stdout", "").strip()
            return {
                "active_config_hash": receipt.get("active_config_hash"),
                "runtime_incarnation": receipt.get("runtime_incarnation"),
                "version": receipt.get("version"),
                "udp_policy_version": receipt.get("udp_policy_version"),
                "native_proof": (
                    receipt.get("native_top_level_rules_match"),
                    receipt.get("native_selector_membership_match"),
                    receipt.get("subrule_config_guard_match"),
                ),
                "runtime_incarnation_readback": incarnation,
            }

        receipt_before_startup = mihomo_receipt_semantics()
        before_startup_native, before_startup_bindings = startup_native_snapshot()
        before_startup_profile_snapshots = subscription_profile_snapshot_rows()
        before_startup_server_overrides, before_startup_user_overrides = subject_override_rows()
        before_startup_durable = durable_fingerprint(core_apply_id)
        before_startup_subjects = subject_bootstrap_fingerprint()
        before_startup_inventory_rows = subject_inventory_rows()
        marker_before_startup = generation(api)

        previous_worker = stack["worker"]
        previous_worker.terminate()
        previous_worker.wait(timeout=5)
        worker, restarted_api = stack["start_worker"](startup_tasks=True, startup_recovery=True)
        stack["worker"], stack["api"] = worker, restarted_api

        after_first_startup_native, after_first_startup_bindings = startup_native_snapshot()
        _assert_xray_bindings_startup_equivalent(before_startup_bindings, after_first_startup_bindings)
        after_first_startup_profile_snapshots = subscription_profile_snapshot_rows()
        _assert_subscription_profile_startup_equivalent(
            before_startup_profile_snapshots, after_first_startup_profile_snapshots,
        )
        after_first_server_overrides, after_first_user_overrides = subject_override_rows()
        _assert_subject_overrides_startup_equivalent(
            before_startup_server_overrides, after_first_server_overrides,
            before_startup_user_overrides, after_first_user_overrides,
            allow_first_startup_owner=True,
        )
        assert generation(restarted_api) == marker_before_startup
        assert global_rules_source_fingerprint(
            core_apply_id,
            previous_applied_mode="selective",
        ) == rules_source_baseline
        assert packet_selection_snapshot(restarted_api) == packet_selective_selection_baseline
        assert after_first_startup_native == before_startup_native
        assert durable_fingerprint(core_apply_id) == before_startup_durable
        assert protected_direct_rows() == protected_direct_baseline
        after_first_startup_subjects = subject_bootstrap_fingerprint()
        assert_builtin_bootstrap_rows(before_startup_subjects, after_first_startup_subjects)
        builtin_subject_ids = {"fwrouter:global", "host:ssh"}
        allowed_bootstrap_additions = builtin_subject_ids - {
            row["subject_id"] for row in before_startup_inventory_rows
        }
        _assert_subject_inventory_startup_equivalent(
            before_startup_inventory_rows,
            subject_inventory_rows(),
            allowed_added_ids=allowed_bootstrap_additions,
        )
        assert source_bridge.snapshot_calls() == calls_before_admission
        assert source.snapshot_requests() == requests_before_admission
        assert_native_matches_active(native, state / "xray" / "config.json")
        assert_mihomo_launch_matches_active(native, state / "generated" / "mihomo" / "config.yaml")
        assert mihomo_receipt_semantics() == receipt_before_startup
        receipt_after_startup = mihomo_receipt_semantics()
        assert receipt_after_startup.get("active_config_hash") == hashlib.sha256(
            (state / "generated" / "mihomo" / "config.yaml").read_bytes()
        ).hexdigest(), receipt_after_startup
        assert receipt_after_startup.get("runtime_incarnation") == receipt_after_startup.get(
            "runtime_incarnation_readback",
        ), receipt_after_startup
        assert receipt_after_startup.get("native_proof") == (True, True, True), receipt_after_startup
        report_code, bootstrap_report = http_json(
            f"{restarted_api}/__acceptance/bootstrap/report",
        )
        assert report_code == 200 and bootstrap_report.get("completed") is True, bootstrap_report
        assert bootstrap_report.get("recovery_enabled") is True and bootstrap_report.get("foundation_ok") is True
        live_recovery = bootstrap_report.get("live_recovery", {})
        intended = bootstrap_report.get("apply_reconcile", {}).get("intended_routing", {})
        scoped = bootstrap_report.get("apply_reconcile", {}).get("scoped_subject_routing", {})
        xray_recovery = bootstrap_report.get("apply_reconcile", {}).get("xray_subscription_profiles", {})
        profile_promotion = xray_recovery.get("public_profile_promote", {})
        dns_reconcile = bootstrap_report.get("apply_reconcile", {}).get("dnsmasq_reconcile", {})
        live_routing = live_recovery.get("live_routing", {})
        selector = live_recovery.get("selector", {})
        assert live_routing.get("recovery_required") is False and live_routing.get("recovered") is False
        assert selector.get("ok") is True and selector.get("restored") is True
        assert selector.get("changed") is False and selector.get("skipped") is False, selector
        assert (intended.get("mode_matches") is True and intended.get("reapply_required") is False
                and intended.get("reapplied") is False), intended
        assert (scoped.get("classify_probe_ok") is True and scoped.get("reapply_required") is False
                and scoped.get("reapplied") is False and scoped.get("persisted_scoped_subjects_count") == 0), scoped
        assert (xray_recovery.get("ok") is True
                and xray_recovery.get("status") in {"success", "noop", "skipped"}
                and xray_recovery.get("error_code") is None), xray_recovery
        assert (isinstance(profile_promotion, dict)
                and type(profile_promotion.get("profiles_count")) is int
                and profile_promotion.get("profiles_count") >= 0
                and type(profile_promotion.get("nodes_count")) is int
                and profile_promotion.get("nodes_count") >= 0), xray_recovery
        assert dns_reconcile.get("ok") is True, dns_reconcile
        assert bootstrap_report.get("dns_fixture") == {
            "configured": True, "daemon_running": True, "daemon_uid_matches": True,
        }, bootstrap_report
        assert_owned_dns_daemon(restarted_api)

        # A second ordinary startup must be idempotent after the one exact,
        # documented first-run built-in subject initialization above.
        stable_native, stable_bindings = startup_native_snapshot()
        stable_profile_snapshots = subscription_profile_snapshot_rows()
        stable_server_overrides, stable_user_overrides = subject_override_rows()
        stable_durable = durable_fingerprint(core_apply_id)
        stable_subjects = subject_bootstrap_fingerprint()
        stable_inventory_rows = subject_inventory_rows()
        stable_marker = generation(restarted_api)
        previous_worker = stack["worker"]
        previous_worker.terminate()
        previous_worker.wait(timeout=5)
        worker, restarted_api = stack["start_worker"](startup_tasks=True, startup_recovery=True)
        stack["worker"], stack["api"] = worker, restarted_api
        after_second_startup_native, after_second_startup_bindings = startup_native_snapshot()
        _assert_xray_bindings_startup_equivalent(stable_bindings, after_second_startup_bindings)
        after_second_startup_profile_snapshots = subscription_profile_snapshot_rows()
        _assert_subscription_profile_startup_equivalent(
            stable_profile_snapshots, after_second_startup_profile_snapshots,
        )
        after_second_server_overrides, after_second_user_overrides = subject_override_rows()
        _assert_subject_overrides_startup_equivalent(
            stable_server_overrides, after_second_server_overrides,
            stable_user_overrides, after_second_user_overrides,
            allow_first_startup_owner=False,
        )
        assert generation(restarted_api) == stable_marker
        assert global_rules_source_fingerprint(
            core_apply_id,
            previous_applied_mode="selective",
        ) == rules_source_baseline
        assert packet_selection_snapshot(restarted_api) == packet_selective_selection_baseline
        assert after_second_startup_native == stable_native
        assert durable_fingerprint(core_apply_id) == stable_durable
        after_second_startup_subjects = subject_bootstrap_fingerprint()
        assert_builtin_bootstrap_rows(stable_subjects, after_second_startup_subjects)
        _assert_subject_inventory_startup_equivalent(stable_inventory_rows, subject_inventory_rows())
        assert source_bridge.snapshot_calls() == calls_before_admission
        assert source.snapshot_requests() == requests_before_admission
        report_code, second_report = http_json(f"{restarted_api}/__acceptance/bootstrap/report")
        assert report_code == 200 and second_report.get("completed") is True, second_report
        second_intended = second_report.get("apply_reconcile", {}).get("intended_routing", {})
        second_scoped = second_report.get("apply_reconcile", {}).get("scoped_subject_routing", {})
        second_xray = second_report.get("apply_reconcile", {}).get("xray_subscription_profiles", {})
        second_profile_promotion = second_xray.get("public_profile_promote", {})
        second_dns = second_report.get("apply_reconcile", {}).get("dnsmasq_reconcile", {})
        second_live = second_report.get("live_recovery", {})
        second_live_routing = second_live.get("live_routing", {})
        second_selector = second_live.get("selector", {})
        assert second_report.get("recovery_enabled") is True and second_report.get("foundation_ok") is True
        assert second_live_routing.get("recovery_required") is False and second_live_routing.get("recovered") is False
        assert second_selector.get("ok") is True and second_selector.get("restored") is True
        assert second_selector.get("changed") is False and second_selector.get("skipped") is False
        assert (second_intended.get("mode_matches") is True and second_intended.get("reapply_required") is False
                and second_intended.get("reapplied") is False), second_intended
        assert (second_scoped.get("classify_probe_ok") is True and second_scoped.get("reapply_required") is False
                and second_scoped.get("reapplied") is False
                and second_scoped.get("persisted_scoped_subjects_count") == 0), second_scoped
        assert (second_xray.get("ok") is True
                and second_xray.get("status") in {"success", "noop", "skipped"}
                and second_xray.get("error_code") is None), second_xray
        assert (isinstance(second_profile_promotion, dict)
                and type(second_profile_promotion.get("profiles_count")) is int
                and second_profile_promotion.get("profiles_count") >= 0
                and type(second_profile_promotion.get("nodes_count")) is int
                and second_profile_promotion.get("nodes_count") >= 0), second_xray
        assert second_dns.get("ok") is True, second_dns
        assert protected_direct_rows() == protected_direct_baseline
        assert second_report.get("dns_fixture") == {
            "configured": True, "daemon_running": True, "daemon_uid_matches": True,
        }, second_report
        assert_owned_dns_daemon(restarted_api)
        record_property("legacy_startup_lifecycle", json.dumps({
            "schema": "fwrouter-legacy-startup-lifecycle/v1",
            "server_mode": "fixed" if fixed_target else "auto",
            "normal_startups_completed": 2,
            "native_unchanged": True,
            "durable_identity_mapping_unchanged": True,
            "identity_rows": before_startup_durable["identity_rows"],
            "credential_rows": before_startup_durable["credential_rows"],
            "inventory_digest": expected_digest,
            "core_apply_id": core_apply_id,
            "selection_revision": packet_selective_selection_baseline["selection_revision"],
            "proof_receipt_written": admission["proof_receipt_written"],
        }, sort_keys=True, separators=(",", ":")))
    finally:
        source.close()


def _packet_tree_fingerprint(root: Path, *, excluded: set[str] | None = None) -> tuple[tuple[str, str], ...]:
    excluded_paths = excluded or set()
    if not root.exists():
        return ()
    entries = []
    for path in sorted(root.rglob("*")):
        assert not path.is_symlink(), f"unexpected packet qualification symlink: {path.name}"
        if path.is_dir():
            continue
        assert path.is_file(), f"unexpected packet qualification artifact: {path.name}"
        relative = path.relative_to(root).as_posix()
        if relative not in excluded_paths:
            entries.append((relative, hashlib.sha256(path.read_bytes()).hexdigest()))
    return tuple(entries)


def _role_json(url: str, *, method: str = "GET", payload: dict[str, Any] | None = None,
               failure_record_property: Any | None = None) -> dict[str, Any]:
    body = json.dumps(payload, separators=(",", ":")).encode("ascii") if payload is not None else None
    assert body is None or len(body) <= 256, "packet role request exceeded its fixed byte limit"
    request = urllib.request.Request(
        url, data=body, method=method,
        headers={"Content-Type": "application/json"} if body is not None else {},
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            code = response.status
            raw = response.read(8193)
    except urllib.error.HTTPError as exc:
        code = exc.code
        raw = exc.read(8193)
    except (OSError, TimeoutError) as exc:
        raise AssertionError("packet role control request did not complete within its bound") from exc
    assert len(raw) <= 8192, "packet role response exceeded its fixed byte limit"
    try:
        value = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise AssertionError("packet role response was not valid bounded JSON") from exc
    if code != 200 and isinstance(value, dict) and failure_record_property is not None:
        if isinstance(value.get("dns53_flow_failure"), dict):
            _record_dns53_flow_failure(failure_record_property, value.get("dns53_flow_failure"))
        else:
            _record_dns53_probe_failure(failure_record_property, value.get("dns53_failure"))
    assert code == 200 and isinstance(value, dict), {"status": code, "shape": type(value).__name__}
    return value


def _udp_negative_control() -> dict[str, Any]:
    """Return a bounded client-side UDP failure, never interpreting HTTP 200 as packet success."""
    request = urllib.request.Request(f"{CLIENT_CONTROL}/probe/udp-negative", data=b"", method="POST")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            code, raw = response.status, response.read(8193)
    except urllib.error.HTTPError as exc:
        code, raw = exc.code, exc.read(8193)
    except (OSError, TimeoutError) as exc:
        raise AssertionError("bounded negative UDP control request did not complete") from exc
    assert len(raw) <= 8192, "negative UDP control response exceeded its byte bound"
    try:
        value = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise AssertionError("negative UDP control response was not valid bounded JSON") from exc
    assert code == 502 and isinstance(value, dict) and value.get("ok") is False, {
        "control_status": code, "control_body_shape": type(value).__name__,
    }
    assert value.get("error") in {"TimeoutError", "ConnectionRefusedError", "OSError"}, {
        "udp_failure_class": value.get("error"),
    }
    return {"control_status": code, "failure_class": value["error"]}


def _tcp_negative_control() -> dict[str, Any]:
    """Return a bounded client TCP rejection; a successful HTTP response fails the contract."""
    request = urllib.request.Request(f"{CLIENT_CONTROL}/probe/tcp-negative", data=b"", method="POST")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            code, raw = response.status, response.read(8193)
    except urllib.error.HTTPError as exc:
        code, raw = exc.code, exc.read(8193)
    except (OSError, TimeoutError) as exc:
        raise AssertionError("bounded negative TCP control request did not complete") from exc
    assert len(raw) <= 8192, "negative TCP control response exceeded its byte bound"
    try:
        value = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise AssertionError("negative TCP control response was not valid bounded JSON") from exc
    assert code == 502 and isinstance(value, dict) and value.get("ok") is False, {
        "control_status": code, "control_body_shape": type(value).__name__,
    }
    assert value.get("error") in {"TimeoutError", "ConnectionRefusedError", "ConnectionResetError",
                                   "OSError", "FixtureError"}, {
        "tcp_failure_class": value.get("error"),
    }
    return {"control_status": code, "failure_class": value["error"]}


def _set_packet_provider_capability(bridge: Any, capability: str | None) -> None:
    if capability not in {None, "true", "false"}:
        raise AssertionError("packet provider capability is outside the fixed URI matrix")
    declaration = "" if capability is None else f"&udp={capability}"
    row = dict(bridge.current_config)
    row["connection_url"] = (
        f"vless://88c7ce2a-465e-4e72-9c56-2a9e2fc84a51@{ENDPOINT_IP}:5301"
        f"?type=tcp&encryption=none{declaration}#acceptance"
    )
    with bridge._lock:
        bridge.current_config = row


def _packet_refresh_state(source_ref: str, logical_id: str) -> dict[str, Any]:
    """Capture read-only selector/inventory facts with identifiers hashed."""
    from fwrouter_api.db.connection import db_session
    from fwrouter_api.services.logical_topology import get_logical_topology
    from fwrouter_api.services.selector import get_vpn_auto_state

    state = get_vpn_auto_state(read_only=True)
    selector_runtime = state.get("selector_runtime") if isinstance(state.get("selector_runtime"), dict) else {}
    scores = state.get("candidate_scores") if isinstance(state.get("candidate_scores"), list) else []
    active_id = state.get("active_auto_server_id")
    with db_session() as connection:
        member_rows = connection.execute(
            "SELECT member_id, member_config_json, is_active FROM logical_server_members "
            "WHERE logical_server_id=? ORDER BY member_order, member_id",
            (logical_id,),
        ).fetchall()
        ping_row = connection.execute(
            "SELECT status, checked_by, error_code, checked_at, "
            "json_extract(metadata_json, '$.source') AS source "
            "FROM server_ping_state WHERE server_id=?",
            (active_id,),
        ).fetchone() if isinstance(active_id, str) and active_id else None

    def digest(value: Any) -> str | None:
        if not isinstance(value, str) or not value:
            return None
        return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]

    def digest_list(values: Any) -> list[str]:
        if not isinstance(values, list):
            return []
        return sorted(value for item in values if (value := digest(item)) is not None)[:3]

    def safe_code(value: Any, allowed: set[str] | None = None) -> str | None:
        if not isinstance(value, str) or len(value) > 64 or not all(ch.isalnum() or ch in "_.:-" for ch in value):
            return None
        return value if allowed is None or value in allowed else "other"

    capability_counts = {"supported": 0, "unsupported": 0, "unknown": 0, "invalid": 0}
    for row in member_rows:
        try:
            member_config = json.loads(row["member_config_json"])
        except (TypeError, json.JSONDecodeError):
            member_config = None
        declared = member_config.get("_fwrouter_udp_capability") if isinstance(member_config, dict) else None
        capability_counts[declared if declared in {"supported", "unsupported", "unknown"} else "invalid"] += 1

    active_score = next((row for row in scores if isinstance(row, dict)
                         and row.get("server_id") == active_id), None)
    topology = get_logical_topology(logical_id)
    topology = topology if isinstance(topology, dict) else {}
    member_health = []
    for member in topology.get("members", []) if isinstance(topology.get("members"), list) else []:
        if not isinstance(member, dict):
            continue
        member_health.append({
            "member_hash": digest(member.get("member_id")),
            "active": member.get("is_active") is True,
            "effective_active": member.get("is_effective_active") is True,
            "status": safe_code(member.get("status"), {"unknown", "healthy", "failed", "stale", "unsupported"}) or "other",
            "fresh": member.get("fresh") if type(member.get("fresh")) is bool else None,
            "stale": member.get("stale") if type(member.get("stale")) is bool else None,
            "source": safe_code(member.get("source")),
            "probe_lane": safe_code(member.get("probe_lane")),
            "error_code": safe_code(member.get("error_code")),
            "checked_at_hash": digest(member.get("checked_at")),
        })
    return {
        "server_mode": state.get("server_mode") if state.get("server_mode") in {"auto", "fixed"} else None,
        "global_mode": state.get("global_mode") if state.get("global_mode") in {"vpn", "direct"} else None,
        "runtime_state": state.get("runtime_state") if state.get("runtime_state") in {"running", "failed", "unknown"} else "other",
        "problem_code": safe_code(state.get("problem_code")),
        "enabled_candidates_count": state.get("enabled_candidates_count") if type(state.get("enabled_candidates_count")) is int else None,
        "auto_selectable_candidates_count": state.get("auto_selectable_candidates_count") if type(state.get("auto_selectable_candidates_count")) is int else None,
        "active_candidate_hash": digest(active_id),
        "enabled_candidate_contains_active": isinstance(state.get("enabled_candidate_ids"), list)
        and active_id in state["enabled_candidate_ids"],
        "active_auto_target_valid": state.get("active_auto_target_valid") is True,
        "active_auto_server_valid": state.get("active_auto_server_valid") is True,
        "config_consistent": state.get("config_consistent") is True,
        "candidate_ping": {
            "status": active_score.get("ping_status") if isinstance(active_score, dict)
            and active_score.get("ping_status") in {"success", "failed", "unknown", "stale"} else "other",
            "stored_status": safe_code(ping_row["status"], {"success", "failed", "unknown", "skipped"})
            if ping_row else "unknown",
            "source": safe_code(ping_row["source"]) if ping_row else None,
            "checked_by": safe_code(ping_row["checked_by"]) if ping_row else None,
            "error_code": safe_code(ping_row["error_code"]) if ping_row else None,
            "checked_at_hash": digest(ping_row["checked_at"]) if ping_row else None,
        },
        "provider_udp_capability": {
            "declaration_counts": capability_counts,
        },
        "canonical_member_health": {
            "health_status": safe_code(topology.get("health", {}).get("status")
                                        if isinstance(topology.get("health"), dict) else None,
                                        {"usable", "unavailable", "unknown"}) or "other",
            "usable_members": topology.get("health", {}).get("usable_members")
            if isinstance(topology.get("health"), dict)
            and type(topology["health"].get("usable_members")) is int else None,
            "members": member_health[:3],
        },
        "selector_runtime": {
            "vpn_auto_now_hash": digest(selector_runtime.get("vpn_auto_now")),
            "vpn_auto_targets_count": len(selector_runtime.get("vpn_auto_targets", []))
            if isinstance(selector_runtime.get("vpn_auto_targets"), list) else 0,
            "vpn_auto_target_hashes": digest_list(selector_runtime.get("vpn_auto_targets")),
            "vpn_global_now_hash": digest(selector_runtime.get("vpn_global_now")),
            "vpn_global_targets_count": len(selector_runtime.get("vpn_global_targets", []))
            if isinstance(selector_runtime.get("vpn_global_targets"), list) else 0,
            "vpn_global_target_hashes": digest_list(selector_runtime.get("vpn_global_targets")),
        },
    }


def _packet_refresh_native_proof(stack: dict[str, Any]) -> dict[str, Any]:
    proof = _mihomo_packet_path_proof(stack)
    receipt_matches = {"available": False}
    try:
        receipt_path = stack["state"] / "generated" / "mihomo" / "reconcile-state.json"
        receipt_info = receipt_path.lstat()
        if (stat.S_ISREG(receipt_info.st_mode) and not receipt_path.is_symlink()
                and receipt_info.st_size <= 16384):
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            if isinstance(receipt, dict):
                receipt_matches = {
                    "available": True,
                    "active_hash_matches": receipt.get("active_config_hash") == proof.get("active_config_sha256"),
                    "runtime_incarnation_matches": (
                        isinstance(receipt.get("runtime_incarnation"), str)
                        and bool(receipt.get("runtime_incarnation"))
                        and hashlib.sha256(receipt["runtime_incarnation"].strip().encode("utf-8")).hexdigest()
                        == proof.get("native_incarnation_sha256")
                    ),
                    "version_supported": receipt.get("udp_policy_version") == 1,
                }
    except (OSError, ValueError, TypeError):
        pass
    allowed = (
        "available", "native_loaded_config_matches_active", "runtime_running",
        "vpn_global_selects_vpn_auto", "vpn_auto_selected_target_kind",
        "vpn_auto_has_direct_target", "vpn_global_has_direct_target",
        "full_vpn_udp_listener_count", "vless_proxy_count", "vless_udp_enabled_count",
        "active_config_sha256", "native_incarnation_sha256",
        "vpn_udp_policy_installed", "vpn_udp_policy_wiring_proven",
        "vpn_udp_policy_receipt_matches", "vpn_udp_selector_membership_matches",
        "vpn_udp_guarded_rules_match", "vpn_udp_native_rules_match",
    )
    result = {key: proof[key] for key in allowed if key in proof}
    result["reconcile_receipt"] = receipt_matches
    return result


def _refresh_packet_provider(api: str, source_ref: str, *, stack: dict[str, Any], logical_id: str,
                             phase: str, record_property: Any) -> dict[str, Any]:
    before = _packet_refresh_state(source_ref, logical_id)
    code, accepted = http_json(f"{api}/subscription/sources/{source_ref}/provider", method="POST",
                               payload={"action": "refresh"})
    accepted_ok = code == 200 and isinstance(accepted, dict) and accepted.get("ok") is True \
        and accepted.get("data", {}).get("accepted") is True
    job = None
    await_error_class = None
    if accepted_ok:
        try:
            job = await_core_job(api, accepted)
        except Exception as exc:
            await_error_class = type(exc).__name__
    after = _packet_refresh_state(source_ref, logical_id)
    job_summary = _bounded_provider_refresh_summary(job)
    native_proof = _packet_refresh_native_proof(stack)
    summary = {
        "phase": phase,
        "http_status": code if type(code) is int else None,
        "accepted": accepted_ok,
        "await_error_class": await_error_class[:64] if isinstance(await_error_class, str) else None,
        "job": job_summary,
        "before": before,
        "after": after,
        "mihomo_path": native_proof,
    }
    _record_phase_diagnostic(record_property, summary)
    assert accepted_ok, {"provider_refresh_phase": phase, "accepted": accepted_ok}
    if await_error_class is not None:
        raise AssertionError("provider refresh job polling failed within its bound")
    assert isinstance(job, dict) and job.get("status") == "success", {
        "provider_refresh_phase": phase, "job": job_summary, "before": before, "after": after,
    }
    receipt = native_proof.get("reconcile_receipt")
    assert (
        native_proof.get("vpn_udp_policy_installed") is True
        and native_proof.get("vpn_udp_policy_receipt_matches") is True
        and isinstance(receipt, dict)
        and receipt.get("available") is True
        and receipt.get("active_hash_matches") is True
        and receipt.get("runtime_incarnation_matches") is True
        and receipt.get("version_supported") is True
    ), {
        "provider_refresh_phase": phase,
        "mihomo_policy_installed": native_proof.get("vpn_udp_policy_installed"),
        "mihomo_receipt_matches": native_proof.get("vpn_udp_policy_receipt_matches"),
        "receipt_parity": receipt,
    }
    from fwrouter_api.services.provider_managed import binding_for
    binding = binding_for(source_ref)
    assert isinstance(binding, dict) and binding.get("logical_server_id")
    return binding


def _packet_capability_state(stack: dict[str, Any], source_ref: str, logical_id: str,
                             expected: str, expected_udp: bool,
                             *, expect_active_server: bool = True,
                             secondary_selector_candidate: bool = False) -> dict[str, Any]:
    from fwrouter_api.db.connection import db_session
    from fwrouter_api.services.logical_topology import get_logical_runtime_name
    from fwrouter_api.services.provider_managed import binding_for

    with db_session() as connection:
        rows = connection.execute(
            "SELECT member_id, member_config_json FROM logical_server_members "
            "WHERE logical_server_id=? AND is_active=1 ORDER BY member_order, member_id",
            (logical_id,),
        ).fetchall()
    assert len(rows) == 1, {"active_member_count": len(rows)}
    member = json.loads(rows[0]["member_config_json"])
    assert isinstance(member, dict) and member.get("_fwrouter_udp_capability") == expected, {
        "member_udp_capability": member.get("_fwrouter_udp_capability") if isinstance(member, dict) else None,
    }
    config_path = stack["state"] / "generated" / "mihomo" / "config.yaml"
    config = yaml.safe_load(config_path.read_bytes())
    proxies = config.get("proxies") if isinstance(config, dict) else None
    vless = [row for row in proxies if isinstance(row, dict) and row.get("type") == "vless"] if isinstance(proxies, list) else []
    expected_count = 2 if secondary_selector_candidate else 1
    assert len(vless) == expected_count, {"native_vless_count": len(vless), "expected": expected_count}
    primary = [row for row in vless if row.get("port") == 5301 and row.get("server") == ENDPOINT_IP]
    assert len(primary) == 1 and primary[0].get("udp") is expected_udp
    if secondary_selector_candidate:
        secondary = [row for row in vless if row.get("port") == ENDPOINT_SELECTOR_PORT and row.get("server") == ENDPOINT_IP]
        assert len(secondary) == 1 and secondary[0].get("udp") is True
    assert all(not any(str(key).startswith("_fwrouter") for key in row) for row in vless), "internal capability marker leaked into native Mihomo config"
    native = _mihomo_packet_path_proof(stack)
    assert native.get("available") is True and native.get("runtime_running") is True, native
    assert native.get("native_loaded_config_matches_active") is True, native
    assert native.get("vless_udp_enabled_count") == int(expected_udp) + int(secondary_selector_candidate), native
    binding = binding_for(source_ref)
    assert binding["logical_server_id"] == logical_id
    runtime_name = get_logical_runtime_name(logical_id)
    code, vpn_state = http_json(f"{stack['api']}/state/vpn")
    assert code == 200 and vpn_state.get("ok") is True
    effective = vpn_state["data"]["vpn"].get("effective")
    assert isinstance(effective, dict), {"effective_vpn_state": "missing"}
    effective_active_id = effective.get("active_server_id")
    health = effective.get("server_health")
    active = health.get("active") if isinstance(health, dict) else None
    if expect_active_server:
        assert isinstance(runtime_name, str) and bool(runtime_name) \
            and effective_active_id == runtime_name and isinstance(active, dict) \
            and active.get("server_id") == logical_id, {
                "effective_active_runtime_name_present": isinstance(runtime_name, str) and bool(runtime_name),
                "effective_active_runtime_name_matches": effective_active_id == runtime_name,
                "health_active_server_id_matches": isinstance(active, dict)
                and active.get("server_id") == logical_id,
            }
    else:
        assert effective_active_id is None and (active is None or (
            isinstance(active, dict) and active.get("server_id") is None
        )), {
            "effective_active_server_is_null": effective_active_id is None,
            "health_active_summary_is_absent_or_null": active is None or (
                isinstance(active, dict) and active.get("server_id") is None
            ),
        }
    return {"declared_capability": expected, "native_udp_attempt": expected_udp,
            "member_id": rows[0]["member_id"], "native": native}


def _provider_selection_snapshot(source_ref: str) -> dict[str, Any]:
    from fwrouter_api.db.connection import db_session
    from fwrouter_api.services.provider_managed import binding_for
    from fwrouter_api.services.vpn_auto_exclusive import get_vpn_auto_exclusive_source_ref
    from fwrouter_api.services.vpn_auto_selection_state import read_selection_fence

    binding = binding_for(source_ref)
    with db_session() as connection:
        fence = read_selection_fence(connection)
    return {
        "logical_server_id": binding.get("logical_server_id"),
        "current_member_id": binding.get("current_member_id"),
        "applied_member_id": binding.get("applied_member_id"),
        "binding_revision": binding.get("binding_revision"),
        "selection_revision": fence.get("revision"),
        "fenced_active_server_id": fence.get("active_server_id"),
        "exclusive_source_ref": get_vpn_auto_exclusive_source_ref(),
    }


def _assert_packet_selection_stable(before: dict[str, Any], after: dict[str, Any]) -> None:
    if before != after:
        raise AssertionError({"selection_fence_changed_during_packet_probe": {"before": before, "after": after}})


def _read_packet_selection_fence() -> dict[str, Any]:
    from fwrouter_api.db.connection import db_session
    from fwrouter_api.services.vpn_auto_selection_state import read_selection_fence

    with db_session() as connection:
        return read_selection_fence(connection)


def _read_packet_selector_state() -> dict[str, Any]:
    from fwrouter_api.services.selector import get_vpn_auto_state

    return get_vpn_auto_state(read_only=True)


def _bind_packet_parent_selector_runtime(stack: dict[str, Any], monkeypatch: Any) -> Any:
    """Bind parent-process selector reads to this packet fixture's owned runtime."""
    from fwrouter_api.adapters.mihomo import MihomoHttpAdapter
    from fwrouter_api.services import runtime_adapters

    state_root = Path(stack["state"]).resolve(strict=True)
    config_path = state_root / "generated" / "mihomo" / "config.yaml"
    contours_path = state_root / "generated" / "mihomo" / "contours.json"
    expected_config_path = state_root / "generated" / "mihomo" / "config.yaml"
    expected_contours_path = state_root / "generated" / "mihomo" / "contours.json"
    try:
        config_stat = config_path.lstat()
        config_resolved = config_path.resolve(strict=True)
        contours_resolved = contours_path.resolve(strict=False)
    except OSError:
        raise AssertionError("packet selector runtime paths are unavailable") from None
    assert (stat.S_ISREG(config_stat.st_mode) and not stat.S_ISLNK(config_stat.st_mode)
            and config_resolved == expected_config_path
            and contours_resolved == expected_contours_path), {
        "packet_selector_runtime_paths_owned": False,
    }

    adapter = MihomoHttpAdapter(
        base_url="http://127.0.0.1:5200",
        config_path=config_path,
        contours_path=contours_path,
        timeout_seconds=3.0,
    )
    monkeypatch.setattr(runtime_adapters, "DEFAULT_MIHOMO_ADAPTER", adapter)
    return adapter


def _refresh_packet_capability(stack: dict[str, Any], api: str, bridge: Any, source_ref: str, capability: str | None,
                               expected_capability: str, logical_id: str, expected_member: str,
                               record_property: Any) -> dict[str, Any]:
    before = _provider_selection_snapshot(source_ref)
    _set_packet_provider_capability(bridge, capability)
    binding = _refresh_packet_provider(
        api, source_ref, stack=stack, logical_id=logical_id,
        phase=f"provider-refresh-{expected_capability}", record_property=record_property,
    )
    after = _provider_selection_snapshot(source_ref)
    assert binding.get("logical_server_id") == logical_id
    assert after["logical_server_id"] == logical_id
    assert after["current_member_id"] == expected_member and after["applied_member_id"] == expected_member, {
        "provider_member_changed_during_capability_refresh": after,
    }
    assert after["exclusive_source_ref"] == source_ref
    assert type(before["selection_revision"]) is int and type(after["selection_revision"]) is int
    assert after["selection_revision"] >= before["selection_revision"], {
        "selection_revision_moved_backwards": {"before": before, "after": after},
    }
    return _packet_capability_state(stack, source_ref, logical_id, expected_capability,
                                    expected_capability != "unsupported")


def _pcap_counts(path: Path, expected_snaplen: int, expected_protocol: int,
                 expected_ports: set[int], *, record_counts: dict[str, int] | None = None,
                 capture_name: str | None = None) -> dict[tuple[str, str, str, int], int]:
    """Read only fixed IPv4 headers from a launcher-owned bounded live pcap."""
    try:
        root_info = CAPTURE_ROOT.lstat()
        capture_info = path.lstat()
        if (CAPTURE_ROOT.is_symlink() or not stat.S_ISDIR(root_info.st_mode)
                or root_info.st_uid != 0 or root_info.st_mode & 0o777 != 0o700
                or path.is_symlink() or not stat.S_ISREG(capture_info.st_mode)
                or capture_info.st_uid != 0
                or capture_info.st_size > 24 + CAPTURE_LIMIT * (16 + expected_snaplen)):
            raise AssertionError("owned packet capture file violates its bounded path or size contract")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            opened = os.fstat(fd)
            if (not stat.S_ISREG(opened.st_mode) or opened.st_uid != 0
                    or opened.st_size > 24 + CAPTURE_LIMIT * (16 + expected_snaplen)):
                raise AssertionError("opened packet capture differs from its owned-file contract")
            with os.fdopen(fd, "rb") as stream:
                fd = -1
                raw = stream.read(24 + CAPTURE_LIMIT * (16 + expected_snaplen) + 1)
        finally:
            if fd >= 0:
                os.close(fd)
    except OSError as exc:
        raise AssertionError("launcher-owned fixed packet capture is unavailable") from exc
    if len(raw) > 24 + CAPTURE_LIMIT * (16 + expected_snaplen) or len(raw) < 24:
        raise AssertionError("bounded packet capture has an invalid length")
    endians = {
        b"\xd4\xc3\xb2\xa1": "<", b"\xa1\xb2\xc3\xd4": ">",
        b"\x4d\x3c\xb2\xa1": "<", b"\xa1\xb2\x3c\x4d": ">",
    }
    endian = endians.get(raw[:4])
    if endian is None:
        raise AssertionError("packet capture pcap magic is unsupported")
    major, minor, _zone, _sigfigs, snaplen, linktype = struct.unpack_from(endian + "HHiiII", raw, 4)
    if major != 2 or minor != 4 or snaplen != expected_snaplen or linktype != 1:
        raise AssertionError("packet capture header differs from the fixed packet profile")

    counts: dict[tuple[str, str, str, int], int] = {}
    offset = 24
    records = 0
    while offset + 16 <= len(raw):
        _seconds, _fraction, captured, original = struct.unpack_from(endian + "IIII", raw, offset)
        offset += 16
        if captured > expected_snaplen or captured > original:
            raise AssertionError("packet capture record exceeds the header-only bound")
        if offset + captured > len(raw):
            break  # tcpdump may be writing the last packet while the live snapshot is read
        frame = raw[offset:offset + captured]
        offset += captured
        records += 1
        if records > CAPTURE_LIMIT:
            raise AssertionError("packet capture exceeded its packet-count limit")
        if linktype == 1:
            if len(frame) < 14 or frame[12:14] != b"\x08\x00":
                continue
            network_offset = 14
        else:
            raise AssertionError("packet capture link type differs from the fixed Ethernet contract")
        if len(frame) < network_offset + 24:
            raise AssertionError("captured IPv4 packet omitted fixed address and port headers")
        first = frame[network_offset]
        ihl = first & 0x0F
        if first >> 4 != 4 or ihl != 5:
            raise AssertionError("captured packet is not fixed IPv4 without options")
        protocol = frame[network_offset + 9]
        if protocol != expected_protocol:
            continue
        transport = network_offset + 20
        src = str(ipaddress.IPv4Address(frame[network_offset + 12:network_offset + 16]))
        dst = str(ipaddress.IPv4Address(frame[network_offset + 16:network_offset + 20]))
        _src_port, dst_port = struct.unpack_from("!HH", frame, transport)
        if dst_port not in expected_ports:
            continue
        if dst_port in {5301, ENDPOINT_SELECTOR_PORT} and (src, dst, protocol) != (ROUTER_WAN_IP, ENDPOINT_IP, 6):
            raise AssertionError("captured VLESS flow differs from the fixed router-to-endpoint tuple")
        if dst_port == 53 and (src, dst, protocol) != (ROUTER_WAN_IP, "1.1.1.1", protocol):
            raise AssertionError("captured DNS53 flow differs from the fixed router-to-owned-upstream tuple")
        if ((dst_port == 9080 and dst not in {SERVICE_VIP, DNS_VPN_SERVICE_VIP, PROTECTED_VIP})
                or (dst_port in {9081, 5353} and dst not in {SERVICE_VIP, DNS_VPN_SERVICE_VIP, PROTECTED_VIP})
                or (dst_port == 5353 and dst != SERVICE_VIP)):
            raise AssertionError("captured service flow differs from the fixed VIP")
        counts[(src, dst, "tcp" if protocol == 6 else "udp", dst_port)] = (
            counts.get((src, dst, "tcp" if protocol == 6 else "udp", dst_port), 0) + 1
        )
    if record_counts is not None and capture_name is not None:
        record_counts[capture_name] = records
    return counts


def _capture_budget_exhausted(record_counts: dict[str, int]) -> bool:
    """A full bounded capture cannot support a complete dataplane acceptance."""
    return any(type(count) is int and count >= CAPTURE_LIMIT for count in record_counts.values())


def _packet_counts() -> dict[tuple[str, str, str, int], int]:
    record_counts: dict[str, int] = {}
    tcp = _pcap_counts(TCP_CAPTURE_PATH, TCP_SNAPLEN, 6,
                       {53, 5301, ENDPOINT_SELECTOR_PORT, 9080},
                       record_counts=record_counts, capture_name="tcp")
    udp = _pcap_counts(UDP_CAPTURE_PATH, UDP_SNAPLEN, 17, {53, 9081, 5353},
                       record_counts=record_counts, capture_name="udp")
    if _capture_budget_exhausted(record_counts):
        raise AssertionError("packet capture budget exhausted before complete dataplane acceptance")
    return {**tcp, **udp}


def _wait_packet_counts(predicate, *, timeout_seconds: float = 3.0) -> dict[tuple[str, str, str, int], int]:
    """Wait on owned pcap file-change events until the exact flow predicate holds."""
    libc = ctypes.CDLL(None, use_errno=True)
    fd = libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
    if fd < 0:
        raise AssertionError("bounded packet capture event reader could not initialize")
    selector = selectors.DefaultSelector()
    try:
        if libc.inotify_add_watch(fd, os.fsencode(CAPTURE_ROOT), 0x00000002) < 0:
            raise AssertionError("bounded packet capture event reader could not watch its owned directory")
        selector.register(fd, selectors.EVENT_READ)
        deadline = time.monotonic() + timeout_seconds
        while True:
            current = _packet_counts()
            if predicate(current):
                return current
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                raise AssertionError("owned packet capture did not satisfy its flow predicate before the deadline")
            while True:
                try:
                    events = os.read(fd, 4096)
                except BlockingIOError:
                    break
                if not events:
                    break
                offset = 0
                while offset + 16 <= len(events):
                    _watch, mask, _cookie, name_length = struct.unpack_from("iIII", events, offset)
                    if mask & 0x00004000:
                        raise AssertionError("owned packet capture inotify queue overflowed")
                    offset += 16 + name_length
    finally:
        selector.close()
        os.close(fd)


def _core_counter_snapshot() -> dict[str, int | None]:
    """Read the real FWRouter-owned table counters from the router namespace."""
    try:
        result = subprocess.run(
            ("/usr/sbin/nft", "-j", "list", "table", "inet", CORE_NFT_TABLE),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=3, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AssertionError("FWRouter nft counter readback did not complete within its bound") from exc
    if (result.returncode or len(result.stdout) > MAX_CORE_NFT_JSON
            or len(result.stderr) > 4096):
        raise AssertionError("FWRouter nft counter readback failed or exceeded its fixed byte bound")
    try:
        payload = json.loads(result.stdout)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise AssertionError("FWRouter nft counter readback was not valid JSON") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("nftables"), list):
        raise AssertionError("FWRouter nft counter readback has an invalid top-level shape")

    counters: dict[str, list[int]] = {
        "direct_lan_forward": [], "vpn_tcp": [], "vpn_udp": [], "vpn_udp_handoff": [],
    }
    for item in payload["nftables"]:
        rule = item.get("rule") if isinstance(item, dict) else None
        if (not isinstance(rule, dict) or rule.get("family") != "inet"
                or rule.get("table") != CORE_NFT_TABLE):
            continue
        chain = rule.get("chain")
        comment = rule.get("comment")
        if not isinstance(chain, str) or not isinstance(comment, str):
            continue
        kind = None
        if chain == "fwrouter_direct" and comment == "global direct path":
            kind = "direct_lan_forward"
        elif chain == "fwrouter_vpn_full" and comment.startswith("fwrouter vpn mark tcp:5204"):
            kind = "vpn_tcp"
        elif chain == "fwrouter_vpn_full" and comment.startswith("fwrouter vpn mark udp:5205"):
            kind = "vpn_udp"
        elif (chain == "prerouting"
              and comment.startswith("fwrouter full-vpn tproxy handoff udp:5205")):
            kind = "vpn_udp_handoff"
        if kind is None:
            continue
        expr = rule.get("expr")
        matches = [entry["counter"] for entry in expr if isinstance(entry, dict)
                   and isinstance(entry.get("counter"), dict)] if isinstance(expr, list) else []
        if len(matches) != 1 or type(matches[0].get("packets")) is not int:
            raise AssertionError("FWRouter path rule has no unique packet counter")
        counters[kind].append(matches[0]["packets"])
    if (len(counters["direct_lan_forward"]) != 1 or len(counters["vpn_tcp"]) != 1
            or len(counters["vpn_udp"]) != 1 or len(counters["vpn_udp_handoff"]) > 1):
        raise AssertionError("FWRouter dataplane did not expose the exact terminal direct and global-VPN counters")
    return {
        "direct_lan_forward": counters["direct_lan_forward"][0],
        # Core has no packet counter on its input hook. These source-defined
        # Global VPN mode classifies into fwrouter_vpn_full; these are its
        # fixed full-VPN TCP REDIR and UDP TProxy mark counters.
        "vpn_tcp": counters["vpn_tcp"][0],
        "vpn_udp": counters["vpn_udp"][0],
        # The handoff rule exists only while global VPN/selective policy needs it.
        "vpn_udp_handoff": counters["vpn_udp_handoff"][0] if counters["vpn_udp_handoff"] else None,
    }


def _core_selective_vpn_counter_snapshot() -> dict[str, int]:
    """Read the transparent TCP/UDP VPN marks used by selective-mode traffic."""
    try:
        result = subprocess.run(
            ("/usr/sbin/nft", "-j", "list", "table", "inet", CORE_NFT_TABLE),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=3, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AssertionError("selective VPN counter readback did not complete within its bound") from exc
    if (result.returncode or len(result.stdout) > MAX_CORE_NFT_JSON
            or len(result.stderr) > 4096):
        raise AssertionError("selective VPN counter readback failed or exceeded its fixed byte bound")
    try:
        payload = json.loads(result.stdout)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise AssertionError("selective VPN counter readback was not valid JSON") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("nftables"), list):
        raise AssertionError("selective VPN counter readback has an invalid top-level shape")
    wanted = {
        "tcp": "fwrouter vpn mark tcp:5202",
        "udp": "fwrouter vpn mark udp:5203",
    }
    counters: dict[str, list[int]] = {key: [] for key in wanted}
    for item in payload["nftables"]:
        rule = item.get("rule") if isinstance(item, dict) else None
        if (not isinstance(rule, dict) or rule.get("family") != "inet"
                or rule.get("table") != CORE_NFT_TABLE or rule.get("chain") != "fwrouter_vpn"):
            continue
        comment = rule.get("comment")
        kind = next((key for key, prefix in wanted.items()
                     if isinstance(comment, str) and comment.startswith(prefix)), None)
        if kind is None:
            continue
        expr = rule.get("expr")
        matches = [entry["counter"] for entry in expr if isinstance(entry, dict)
                   and isinstance(entry.get("counter"), dict)] if isinstance(expr, list) else []
        if len(matches) != 1 or type(matches[0].get("packets")) is not int:
            raise AssertionError("selective VPN mark rule has no unique packet counter")
        counters[kind].append(matches[0]["packets"])
    if any(len(counters[key]) != 1 for key in wanted):
        raise AssertionError("selective VPN TCP/UDP mark counters are not uniquely installed")
    return {key: counters[key][0] for key in wanted}


def _core_protected_exception_readback() -> dict[str, bool]:
    """Read the installed protected-address set and exact classify rule from native nft."""
    try:
        result = subprocess.run(
            ("/usr/sbin/nft", "-j", "list", "table", "inet", CORE_NFT_TABLE),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=3, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AssertionError("native protected-route readback did not complete within its bound") from exc
    if result.returncode or len(result.stdout) > MAX_CORE_NFT_JSON or len(result.stderr) > 4096:
        raise AssertionError("native protected-route readback failed or exceeded its byte bound")
    try:
        rows = json.loads(result.stdout).get("nftables")
    except (UnicodeError, json.JSONDecodeError, AttributeError) as exc:
        raise AssertionError("native protected-route readback was not valid nft JSON") from exc
    if not isinstance(rows, list):
        raise AssertionError("native protected-route readback has an invalid nftables shape")
    classifier = any(
        isinstance(item, dict) and isinstance(item.get("rule"), dict)
        and item["rule"].get("table") == CORE_NFT_TABLE
        and item["rule"].get("chain") == "fwrouter_classify"
        and item["rule"].get("comment") == "protected IPv4 always direct"
        for item in rows
    )
    vip = ipaddress.ip_address(PROTECTED_VIP)
    member = False
    for item in rows:
        entry = item.get("set") if isinstance(item, dict) else None
        if not isinstance(entry, dict) or entry.get("table") != CORE_NFT_TABLE \
                or entry.get("name") != "protected_ipv4":
            continue
        elements = entry.get("elem")
        if not isinstance(elements, list):
            continue
        for element in elements:
            token = element if isinstance(element, str) else None
            if isinstance(element, dict) and isinstance(element.get("prefix"), dict):
                prefix = element["prefix"]
                address, length = prefix.get("addr"), prefix.get("len")
                if isinstance(address, str) and type(length) is int:
                    token = f"{address}/{length}"
            try:
                if token is not None and vip in ipaddress.ip_network(token, strict=False):
                    member = True
            except ValueError:
                continue
    return {"classifier_bypass_installed": classifier, "protected_vip_in_active_set": member}


def _core_counter_delta(before: dict[str, int | None], after: dict[str, int | None]) -> dict[str, int]:
    if set(before) != {"direct_lan_forward", "vpn_tcp", "vpn_udp", "vpn_udp_handoff"} or set(after) != set(before):
        raise AssertionError("FWRouter counter snapshots do not match the fixed path contract")
    if before["vpn_udp_handoff"] is None and after["vpn_udp_handoff"] is None:
        udp_handoff_delta = 0
    elif type(before["vpn_udp_handoff"]) is int and type(after["vpn_udp_handoff"]) is int:
        udp_handoff_delta = after["vpn_udp_handoff"] - before["vpn_udp_handoff"]
    else:
        raise AssertionError("FWRouter UDP TProxy handoff counter appeared or disappeared during a packet phase")
    raw_delta = {name: after[name] - before[name] for name in ("direct_lan_forward", "vpn_tcp", "vpn_udp")}
    delta = {
        "direct_lan_forward": raw_delta["direct_lan_forward"],
        "vpn_lan_input": raw_delta["vpn_tcp"] + raw_delta["vpn_udp"],
        "vpn_tcp_classified": raw_delta["vpn_tcp"],
        "vpn_udp_classified": raw_delta["vpn_udp"],
        "vpn_udp_handoff_packets": udp_handoff_delta,
    }
    if any(value < 0 for value in delta.values()):
        raise AssertionError("FWRouter path counter decreased during a packet phase")
    return delta


def _router_guard_counter_snapshot() -> dict[str, dict[str, int]]:
    """Read fixed named drops from the launcher-owned guard in the router namespace."""
    try:
        result = subprocess.run(
            ("/usr/sbin/nft", "-j", "list", "table", "inet", ROUTER_GUARD_TABLE),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=3, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AssertionError("router egress guard counter readback did not complete within its bound") from exc
    if (result.returncode or len(result.stdout) > MAX_CORE_NFT_JSON or len(result.stderr) > 4096):
        raise AssertionError("router egress guard readback failed or exceeded its fixed byte bound")
    try:
        payload = json.loads(result.stdout)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise AssertionError("router egress guard counter readback was not valid JSON") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("nftables"), list):
        raise AssertionError("router egress guard counter readback has an invalid shape")
    wanted = {
        "router_stub_drop", "router_egress_drop", "router_forward_drop",
        "router_protected_tcp_output", "router_protected_udp_output",
        "router_protected_tcp_forward", "router_protected_udp_forward",
        "router_dns_tcp_output", "router_dns_udp_output", "router_dns_tcp_input", "router_dns_udp_input",
        "router_dns_vpn_direct_output", "router_dns_vpn_direct_forward",
    }
    found: dict[str, list[dict[str, int]]] = {name: [] for name in wanted}
    for item in payload["nftables"]:
        counter = item.get("counter") if isinstance(item, dict) else None
        if (isinstance(counter, dict) and counter.get("table") == ROUTER_GUARD_TABLE
                and counter.get("name") in wanted and type(counter.get("packets")) is int
                and type(counter.get("bytes")) is int):
            found[counter["name"]].append({"packets": counter["packets"], "bytes": counter["bytes"]})
    if any(len(values) != 1 for values in found.values()):
        raise AssertionError("router guard did not expose each required fixed named packet counter exactly once")
    return {name: values[0] for name, values in found.items()}


def _router_guard_counter_delta(before: dict[str, dict[str, int]],
                                after: dict[str, dict[str, int]]) -> dict[str, dict[str, int]]:
    if set(before) != {
            "router_stub_drop", "router_egress_drop", "router_forward_drop",
            "router_protected_tcp_output", "router_protected_udp_output",
            "router_protected_tcp_forward", "router_protected_udp_forward",
            "router_dns_tcp_output", "router_dns_udp_output", "router_dns_tcp_input", "router_dns_udp_input",
            "router_dns_vpn_direct_output", "router_dns_vpn_direct_forward",
    } or set(after) != set(before):
        raise AssertionError("router guard counter snapshots differ from the fixed fixture contract")
    delta = {name: {field: after[name][field] - before[name][field]
                    for field in ("packets", "bytes")} for name in before}
    if any(value < 0 for row in delta.values() for value in row.values()):
        raise AssertionError("router guard counter decreased during a packet phase")
    return delta


def _probe_router_egress_guards() -> dict[str, Any]:
    """Exercise fixed router output-deny targets and one LAN forward-deny target."""
    before = _router_guard_counter_snapshot()
    stub_send_error = None
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(0.5)
        try:
            sock.sendto(b"fwrouter-packet-guard", ("127.0.0.11", 53))
        except OSError as exc:
            # The kernel may surface a synchronous denial. The later counter
            # delta remains mandatory and is the only accepted proof of the guard.
            stub_send_error = type(exc).__name__
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        try:
            sock.connect((ENDPOINT_IP, 65000))
        except OSError:
            pass
        else:
            raise AssertionError("fixed router egress negative TCP probe unexpectedly connected")
    client_forward = _role_json(f"{CLIENT_CONTROL}/probe/router-forward-leak", method="POST")
    assert client_forward.get("ok") is True and client_forward.get("blocked") is True, {
        "router_forward_probe": "fixed client-to-endpoint negative connect was not blocked"
    }
    after = _router_guard_counter_snapshot()
    delta = _router_guard_counter_delta(before, after)
    if any(delta[name]["packets"] <= 0 or delta[name]["bytes"] <= 0
           for name in ("router_stub_drop", "router_egress_drop", "router_forward_drop")):
        raise AssertionError("fixed router output/forward negative probes did not increment all owned drop counters")
    return {**delta, "router_stub_send_oserror": stub_send_error}


def _flow_delta(before: dict[tuple[str, str, str, int], int],
                after: dict[tuple[str, str, str, int], int],
                flow: tuple[str, str, str, int]) -> int:
    return after.get(flow, 0) - before.get(flow, 0)


def _endpoint_observations() -> dict[str, Any]:
    response = _role_json(f"{ENDPOINT_CONTROL}/observations")
    assert set(response) == {"counts", "peers", "last_peers"}, {"keys": sorted(response)}
    assert (isinstance(response.get("counts"), dict) and isinstance(response.get("peers"), dict)
            and isinstance(response.get("last_peers"), dict)), {
        "shape": "invalid observations"
    }
    return response


def _protected_endpoint_observations() -> dict[str, Any]:
    response = _role_json(f"{ENDPOINT_CONTROL}/protected-observations")
    assert set(response) == {"counts", "peers"}, {"keys": sorted(response)}
    assert (isinstance(response.get("counts"), dict) and isinstance(response.get("peers"), dict)
            and set(response["counts"]) == {"tcp", "udp"}
            and set(response["peers"]) == {"tcp", "udp"}), {"shape": "invalid protected observations"}
    return response


def _probe_protected_direct_phase(stack: dict[str, Any], source_ref: str,
                                  record_property: Any) -> dict[str, Any]:
    """Prove protected TCP/UDP stay direct while global VPN policy remains active."""
    _assert_packet_runtime(stack, mode="vpn", policy_required=True)
    native_before = _packet_vpn_runtime_selection(stack)
    selection_before = _provider_selection_snapshot(source_ref)
    protected_rule_before = _core_protected_exception_readback()
    core_before = _core_counter_snapshot()
    before_capture = _packet_counts()
    before_endpoint = _protected_endpoint_observations()
    before_guard = _router_guard_counter_snapshot()
    tcp = _role_json(f"{CLIENT_CONTROL}/probe/protected-tcp", method="POST")
    udp = _role_json(f"{CLIENT_CONTROL}/probe/protected-udp", method="POST")
    after_capture = _wait_packet_counts(lambda counts: (
        _flow_delta(before_capture, counts, (CLIENT_IP, PROTECTED_VIP, "tcp", 9080)) > 0
        and _flow_delta(before_capture, counts, (CLIENT_IP, PROTECTED_VIP, "udp", 9081)) > 0
    ))
    after_endpoint = _protected_endpoint_observations()
    core_delta = _core_counter_delta(core_before, _core_counter_snapshot())
    protected_rule_after = _core_protected_exception_readback()
    after_guard = _router_guard_counter_snapshot()
    guard_delta = _router_guard_counter_delta(before_guard, after_guard)
    selection_after = _provider_selection_snapshot(source_ref)
    _assert_packet_runtime(stack, mode="vpn", policy_required=True)
    native_after = _packet_vpn_runtime_selection(stack)
    endpoint_delta = {
        kind: after_endpoint["counts"][kind] - before_endpoint["counts"][kind]
        for kind in ("tcp", "udp")
    }
    flows = {
        "tcp_9080": _flow_delta(before_capture, after_capture, (CLIENT_IP, PROTECTED_VIP, "tcp", 9080)),
        "udp_9081": _flow_delta(before_capture, after_capture, (CLIENT_IP, PROTECTED_VIP, "udp", 9081)),
    }
    proof = {
        "phase": "protected-direct-under-global-vpn",
        "endpoint_counts_delta": endpoint_delta,
        "endpoint_peers": after_endpoint["peers"],
        "capture_flow_delta": flows,
        "router_guard_delta": guard_delta,
        "core_vpn_delta": {
            "vpn_tcp_classified": core_delta["vpn_tcp_classified"],
            "vpn_udp_classified": core_delta["vpn_udp_classified"],
        },
        "native_protected_rule": protected_rule_after,
        "native_protected_rule_stable": protected_rule_before == protected_rule_after,
        "native_vpn_active": True,
        "native_target_stable": native_before == native_after,
        "selection_stable": selection_before == selection_after,
        "selection_revision_stable": selection_before.get("selection_revision")
        == selection_after.get("selection_revision"),
        "tcp_peer": tcp.get("peer"),
        "udp_peer": udp.get("peer"),
    }
    _record_phase_diagnostic(record_property, proof)
    assert tcp.get("ok") is True and tcp.get("service") == "protected-tcp"
    assert udp.get("ok") is True and udp.get("service") == "protected-udp"
    assert endpoint_delta == {"tcp": 1, "udp": 1}, proof
    assert CLIENT_IP in after_endpoint["peers"]["tcp"] and CLIENT_IP in after_endpoint["peers"]["udp"], proof
    assert flows["tcp_9080"] > 0 and flows["udp_9081"] > 0, proof
    assert core_delta["vpn_tcp_classified"] == 0 and core_delta["vpn_udp_classified"] == 0, proof
    assert protected_rule_before == {
        "classifier_bypass_installed": True, "protected_vip_in_active_set": True,
    }, proof
    assert protected_rule_after == protected_rule_before and proof["native_protected_rule_stable"] is True, proof
    assert proof["selection_stable"] is True and proof["selection_revision_stable"] is True, proof
    assert proof["native_target_stable"] is True, proof
    assert guard_delta["router_protected_tcp_forward"]["packets"] > 0
    assert guard_delta["router_protected_udp_forward"]["packets"] > 0
    assert guard_delta["router_protected_tcp_output"]["packets"] == 0
    assert guard_delta["router_protected_udp_output"]["packets"] == 0
    return proof


def _dns53_phase_junit_projection(proof: dict[str, Any]) -> dict[str, Any]:
    """Keep only nonduplicate, fixed DNS-phase facts in the bounded JUnit receipt."""
    def counts(source: Any, keys: tuple[str, ...]) -> dict[str, int | None]:
        source = source if isinstance(source, dict) else {}
        return {
            key: source[key] if type(source.get(key)) is int and 0 <= source[key] <= 1_000_000 else None
            for key in keys
        }

    native_dns = proof.get("native_dnsmasq") if isinstance(proof.get("native_dnsmasq"), dict) else {}
    raw_targets = native_dns.get("nft_probe_targets") if isinstance(native_dns.get("nft_probe_targets"), list) else []
    targets = []
    for domain in ("vpn.test", "direct.test"):
        row = next((item for item in raw_targets if isinstance(item, dict) and item.get("domain") == domain), {})
        amount = row.get("materialized_count")
        targets.append({
            "domain": domain,
            "ok": row.get("ok") is True,
            "resolved": row.get("resolved") is True,
            "materialized_count": amount if type(amount) is int and 0 <= amount <= 1_000_000 else None,
        })
    endpoint_peers = proof.get("endpoint_peers")
    cleanup = proof.get("cleanup") if isinstance(proof.get("cleanup"), dict) else {}
    guard = proof.get("router_guard_delta") if isinstance(proof.get("router_guard_delta"), dict) else {}
    raw_peer_proof = proof.get("dns_selected_endpoint_peer_proof")
    raw_peer_proof = raw_peer_proof if isinstance(raw_peer_proof, dict) else {}
    guard_names = (
        "router_dns_vpn_direct_output", "router_dns_vpn_direct_forward",
        "router_dns_udp_input", "router_dns_tcp_input", "router_dns_udp_output",
    )
    guard_packets = {
        name: counts(guard.get(name), ("packets",))["packets"] for name in guard_names
    }
    selective_priority = proof.get("selective_direct_priority")
    selective_priority = selective_priority if isinstance(selective_priority, dict) else {}
    raw_selective_peer_classes = selective_priority.get("last_peer_classes")
    raw_selective_peer_classes = raw_selective_peer_classes if isinstance(raw_selective_peer_classes, dict) else {}
    peer_class_values = {"service_vip", "dns_vpn_vip", "client", "localhost", "other", "missing"}
    selective_peer_classes = {}
    for key in ("vpn_domain_tcp", "vpn_domain_udp", "direct_domain_tcp", "direct_domain_udp"):
        value = raw_selective_peer_classes.get(key)
        selective_peer_classes[key] = value if isinstance(value, str) and value in peer_class_values else (
            "missing" if value is None else "other"
        )
    return {
        "phase": "native-dns53-under-global-vpn",
        "client_answers_fixed": isinstance(proof.get("client_queries"), dict)
        and proof["client_queries"].get("answers") == {
            "probe.vpn.test_udp": "198.51.100.77", "probe.vpn.test_tcp": "198.51.100.77",
            "probe.direct.test_udp": "203.0.113.53", "probe.direct.test_tcp": "203.0.113.53",
        },
        "external_nxdomain_both": isinstance(proof.get("external_resolver_queries"), dict)
        and proof["external_resolver_queries"].get("outcomes") == {
            "udp": "router_nxdomain", "tcp": "router_nxdomain",
        },
        "resolved_user_flows_fixed": isinstance(proof.get("resolved_user_flows"), dict)
        and proof["resolved_user_flows"].get("results") == {
            "vpn": {"answer": "198.51.100.77", "tcp": True, "udp": True},
            "direct": {"answer": "203.0.113.53", "tcp": True, "udp": True},
        },
        "dns_selected_endpoint_flow_delta": counts(
            proof.get("dns_selected_endpoint_flow_delta"),
            ("vpn_domain_tcp", "vpn_domain_udp", "direct_domain_tcp", "direct_domain_udp"),
        ),
        "dns_selected_endpoint_peer_proof": {
            key: raw_peer_proof.get(key) is True for key in {
                "direct_domain_tcp_local", "direct_domain_udp_local",
                "vpn_domain_tcp_local", "vpn_domain_udp_local",
            }
        },
        "resolved_flow_core_delta": counts(
            proof.get("resolved_flow_core_delta"),
            ("vpn_tcp_classified", "vpn_udp_classified", "vpn_udp_handoff_packets"),
        ),
        "resolved_flow_direct_capture_delta": counts(
            proof.get("resolved_flow_direct_capture_delta"),
            ("service_tcp_router_direct", "service_udp_router_direct", "vpn_alias_tcp_router_direct",
             "vpn_alias_udp_router_direct", "service_tcp_client_direct", "service_udp_client_direct",
             "vpn_alias_tcp_client_direct", "vpn_alias_udp_client_direct"),
        ),
        "native_dnsmasq_reference": "dns53-native-daemon",
        "rescue0_allowlisted": native_dns.get("rescue0_allowlisted") is True,
        "rescue0_role_excluded_from_discovery": native_dns.get("rescue0_role_excluded_from_discovery") is True,
        "dhcp_excludes_rescue_vip": native_dns.get("dhcp_excludes_rescue_vip") is True,
        "local_hosts_exclude_rescue_vip": native_dns.get("local_hosts_exclude_rescue_vip") is True,
        "dns_capture_excludes_rescue0": native_dns.get("dns_capture_excludes_rescue0") is True,
        "router_lan_hostname_query_ok": native_dns.get("router_lan_hostname_query_ok") is True,
        "selective_direct_priority_peer_classes": selective_peer_classes,
        "native_nftset_probe_targets": targets,
        "capture_router_to_owned_upstream": counts(
            proof.get("capture_router_to_owned_upstream"), ("udp_upstream", "tcp_upstream"),
        ),
        "endpoint_observations_delta": counts(
            proof.get("endpoint_observations_delta"),
            ("udp", "tcp", "vpn_name", "direct_name", "other_name"),
        ),
        "endpoint_reply_delta": counts(
            proof.get("endpoint_reply_delta"),
            ("udp", "tcp", "vpn_name", "direct_name", "other_name"),
        ),
        "endpoint_peers_router_wan_only": isinstance(endpoint_peers, list)
        and bool(endpoint_peers) and set(endpoint_peers) == {ROUTER_WAN_IP},
        "router_guard_dns_packets": guard_packets,
        "native_vpn_stable": proof.get("native_vpn_stable") is True,
        "selection_stable": proof.get("selection_stable") is True,
        "api_status_ok": proof.get("api_status") == 200,
        "cleanup_ok": cleanup.get("http_status") == 200 and cleanup.get("process_reaped") is True
        and cleanup.get("owned_paths_cleaned") is True
        and cleanup.get("rescue_interface_cleaned") is True,
    }


def _probe_dns53_policy_phase(stack: dict[str, Any], source_ref: str, record_property: Any) -> dict[str, Any]:
    """Exercise generated DNS policy with real dnsmasq, endpoint, captures, and native counters."""
    api = stack["api"]
    prepared_code, prepared = http_json(f"{api}/__acceptance/dns53/configure", method="POST")
    assert prepared_code == 200 and prepared.get("ok") is True and prepared.get("fixture_configured") is True, prepared
    rules_text = "VPN vpn.test\nDIRECT direct.test"
    code, saved = http_json(f"{api}/rules/manual", method="POST", payload={"text": rules_text})
    assert code == 200 and saved.get("ok") is True, saved
    endpoint_before_manual_apply = _role_json(f"{ENDPOINT_CONTROL}/dns53-observations")
    code, accepted = http_json(f"{api}/rules/manual/apply", method="POST", payload={"requested_by": "packet-dns53", "run_now": True})
    accepted_data = accepted.get("data") if isinstance(accepted.get("data"), dict) else {}
    accepted_job_ref = accepted_data.get("job") if isinstance(accepted_data.get("job"), dict) else {}
    job = (await_core_job(api, accepted, timeout_seconds=90)
           if isinstance(accepted_job_ref.get("job_id"), str) and accepted_job_ref.get("job_id")
           else None)
    endpoint_after_manual_apply = _role_json(f"{ENDPOINT_CONTROL}/dns53-observations")
    accepted_error = accepted_data.get("error") if isinstance(accepted_data.get("error"), dict) else accepted.get("error")
    accepted_error_code = accepted_error.get("code") if isinstance(accepted_error, dict) else None
    if (not isinstance(accepted_error_code, str) or not accepted_error_code
            or len(accepted_error_code) > 64
            or not all(char.isascii() and (char.isalnum() or char in "_.:-") for char in accepted_error_code)):
        accepted_error_code = None
    manual_apply_diagnostic = {
        "phase": "dns53-manual-apply",
        "http_status": code if type(code) is int else None,
        "accepted": accepted.get("ok") is True,
        "response_error_code": accepted_error_code,
        "job": _bounded_job_summary(job),
    }
    _record_phase_diagnostic(record_property, manual_apply_diagnostic)
    assert code == 200 and accepted.get("ok") is True and isinstance(job, dict) \
        and job.get("status") == "success", manual_apply_diagnostic

    def endpoint_observation_delta(before: dict[str, Any], after: dict[str, Any]) -> dict[str, int | None]:
        result: dict[str, int | None] = {}
        for family in ("counts", "replies"):
            before_family = before.get(family) if isinstance(before.get(family), dict) else {}
            after_family = after.get(family) if isinstance(after.get(family), dict) else {}
            for key in ("udp", "tcp", "vpn_name", "direct_name", "other_name"):
                left, right = before_family.get(key), after_family.get(key)
                result[f"{family}_{key}"] = right - left if type(left) is int and type(right) is int and right >= left else None
        return result

    def bounded_endpoint_delta(before: dict[str, Any], after: dict[str, Any]) -> dict[str, int | None]:
        delta = endpoint_observation_delta(before, after)
        return {
            key: value if type(value) is int and 0 <= value <= 1_000_000_000 else None
            for key, value in delta.items()
        }

    def endpoint_rejection_delta(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
        reasons = ("query_size_invalid", "flags_unsupported",
                   "question_boundary_invalid", "name_invalid")
        before_counts = before.get("rejection_counts") if isinstance(before.get("rejection_counts"), dict) else {}
        after_counts = after.get("rejection_counts") if isinstance(after.get("rejection_counts"), dict) else {}
        counts: dict[str, int | None] = {}
        for reason in reasons:
            left, right = before_counts.get(reason), after_counts.get(reason)
            counts[reason] = right - left if (type(left) is int and type(right) is int
                                               and 0 <= left <= right <= 1_000_000_000) else None
        observed = after.get("last_rejection") if isinstance(after.get("last_rejection"), dict) else {}
        reason = observed.get("reason")
        transport = observed.get("transport")
        flags = observed.get("flags")
        has_new_observation = any(type(value) is int and value > 0 for value in counts.values())
        if not has_new_observation or reason not in reasons or transport not in {"udp", "tcp"}:
            reason = transport = None
            flags = None
        elif type(flags) is not int or not 0 <= flags <= 0xFFFF:
            flags = None
        return {"counts": counts, "last_reason": reason, "last_transport": transport,
                "last_flags": flags}

    def endpoint_snapshot_error(exc: BaseException) -> str:
        if isinstance(exc, TimeoutError):
            return "timeout"
        if isinstance(exc, urllib.error.URLError):
            return "url_error"
        return "os_error"

    endpoint_before_status: dict[str, Any] = {}
    endpoint_before_status_error = None
    try:
        endpoint_before_status = _role_json(f"{ENDPOINT_CONTROL}/dns53-observations")
    except (TimeoutError, OSError, urllib.error.URLError) as exc:
        endpoint_before_status_error = endpoint_snapshot_error(exc)
    try:
        setup_status_code, setup_dns = http_json(f"{api}/__acceptance/dns53/status", timeout=3.0)
    except (TimeoutError, OSError, urllib.error.URLError) as exc:
        endpoint_after_status: dict[str, Any] = {}
        endpoint_after_status_error = None
        try:
            endpoint_after_status = _role_json(f"{ENDPOINT_CONTROL}/dns53-observations")
        except (TimeoutError, OSError, urllib.error.URLError) as snapshot_exc:
            endpoint_after_status_error = endpoint_snapshot_error(snapshot_exc)
        failure_diagnostic = {
            "phase": "dns53-status-read",
            "http_status": None,
            "status_read_error": endpoint_snapshot_error(exc),
            "endpoint_before_status_error": endpoint_before_status_error,
            "endpoint_after_status_error": endpoint_after_status_error,
            "endpoint_status_read_delta": bounded_endpoint_delta(
                endpoint_before_status, endpoint_after_status),
            "endpoint_status_rejection_delta": endpoint_rejection_delta(
                endpoint_before_status, endpoint_after_status),
            "endpoint_during_manual_apply": bounded_endpoint_delta(
                endpoint_before_manual_apply, endpoint_after_manual_apply),
        }
        _record_phase_diagnostic(record_property, failure_diagnostic)
        raise
    assert job.get("status") == "success", {
        "dns53_manual_apply": _bounded_job_summary(job), "native_dnsmasq": setup_dns,
        "native_status_code": setup_status_code,
    }
    startup_error_code = setup_dns.get("startup_error_code")
    if startup_error_code not in {
        "native_uid_mismatch", "native_process_exited", "readiness_probe_timeout", "readiness_deadline",
    }:
        startup_error_code = None
    process_exit_code = setup_dns.get("process_exit_code")
    if type(process_exit_code) is not int or not -255 <= process_exit_code <= 255:
        process_exit_code = None
    config_test_status = setup_dns.get("config_test_status")
    if config_test_status not in {"passed", "failed", "timeout"}:
        config_test_status = None
    config_test_returncode = setup_dns.get("config_test_returncode")
    if type(config_test_returncode) is not int or not -255 <= config_test_returncode <= 255:
        config_test_returncode = None
    config_test_error_code = setup_dns.get("config_test_error_code")
    if config_test_error_code not in {
        "config_test_timeout", "config_test_output_bound_exceeded", "config_test_rejected",
    }:
        config_test_error_code = None
    reconcile_result = setup_dns.get("reconcile_result")
    if isinstance(reconcile_result, dict):
        artifact_read = reconcile_result.get("artifact_read")
        reconcile_error_code = reconcile_result.get("error_code")
        if reconcile_error_code not in {
            "DNSMASQ_DNS_CAPTURE_FAILED", "DNSMASQ_RESTART_FAILED",
            "DNSMASQ_SELECTIVE_CONTRACT_INCOMPLETE",
        }:
            reconcile_error_code = None
        reconcile_reason = reconcile_result.get("restart_reason")
        if reconcile_reason not in {None, "config_changed", "nftset_probe_unhealthy"}:
            reconcile_reason = None
        reconcile_result = {
            "ok": reconcile_result.get("ok") is True,
            "error_code": reconcile_error_code,
            "restart_required": reconcile_result.get("restart_required") is True,
            "restart_reason": reconcile_reason,
            "selective_status_ok": reconcile_result.get("selective_status_ok")
            if type(reconcile_result.get("selective_status_ok")) is bool else None,
        }
        if isinstance(artifact_read, dict):
            reconcile_result["artifact_read"] = {
                key: artifact_read.get(key) if type(artifact_read.get(key)) is bool else None
                for key in ("cache_entry_present", "cached_artifact_present", "uncached_artifact_present",
                            "cache_inflight_present", "cache_entry_valid_at_reader", "cached_matches_uncached",
                            "writer_path_matches_reader", "writer_path_exists",
                            "reader_returned_artifact", "uncached_presence_matches_reader_result",
                            "cached_presence_matches_reader_result")
            }
        else:
            reconcile_result["artifact_read"] = None
    else:
        reconcile_result = None
    capture_missing_codes = []
    for item in setup_dns.get("dns_capture_missing_codes", [])[:2] if isinstance(
            setup_dns.get("dns_capture_missing_codes"), list) else []:
        if item in {"router_dns_interfaces_missing", "dns_capture_udp_missing", "dns_capture_tcp_missing"}:
            capture_missing_codes.append(item)
    capture_rule_status = []
    raw_capture_rules = setup_dns.get("dns_capture_rules_present")
    if isinstance(raw_capture_rules, list):
        for item in raw_capture_rules[:1]:
            if isinstance(item, dict):
                capture_rule_status.append({"udp": item.get("udp") is True, "tcp": item.get("tcp") is True})
    nft_probe_targets = []
    raw_nft_targets = setup_dns.get("nft_probe_targets")
    if isinstance(raw_nft_targets, list):
        for item in raw_nft_targets[:2]:
            if not isinstance(item, dict) or item.get("domain") not in {"vpn.test", "direct.test"}:
                continue
            materialized_count = item.get("materialized_count")
            nft_probe_targets.append({
                "domain": item["domain"],
                "ok": item.get("ok") is True,
                "resolved": item.get("resolved") is True,
                "materialized_count": min(materialized_count, 1)
                if type(materialized_count) is int and materialized_count >= 0 else None,
            })

    daemon_diagnostic = {
        "phase": "dns53-native-daemon",
        "http_status": setup_status_code if type(setup_status_code) is int else None,
        "configured": setup_dns.get("configured") is True,
        "daemon_running": setup_dns.get("daemon_running") is True,
        "daemon_uid_matches": setup_dns.get("daemon_uid_matches") is True,
        "native_process_arguments_match": setup_dns.get("native_process_arguments_match") is True,
        "selective_status_ok": setup_dns.get("selective_status_ok") is True,
        "dns_capture_ok": setup_dns.get("dns_capture_ok") is True,
        "nftset_probe_ok": setup_dns.get("nftset_probe_ok") is True,
        "startup_error_code": startup_error_code,
        "process_exit_code": process_exit_code,
        "config_test_status": config_test_status,
        "config_test_returncode": config_test_returncode,
        "config_test_error_code": config_test_error_code,
        "dns_capture_missing_codes": sorted(set(capture_missing_codes)),
        "dns_capture_missing_unknown_count_capped": setup_dns.get("dns_capture_missing_unknown_count_capped")
        if setup_dns.get("dns_capture_missing_unknown_count_capped") in {0, 1, 2} else None,
        "dns_capture_missing_truncated": setup_dns.get("dns_capture_missing_truncated") is True,
        "dns_capture_rule_count_capped": setup_dns.get("dns_capture_rule_count_capped")
        if setup_dns.get("dns_capture_rule_count_capped") in {0, 1, 2} else None,
        "dns_capture_rules_present": capture_rule_status,
        "rescue0_allowlisted": setup_dns.get("rescue0_allowlisted") is True,
        "rescue0_role_excluded_from_discovery": setup_dns.get("rescue0_role_excluded_from_discovery") is True,
        "router_dns_interfaces": setup_dns.get("router_dns_interfaces")
        if isinstance(setup_dns.get("router_dns_interfaces"), list) else [],
        "router_dns_ipv4": setup_dns.get("router_dns_ipv4")
        if isinstance(setup_dns.get("router_dns_ipv4"), list) else [],
        "dhcp_excludes_rescue_vip": setup_dns.get("dhcp_excludes_rescue_vip") is True,
        "dhcp_forces_router_dns": setup_dns.get("dhcp_forces_router_dns") is True,
        "local_hosts_exclude_rescue_vip": setup_dns.get("local_hosts_exclude_rescue_vip") is True,
        "dns_capture_interfaces": setup_dns.get("dns_capture_interfaces")
        if isinstance(setup_dns.get("dns_capture_interfaces"), list) else [],
        "dns_capture_excludes_rescue0": setup_dns.get("dns_capture_excludes_rescue0") is True,
        "router_lan_hostname_query_ok": setup_dns.get("router_lan_hostname_query_ok") is True,
        "nftset_probe_status": setup_dns.get("nftset_probe_status")
        if setup_dns.get("nftset_probe_status") in {"ok", "unhealthy", "not_applicable", "unknown"} else "unknown",
        "nftset_probe_target_count_capped": setup_dns.get("nftset_probe_target_count_capped")
        if setup_dns.get("nftset_probe_target_count_capped") in {0, 1, 2, 3} else None,
        "nftset_probe_targets_truncated": setup_dns.get("nftset_probe_targets_truncated") is True,
        "nft_probe_targets": nft_probe_targets,
        "endpoint_during_manual_apply": endpoint_observation_delta(
            endpoint_before_manual_apply, endpoint_after_manual_apply),
        "reconcile_result": reconcile_result,
    }
    _record_phase_diagnostic(record_property, daemon_diagnostic)
    assert setup_status_code == 200 and setup_dns.get("daemon_running") is True
    assert setup_dns.get("daemon_uid_matches") is True and setup_dns.get("selective_status_ok") is True
    assert setup_dns.get("native_process_arguments_match") is True
    assert setup_dns.get("upstream_domains") == ["vpn.test"]
    assert setup_dns.get("unexpected_upstream_rule_count") == 0
    assert setup_dns.get("dns_capture_ok") is True and setup_dns.get("nftset_probe_ok") is True
    assert setup_dns.get("rescue0_allowlisted") is True
    assert setup_dns.get("rescue0_role_excluded_from_discovery") is True
    assert setup_dns.get("router_dns_ipv4") == ["10.240.0.1"]
    assert isinstance(setup_dns.get("router_dns_interfaces"), list)
    assert len(setup_dns["router_dns_interfaces"]) == 1
    assert setup_dns.get("dhcp_excludes_rescue_vip") is True
    assert setup_dns.get("dhcp_forces_router_dns") is True
    assert setup_dns.get("local_hosts_exclude_rescue_vip") is True
    assert setup_dns.get("dns_capture_excludes_rescue0") is True
    assert setup_dns.get("dns_capture_interfaces") == setup_dns.get("router_dns_interfaces")
    assert setup_dns.get("router_lan_hostname_query_ok") is True

    _assert_packet_runtime(stack, mode="vpn", policy_required=True)
    native_before = _packet_vpn_runtime_selection(stack)
    selection_before = _provider_selection_snapshot(source_ref)
    guard_before = _router_guard_counter_snapshot()
    packets_before = _packet_counts()
    endpoint_before = _role_json(f"{ENDPOINT_CONTROL}/dns53-observations")
    selected_flows_before = _role_json(f"{ENDPOINT_CONTROL}/dns53-flow-observations")
    core_before = _core_counter_snapshot()
    client = _role_json(f"{CLIENT_CONTROL}/probe/dns53", method="POST",
                        failure_record_property=record_property)
    external = _role_json(f"{CLIENT_CONTROL}/probe/dns53-external", method="POST")
    resolved_flows = _role_json(f"{CLIENT_CONTROL}/probe/dns53-flows", method="POST",
                                failure_record_property=record_property)
    packets_after = _wait_packet_counts(lambda counts:
        _flow_delta(packets_before, counts, (ROUTER_WAN_IP, "1.1.1.1", "udp", 53)) > 0
        and _flow_delta(packets_before, counts, (ROUTER_WAN_IP, ENDPOINT_IP, "tcp", 5301)) > 0)
    guard_after = _router_guard_counter_snapshot()
    endpoint_after = _role_json(f"{ENDPOINT_CONTROL}/dns53-observations")
    selected_flows_after = _role_json(f"{ENDPOINT_CONTROL}/dns53-flow-observations")
    core_after = _core_counter_snapshot()
    # Keep the expensive uncached native DNS/nftset proof single-shot for this
    # phase; the actual subsequent LAN queries and endpoint packets prove forwarding.
    status_code, native_dns = setup_status_code, setup_dns
    _assert_packet_runtime(stack, mode="vpn", policy_required=True)
    native_after = _packet_vpn_runtime_selection(stack)
    selection_after = _provider_selection_snapshot(source_ref)

    routing_code, routing_projection = http_json(f"{api}/routing/global")
    routing_data = routing_projection.get("data") if isinstance(routing_projection, dict) else None
    original_routing = routing_data.get("routing") if isinstance(routing_data, dict) else None
    original_mode = original_routing.get("applied_mode") if isinstance(original_routing, dict) else None
    original_selective_default = (original_routing.get("selective_default")
                                  if isinstance(original_routing, dict) else None)
    assert (routing_code == 200 and isinstance(original_routing, dict)
            and original_mode == "vpn" and original_selective_default in {"direct", "vpn"}), {
                "routing_readback_status": routing_code,
                "global_vpn_before_selective_probe": original_mode == "vpn",
                "selective_default_valid": original_selective_default in {"direct", "vpn"},
            }
    selective_priority_proof: dict[str, Any] = {
        "phase": "native-dns53-selective-direct-priority",
    }
    selective_provider_calls_before = stack["provider_bridge"].snapshot_calls()
    selective_primary_error: Exception | None = None
    selective_primary_traceback = None
    selective_restore_errors: dict[str, Exception] = {}
    try:
        _apply_selective_default(api, "vpn", stack=stack, record_property=record_property,
                                 phase="selective-default-vpn")
        selective_data = _apply_global_mode(api, "selective", stack=stack, record_property=record_property)
        _assert_packet_runtime(stack, mode="selective", policy_required=True)
        selective_routing = selective_data.get("routing") if isinstance(selective_data, dict) else None
        selective_runtime = selective_data.get("runtime_enforcement") if isinstance(selective_data, dict) else None
        selective_mode_ready = (
            isinstance(selective_routing, dict)
            and selective_routing.get("applied_mode") == "selective"
            and selective_routing.get("selective_default") == "vpn"
            and isinstance(selective_runtime, dict)
            and selective_runtime.get("live_global_mode") == "selective"
            and selective_runtime.get("active_mode_matches_intent") is True
        )
        native_selective_before = _packet_vpn_runtime_selection(stack)
        selection_selective_before = _provider_selection_snapshot(source_ref)
        packets_selective_before = _packet_counts()
        endpoint_flows_selective_before = _role_json(f"{ENDPOINT_CONTROL}/dns53-flow-observations")
        core_selective_before = _core_counter_snapshot()
        selective_vpn_before = _core_selective_vpn_counter_snapshot()
        selective_flows = _role_json(f"{CLIENT_CONTROL}/probe/dns53-flows", method="POST",
                                     failure_record_property=record_property)
        packets_selective_after = _wait_packet_counts(lambda counts:
            _flow_delta(packets_selective_before, counts, (CLIENT_IP, SERVICE_VIP, "tcp", 9080)) > 0
            and _flow_delta(packets_selective_before, counts, (CLIENT_IP, SERVICE_VIP, "udp", 9081)) > 0
            and _flow_delta(packets_selective_before, counts, (ROUTER_WAN_IP, ENDPOINT_IP, "tcp", 5301)) > 0)
        endpoint_flows_selective_after = _role_json(f"{ENDPOINT_CONTROL}/dns53-flow-observations")
        core_selective_after = _core_counter_snapshot()
        selective_vpn_after = _core_selective_vpn_counter_snapshot()
        _assert_packet_runtime(stack, mode="selective", policy_required=True)
        native_selective_after = _packet_vpn_runtime_selection(stack)
        selection_selective_after = _provider_selection_snapshot(source_ref)

        counts_before = endpoint_flows_selective_before.get("counts")
        counts_after = endpoint_flows_selective_after.get("counts")
        counts_before = counts_before if isinstance(counts_before, dict) else {}
        counts_after = counts_after if isinstance(counts_after, dict) else {}
        selective_flow_delta = {
            key: counts_after.get(key) - counts_before.get(key)
            if type(counts_before.get(key)) is int and type(counts_after.get(key)) is int
            and counts_after.get(key) >= counts_before.get(key) else None
            for key in ("vpn_domain_tcp", "vpn_domain_udp", "direct_domain_tcp", "direct_domain_udp")
        }
        last_peers = endpoint_flows_selective_after.get("last_peers")
        last_peers = last_peers if isinstance(last_peers, dict) else {}
        def fixed_peer_class(peer: Any) -> str:
            if not isinstance(peer, str):
                return "missing"
            return {
                SERVICE_VIP: "service_vip",
                DNS_VPN_SERVICE_VIP: "dns_vpn_vip",
                CLIENT_IP: "client",
                "127.0.0.1": "localhost",
            }.get(peer, "other")

        selective_last_peer_matches = {
            "vpn_domain_tcp": last_peers.get("vpn_domain_tcp") == SERVICE_VIP,
            "vpn_domain_udp": last_peers.get("vpn_domain_udp") == SERVICE_VIP,
            "direct_domain_tcp": last_peers.get("direct_domain_tcp") == CLIENT_IP,
            "direct_domain_udp": last_peers.get("direct_domain_udp") == CLIENT_IP,
        }
        selective_last_peer_classes = {
            key: fixed_peer_class(last_peers.get(key))
            for key in ("vpn_domain_tcp", "vpn_domain_udp", "direct_domain_tcp", "direct_domain_udp")
        }
        selective_packet_delta = {
            "direct_client_tcp": _flow_delta(packets_selective_before, packets_selective_after,
                                               (CLIENT_IP, SERVICE_VIP, "tcp", 9080)),
            "direct_client_udp": _flow_delta(packets_selective_before, packets_selective_after,
                                               (CLIENT_IP, SERVICE_VIP, "udp", 9081)),
            "direct_client_to_vpn_tcp": _flow_delta(packets_selective_before, packets_selective_after,
                                                      (CLIENT_IP, DNS_VPN_SERVICE_VIP, "tcp", 9080)),
            "direct_client_to_vpn_udp": _flow_delta(packets_selective_before, packets_selective_after,
                                                      (CLIENT_IP, DNS_VPN_SERVICE_VIP, "udp", 9081)),
            "direct_router_to_vpn_tcp": _flow_delta(packets_selective_before, packets_selective_after,
                                                      (ROUTER_WAN_IP, DNS_VPN_SERVICE_VIP, "tcp", 9080)),
            "direct_router_to_vpn_udp": _flow_delta(packets_selective_before, packets_selective_after,
                                                      (ROUTER_WAN_IP, DNS_VPN_SERVICE_VIP, "udp", 9081)),
            "provider_vless_tcp": _flow_delta(packets_selective_before, packets_selective_after,
                                                (ROUTER_WAN_IP, ENDPOINT_IP, "tcp", 5301)),
        }
        selective_core = _core_counter_delta(core_selective_before, core_selective_after)
        selective_vpn_delta = {
            key: selective_vpn_after[key] - selective_vpn_before[key]
            for key in ("tcp", "udp")
        }
        selective_priority_proof = {
            "phase": "native-dns53-selective-direct-priority",
            "selective_mode_with_vpn_default": selective_mode_ready,
            "resolved_flows_fixed": selective_flows.get("results") == {
                "vpn": {"answer": DNS_VPN_SERVICE_VIP, "tcp": True, "udp": True},
                "direct": {"answer": SERVICE_VIP, "tcp": True, "udp": True},
            },
            "flow_delta": selective_flow_delta,
            "last_peer_matches": selective_last_peer_matches,
            "last_peer_classes": selective_last_peer_classes,
            "direct_core_path_delta": selective_core["direct_lan_forward"],
            "selective_vpn_mark_delta": selective_vpn_delta,
            "packet_delta": selective_packet_delta,
            "native_selector_stable": native_selective_before == native_selective_after,
            "provider_selection_stable": selection_selective_before == selection_selective_after,
        }
    except Exception as exc:
        selective_primary_error = exc
        selective_primary_traceback = exc.__traceback__
    finally:
        restored_mode_data = None
        restored_default_data = None
        try:
            restored_mode_data = _apply_global_mode(api, original_mode, stack=stack,
                                                    record_property=record_property)
        except Exception as exc:
            selective_restore_errors["mode"] = exc
        try:
            restored_default_data = _apply_selective_default(
                api, original_selective_default, stack=stack, record_property=record_property,
                phase="selective-default-restore",
            )
        except Exception as exc:
            selective_restore_errors["default"] = exc
        restored_routing = (restored_default_data.get("routing")
                            if isinstance(restored_default_data, dict) else None)
        restored_runtime = (restored_default_data.get("runtime_enforcement")
                            if isinstance(restored_default_data, dict) else None)
        selective_priority_proof["state_restored"] = (
            isinstance(restored_routing, dict)
            and restored_routing.get("applied_mode") == original_mode
            and restored_routing.get("selective_default") == original_selective_default
            and isinstance(restored_runtime, dict)
            and restored_runtime.get("live_global_mode") == original_mode
            and restored_runtime.get("active_mode_matches_intent") is True
            and isinstance(restored_mode_data, dict)
        )
        def fixed_exception_class(exc: Exception) -> str:
            name = type(exc).__name__
            return name if name in {"AssertionError", "TimeoutError", "OSError", "RuntimeError",
                                    "ValueError", "KeyError"} else "other"

        selective_priority_proof["primary_failure_class"] = (
            fixed_exception_class(selective_primary_error) if selective_primary_error is not None else None
        )
        selective_priority_proof["restoration_receipt"] = {
            "mode_ok": isinstance(restored_mode_data, dict),
            "mode_error_class": fixed_exception_class(selective_restore_errors["mode"])
            if "mode" in selective_restore_errors else None,
            "default_ok": isinstance(restored_default_data, dict),
            "default_error_class": fixed_exception_class(selective_restore_errors["default"])
            if "default" in selective_restore_errors else None,
        }
        selective_priority_proof["provider_api_calls_unchanged"] = (
            stack["provider_bridge"].snapshot_calls() == selective_provider_calls_before
        )

    cleanup_error = None
    cleanup_code = None
    cleanup: dict[str, Any] = {}
    try:
        cleanup_code, cleanup = http_json(f"{api}/__acceptance/dns53/stop", method="POST")
    except Exception as exc:
        cleanup_error = exc
    selective_priority_proof["cleanup_receipt"] = {
        "http_status": cleanup_code if type(cleanup_code) is int else None,
        "stopped": cleanup.get("stopped") is True,
        "process_reaped": cleanup.get("process_reaped") is True,
        "owned_paths_cleaned": cleanup.get("owned_paths_cleaned") is True,
        "error_class": fixed_exception_class(cleanup_error) if cleanup_error is not None else None,
    }
    _record_phase_diagnostic(record_property, selective_priority_proof)
    if selective_primary_error is not None:
        if selective_restore_errors:
            raise selective_primary_error.with_traceback(selective_primary_traceback) from next(
                iter(selective_restore_errors.values()))
        if cleanup_error is not None:
            raise selective_primary_error.with_traceback(selective_primary_traceback) from cleanup_error
        raise selective_primary_error.with_traceback(selective_primary_traceback)
    if selective_restore_errors:
        raise next(iter(selective_restore_errors.values()))
    if cleanup_error is not None:
        raise cleanup_error

    def count_delta(name: str) -> int:
        before = (endpoint_before.get("counts") or {}).get(name)
        after = (endpoint_after.get("counts") or {}).get(name)
        assert type(before) is int and type(after) is int and after >= before, {
            "dns53_endpoint_counter": name,
        }
        return after - before

    capture_delta = {
        "udp_upstream": _flow_delta(packets_before, packets_after, (ROUTER_WAN_IP, "1.1.1.1", "udp", 53)),
        "tcp_upstream": _flow_delta(packets_before, packets_after, (ROUTER_WAN_IP, "1.1.1.1", "tcp", 53)),
    }
    guard_delta = _router_guard_counter_delta(guard_before, guard_after)
    core_delta = _core_counter_delta(core_before, core_after)
    selected_counts_before = selected_flows_before.get("counts") or {}
    selected_counts_after = selected_flows_after.get("counts") or {}
    selected_flow_delta = {
        name: selected_counts_after.get(name) - selected_counts_before.get(name)
        if type(selected_counts_before.get(name)) is int and type(selected_counts_after.get(name)) is int
        and selected_counts_after.get(name) >= selected_counts_before.get(name) else None
        for name in ("vpn_domain_tcp", "vpn_domain_udp", "direct_domain_tcp", "direct_domain_udp")
    }
    selected_peers = selected_flows_after.get("peers") or {}
    endpoint_peer_proof = {
        "direct_domain_tcp_local": bool(selected_peers.get("direct_domain_tcp"))
        and set(selected_peers.get("direct_domain_tcp", [])) <= {"127.0.0.1", SERVICE_VIP, DNS_VPN_SERVICE_VIP},
        "direct_domain_udp_local": bool(selected_peers.get("direct_domain_udp"))
        and set(selected_peers.get("direct_domain_udp", [])) <= {"127.0.0.1", SERVICE_VIP, DNS_VPN_SERVICE_VIP},
        "vpn_domain_tcp_local": bool(selected_peers.get("vpn_domain_tcp"))
        and set(selected_peers.get("vpn_domain_tcp", [])) <= {"127.0.0.1", SERVICE_VIP, DNS_VPN_SERVICE_VIP},
        "vpn_domain_udp_local": bool(selected_peers.get("vpn_domain_udp"))
        and set(selected_peers.get("vpn_domain_udp", [])) <= {"127.0.0.1", SERVICE_VIP, DNS_VPN_SERVICE_VIP},
    }
    direct_capture_delta = {
        "service_tcp_router_direct": _flow_delta(packets_before, packets_after, (ROUTER_WAN_IP, SERVICE_VIP, "tcp", 9080)),
        "service_udp_router_direct": _flow_delta(packets_before, packets_after, (ROUTER_WAN_IP, SERVICE_VIP, "udp", 9081)),
        "vpn_alias_tcp_router_direct": _flow_delta(packets_before, packets_after,
                                                       (ROUTER_WAN_IP, DNS_VPN_SERVICE_VIP, "tcp", 9080)),
        "vpn_alias_udp_router_direct": _flow_delta(packets_before, packets_after,
                                                       (ROUTER_WAN_IP, DNS_VPN_SERVICE_VIP, "udp", 9081)),
        "service_tcp_client_direct": _flow_delta(packets_before, packets_after,
                                                       (CLIENT_IP, SERVICE_VIP, "tcp", 9080)),
        "service_udp_client_direct": _flow_delta(packets_before, packets_after,
                                                       (CLIENT_IP, SERVICE_VIP, "udp", 9081)),
        "vpn_alias_tcp_client_direct": _flow_delta(packets_before, packets_after,
                                                       (CLIENT_IP, DNS_VPN_SERVICE_VIP, "tcp", 9080)),
        "vpn_alias_udp_client_direct": _flow_delta(packets_before, packets_after,
                                                       (CLIENT_IP, DNS_VPN_SERVICE_VIP, "udp", 9081)),
    }
    endpoint_delta = {name: count_delta(name) for name in ("udp", "tcp", "vpn_name", "direct_name", "other_name")}
    endpoint_reply_delta = {
        name: endpoint_observation_delta(endpoint_before, endpoint_after).get(f"replies_{name}")
        for name in ("udp", "tcp", "vpn_name", "direct_name", "other_name")
    }
    proof = {
        "phase": "native-dns53-under-global-vpn",
        "client_queries": client,
        "external_resolver_queries": external,
        "resolved_user_flows": resolved_flows,
        "dns_selected_endpoint_flow_delta": selected_flow_delta,
        "dns_selected_endpoint_peer_proof": endpoint_peer_proof,
        "dns_selected_endpoint_peers": selected_flows_after.get("peers"),
        "resolved_flow_core_delta": core_delta,
        "resolved_flow_direct_capture_delta": direct_capture_delta,
        "native_dnsmasq": native_dns,
        "capture_router_to_owned_upstream": capture_delta,
        "endpoint_observations_delta": endpoint_delta,
        "endpoint_reply_delta": endpoint_reply_delta,
        "endpoint_peers": endpoint_after.get("peers"),
        "router_guard_delta": guard_delta,
        "native_vpn_before": native_before,
        "native_vpn_after": native_after,
        "native_vpn_stable": native_before == native_after,
        "selection_stable": selection_before == selection_after,
        "selective_direct_priority": selective_priority_proof,
        "api_status": status_code,
        "cleanup": {"http_status": cleanup_code, "process_reaped": cleanup.get("process_reaped") is True,
                    "owned_paths_cleaned": cleanup.get("owned_paths_cleaned") is True,
                    "rescue_interface_cleaned": cleanup.get("rescue_interface_cleaned") is True},
    }
    _record_phase_diagnostic(record_property, _dns53_phase_junit_projection(proof))
    assert client.get("answers") == {
        "probe.vpn.test_udp": "198.51.100.77", "probe.vpn.test_tcp": "198.51.100.77",
        "probe.direct.test_udp": "203.0.113.53", "probe.direct.test_tcp": "203.0.113.53",
    }, proof
    assert resolved_flows.get("results") == {
        "vpn": {"answer": DNS_VPN_SERVICE_VIP, "tcp": True, "udp": True},
        "direct": {"answer": SERVICE_VIP, "tcp": True, "udp": True},
    }, proof
    assert external.get("outcomes") == {"udp": "router_nxdomain", "tcp": "router_nxdomain"}, proof
    assert status_code == 200 and native_dns.get("configured") is True and native_dns.get("daemon_running") is True
    assert native_dns.get("daemon_uid_matches") is True and native_dns.get("native_process_arguments_match") is True
    assert native_dns.get("upstream_domains") == ["vpn.test"]
    assert native_dns.get("unexpected_upstream_rule_count") == 0
    assert native_dns.get("selective_status_ok") is True and native_dns.get("dns_capture_ok") is True
    assert native_dns.get("nftset_probe_ok") is True
    assert native_dns.get("vpn_rule_generated") is True and native_dns.get("vpn_nftset_generated") is True
    assert native_dns.get("direct_rule_generated") is True and native_dns.get("direct_nftset_generated") is True
    assert {row.get("domain") for row in native_dns.get("nft_probe_targets", []) if row.get("ok") and row.get("resolved") and row.get("materialized_count", 0) > 0} == {"vpn.test", "direct.test"}, proof
    assert endpoint_delta["udp"] > 0
    assert endpoint_reply_delta["udp"] is not None and endpoint_reply_delta["udp"] > 0, proof
    assert endpoint_reply_delta["vpn_name"] is not None and endpoint_reply_delta["vpn_name"] > 0, proof
    assert endpoint_delta["vpn_name"] > 0 and endpoint_delta["direct_name"] > 0 and endpoint_delta["other_name"] > 0
    assert set(endpoint_after.get("peers", [])) == {ROUTER_WAN_IP}, proof
    assert capture_delta["udp_upstream"] > 0, proof
    assert selected_flow_delta == {"vpn_domain_tcp": 1, "vpn_domain_udp": 1,
                                   "direct_domain_tcp": 1, "direct_domain_udp": 1}, proof
    assert all(direct_capture_delta[name] == 0 for name in (
        "service_tcp_router_direct", "service_udp_router_direct", "vpn_alias_tcp_router_direct",
        "vpn_alias_udp_router_direct", "service_tcp_client_direct", "service_udp_client_direct",
        "vpn_alias_tcp_client_direct", "vpn_alias_udp_client_direct")), proof
    assert core_delta["vpn_tcp_classified"] > 0 and core_delta["vpn_udp_classified"] > 0
    assert core_delta["vpn_udp_handoff_packets"] > 0
    assert selective_priority_proof.get("selective_mode_with_vpn_default") is True, selective_priority_proof
    assert selective_priority_proof.get("resolved_flows_fixed") is True, selective_priority_proof
    assert selective_priority_proof.get("flow_delta") == {
        "vpn_domain_tcp": 1, "vpn_domain_udp": 1,
        "direct_domain_tcp": 1, "direct_domain_udp": 1,
    }, selective_priority_proof
    assert selective_priority_proof.get("last_peer_matches") == {
        "vpn_domain_tcp": True, "vpn_domain_udp": True,
        "direct_domain_tcp": True, "direct_domain_udp": True,
    }, selective_priority_proof
    assert selective_priority_proof.get("direct_core_path_delta", 0) > 0, selective_priority_proof
    assert all(selective_priority_proof.get("selective_vpn_mark_delta", {}).get(key, 0) > 0
               for key in ("tcp", "udp")), selective_priority_proof
    selective_packets = selective_priority_proof.get("packet_delta", {})
    assert (selective_packets.get("direct_client_tcp", 0) > 0
            and selective_packets.get("direct_client_udp", 0) > 0
            and selective_packets.get("provider_vless_tcp", 0) > 0
            and all(selective_packets.get(key) == 0 for key in (
                "direct_client_to_vpn_tcp", "direct_client_to_vpn_udp",
                "direct_router_to_vpn_tcp", "direct_router_to_vpn_udp",
            ))), selective_priority_proof
    assert selective_priority_proof.get("native_selector_stable") is True, selective_priority_proof
    assert selective_priority_proof.get("provider_selection_stable") is True, selective_priority_proof
    assert selective_priority_proof.get("provider_api_calls_unchanged") is True, selective_priority_proof
    assert selective_priority_proof.get("state_restored") is True, selective_priority_proof
    assert guard_delta["router_dns_vpn_direct_output"]["packets"] == 0
    assert guard_delta["router_dns_vpn_direct_forward"]["packets"] == 0
    assert all(endpoint_peer_proof.values()), proof
    assert all(guard_delta[name]["packets"] > 0 for name in (
        "router_dns_udp_input", "router_dns_tcp_input", "router_dns_udp_output")), proof
    assert proof["native_vpn_stable"] is True and proof["selection_stable"] is True, proof
    assert cleanup_code == 200 and cleanup.get("stopped") is True and cleanup.get("process_reaped") is True
    assert cleanup.get("owned_paths_cleaned") is True and cleanup.get("rescue_interface_cleaned") is True, proof
    return proof


def _mihomo_packet_path_proof(stack: dict[str, Any]) -> dict[str, Any]:
    """Return bounded semantic Mihomo facts without exposing config or proxy identifiers."""
    try:
        config_path = stack["state"] / "generated" / "mihomo" / "config.yaml"
        info = config_path.lstat()
        if (not stat.S_ISREG(info.st_mode) or config_path.is_symlink() or info.st_size > 262144):
            return {"available": False, "reason": "active_config_bounds"}
        config = yaml.safe_load(config_path.read_bytes())
        if not isinstance(config, dict):
            return {"available": False, "reason": "active_config_shape"}
        listeners = config.get("listeners") if isinstance(config.get("listeners"), list) else []
        full_udp = [row for row in listeners if isinstance(row, dict)
                    and row.get("name") == "fwrouter-full-tproxy"
                    and row.get("type") == "tproxy" and row.get("port") == 5205
                    and row.get("rule") == "fwrouter-full-vpn" and row.get("udp") is True]
        proxies = config.get("proxies") if isinstance(config.get("proxies"), list) else []
        vless = [row for row in proxies if isinstance(row, dict) and row.get("type") == "vless"]
        launch = assert_mihomo_launch_matches_active(stack["native"], config_path)
        native_config_sha256 = launch.get("sha256") if isinstance(launch, dict) else None
        incarnation_sha256 = None
        try:
            incarnation = stack["native"].rpc("mihomo_incarnation", {})
            raw_incarnation = incarnation.get("details", {}).get("stdout") if isinstance(incarnation, dict) else None
            if isinstance(raw_incarnation, str) and raw_incarnation.strip():
                incarnation_sha256 = hashlib.sha256(raw_incarnation.strip().encode("utf-8")).hexdigest()
        except Exception:
            pass
        reconcile_state = {
            "receipt_present": False,
            "shape_valid": False,
            "active_hash_matches": False,
            "runtime_incarnation_matches": False,
            "version_matches": False,
            "native_top_level_rules_match": False,
            "native_selector_membership_match": False,
            "subrule_config_guard_match": False,
            "error_code": "missing",
        }
        receipt_path = config_path.parent / "reconcile-state.json"
        try:
            receipt_info = receipt_path.lstat()
            if (receipt_path.is_symlink() or not stat.S_ISREG(receipt_info.st_mode)
                    or receipt_info.st_size > 262144):
                reconcile_state["error_code"] = "bounds"
            else:
                reconcile_state["receipt_present"] = True
                with receipt_path.open("rb") as handle:
                    receipt_bytes = handle.read(262145)
                if len(receipt_bytes) > 262144:
                    reconcile_state["error_code"] = "bounds"
                else:
                    receipt = json.loads(receipt_bytes)
                    if not isinstance(receipt, dict):
                        reconcile_state["error_code"] = "shape"
                    else:
                        active_hash = receipt.get("active_config_hash")
                        runtime_incarnation = receipt.get("runtime_incarnation")
                        fingerprint_version = receipt.get("version")
                        udp_policy_version = receipt.get("udp_policy_version")
                        native_flags = (
                            receipt.get("native_top_level_rules_match"),
                            receipt.get("native_selector_membership_match"),
                            receipt.get("subrule_config_guard_match"),
                        )
                        hash_shape = (isinstance(active_hash, str) and len(active_hash) == 64
                                      and all(ch in "0123456789abcdef" for ch in active_hash))
                        runtime_shape = (isinstance(runtime_incarnation, str)
                                         and 0 < len(runtime_incarnation.strip())
                                         and len(runtime_incarnation) <= 1024)
                        version_shape = (type(fingerprint_version) is int
                                         and type(udp_policy_version) is int)
                        flags_shape = all(type(flag) is bool for flag in native_flags)
                        reconcile_state.update({
                            "shape_valid": hash_shape and runtime_shape and version_shape and flags_shape,
                            "active_hash_matches": hash_shape and active_hash == native_config_sha256,
                            "runtime_incarnation_matches": (
                                runtime_shape and hashlib.sha256(runtime_incarnation.strip().encode("utf-8")).hexdigest()
                                == incarnation_sha256
                            ),
                            "version_matches": (version_shape and fingerprint_version == 2
                                                and udp_policy_version == 1),
                            "native_top_level_rules_match": native_flags[0] is True,
                            "native_selector_membership_match": native_flags[1] is True,
                            "subrule_config_guard_match": native_flags[2] is True,
                            "error_code": None if hash_shape and runtime_shape and version_shape and flags_shape
                            else "shape",
                        })
        except FileNotFoundError:
            pass
        except (OSError, TypeError):
            reconcile_state["error_code"] = "io_error"
        except (UnicodeError, json.JSONDecodeError):
            reconcile_state["error_code"] = "invalid_json"
        code, response = http_json(f"{stack['api']}/mihomo")
        data = response.get("data") if isinstance(response, dict) else None
        mihomo = data.get("mihomo") if isinstance(data, dict) else None
        details = mihomo.get("details") if isinstance(mihomo, dict) else None
        selectors = details.get("selectors") if isinstance(details, dict) else None
        selectors = selectors if isinstance(selectors, dict) else {}
        udp_policy = details.get("vpn_udp_policy") if isinstance(details, dict) else None
        udp_policy = udp_policy if isinstance(udp_policy, dict) else {}
        transparent = details.get("transparent_runtime") if isinstance(details, dict) else None
        transparent = transparent if isinstance(transparent, dict) else {}
        count = transparent.get("transparent_udp_sessions_count")
        if type(count) is not int or count < 0 or count > 4096:
            count = None
        current_auto = selectors.get("vpn_auto_now")
        auto_targets = selectors.get("vpn_auto_targets")
        global_targets = selectors.get("vpn_global_targets")
        auto_targets = auto_targets if isinstance(auto_targets, list) else []
        global_targets = global_targets if isinstance(global_targets, list) else []
        return {
            "available": code == 200 and isinstance(mihomo, dict),
            "native_loaded_config_matches_active": True,
            "runtime_running": isinstance(mihomo, dict) and mihomo.get("runtime_state") == "running",
            "vpn_global_selects_vpn_auto": selectors.get("vpn_global_now") == "vpn-auto",
            "vpn_auto_selects_non_direct": isinstance(current_auto, str) and current_auto.upper() != "DIRECT",
            "vpn_auto_selected_reject": str(current_auto or "").upper() == "REJECT",
            "vpn_auto_selected_target_kind": (
                str(selectors.get("vpn_auto_selected_target")).upper()
                if str(selectors.get("vpn_auto_selected_target") or "").upper() in {"REJECT", "DIRECT"}
                else "provider" if selectors.get("vpn_auto_selected_target") else None
            ),
            "vpn_auto_reject_only": auto_targets == ["REJECT"],
            "vpn_auto_has_direct_target": any(str(item).upper() == "DIRECT" for item in auto_targets),
            "vpn_global_has_direct_target": any(str(item).upper() == "DIRECT" for item in global_targets),
            "full_vpn_udp_listener_count": len(full_udp),
            "vless_proxy_count": len(vless),
            "vless_udp_enabled_count": sum(row.get("udp") is True for row in vless),
            "transparent_udp_sessions_count": count,
            "transparent_udp_session_materialized": transparent.get("transparent_udp_session_materialized") is True,
            "active_config_sha256": native_config_sha256 if isinstance(native_config_sha256, str) else None,
            "native_incarnation_sha256": incarnation_sha256,
            "vpn_udp_policy_installed": udp_policy.get("policy_installed") is True,
            "vpn_udp_policy_wiring_proven": udp_policy.get("wiring_proven") is True,
            "vpn_udp_policy_receipt_matches": udp_policy.get("reconcile_receipt_matches") is True,
            "vpn_udp_selector_membership_matches": udp_policy.get("selector_membership_matches") is True,
            "vpn_udp_guarded_rules_match": udp_policy.get("guarded_rules_match") is True,
            "vpn_udp_native_rules_match": udp_policy.get("native_top_level_rules_match") is True,
            "reconcile_state_receipt": reconcile_state,
        }
    except Exception as exc:
        return {"available": False, "error_class": type(exc).__name__[:64]}


def _record_phase_diagnostic(record_property: Any, diagnostic: dict[str, Any]) -> None:
    """Persist only the fixed, bounded packet-path observations in JUnit."""
    label = diagnostic.get("phase")
    if label not in {"direct-before-vpn", "vpn", "vpn-supported-udp", "vpn-unknown-udp",
                     "vpn-unsupported-udp", "vpn-endpoint-unavailable", "vpn-empty-pool",
                     "vpn-empty-pool-native", "stale-rescue-lan-cleanup",
                     "provider-recovery-confirmations",
                     "provider-recovery-confirmation-1", "dns53-manual-apply", "dns53-status-read", "dns53-native-daemon",
                     "provider-reentry",
                     "vpn-tcp-after-unknown", "vpn-tcp-after-unsupported", "vpn-empty-pool-tcp",
                     "protected-direct-under-global-vpn",
                     "emergency-direct", "vpn-after-reentry", "vpn-after-selector-switch",
                     "native-dns53-under-global-vpn", "provider-enable-job",
                     "vpn-auto-exclusive-job", "vpn-auto-exclusive-disable-selector-packet",
                     "vpn-auto-exclusive-restore-selector-packet",
                     "vpn-auto-selector-change",
                     "provider-refresh-supported", "provider-refresh-unknown",
                     "provider-refresh-unsupported", "global-mode-direct", "global-mode-vpn",
                     "global-mode-selective", "selective-default-vpn", "selective-default-restore",
                     "native-dns53-selective-direct-priority", "provider-reentry-checkpoint-recovery",
                     "docker-direct-global-vpn", "docker-vpn-global-direct", "docker-vpn-repeat-apply",
                     "docker-vpn-endpoint-unavailable", "docker-direct-restored"}:
        raise AssertionError("packet phase diagnostic has an unknown fixed phase")
    if label == "provider-reentry-checkpoint-recovery":
        expected_fields = {
            "phase", "checkpoint_recovered_before_reentry_apply", "inventory_verified",
            "inventory_digest_preserved", "identity_digest_preserved", "routing_mode", "live_global_mode",
        }
        if set(diagnostic) != expected_fields:
            raise AssertionError("provider re-entry checkpoint diagnostic has an unknown field")
        if any(type(diagnostic.get(field)) is not bool for field in (
            "checkpoint_recovered_before_reentry_apply", "inventory_verified",
            "inventory_digest_preserved", "identity_digest_preserved",
        )):
            raise AssertionError("provider re-entry checkpoint diagnostic has a non-boolean proof field")
        valid_modes = {"vpn", "direct", "selective"}
        if (diagnostic.get("routing_mode") not in valid_modes
                or diagnostic.get("live_global_mode") not in valid_modes):
            raise AssertionError("provider re-entry checkpoint diagnostic has an unknown routing mode")
    encoded = json.dumps(diagnostic, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    if len(encoded.encode("ascii")) > MAX_PHASE_DIAGNOSTIC_BYTES:
        raise AssertionError("packet phase diagnostic exceeded its fixed byte bound")
    record_property("packet_phase_" + label.replace("-", "_"), encoded)


def _bounded_job_summary(job: Any) -> dict[str, Any]:
    """Keep failure attribution useful while excluding payloads, IDs, and messages."""
    if not isinstance(job, dict):
        return {"shape": type(job).__name__[:48]}
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    apply = result.get("apply") if isinstance(result.get("apply"), dict) else {}
    verification = apply.get("verification") if isinstance(apply.get("verification"), dict) else {}
    target_readback = result.get("target_readback")
    if not isinstance(target_readback, dict):
        target_readback = verification.get("target_readback")
    target_readback = target_readback if isinstance(target_readback, dict) else {}
    def short(value: Any) -> str | None:
        return value[:96] if isinstance(value, str) and value and all(ch.isalnum() or ch in "_.:-" for ch in value) else None
    def boolean(value: Any) -> bool | None:
        return value if type(value) is bool else None
    return {
        "status": short(job.get("status")),
        "job_type": short(job.get("job_type")),
        "error_code": short(job.get("error_code") or result.get("error_code")),
        "result_status": short(result.get("job_status")),
        "stage": short(result.get("stage")),
        "runtime_verified": boolean(result.get("runtime_verified")),
        "intent_saved": boolean(result.get("intent_saved")),
        "last_good_retained": boolean(result.get("last_good_retained")),
        "apply_reconcile_action": short(apply.get("reconcile_action")),
        "apply_reconcile_reason": short(apply.get("reconcile_reason")),
        "apply_verification_error": short(verification.get("error_code")),
        "target_readback_matches": boolean(target_readback.get("target_set_matches")),
        "target_expected_count": target_readback.get("expected_target_count")
        if type(target_readback.get("expected_target_count")) is int else None,
        "target_observed_count": target_readback.get("observed_target_count")
        if type(target_readback.get("observed_target_count")) is int else None,
        "mihomo_reconcile": _bounded_reconcile_result(result),
    }


def _bounded_provider_refresh_summary(job: Any) -> dict[str, Any]:
    """Keep only fixed provider-refresh outcome fields; omit payloads/messages."""
    if not isinstance(job, dict):
        return {"shape": type(job).__name__[:48]}
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    error = result.get("error") if isinstance(result.get("error"), dict) else {}
    refresh = result.get("refresh") if isinstance(result.get("refresh"), dict) else {}
    source_outcomes = result.get("source_outcomes") if isinstance(result.get("source_outcomes"), list) else []

    def code(value: Any) -> str | None:
        return value[:64] if isinstance(value, str) and value and all(
            ch.isalnum() or ch in "_.:-" for ch in value
        ) else None

    return {
        "status": code(job.get("status")),
        "job_type": code(job.get("job_type")),
        "error_code": code(job.get("error_code") or result.get("error_code")),
        "reason_code": code(error.get("code")),
        "result_status": code(result.get("job_status")),
        "stage": code(result.get("stage")),
        "refresh_stage": code(refresh.get("stage")),
        "refresh_ok": refresh.get("ok") if type(refresh.get("ok")) is bool else None,
        "runtime_verified": result.get("runtime_verified") if type(result.get("runtime_verified")) is bool else None,
        "intent_saved": result.get("intent_saved") if type(result.get("intent_saved")) is bool else None,
        "applied": result.get("applied") if type(result.get("applied")) is bool else None,
        "promoted": result.get("promoted") if type(result.get("promoted")) is bool else None,
        "container_restarted": result.get("container_restarted")
        if type(result.get("container_restarted")) is bool else None,
        "last_good_retained": result.get("last_good_retained")
        if type(result.get("last_good_retained")) is bool else None,
        "source_outcome_statuses": [code(item.get("outcome")) for item in source_outcomes[:3]
                                    if isinstance(item, dict)],
    }


def _bounded_reconcile_result(result: dict[str, Any]) -> dict[str, Any]:
    details = result.get("details") if isinstance(result.get("details"), dict) else {}
    reconcile = details.get("mihomo_reconcile") if isinstance(details.get("mihomo_reconcile"), dict) else {}
    native = reconcile.get("native_udp_policy_proof")
    native = native if isinstance(native, dict) else {}
    rollback = reconcile.get("rollback")
    rollback = rollback if isinstance(rollback, dict) else {}
    safe_native_keys = (
        "ok", "native_top_level_rules_match", "selector_membership_matches",
        "guarded_rules_match", "wiring_proven",
    )
    def short(value: Any) -> str | None:
        return value[:96] if isinstance(value, str) and value and all(ch.isalnum() or ch in "_.:-" for ch in value) else None
    def boolean(value: Any) -> bool | None:
        return value if type(value) is bool else None
    return {
        "ok": boolean(reconcile.get("ok")),
        "stage": short(reconcile.get("stage")),
        "error_code": short(reconcile.get("error_code")),
        "reconcile_reason": short(reconcile.get("reconcile_reason") or reconcile.get("reason")),
        "reconcile_action": short(reconcile.get("reconcile_action")),
        "native_udp_policy_proof": {key: boolean(native.get(key)) for key in safe_native_keys
                                     if type(native.get(key)) is bool},
        "rollback_ok": boolean(rollback.get("ok")),
        "rollback_recovered": boolean(rollback.get("recovered")),
        "rollback_reason": short(rollback.get("reason")),
    }


def _record_job_stage(record_property: Any, phase: str, *, http_status: int,
                      accepted: Any, job: Any = None, error_class: str | None = None,
                      stack: dict[str, Any]) -> None:
    revision = None
    try:
        from fwrouter_api.services.vpn_auto_selection_state import read_selection_fence
        from fwrouter_api.db.connection import db_session

        with db_session() as connection:
            fence = read_selection_fence(connection)
        revision = fence.get("revision") if isinstance(fence, dict) else None
    except Exception:
        pass
    diagnostic = {
        "phase": phase,
        "http_status": http_status if type(http_status) is int else None,
        "accepted": (accepted.get("data", {}).get("accepted") is True
                     if isinstance(accepted, dict) and isinstance(accepted.get("data"), dict) else False),
        "job": _bounded_job_summary(job),
        "await_error_class": error_class[:64] if isinstance(error_class, str) else None,
        "selection_revision": revision if type(revision) is int else None,
        "mihomo_path": _mihomo_packet_path_proof(stack),
    }
    _record_phase_diagnostic(record_property, diagnostic)


def _bounded_reconcile_summary(response: Any) -> dict[str, Any]:
    """Extract only allowlisted reconciliation state from a global-mode API error."""
    if not isinstance(response, dict):
        return {"shape": type(response).__name__[:48]}
    data = response.get("data") if isinstance(response.get("data"), dict) else {}
    error = data.get("error") if isinstance(data.get("error"), dict) else {}
    details = error.get("details") if isinstance(error.get("details"), dict) else {}
    mutation = data.get("routing_mutation") if isinstance(data.get("routing_mutation"), dict) else {}
    job = data.get("job") if isinstance(data.get("job"), dict) else {}
    job_result = job.get("result") if isinstance(job.get("result"), dict) else {}
    job_details = job_result.get("details") if isinstance(job_result.get("details"), dict) else {}
    reconcile = next((value.get("mihomo_reconcile") for value in
                      (details, data, mutation, job_result, job_details)
                      if isinstance(value.get("mihomo_reconcile"), dict)), {})
    native = reconcile.get("native_udp_policy_proof")
    native = native if isinstance(native, dict) else {}
    rollback = reconcile.get("rollback")
    rollback = rollback if isinstance(rollback, dict) else {}
    def short(value: Any) -> str | None:
        return value[:96] if isinstance(value, str) and value and all(ch.isalnum() or ch in "_.:-" for ch in value) else None
    def boolean(value: Any) -> bool | None:
        return value if type(value) is bool else None
    safe_native_keys = (
        "ok", "native_top_level_rules_match", "selector_membership_matches",
        "guarded_rules_match", "wiring_proven",
    )
    safe_fence_keys = (
        "selection_fenced", "selection_revision_matches", "runtime_incarnation_matches",
        "receipt_matches", "publication_still_owned",
    )
    fence = reconcile.get("fence") if isinstance(reconcile.get("fence"), dict) else {}
    return {
        "api_error_code": short(error.get("code") or data.get("code")),
        "api_error_stage": short(error.get("stage") or data.get("stage")),
        "reconcile_ok": boolean(reconcile.get("ok")),
        "reconcile_stage": short(reconcile.get("stage")),
        "reconcile_error_code": short(reconcile.get("error_code")),
        "reconcile_reason": short(reconcile.get("reconcile_reason") or reconcile.get("reason")),
        "reconcile_action": short(reconcile.get("reconcile_action")),
        "native_udp_policy_proof": {key: boolean(native.get(key)) for key in safe_native_keys
                                     if type(native.get(key)) is bool},
        "fence_proof": {key: boolean(fence.get(key)) for key in safe_fence_keys
                         if type(fence.get(key)) is bool},
        "rollback_ok": boolean(rollback.get("ok")),
        "rollback_recovered": boolean(rollback.get("recovered")),
        "rollback_reason": short(rollback.get("reason")),
        "rollback_error_code": short(rollback.get("error_code")),
    }


def _probe_phase(stack: dict[str, Any], expected_peer: str, label: str,
                 core_before: dict[str, int], record_property: Any, source_ref: str) -> dict[str, Any]:
    selection_before = _provider_selection_snapshot(source_ref)
    before = _endpoint_observations()
    tcp = _role_json(f"{CLIENT_CONTROL}/probe/tcp", method="POST")
    udp = _role_json(f"{CLIENT_CONTROL}/probe/udp", method="POST")
    dns = _role_json(f"{CLIENT_CONTROL}/probe/dns", method="POST")
    negative = _role_json(f"{CLIENT_CONTROL}/probe/dns-negative", method="POST")
    leak = _role_json(f"{CLIENT_CONTROL}/probe/leak", method="POST")
    after = _endpoint_observations()
    selection_after = _provider_selection_snapshot(source_ref)
    try:
        core_counters = _core_counter_delta(core_before, _core_counter_snapshot())
    except Exception as exc:
        core_counters = {"available": False, "error_class": type(exc).__name__[:64]}
    diagnostic = {
        "phase": label,
        "endpoint_observation_delta": {
            kind: {"count": after["counts"].get(kind, -1) - before["counts"].get(kind, 0),
                  "last_peer": after["last_peers"].get(kind)}
            for kind in ("http", "udp", "dns")
        },
        "core_counter_delta": core_counters,
        "mihomo_path": _mihomo_packet_path_proof(stack),
        "provider_selection_before": selection_before,
        "provider_selection_after": selection_after,
    }
    _record_phase_diagnostic(record_property, diagnostic)
    _assert_packet_selection_stable(selection_before, selection_after)
    assert tcp.get("ok") is True and tcp.get("peer") == expected_peer, {**diagnostic, "service": "tcp"}
    assert udp.get("ok") is True and udp.get("peer") == SERVICE_VIP, {**diagnostic, "service": "udp"}
    assert dns.get("ok") is True and dns.get("peer") == SERVICE_VIP and dns.get("answer") == SERVICE_VIP, {
        **diagnostic, "service": "dns"
    }
    assert negative.get("ok") is True and negative.get("rcode") == "NXDOMAIN", {
        **diagnostic, "service": "dns-negative"
    }
    assert leak.get("ok") is True and leak.get("blocked") is True, {**diagnostic, "service": "egress-guard"}
    for kind in ("http", "udp", "dns"):
        assert after["counts"].get(kind, -1) == before["counts"].get(kind, 0) + 1, {
            **diagnostic, "service": kind,
            "counts": {"before": before["counts"], "after": after["counts"]},
        }
        assert after["last_peers"].get(kind) == expected_peer, {
            **diagnostic, "service": kind, "expected_peer": expected_peer,
        }
    assert leak.get("stub_drop_packets", 0) > 0 and leak.get("reserved_drop_packets", 0) > 0, {
        "phase": label, "service": "egress-guard"
    }
    router_guard = _probe_router_egress_guards()
    return {"observations": after, "endpoint_observation_delta": diagnostic["endpoint_observation_delta"],
            "core_counters": core_counters,
            "mihomo_path": diagnostic["mihomo_path"], "leak": {
        "stub_drop_packets": leak["stub_drop_packets"],
        "reserved_drop_packets": leak["reserved_drop_packets"],
    }, "router_guard": router_guard}


def _wait_direct_udp_capture_quiet(baseline: dict[tuple[str, str, str, int], int],
                                   *, timeout_seconds: float = NEGATIVE_CAPTURE_DRAIN_SECONDS) -> dict[tuple[str, str, str, int], int]:
    """Drain owned capture writes for the failed probe without a sleep or an unbounded wait."""
    libc = ctypes.CDLL(None, use_errno=True)
    fd = libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
    if fd < 0:
        raise AssertionError("bounded negative capture observer could not initialize")
    selector = selectors.DefaultSelector()
    try:
        if libc.inotify_add_watch(fd, os.fsencode(UDP_CAPTURE_PATH), 0x00000002) < 0:
            raise AssertionError("bounded negative capture observer could not watch its owned UDP capture")
        selector.register(fd, selectors.EVENT_READ)
        deadline = time.monotonic() + timeout_seconds
        while True:
            current = _packet_counts()
            direct = sum(_flow_delta(baseline, current, flow) for flow in (
                (CLIENT_IP, SERVICE_VIP, "udp", 9081),
                (ROUTER_WAN_IP, SERVICE_VIP, "udp", 9081),
            ))
            if direct > 0:
                return current
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                return _packet_counts()
            while True:
                try:
                    events = os.read(fd, 4096)
                except BlockingIOError:
                    break
                if not events:
                    break
                offset = 0
                while offset + 16 <= len(events):
                    _watch, mask, _cookie, name_length = struct.unpack_from("iIII", events, offset)
                    if mask & 0x00004000:
                        raise AssertionError("negative capture observer queue overflowed")
                    offset += 16 + name_length
    finally:
        selector.close()
        os.close(fd)


def _wait_direct_tcp_capture_quiet(baseline: dict[tuple[str, str, str, int], int],
                                   *, timeout_seconds: float = NEGATIVE_CAPTURE_DRAIN_SECONDS) -> dict[tuple[str, str, str, int], int]:
    """Boundedly observe TCP capture writes while rejecting direct VIP connections."""
    libc = ctypes.CDLL(None, use_errno=True)
    fd = libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
    if fd < 0:
        raise AssertionError("bounded negative TCP capture observer could not initialize")
    selector = selectors.DefaultSelector()
    try:
        if libc.inotify_add_watch(fd, os.fsencode(TCP_CAPTURE_PATH), 0x00000002) < 0:
            raise AssertionError("bounded negative TCP capture observer could not watch its owned capture")
        selector.register(fd, selectors.EVENT_READ)
        deadline = time.monotonic() + timeout_seconds
        while True:
            current = _packet_counts()
            direct = sum(_flow_delta(baseline, current, flow) for flow in (
                (CLIENT_IP, SERVICE_VIP, "tcp", 9080),
                (ROUTER_WAN_IP, SERVICE_VIP, "tcp", 9080),
            ))
            if direct > 0:
                return current
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                return _packet_counts()
            while True:
                try:
                    events = os.read(fd, 4096)
                except BlockingIOError:
                    break
                if not events:
                    break
                offset = 0
                while offset + 16 <= len(events):
                    _watch, mask, _cookie, name_length = struct.unpack_from("iIII", events, offset)
                    if mask & 0x00004000:
                        raise AssertionError("negative TCP capture observer queue overflowed")
                    offset += 16 + name_length
    finally:
        selector.close()
        os.close(fd)


def _probe_vpn_udp_contract_phase(stack: dict[str, Any], source_ref: str, logical_id: str,
                                  capability: str, expected_udp: bool, label: str,
                                  record_property: Any, *, expect_success: bool,
                                  endpoint_unavailable: bool = False,
                                  expect_vpn_handoff: bool | None = None,
                                  expect_active_server: bool = True,
                                  secondary_selector_candidate: bool = False) -> dict[str, Any]:
    _assert_packet_runtime(stack, mode="vpn", policy_required=True)
    capability_state = _packet_capability_state(stack, source_ref, logical_id, capability, expected_udp,
                                                 expect_active_server=expect_active_server,
                                                 secondary_selector_candidate=secondary_selector_candidate)
    selection_before = _provider_selection_snapshot(source_ref)
    observations_before = _endpoint_observations()
    capture_before = _packet_counts()
    core_before = _core_counter_snapshot()
    if endpoint_unavailable:
        endpoint_state = _role_json(f"{ENDPOINT_CONTROL}/ready")
        assert endpoint_state.get("sockets", {}).get("xray_tcp_5301") is False, endpoint_state
        attempt = _udp_negative_control()
        capture_after = _wait_direct_udp_capture_quiet(capture_before)
    elif expect_success:
        attempt = _role_json(f"{CLIENT_CONTROL}/probe/udp", method="POST")
        capture_after = _wait_packet_counts(lambda counts:
            _flow_delta(capture_before, counts, (ROUTER_WAN_IP, ENDPOINT_IP, "tcp", 5301)) > 0)
    else:
        attempt = _udp_negative_control()
        capture_after = _wait_direct_udp_capture_quiet(capture_before)
    observations_after = _endpoint_observations()
    selection_after = _provider_selection_snapshot(source_ref)
    core_delta = _core_counter_delta(core_before, _core_counter_snapshot())
    direct_flow_deltas = {
        "client_source_to_service": _flow_delta(capture_before, capture_after,
                                                 (CLIENT_IP, SERVICE_VIP, "udp", 9081)),
        "mihomo_direct_to_service": _flow_delta(capture_before, capture_after,
                                                 (ROUTER_WAN_IP, SERVICE_VIP, "udp", 9081)),
    }
    endpoint_udp_delta = observations_after["counts"].get("udp", -1) - observations_before["counts"].get("udp", 0)
    diagnostic = {
        "phase": label,
        "capability": capability_state,
        "attempt": attempt,
        "expected_success": expect_success,
        "endpoint_unavailable": endpoint_unavailable,
        "endpoint_udp_observation_delta": endpoint_udp_delta,
        "core_counter_delta": core_delta,
        "direct_udp_capture_delta": direct_flow_deltas,
        "provider_selection_before": selection_before,
        "provider_selection_after": selection_after,
        "endpoint_xray_running": endpoint_state.get("xray", {}).get("running") if endpoint_unavailable else True,
    }
    _record_phase_diagnostic(record_property, diagnostic)
    _assert_packet_selection_stable(selection_before, selection_after)
    if expect_vpn_handoff is None:
        # Unsupported and empty-pool packets still enter Mihomo through TPROXY;
        # its generated UDP REJECT policy must stop them there without DIRECT.
        expect_vpn_handoff = True
    assert core_delta["vpn_udp_classified"] > 0, diagnostic
    assert (core_delta["vpn_udp_handoff_packets"] > 0) is expect_vpn_handoff, diagnostic
    # This counter is the shared fwrouter_direct terminal-chain total; host-output
    # and host-local control traffic also reach it. Leak proof is tuple-scoped below.
    assert direct_flow_deltas == {"client_source_to_service": 0, "mihomo_direct_to_service": 0}, diagnostic
    if expect_success:
        assert attempt.get("ok") is True and attempt.get("peer") == SERVICE_VIP, diagnostic
        assert endpoint_udp_delta == 1 and observations_after["last_peers"].get("udp") == SERVICE_VIP, diagnostic
        assert _flow_delta(capture_before, capture_after,
                           (ROUTER_WAN_IP, ENDPOINT_IP, "tcp", 5301)) > 0, diagnostic
    else:
        assert attempt.get("control_status") == 502 and attempt.get("failure_class") in {
            "TimeoutError", "ConnectionRefusedError", "OSError",
        }, diagnostic
        assert endpoint_udp_delta == 0, diagnostic
        assert observations_after["counts"].get("udp", -1) == observations_before["counts"].get("udp", 0), diagnostic
    return diagnostic


def _probe_vpn_tcp_regression(stack: dict[str, Any], label: str,
                              record_property: Any) -> dict[str, Any]:
    """Prove TCP still traverses the selected VPN proxy after UDP capability changes."""
    before = _packet_counts()
    core_before = _core_counter_snapshot()
    endpoint_before = _endpoint_observations()
    result = _role_json(f"{CLIENT_CONTROL}/probe/tcp", method="POST")
    after = _wait_packet_counts(lambda counts:
        _flow_delta(before, counts, (ROUTER_WAN_IP, ENDPOINT_IP, "tcp", 5301)) > 0)
    core_delta = _core_counter_delta(core_before, _core_counter_snapshot())
    endpoint_after = _endpoint_observations()
    direct = {
        "client": _flow_delta(before, after, (CLIENT_IP, SERVICE_VIP, "tcp", 9080)),
        "mihomo": _flow_delta(before, after, (ROUTER_WAN_IP, SERVICE_VIP, "tcp", 9080)),
    }
    diagnostic = {
        "phase": label,
        "result": result,
        "core_counter_delta": core_delta,
        "endpoint_http_delta": endpoint_after["counts"].get("http", -1) - endpoint_before["counts"].get("http", 0),
        "vpn_proxy_capture_delta": _flow_delta(before, after, (ROUTER_WAN_IP, ENDPOINT_IP, "tcp", 5301)),
        "direct_tcp_capture_delta": direct,
        "mihomo_path": _mihomo_packet_path_proof(stack),
    }
    _record_phase_diagnostic(record_property, diagnostic)
    assert result.get("ok") is True and result.get("peer") == SERVICE_VIP, diagnostic
    assert core_delta["vpn_tcp_classified"] > 0, diagnostic
    assert diagnostic["vpn_proxy_capture_delta"] > 0 and diagnostic["endpoint_http_delta"] > 0, diagnostic
    assert direct == {"client": 0, "mihomo": 0}, diagnostic
    return diagnostic


def _probe_vpn_tcp_empty_pool(stack: dict[str, Any], record_property: Any) -> dict[str, Any]:
    """Full-VPN TCP remains classified but has no target or DIRECT escape."""
    _assert_packet_runtime(stack, mode="vpn", policy_required=True)
    before = _packet_counts()
    core_before = _core_counter_snapshot()
    endpoint_before = _endpoint_observations()
    attempt = _tcp_negative_control()
    after = _wait_direct_tcp_capture_quiet(before)
    core_delta = _core_counter_delta(core_before, _core_counter_snapshot())
    endpoint_after = _endpoint_observations()
    deltas = {
        "client_direct": _flow_delta(before, after, (CLIENT_IP, SERVICE_VIP, "tcp", 9080)),
        "router_direct": _flow_delta(before, after, (ROUTER_WAN_IP, SERVICE_VIP, "tcp", 9080)),
        "vpn_proxy": _flow_delta(before, after, (ROUTER_WAN_IP, ENDPOINT_IP, "tcp", 5301)),
    }
    endpoint_http_delta = endpoint_after["counts"].get("http", -1) - endpoint_before["counts"].get("http", 0)
    diagnostic = {"phase": "vpn-empty-pool-tcp", "attempt": attempt,
                  "core_counter_delta": core_delta, "capture_delta": deltas,
                  "endpoint_http_observation_delta": endpoint_http_delta}
    _record_phase_diagnostic(record_property, diagnostic)
    assert core_delta["vpn_tcp_classified"] > 0, diagnostic
    # The shared fwrouter_direct counter includes host-output control traffic;
    # exact client/router destination tuples below are the direct-leak assertion.
    assert deltas == {"client_direct": 0, "router_direct": 0, "vpn_proxy": 0}, diagnostic
    assert endpoint_http_delta == 0, diagnostic
    return diagnostic


def _set_health_available(available: bool) -> dict[str, Any]:
    result = _role_json(f"{ENDPOINT_CONTROL}/health", method="POST", payload={"available": available})
    assert result.get("available") is available, {"health_control": "unexpected response"}
    return result


def _apply_global_mode(api: str, mode: str, *, stack: dict[str, Any], record_property: Any) -> dict[str, Any]:
    phase = f"global-mode-{mode}"
    code, accepted = http_json(f"{api}/routing/global", method="POST",
                               payload={"mode": mode, "requested_by": "hosted-packet-acceptance", "run_now": True})
    job = None
    await_error_class = None
    if code == 200 and isinstance(accepted, dict) and accepted.get("ok") is True:
        try:
            job = await_core_job(api, accepted)
        except Exception as exc:
            await_error_class = type(exc).__name__
    _record_phase_diagnostic(record_property, {
        "phase": phase,
        "http_status": code if type(code) is int else None,
        "accepted": isinstance(accepted, dict) and accepted.get("ok") is True,
        "api_error": _bounded_reconcile_summary(accepted),
        "job": _bounded_job_summary(job),
        "await_error_class": await_error_class[:64] if isinstance(await_error_class, str) else None,
        "mihomo_path": _mihomo_packet_path_proof(stack),
    })
    assert code == 200 and accepted.get("ok") is True, {"mode": mode, "accepted": accepted.get("ok")}
    if await_error_class is not None:
        raise AssertionError("global mode job polling failed within its bound")
    assert job.get("status") == "success", {"mode": mode, "job_status": job.get("status")}
    code, projection = http_json(f"{api}/routing/global")
    assert code == 200 and projection.get("ok") is True
    routing = projection["data"]["routing"]
    enforcement = projection["data"]["runtime_enforcement"]
    assert routing.get("applied_mode") == mode and enforcement.get("live_global_mode") == mode, {
        "mode": mode, "applied_mode": routing.get("applied_mode"),
        "live_global_mode": enforcement.get("live_global_mode"),
    }
    assert enforcement.get("active_mode_matches_intent") is True
    return projection["data"]


def _apply_selective_default(api: str, selective_default: str, *, stack: dict[str, Any],
                             record_property: Any, phase: str) -> dict[str, Any]:
    if (selective_default not in {"direct", "vpn"}
            or phase not in {"selective-default-vpn", "selective-default-restore"}):
        raise AssertionError("selective-default acceptance request is outside its fixed contract")
    code, accepted = http_json(
        f"{api}/routing/global", method="POST",
        payload={"selective_default": selective_default,
                 "requested_by": "hosted-packet-dns-selective-priority", "run_now": True},
    )
    job = None
    await_error_class = None
    if code == 200 and isinstance(accepted, dict) and accepted.get("ok") is True:
        try:
            job = await_core_job(api, accepted)
        except Exception as exc:
            await_error_class = type(exc).__name__
    diagnostic = {
        "phase": phase,
        "http_status": code if type(code) is int else None,
        "accepted": isinstance(accepted, dict) and accepted.get("ok") is True,
        "api_error": _bounded_reconcile_summary(accepted),
        "job": _bounded_job_summary(job),
        "await_error_class": await_error_class[:64] if isinstance(await_error_class, str) else None,
        "mihomo_path": _mihomo_packet_path_proof(stack),
    }
    _record_phase_diagnostic(record_property, diagnostic)
    assert code == 200 and isinstance(accepted, dict) and accepted.get("ok") is True, diagnostic
    assert await_error_class is None and isinstance(job, dict) and job.get("status") == "success", diagnostic
    read_code, projection = http_json(f"{api}/routing/global")
    data = projection.get("data") if isinstance(projection, dict) else None
    routing = data.get("routing") if isinstance(data, dict) else None
    assert (read_code == 200 and isinstance(routing, dict)
            and routing.get("selective_default") == selective_default), {
                **diagnostic, "readback_status": read_code,
                "selective_default_matches": isinstance(routing, dict)
                and routing.get("selective_default") == selective_default,
            }
    return data


def _set_server_auto_eligibility(api: str, logical_id: str, enabled: bool,
                                 *, priority: int | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "vpn_auto": enabled,
        "reconcile_mihomo": True,
        "requested_by": "hosted-packet-acceptance-empty-pool",
    }
    if priority is not None:
        payload["vpn_auto_priority"] = priority
    code, response = http_json(
        f"{api}/servers/{logical_id}/preferences", method="PATCH",
        payload=payload,
    )
    data = response.get("data") if isinstance(response, dict) else None
    result = data.get("server_preferences") if isinstance(data, dict) else None
    assert code == 200 and response.get("ok") is True and isinstance(result, dict), {
        "auto_eligibility_status": code,
        "auto_eligibility_error": response.get("error") if isinstance(response, dict) else "invalid_response",
    }
    preferences = (result.get("server") or {}).get("preferences") or {}
    assert preferences.get("vpn_auto") is enabled, {
        "expected_vpn_auto": enabled, "observed_vpn_auto": preferences.get("vpn_auto"),
    }
    reconcile = result.get("mihomo_reconcile")
    assert not isinstance(reconcile, dict) or reconcile.get("ok") is True, {
        "mihomo_reconcile": reconcile,
    }
    return result


def _server_user_preferences(api: str, logical_id: str) -> dict[str, Any]:
    code, response = http_json(f"{api}/servers/{logical_id}")
    server = response.get("data", {}).get("server") if isinstance(response, dict) else None
    preferences = server.get("preferences") if isinstance(server, dict) else None
    assert code == 200 and response.get("ok") is True and isinstance(preferences, dict), {
        "server_preferences_readback": code,
    }
    keys = ("vpn_auto", "vpn_auto_priority", "vpn_auto_priority_origin", "global_list",
            "remembered_until", "manually_deleted_at")
    return {key: preferences.get(key) for key in keys}


def _recovery_health_snapshot(source_ref: str, logical_id: str) -> dict[str, Any]:
    state = _packet_refresh_state(source_ref, logical_id)
    canonical = state.get("canonical_member_health") if isinstance(state, dict) else None
    canonical = canonical if isinstance(canonical, dict) else {}
    members = canonical.get("members") if isinstance(canonical.get("members"), list) else []
    member = members[0] if members and isinstance(members[0], dict) else {}
    ping = state.get("candidate_ping") if isinstance(state.get("candidate_ping"), dict) else {}
    return {
        "runtime_state": state.get("runtime_state")
        if state.get("runtime_state") in {"running", "failed", "unknown"} else "other",
        "problem_code": state.get("problem_code"),
        "active_auto_target_valid": state.get("active_auto_target_valid") is True,
        "active_auto_server_valid": state.get("active_auto_server_valid") is True,
        "candidate_ping_status": ping.get("status")
        if ping.get("status") in {"success", "failed", "unknown", "stale"} else "other",
        "canonical_health_status": canonical.get("health_status")
        if canonical.get("health_status") in {"usable", "unavailable", "unknown"} else "other",
        "usable_members": canonical.get("usable_members")
        if type(canonical.get("usable_members")) is int else None,
        "active_member": {
            "status": member.get("status")
            if member.get("status") in {"healthy", "failed", "stale", "unsupported", "unknown"} else "other",
            "fresh": member.get("fresh") if type(member.get("fresh")) is bool else None,
            "stale": member.get("stale") if type(member.get("stale")) is bool else None,
            "probe_lane": member.get("probe_lane"),
            "error_code": member.get("error_code"),
        },
    }


def _recovery_decision_summary(value: Any) -> dict[str, Any]:
    response = value if isinstance(value, dict) else {}
    allowed_statuses = {
        "provider_recovery_pending", "provider_auto_switch_disabled", "provider_vpn_recovered",
        "emergency_direct", "provider_recovery_deferred", "provider_recovery_stale",
    }
    allowed_actions = {"none", "provider_recovery", "observe_recovery", "verified_vpn_reentry", "emergency_direct"}
    error_code = response.get("error_code")
    if not isinstance(error_code, str) or len(error_code) > 64 \
            or not all(character.isalnum() or character in "_.:-" for character in error_code):
        error_code = None
    override = response.get("effective_override")
    return {
        "ok": response.get("ok") is True,
        "provider_recovery": response.get("provider_recovery") is True,
        "provider_confirmation": response.get("provider_confirmation")
        if type(response.get("provider_confirmation")) is int else None,
        "status": response.get("status") if response.get("status") in allowed_statuses else "other",
        "action": response.get("action") if response.get("action") in allowed_actions else "other",
        "error_code": error_code,
        "effective_override": override if override in {None, "emergency_direct"} else "other",
    }


def _provider_reentry_summary(value: Any) -> dict[str, Any]:
    response = value if isinstance(value, dict) else {}
    statuses = {"provider_vpn_recovered", "emergency_direct", "emergency_direct_unconfirmed",
                "provider_recovery_stale", "provider_recovery_deferred", "provider_reentry_in_progress",
                "provider_switch_outcome_unconfirmed", "provider_reentry_suppressed_by_exclusive_source"}
    actions = {"none", "verified_vpn_reentry", "emergency_direct"}

    def safe_code(field: str) -> str | None:
        code = response.get(field)
        if (not isinstance(code, str) or len(code) > 64
                or not all(character.isalnum() or character in "_.:-" for character in code)):
            return None
        return code

    override = response.get("effective_override")
    outcome = response.get("outcome")
    status = response.get("status")
    action = response.get("action")
    return {
        "ok": response.get("ok") is True,
        "status": status if isinstance(status, str) and status in statuses else "other",
        "action": action if isinstance(action, str) and action in actions else "other",
        "outcome": outcome if isinstance(outcome, str)
        and outcome in {"success", "failed", "deferred", "unconfirmed"} else "other",
        "error_code": safe_code("error_code"),
        "evidence_code": safe_code("evidence_code"),
        "effective_override": override if override is None or override == "emergency_direct" else "other",
        "fallback_verified": response.get("fallback_verified") is True,
        "local_connectivity_restored": response.get("local_connectivity_restored") is True,
        "last_good_retained": response.get("last_good_retained") is True,
        "pending_preserved": response.get("pending_preserved") is True,
    }


def _provider_reentry_routing_summary(http_status: Any, response: Any) -> dict[str, Any]:
    payload = response if isinstance(response, dict) else {}
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    routing = data.get("routing") if isinstance(data.get("routing"), dict) else {}
    enforcement = data.get("runtime_enforcement") if isinstance(data.get("runtime_enforcement"), dict) else {}
    modes = {"vpn", "direct"}
    levels = {"global_vpn_enforced", "global_direct_only", "selective_vpn_enforced", "degraded", "unknown"}
    desired = routing.get("desired_mode")
    applied = routing.get("applied_mode")
    live = enforcement.get("live_global_mode")
    level = enforcement.get("enforcement_level")
    return {
        "http_status": http_status if type(http_status) is int else None,
        "api_ok": payload.get("ok") is True,
        "desired_mode": desired if isinstance(desired, str) and desired in modes else "other",
        "applied_mode": applied if isinstance(applied, str) and applied in modes else "other",
        "live_global_mode": live if isinstance(live, str) and live in modes else "other",
        "enforcement_level": level if isinstance(level, str) and level in levels else "other",
        "active_mode_matches_intent": enforcement.get("active_mode_matches_intent")
        if type(enforcement.get("active_mode_matches_intent")) is bool else None,
        "vpn_egress_ready": enforcement.get("vpn_egress_ready")
        if type(enforcement.get("vpn_egress_ready")) is bool else None,
        "traffic_enforcement_guaranteed": enforcement.get("traffic_enforcement_guaranteed")
        if type(enforcement.get("traffic_enforcement_guaranteed")) is bool else None,
    }


def _assert_native_empty_vpn_pool(stack: dict[str, Any], *, expected_fence: dict[str, Any],
                                  preference_change: dict[str, Any], record_property: Any) -> dict[str, Any]:
    fence_before_readback = _read_packet_selection_fence()
    selector_state = _read_packet_selector_state()
    _assert_packet_runtime(stack, mode="vpn", policy_required=True)
    config_path = stack["state"] / "generated" / "mihomo" / "config.yaml"
    config = yaml.safe_load(config_path.read_bytes())
    groups = config.get("proxy-groups") if isinstance(config, dict) else None
    auto = next((row for row in groups if isinstance(row, dict) and row.get("name") == "vpn-auto"), None) \
        if isinstance(groups, list) else None
    global_group = next((row for row in groups if isinstance(row, dict) and row.get("name") == "vpn-global"), None) \
        if isinstance(groups, list) else None
    assert isinstance(auto, dict) and auto.get("proxies") == ["REJECT"], {
        "vpn_auto_group": auto,
    }
    assert isinstance(global_group, dict) and "DIRECT" not in global_group.get("proxies", []), {
        "vpn_global_group": global_group,
    }
    code, response = http_json(f"{stack['api']}/mihomo")
    assert code == 200 and response.get("ok") is True
    details = response["data"]["mihomo"].get("details") or {}
    selectors = details.get("selectors") or {}
    assert (selectors.get("vpn_auto_now") is None
            and selectors.get("vpn_auto_selected_target") == "REJECT"
            and selectors.get("vpn_auto_targets") == ["REJECT"]), {
        "native_vpn_auto_selection": selectors.get("vpn_auto_now"),
        "native_vpn_auto_selected_target": selectors.get("vpn_auto_selected_target"),
        "native_vpn_auto_targets": selectors.get("vpn_auto_targets"),
    }
    code, vpn_state = http_json(f"{stack['api']}/state/vpn")
    effective = vpn_state.get("data", {}).get("vpn", {}).get("effective") if isinstance(vpn_state, dict) else None
    assert code == 200 and vpn_state.get("ok") is True and isinstance(effective, dict) \
        and effective.get("active_server_id") is None, {
            "empty_pool_active_server": effective.get("active_server_id") if isinstance(effective, dict) else None,
        }
    fence = _read_packet_selection_fence()
    assert (fence.get("routing_state_present") is True
            and fence.get("active_server_id") == expected_fence.get("active_server_id")
            and fence.get("provenance_server_id") == expected_fence.get("provenance_server_id")
            and type(fence.get("revision")) is int
            and type(expected_fence.get("revision")) is int
            and fence.get("revision") >= expected_fence.get("revision")
            and fence.get("active_server_id") == fence_before_readback.get("active_server_id")
            and fence.get("provenance_server_id") == fence_before_readback.get("provenance_server_id")
            and fence.get("revision") == fence_before_readback.get("revision")), {
        "empty_pool_selection_fence": {
            "routing_state_present": fence.get("routing_state_present") is True,
            "retained_active_matches": fence.get("active_server_id") == expected_fence.get("active_server_id"),
            "retained_provenance_matches": fence.get("provenance_server_id") == expected_fence.get("provenance_server_id"),
            "revision_monotonic": type(fence.get("revision")) is int
            and type(expected_fence.get("revision")) is int
            and fence.get("revision") >= expected_fence.get("revision"),
            "stable_during_readback": fence.get("active_server_id") == fence_before_readback.get("active_server_id")
            and fence.get("provenance_server_id") == fence_before_readback.get("provenance_server_id")
            and fence.get("revision") == fence_before_readback.get("revision"),
        }
    }
    assert (selector_state.get("enabled_candidates_count") == 0
            and selector_state.get("auto_selectable_candidates_count") == 0
            and selector_state.get("active_auto_target_valid") is False
            and selector_state.get("active_auto_server_valid") is False
            and selector_state.get("mihomo_runtime_state") == "running"
            and selector_state.get("problem_code") == "vpn_auto_no_candidates"), {
        "empty_pool_selector_state": {
            "enabled_candidates_count": selector_state.get("enabled_candidates_count"),
            "auto_selectable_candidates_count": selector_state.get("auto_selectable_candidates_count"),
            "active_auto_target_valid": selector_state.get("active_auto_target_valid") is True,
            "active_auto_server_valid": selector_state.get("active_auto_server_valid") is True,
            "mihomo_runtime_running": selector_state.get("mihomo_runtime_state") == "running",
            "problem_code_matches_no_candidates": selector_state.get("problem_code") == "vpn_auto_no_candidates",
        }
    }
    code, routing = http_json(f"{stack['api']}/routing/global")
    enforcement = routing.get("data", {}).get("runtime_enforcement") if isinstance(routing, dict) else None
    assert code == 200 and routing.get("ok") is True and isinstance(enforcement, dict), {
        "global_vpn_enforcement_readback": code,
    }
    from fwrouter_api.services.dataplane_global import (
        _mihomo_health,
        build_global_preflight,
        read_applied_manifest,
        read_effective_rules_artifact,
    )
    from fwrouter_api.services.dataplane_live import probe_live_global_mode
    from fwrouter_api.services.dataplane_status import _runtime_routing_state, read_live_dataplane_payload

    def safe_codes(values: Any) -> list[str]:
        if not isinstance(values, list):
            return []
        return sorted({item for item in values if isinstance(item, str) and len(item) <= 64
                       and all(character.isalnum() or character in "_.:-" for character in item)})[:12]

    # Recompute the same read-only live preflight inputs used by the runtime
    # enforcement projection. Keep only fixed booleans, enums and reason codes.
    preflight_summary: dict[str, Any] = {"available": False}
    try:
        manifest = read_applied_manifest()
        live_payload = read_live_dataplane_payload()
        live_routing = _runtime_routing_state(manifest)
        effective_rules = ((manifest.get("extra") or {}).get("rules_effective")
                           if isinstance(manifest, dict) else None)
        if not isinstance(effective_rules, dict):
            effective_rules = read_effective_rules_artifact()
        preflight = build_global_preflight(
            routing=live_routing,
            check_details=live_payload,
            mihomo_health=_mihomo_health(),
            effective_rules_artifact=effective_rules if isinstance(effective_rules, dict) else None,
        )
        live_mode_probe = probe_live_global_mode()
        preflight_summary = {
            "available": True,
            "routing_applied_mode": (live_routing or {}).get("applied_mode")
            if (live_routing or {}).get("applied_mode") in {"vpn", "direct", "selective"} else "other",
            "routing_desired_mode": (live_routing or {}).get("desired_mode")
            if (live_routing or {}).get("desired_mode") in {"vpn", "direct", "selective"} else "other",
            "live_mode": live_mode_probe.get("mode")
            if isinstance(live_mode_probe, dict)
            and live_mode_probe.get("mode") in {"vpn", "direct", "selective"} else "other",
            "live_mode_ok": live_mode_probe.get("ok") is True if isinstance(live_mode_probe, dict) else False,
            "can_enforce_global_vpn": preflight.get("can_enforce_global_vpn") is True,
            "vpn_policy_installed": preflight.get("vpn_policy_installed") is True,
            "vpn_egress_ready": preflight.get("vpn_egress_ready") is True,
            "vpn_egress_target_present": preflight.get("vpn_egress_target_present") is True,
            "vpn_external_path_verified": preflight.get("vpn_external_path_verified") is True,
            "vpn_prepare_missing": safe_codes(preflight.get("vpn_prepare_missing")),
            "vpn_egress_missing": safe_codes(preflight.get("vpn_egress_missing")),
            "vpn_missing": safe_codes(preflight.get("missing_by_mode", {}).get("vpn", [])),
        }
    except Exception as exc:
        preflight_summary = {"available": False, "error_class": type(exc).__name__[:64]}

    reconcile = preference_change.get("mihomo_reconcile") if isinstance(preference_change, dict) else None
    reconcile_summary = {
        "present": isinstance(reconcile, dict),
        "ok": reconcile.get("ok") is True if isinstance(reconcile, dict) else None,
        "action": reconcile.get("reconcile_action")
        if isinstance(reconcile, dict) and reconcile.get("reconcile_action") in {
            "no_working_candidates", "staged_generation_verified", "noop", "failed",
        } else "other" if isinstance(reconcile, dict) else None,
        "error_code": reconcile.get("error_code")
        if isinstance(reconcile, dict) and isinstance(reconcile.get("error_code"), str)
        and len(reconcile["error_code"]) <= 64
        and all(character.isalnum() or character in "_.:-" for character in reconcile["error_code"])
        else None,
    }
    native_proof = _packet_refresh_native_proof(stack)
    policy_state = {key: native_proof.get(key) for key in (
        "vpn_udp_policy_installed", "vpn_udp_policy_receipt_matches",
        "vpn_udp_selector_membership_matches", "vpn_udp_guarded_rules_match",
        "vpn_udp_native_rules_match", "active_config_sha256", "native_incarnation_sha256",
    ) if key in native_proof}
    nft_counters = _core_counter_snapshot()
    diagnostic = {
        "phase": "vpn-empty-pool-native",
        "runtime_enforcement": {
            "level": enforcement.get("enforcement_level")
            if enforcement.get("enforcement_level") in {
                "global_direct_only", "global_vpn_fail_closed", "global_vpn_enforced",
            } else "other",
            "active_mode_matches_intent": enforcement.get("active_mode_matches_intent") is True,
            "live_mode": enforcement.get("live_global_mode")
            if enforcement.get("live_global_mode") in {"vpn", "direct", "selective"} else "other",
            "traffic_enforcement_guaranteed": enforcement.get("traffic_enforcement_guaranteed") is True,
            "vpn_egress_ready": enforcement.get("vpn_egress_ready") is True
            if type(enforcement.get("vpn_egress_ready")) is bool else None,
            "supported_vpn": (enforcement.get("supported_modes") or {}).get("vpn") is True
            if isinstance(enforcement.get("supported_modes"), dict) else None,
            "missing": safe_codes(enforcement.get("missing_runtime_requirements")),
        },
        "fresh_preflight": preflight_summary,
        "eligibility_reconcile": reconcile_summary,
        "native_policy_readback": policy_state,
        "core_nft_counters": nft_counters,
        "mihomo_path": native_proof,
    }
    _record_phase_diagnostic(record_property, diagnostic)
    assert (enforcement.get("active_mode_matches_intent") is True
            and enforcement.get("enforcement_level") == "global_vpn_fail_closed"
            and enforcement.get("vpn_egress_ready") is False), enforcement
    assert_mihomo_launch_matches_active(stack["native"], config_path)
    return {"vpn_auto_selected_reject": True, "vpn_auto_target_count": 1,
            "vpn_global_has_direct_target": "DIRECT" in global_group["proxies"],
            "selection_fence_revision": fence["revision"],
            "runtime_enforcement": {
                "level": enforcement["enforcement_level"],
                "vpn_egress_ready": enforcement["vpn_egress_ready"],
            }}


def _assert_packet_runtime(stack: dict, *, mode: str, policy_required: bool) -> None:
    active_config = stack["state"] / "generated" / "mihomo" / "config.yaml"
    assert_mihomo_launch_matches_active(stack["native"], active_config)
    code, mihomo = http_json(f"{stack['api']}/mihomo")
    assert code == 200 and mihomo.get("ok") is True
    state = mihomo["data"]["mihomo"]
    details = state.get("details") or {}
    selectors = details.get("selectors") or {}
    selected_global = selectors.get("vpn_global_now")
    allowed_global = {"vpn-auto"} if mode == "vpn" else {"vpn-auto", "DIRECT"}
    targets = selectors.get("vpn_global_targets") or []
    assert (state.get("runtime_state") == "running" and selected_global in allowed_global
            and selected_global in targets), {
        "runtime_state": state.get("runtime_state"), "vpn_global_now": selectors.get("vpn_global_now")
    }
    manifest_path = stack["state"] / "generated" / "dataplane" / "applied-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest.get("summary", {}).get("requires_vpn_policy_routing") is policy_required, {
        "mode": mode, "requires_vpn_policy_routing": manifest.get("summary", {}).get("requires_vpn_policy_routing")
    }
    assert manifest.get("routing_global_state", {}).get("desired_mode") == mode, {
        "mode": mode, "manifest_mode": manifest.get("routing_global_state", {}).get("desired_mode")
    }


def _packet_vpn_runtime_selection(stack: dict[str, Any]) -> dict[str, str]:
    code, payload = http_json(f"{stack['api']}/mihomo")
    assert code == 200 and payload.get("ok") is True
    mihomo = payload.get("data", {}).get("mihomo", {})
    details = mihomo.get("details", {}) if isinstance(mihomo, dict) else {}
    selectors = details.get("selectors", {}) if isinstance(details, dict) else {}
    assert isinstance(selectors, dict)
    now = selectors.get("vpn_global_now")
    auto_target = selectors.get("vpn_auto_now")
    selected_target = selectors.get("vpn_auto_selected_target")
    assert now == "vpn-auto" and isinstance(auto_target, str) and auto_target
    assert isinstance(selected_target, str) and selected_target
    return {"vpn_global_now": now, "vpn_auto_now": auto_target,
            "vpn_auto_selected_target": selected_target}


def _docker_policy_apply_failure_summary(response: dict[str, Any]) -> dict[str, Any]:
    """Expose only bounded, non-sensitive failure classes in hosted receipts."""
    data = response.get("data") if isinstance(response.get("data"), dict) else {}
    error = data.get("error") if isinstance(data.get("error"), dict) else {}
    code = str(error.get("code") or "")
    stage = str(error.get("stage") or "")
    return {
        "api_ok": response.get("ok") is True,
        "error_code": (
            code if code.isascii() and code.replace("_", "").isalnum() and len(code) <= 64 else "unknown"
        ),
        "stage": (
            stage
            if stage in {"validate", "prepare", "apply", "verify", "commit", "unknown"}
            else "unknown"
        ),
    }


def _qualify_docker_scoped_policy(stack: dict[str, Any], source_ref: str,
                                   logical_id: str, record_property: Any) -> dict[str, Any]:
    """Real routed namespace client with Docker subject semantics, not a host Docker daemon.

    Endpoint peer addresses and captured VLESS/direct tuples prove the actual
    controlled egress. This is not a production/provider Internet E2E claim.
    """
    assert stack["profile"].get("profile") == "hosted-kernel-packet"
    from fwrouter_api.db.connection import db_session

    api = stack["api"]
    subject_id = "docker:packet-scoped:ordinary"
    with db_session() as connection:
        assert connection.execute("SELECT 1 FROM subjects WHERE subject_id=?", (subject_id,)).fetchone() is None
        connection.execute(
            "INSERT INTO subjects (subject_id, subject_type, subject_role, implementation_kind, stable_key, display_name, "
            "desired_mode, applied_mode, runtime_state, is_active) "
            "VALUES (?, 'docker', 'docker_runtime', 'docker', ?, 'Scoped packet fixture', 'direct', 'direct', 'running', 1)",
            (subject_id, subject_id),
        )
        connection.execute(
            "INSERT INTO subject_docker (subject_id, compose_project, compose_service, container_name, "
            "container_id, image_name, ip_address, network_name, source_json) "
            "VALUES (?, 'packet-scoped', 'ordinary', 'ordinary', 'owned-fixture', 'fixture:local', ?, 'owned-client-veth', ?)",
            (subject_id, CLIENT_IP, json.dumps({
                "NetworkMode": "bridge",
                "IPv4InventoryComplete": True,
                "IPv4Addresses": [CLIENT_IP],
                "IPv6InventoryComplete": True,
                "IPv6Addresses": [],
            })),
        )

    def apply_subject(mode: str) -> None:
        code, accepted = http_json(f"{api}/subjects/{subject_id}/mode", method="POST",
                                  payload={"mode": mode, "actor_scope": "admin",
                                           "requested_by": "packet-docker-scoped", "run_now": True})
        if code != 200 or accepted.get("ok") is not True:
            failure = _docker_policy_apply_failure_summary(accepted)
            failure["mode"] = mode
            failure["http_status"] = code
            record_property(
                f"docker_policy_apply_failure_{mode}", json.dumps(failure, sort_keys=True)
            )
            assert code == 200 and accepted.get("ok") is True, failure
        job = await_core_job(api, accepted, timeout_seconds=90)
        if job.get("status") != "success":
            failure_response = {
                "ok": False,
                "data": {
                    "error": {
                        "code": job.get("error_code"),
                        "stage": (job.get("result") or {}).get("stage")
                        if isinstance(job.get("result"), dict) else None,
                        "message": job.get("error_message"),
                    }
                },
            }
            failure = _docker_policy_apply_failure_summary(failure_response)
            failure.update({"mode": mode, "job": _bounded_job_summary(job)})
            record_property(
                f"docker_policy_apply_failure_{mode}", json.dumps(failure, sort_keys=True)
            )
            assert job.get("status") == "success", failure
        code, readback = http_json(f"{api}/subjects/{subject_id}")
        assert code == 200 and readback.get("ok") is True
        subject = readback["data"]["subject"]
        assert subject["desired_mode"] == mode and subject["applied_mode"] == mode, {
            "mode": mode, "desired": subject.get("desired_mode"), "applied": subject.get("applied_mode"),
        }

    phases = {}
    try:
        # Docker subject apply uses full pipeline/DNS reconcile, unlike global
        # classify hot swaps. The earlier DNS phase has already stopped and
        # restored its paths: reacquire the existing owned native DNS fixture.
        dns_code, dns_fixture = http_json(f"{api}/__acceptance/dns53/configure", method="POST")
        assert dns_code == 200 and dns_fixture.get("ok") is True and dns_fixture.get("fixture_configured") is True
        # Explicit Direct must win even while the global Core mode is VPN.
        _apply_global_mode(api, "vpn", stack=stack, record_property=record_property)
        apply_subject("direct")
        # _probe_phase already checks endpoint peer observations for TCP, UDP
        # and DNS; its result retains safe endpoint counts for receipts.
        direct = _probe_phase(stack, CLIENT_IP, "docker-direct-global-vpn",
                              _core_counter_snapshot(), record_property, source_ref)
        phases["direct_under_global_vpn"] = direct["observations"]["last_peers"]
        _apply_global_mode(api, "direct", stack=stack, record_property=record_property)
        apply_subject("vpn")
        before_capture = _packet_counts()
        vpn = _probe_phase(stack, SERVICE_VIP, "docker-vpn-global-direct",
                           _core_counter_snapshot(), record_property, source_ref)
        after_capture = _packet_counts()
        assert _flow_delta(before_capture, after_capture, (ROUTER_WAN_IP, ENDPOINT_IP, "tcp", 5301)) > 0
        for protocol, port in (("tcp", 9080), ("udp", 9081), ("udp", 5353)):
            assert _flow_delta(before_capture, after_capture, (CLIENT_IP, SERVICE_VIP, protocol, port)) == 0
        phases["vpn_under_global_direct"] = vpn["observations"]["last_peers"]
        # A complete reapply must preserve the explicit scoped classification.
        _apply_global_mode(api, "vpn", stack=stack, record_property=record_property)
        apply_subject("vpn")
        _probe_phase(stack, SERVICE_VIP, "docker-vpn-repeat-apply",
                     _core_counter_snapshot(), record_property, source_ref)
        _probe_protected_direct_phase(stack, source_ref, record_property)
        stopped = _role_json(f"{ENDPOINT_CONTROL}/xray/availability", method="POST", payload={"available": False})
        assert stopped.get("available") is False
        try:
            _probe_vpn_udp_contract_phase(stack, source_ref, logical_id, "supported", True,
                                          "docker-vpn-endpoint-unavailable", record_property,
                                          expect_success=False, endpoint_unavailable=True,
                                          secondary_selector_candidate=True)
        finally:
            restarted = _role_json(f"{ENDPOINT_CONTROL}/xray/availability", method="POST", payload={"available": True})
            assert restarted.get("available") is True
        apply_subject("direct")
        restored = _probe_phase(stack, CLIENT_IP, "docker-direct-restored",
                                _core_counter_snapshot(), record_property, source_ref)
        phases["direct_restored"] = restored["observations"]["last_peers"]
        record_property("docker_scoped_packet_paths", json.dumps(phases, sort_keys=True))
        return phases
    finally:
        # Fixture-only CRUD, never production. Preserve the stack's original
        # global VPN state after removing the temporary scoped subject.
        with db_session() as connection:
            connection.execute("DELETE FROM subjects WHERE subject_id=?", (subject_id,))
        try:
            _apply_global_mode(api, "vpn", stack=stack, record_property=record_property)
        finally:
            dns_code, dns_cleanup = http_json(f"{api}/__acceptance/dns53/stop", method="POST")
            assert dns_code == 200 and dns_cleanup.get("stopped") is True and dns_cleanup.get("owned_paths_cleaned") is True


def _write_capture_summary(phases: dict[str, Any]) -> None:
    """Write compact packet-proof facts; full diagnostics remain in bounded JUnit properties."""
    def number(value: Any) -> int | None:
        return value if type(value) is int and 0 <= value <= 1_000_000 else None

    def counters(source: Any, keys: tuple[str, ...]) -> dict[str, int | None]:
        values = source if isinstance(source, dict) else {}
        return {key: number(values.get(key)) for key in keys}

    def guard_packets(source: Any, keys: tuple[str, ...]) -> dict[str, int | None]:
        values = source if isinstance(source, dict) else {}
        return {key: number(values.get(key, {}).get("packets")
                            if isinstance(values.get(key), dict) else None) for key in keys}

    def endpoint_peer_classes(source: Any) -> dict[str, str]:
        values = source if isinstance(source, dict) else {}
        allowed = {"service_vip", "dns_vpn_vip", "client", "localhost", "other", "missing"}
        result = {}
        for key in ("vpn_domain_tcp", "vpn_domain_udp", "direct_domain_tcp", "direct_domain_udp"):
            value = values.get(key)
            result[key] = value if isinstance(value, str) and value in allowed else (
                "missing" if value is None else "other"
            )
        return result

    def native_empty(source: Any) -> dict[str, Any]:
        value = source if isinstance(source, dict) else {}
        enforcement = value.get("runtime_enforcement") if isinstance(value.get("runtime_enforcement"), dict) else {}
        return {
            "reject_selected": value.get("vpn_auto_selected_reject") is True,
            "auto_targets": number(value.get("vpn_auto_target_count")),
            "direct_target": value.get("vpn_global_has_direct_target") is True,
            "fence_revision": number(value.get("selection_fence_revision")),
            "enforcement": enforcement.get("level")
            if enforcement.get("level") in {"global_vpn_fail_closed", "global_vpn_enforced", "degraded"} else "other",
            "vpn_egress_ready": enforcement.get("vpn_egress_ready")
            if type(enforcement.get("vpn_egress_ready")) is bool else None,
        }

    capability = phases.get("udp_capability_contract") if isinstance(phases.get("udp_capability_contract"), dict) else {}
    supported = capability.get("supported_packet") if isinstance(capability.get("supported_packet"), dict) else {}
    protected = phases.get("protected_direct") if isinstance(phases.get("protected_direct"), dict) else {}
    unknown = capability.get("unknown_packet") if isinstance(capability.get("unknown_packet"), dict) else {}
    unsupported = capability.get("unsupported_packet") if isinstance(capability.get("unsupported_packet"), dict) else {}
    unavailable = capability.get("endpoint_unavailable") if isinstance(capability.get("endpoint_unavailable"), dict) else {}
    empty = capability.get("empty_pool") if isinstance(capability.get("empty_pool"), dict) else {}
    direct = phases.get("direct_before_vpn") if isinstance(phases.get("direct_before_vpn"), dict) else {}
    vpn = phases.get("vpn") if isinstance(phases.get("vpn"), dict) else {}
    emergency = phases.get("emergency_direct") if isinstance(phases.get("emergency_direct"), dict) else {}
    reentry = phases.get("vpn_after_reentry") if isinstance(phases.get("vpn_after_reentry"), dict) else {}
    dns53 = phases.get("native_dns53_policy") if isinstance(phases.get("native_dns53_policy"), dict) else {}
    dns53_native = dns53.get("native_dnsmasq") if isinstance(dns53.get("native_dnsmasq"), dict) else {}
    nft_probe_targets = dns53_native.get("nft_probe_targets")
    nftset_materialized = (
        dns53_native.get("nftset_probe_status") == "ok"
        and isinstance(nft_probe_targets, list)
        and {item.get("domain") for item in nft_probe_targets if isinstance(item, dict)} == {"vpn.test", "direct.test"}
        and all(item.get("ok") is True and item.get("resolved") is True
                and type(item.get("materialized_count")) is int and item.get("materialized_count") > 0
                for item in nft_probe_targets if isinstance(item, dict))
        and len(nft_probe_targets) == 2
    )
    def observation_counts(source: Any) -> dict[str, int | None]:
        value = source if isinstance(source, dict) else {}
        counts_value = value.get("counts") if isinstance(value.get("counts"), dict) else {}
        return counters(counts_value, ("http", "udp", "dns"))

    summary_phases = {
        "direct_before_vpn": {
            "flows": counters(direct.get("flows"), ("tcp_9080", "udp_9081", "dns_5353")),
            "direct_counter": number((direct.get("core_counters") or {}).get("direct_lan_forward")
                                      if isinstance(direct.get("core_counters"), dict) else None),
            "endpoint": observation_counts(direct.get("observations")),
        },
        "vpn": {
            "endpoint_vless_tcp": number(vpn.get("vless_endpoint_packets")),
            "direct_leaks": counters(vpn.get("direct_client_leaks"), ("tcp_9080", "udp_9081", "dns_5353")),
            "core": counters(vpn.get("core_counters"),
                              ("vpn_tcp_classified", "vpn_udp_classified", "vpn_udp_handoff_packets")),
            "endpoint": observation_counts(vpn.get("observations")),
        },
        "protected_direct": {
            "native_vpn": protected.get("native_vpn_active") is True,
            "native_target_stable": protected.get("native_target_stable") is True,
            "selection_stable": protected.get("selection_stable") is True,
            "protected_rule_installed": (protected.get("native_protected_rule") or {}).get("classifier_bypass_installed") is True
            and (protected.get("native_protected_rule") or {}).get("protected_vip_in_active_set") is True,
            "protected_rule_stable": protected.get("native_protected_rule_stable") is True,
            "core_vpn_classified": counters(protected.get("core_vpn_delta"),
                                              ("vpn_tcp_classified", "vpn_udp_classified")),
            "endpoint": counters(protected.get("endpoint_counts_delta"), ("tcp", "udp")),
            "capture": counters(protected.get("capture_flow_delta"), ("tcp_9080", "udp_9081")),
            "forward_guard": guard_packets(protected.get("router_guard_delta"),
                                             ("router_protected_tcp_forward", "router_protected_udp_forward")),
            "output_guard": guard_packets(protected.get("router_guard_delta"),
                                            ("router_protected_tcp_output", "router_protected_udp_output")),
        },
        "udp_capability": {
            "supported": {
                "declared": capability.get("supported") is True,
                "endpoint_udp": number(supported.get("endpoint_udp_observation")),
                "tunnel_tcp": number(supported.get("vless_tunnel_tcp_capture_packets")),
                "direct_udp": number(supported.get("direct_udp_packets")),
            },
            "unknown": {
                "declared": capability.get("unknown") is True,
                "endpoint_udp": number(unknown.get("endpoint_udp_delta")),
                "handoff": number((unknown.get("core") or {}).get("vpn_udp_handoff_packets")
                                   if isinstance(unknown.get("core"), dict) else None),
                "tunnel_tcp": number(unknown.get("tcp")),
                "direct_udp": counters(unknown.get("direct_udp_capture_delta"),
                                        ("client_source_to_service", "mihomo_direct_to_service")),
            },
            "unsupported": {
                "declared": capability.get("unsupported") is True,
                "endpoint_udp": number(unsupported.get("endpoint_udp_delta")),
                "handoff": number((unsupported.get("core") or {}).get("vpn_udp_handoff_packets")
                                   if isinstance(unsupported.get("core"), dict) else None),
                "tunnel_tcp": number(unsupported.get("tcp")),
                "direct_udp": counters(unsupported.get("direct_udp_capture_delta"),
                                        ("client_source_to_service", "mihomo_direct_to_service")),
            },
            "endpoint_unavailable": {
                "endpoint_udp": number(unavailable.get("endpoint_udp_delta")),
                "handoff": number((unavailable.get("core") or {}).get("vpn_udp_handoff_packets")
                                   if isinstance(unavailable.get("core"), dict) else None),
                "direct_udp": counters(unavailable.get("direct_udp_capture_delta"),
                                        ("client_source_to_service", "mihomo_direct_to_service")),
            },
            "empty_pool": {
                "native": native_empty(empty.get("native")),
                "endpoint_udp": number(empty.get("endpoint_udp_delta")),
                "handoff": number((empty.get("core") or {}).get("vpn_udp_handoff_packets")
                                   if isinstance(empty.get("core"), dict) else None),
                "tcp_direct": counters(empty.get("tcp_capture"), ("client_direct", "router_direct", "vpn_proxy")),
                "direct_udp": counters(empty.get("direct_udp_capture_delta"),
                                        ("client_source_to_service", "mihomo_direct_to_service")),
                "selection_stable": empty.get("selection_stable") is True,
            },
        },
        "emergency_direct": {
            "flows": counters(emergency.get("flows"), ("tcp_9080", "udp_9081", "dns_5353")),
            "direct_counter": number((emergency.get("core_counters") or {}).get("direct_lan_forward")
                                      if isinstance(emergency.get("core_counters"), dict) else None),
            "endpoint": observation_counts(emergency.get("observations")),
        },
        "vpn_after_reentry": {
            "endpoint_vless_tcp": number(reentry.get("vless_endpoint_packets")),
            "direct_leaks": counters(reentry.get("direct_client_leaks"), ("tcp_9080", "udp_9081", "dns_5353")),
            "core": counters(reentry.get("core_counters"),
                              ("vpn_tcp_classified", "vpn_udp_classified", "vpn_udp_handoff_packets")),
            "endpoint": observation_counts(reentry.get("observations")),
        },
        "native_dns53_policy": {
            "api_status": dns53.get("api_status") == 200,
            "queries_answered": isinstance(dns53.get("client_queries"), dict),
            "external_resolver_captured": isinstance(dns53.get("external_resolver_queries"), dict),
            "dnsmasq_native": (dns53.get("native_dnsmasq") or {}).get("daemon_running") is True
            and (dns53.get("native_dnsmasq") or {}).get("selective_status_ok") is True
            and (dns53.get("native_dnsmasq") or {}).get("dns_capture_ok") is True
            and nftset_materialized
            and (dns53.get("native_dnsmasq") or {}).get("native_process_arguments_match") is True,
            "nftset_materialized": nftset_materialized,
            "unexpected_upstream_rules": number((dns53.get("native_dnsmasq") or {}).get("unexpected_upstream_rule_count")),
            "router_upstream_capture": counters(dns53.get("capture_router_to_owned_upstream"), ("udp_upstream", "tcp_upstream")),
            "endpoint_dns": counters(dns53.get("endpoint_observations_delta"), ("udp", "tcp", "vpn_name", "direct_name", "other_name")),
            "endpoint_replies": counters(dns53.get("endpoint_reply_delta"), ("udp", "tcp", "vpn_name")),
            "resolved_user_flows": counters(dns53.get("dns_selected_endpoint_flow_delta"),
                                              ("vpn_domain_tcp", "vpn_domain_udp", "direct_domain_tcp", "direct_domain_udp")),
            "endpoint_peer_proof": {
                key: (dns53.get("dns_selected_endpoint_peer_proof") or {}).get(key) is True
                for key in ("direct_domain_tcp_local", "direct_domain_udp_local",
                            "vpn_domain_tcp_local", "vpn_domain_udp_local")
            },
            "resolved_flow_core": counters(dns53.get("resolved_flow_core_delta"),
                                             ("vpn_tcp_classified", "vpn_udp_classified", "vpn_udp_handoff_packets")),
            "resolved_flow_direct_capture": counters(dns53.get("resolved_flow_direct_capture_delta"),
                ("service_tcp_router_direct", "service_udp_router_direct", "vpn_alias_tcp_router_direct",
                 "vpn_alias_udp_router_direct", "service_tcp_client_direct", "service_udp_client_direct",
                 "vpn_alias_tcp_client_direct", "vpn_alias_udp_client_direct")),
            "dns_guard": guard_packets(dns53.get("router_guard_delta"),
                                       ("router_dns_udp_input", "router_dns_tcp_input", "router_dns_udp_output", "router_dns_tcp_output",
                                        "router_dns_vpn_direct_output", "router_dns_vpn_direct_forward")),
            "selection_stable": dns53.get("selection_stable") is True,
            "cleanup": (dns53.get("cleanup") or {}).get("owned_paths_cleaned") is True,
        },
        "native_dns53_selective_direct_priority": {
            "selective_mode_with_vpn_default": (dns53.get("selective_direct_priority") or {}).get(
                "selective_mode_with_vpn_default") is True,
            "resolved_flows_fixed": (dns53.get("selective_direct_priority") or {}).get(
                "resolved_flows_fixed") is True,
            "endpoint_last_peer_matches": {
                key: ((dns53.get("selective_direct_priority") or {}).get("last_peer_matches") or {}).get(key) is True
                for key in ("vpn_domain_tcp", "vpn_domain_udp", "direct_domain_tcp", "direct_domain_udp")
            },
            "endpoint_peer_classes": endpoint_peer_classes(
                (dns53.get("selective_direct_priority") or {}).get("last_peer_classes")),
            "direct_core_path": number((dns53.get("selective_direct_priority") or {}).get(
                "direct_core_path_delta")),
            "selective_vpn_marks": counters(
                (dns53.get("selective_direct_priority") or {}).get("selective_vpn_mark_delta"), ("tcp", "udp")),
            "packet_delta": counters(
                (dns53.get("selective_direct_priority") or {}).get("packet_delta"),
                ("direct_client_tcp", "direct_client_udp", "direct_client_to_vpn_tcp",
                 "direct_client_to_vpn_udp", "direct_router_to_vpn_tcp",
                 "direct_router_to_vpn_udp", "provider_vless_tcp")),
            "native_selector_stable": (dns53.get("selective_direct_priority") or {}).get(
                "native_selector_stable") is True,
            "provider_selection_stable": (dns53.get("selective_direct_priority") or {}).get(
                "provider_selection_stable") is True,
            "provider_api_calls_unchanged": (dns53.get("selective_direct_priority") or {}).get(
                "provider_api_calls_unchanged") is True,
            "state_restored": (dns53.get("selective_direct_priority") or {}).get("state_restored") is True,
        },
    }
    summary = json.dumps({"schema": "fwrouter-packet-test-flows/v2", "phases": summary_phases},
                         sort_keys=True, separators=(",", ":")).encode("ascii") + b"\n"
    if len(summary) > 4096:
        raise AssertionError("packet flow summary exceeded its fixed byte limit")
    fd = os.open(CAPTURE_SUMMARY_PATH, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(summary)
        stream.flush()
        os.fsync(stream.fileno())


def _qualify_stale_rescue_lan_cleanup(api: str, stack: dict[str, Any], record_property: Any) -> None:
    """Seed historical owned drift only in the already-qualified router namespace."""
    assert stack["profile"].get("profile") == "hosted-kernel-packet"
    from fwrouter_api.services.dataplane_live import applied_nft_markers_match_live
    from fwrouter_api.core.config import get_settings

    def nft(*args: str, script: str | None = None) -> str:
        result = subprocess.run(
            ("/usr/sbin/nft", *args), input=script.encode("ascii") if script else None,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=3, check=False,
        )
        assert result.returncode == 0 and len(result.stdout) < MAX_CORE_NFT_JSON and len(result.stderr) < 4096, {
            "rescue_drift_nft_exit": result.returncode,
        }
        return result.stdout.decode("utf-8")

    fixture_table = "fwrouter_rescue_fixture"
    nft("-f", "-", script=(
        f"add table ip {fixture_table}\n"
        f"add chain ip {fixture_table} prerouting {{ type nat hook prerouting priority -101; policy accept; }}\n"
        f'add rule ip {fixture_table} prerouting iifname "rescue0" udp dport 53 dnat to 10.254.254.1:53\n'
    ))
    try:
        rescue_before = nft("list", "table", "ip", fixture_table)
        nft("-f", "-", script=(
            'add rule inet fwrouter_v2 prerouting iifname "rescue0" meta nfproto ipv6 reject with icmpv6 type admin-prohibited comment "block IPv6 from LAN ingress rescue0"\n'
            'add rule inet fwrouter_v2 prerouting iifname "rescue0" meta l4proto { tcp, udp } th dport 53 accept comment "allow LAN DNS capture before VPN classify rescue0"\n'
        ))
        applied = get_settings().paths.generated_dir / "dataplane" / "applied.nft"
        before = applied_nft_markers_match_live(applied)
        assert before.get("ok") is False and before.get("stale_lan_role_markers_count") == 2, before
        _apply_global_mode(api, "direct", stack=stack, record_property=record_property)
        after = applied_nft_markers_match_live(applied)
        assert after.get("ok") is True and after.get("stale_lan_role_markers_count") == 0, after
        assert nft("list", "table", "ip", fixture_table) == rescue_before
        _record_phase_diagnostic(record_property, {
            "phase": "stale-rescue-lan-cleanup", "stale_before": 2, "stale_after": 0,
            "core_apply_verified": True, "independent_rescue_nat_unchanged": True,
        })
    finally:
        nft("delete", "table", "ip", fixture_table)


@pytest.mark.packet
def test_core_vpn_emergency_direct_reentry_forwards_owned_tcp_udp_dns_without_leaks(
        acceptance_stack, record_property, monkeypatch):
    stack = acceptance_stack
    _bind_packet_parent_selector_runtime(stack, monkeypatch)
    profile = stack["profile"]
    bridge, native, api = stack["provider_bridge"], stack["native"], stack["api"]
    assert profile.get("profile") == "hosted-kernel-packet"
    assert getattr(bridge, "packet_endpoint", False) is True
    provider_target = urlsplit(bridge.current_config.get("connection_url", ""))
    assert (provider_target.hostname, provider_target.port) == (ENDPOINT_IP, 5301), {
        "provider_target": "does not match the fixed fixture endpoint"
    }

    client_ready = _role_json(f"{CLIENT_CONTROL}/ready")
    endpoint_ready = _role_json(f"{ENDPOINT_CONTROL}/ready")
    assert client_ready.get("ready") is True and client_ready.get("role") == "client"
    assert endpoint_ready.get("ready") is True and endpoint_ready.get("role") == "endpoint"
    assert endpoint_ready.get("sockets") == {
        "xray_tcp_5301": True, "xray_tcp_5302": True,
        "xray_tcp_5303": True, "tls_decoy_tcp_9443": True,
        "http_tcp_9080": True, "control_tcp_8082": True,
        "udp_echo_9081": True, "dns_udp_5353": True,
        "protected_tcp_9080": True, "protected_udp_9081": True,
        "dns_upstream_tcp_53": True, "dns_upstream_udp_53": True,
        "vpn_dns_tcp_9080": True, "vpn_dns_udp_9081": True,
    }
    xray = endpoint_ready.get("xray")
    assert isinstance(xray, dict) and xray.get("running") is True
    assert xray.get("binary_sha256") == profile.get("xray", {}).get("sha256")

    _enable_owned_mihomo_runtime_state()
    source_ref = _seed_source(stack)
    _set_packet_provider_capability(bridge, "true")
    _configure_provider(api, source_ref)
    code, enabled = http_json(f"{api}/subscription/sources/{source_ref}/provider", method="POST",
                              payload={"action": "enable"})
    enable_job = None
    enable_error_class = None
    if code == 200 and enabled.get("ok") is True and enabled.get("data", {}).get("accepted") is True:
        try:
            enable_job = await_core_job(api, enabled)
        except Exception as exc:
            enable_error_class = type(exc).__name__
    _record_job_stage(record_property, "provider-enable-job", http_status=code,
                      accepted=enabled, job=enable_job, error_class=enable_error_class, stack=stack)
    assert code == 200 and enabled.get("ok") is True and enabled.get("data", {}).get("accepted") is True
    if enable_error_class is not None:
        raise AssertionError("provider enable job polling failed within its bound")
    assert enable_job.get("status") == "success"
    from fwrouter_api.services.provider_managed import binding_for
    binding = binding_for(source_ref)
    logical_id = binding["logical_server_id"]
    original_member_id = binding["current_member_id"]
    from fwrouter_api.services.vpn_auto_exclusive import get_vpn_auto_exclusive_source_ref
    code, exclusive = http_json(f"{api}/subscription/sources/{source_ref}/vpn-auto-exclusive", method="POST",
                                payload={"enabled": True})
    exclusive_job = None
    exclusive_error_class = None
    if code == 200 and exclusive.get("ok") is True and exclusive.get("data", {}).get("accepted") is True:
        try:
            exclusive_job = await_core_job(api, exclusive)
        except Exception as exc:
            exclusive_error_class = type(exc).__name__
    _record_job_stage(record_property, "vpn-auto-exclusive-job", http_status=code,
                      accepted=exclusive, job=exclusive_job, error_class=exclusive_error_class, stack=stack)
    assert code == 200 and exclusive.get("ok") is True and exclusive.get("data", {}).get("accepted") is True
    if exclusive_error_class is not None:
        raise AssertionError("exclusive provider job polling failed within its bound")
    assert exclusive_job.get("status") == "success"
    assert get_vpn_auto_exclusive_source_ref() == source_ref

    # Normalize this test-owned provider server to a reversible, explicit
    # public-API preference baseline before traffic and before the later exact
    # preference snapshot. Explicit priority 1 retains manual provenance across
    # the empty-pool off/on cycle instead of invoking legacy auto normalization.
    preference_baseline = _set_server_auto_eligibility(api, logical_id, True, priority=1)
    baseline_preferences = _server_user_preferences(api, logical_id)
    assert (baseline_preferences.get("vpn_auto") is True
            and baseline_preferences.get("vpn_auto_priority") == 1
            and baseline_preferences.get("vpn_auto_priority_origin") == "manual"), {
        "canonical_packet_preference_baseline": {
            key: baseline_preferences.get(key)
            for key in ("vpn_auto", "vpn_auto_priority", "vpn_auto_priority_origin")
        },
        "reconcile_ok": (preference_baseline.get("mihomo_reconcile") or {}).get("ok")
        if isinstance(preference_baseline.get("mihomo_reconcile"), dict) else None,
    }

    _set_health_available(True)
    _apply_global_mode(api, "direct", stack=stack, record_property=record_property)
    _qualify_stale_rescue_lan_cleanup(api, stack, record_property)
    _assert_packet_runtime(stack, mode="direct", policy_required=False)
    capture_baseline = _packet_counts()
    core_before_direct = _core_counter_snapshot()
    direct_phase = _probe_phase(stack, CLIENT_IP, "direct-before-vpn", core_before_direct, record_property,
                                source_ref)
    after_direct = _wait_packet_counts(lambda counts: all(
        _flow_delta(capture_baseline, counts, flow) > 0 for flow in (
            (CLIENT_IP, SERVICE_VIP, "tcp", 9080),
            (CLIENT_IP, SERVICE_VIP, "udp", 9081),
            (CLIENT_IP, SERVICE_VIP, "udp", 5353),
        )))
    core_direct = direct_phase["core_counters"]
    assert core_direct["direct_lan_forward"] > 0, {"phase": "direct-before-vpn", "core_counters": core_direct}
    direct_flows = {
        "tcp_9080": _flow_delta(capture_baseline, after_direct, (CLIENT_IP, SERVICE_VIP, "tcp", 9080)),
        "udp_9081": _flow_delta(capture_baseline, after_direct, (CLIENT_IP, SERVICE_VIP, "udp", 9081)),
        "dns_5353": _flow_delta(capture_baseline, after_direct, (CLIENT_IP, SERVICE_VIP, "udp", 5353)),
    }
    assert all(count > 0 for count in direct_flows.values()), {"phase": "direct-before-vpn", "flows": direct_flows}

    _apply_global_mode(api, "vpn", stack=stack, record_property=record_property)
    _assert_packet_runtime(stack, mode="vpn", policy_required=True)
    supported_capability = _packet_capability_state(stack, source_ref, logical_id, "supported", True)
    protected_direct_phase = _probe_protected_direct_phase(stack, source_ref, record_property)
    before_vpn = _packet_counts()
    core_before_vpn = _core_counter_snapshot()
    vpn_phase = _probe_phase(stack, SERVICE_VIP, "vpn-supported-udp", core_before_vpn, record_property,
                             source_ref)
    after_vpn = _wait_packet_counts(lambda counts:
        _flow_delta(before_vpn, counts, (ROUTER_WAN_IP, ENDPOINT_IP, "tcp", 5301)) > 0)
    core_vpn = vpn_phase["core_counters"]
    assert (core_vpn["vpn_tcp_classified"] > 0 and core_vpn["vpn_udp_classified"] > 0
            and core_vpn["vpn_udp_handoff_packets"] > 0), {
        "phase": "vpn", "core_counters": core_vpn,
    }
    vpn_flow = _flow_delta(before_vpn, after_vpn, (ROUTER_WAN_IP, ENDPOINT_IP, "tcp", 5301))
    vpn_direct_leaks = {
        "tcp_9080": _flow_delta(before_vpn, after_vpn, (CLIENT_IP, SERVICE_VIP, "tcp", 9080)),
        "udp_9081": _flow_delta(before_vpn, after_vpn, (CLIENT_IP, SERVICE_VIP, "udp", 9081)),
        "dns_5353": _flow_delta(before_vpn, after_vpn, (CLIENT_IP, SERVICE_VIP, "udp", 5353)),
    }
    assert vpn_flow > 0 and all(count == 0 for count in vpn_direct_leaks.values()), {
        "phase": "vpn", "vless_endpoint_packets": vpn_flow, "direct_client_leaks": vpn_direct_leaks,
    }

    unknown_capability = _refresh_packet_capability(
        stack, api, bridge, source_ref, None, "unknown", logical_id, original_member_id, record_property,
    )
    unknown_udp_phase = _probe_vpn_udp_contract_phase(
        stack, source_ref, logical_id, "unknown", True, "vpn-unknown-udp", record_property,
        expect_success=True,
    )
    unknown_tcp_phase = _probe_vpn_tcp_regression(stack, "vpn-tcp-after-unknown", record_property)
    unsupported_capability = _refresh_packet_capability(
        stack, api, bridge, source_ref, "false", "unsupported", logical_id, original_member_id, record_property,
    )
    unsupported_udp_phase = _probe_vpn_udp_contract_phase(
        stack, source_ref, logical_id, "unsupported", False, "vpn-unsupported-udp", record_property,
        expect_success=False,
    )
    unsupported_tcp_phase = _probe_vpn_tcp_regression(stack, "vpn-tcp-after-unsupported", record_property)
    _refresh_packet_capability(
        stack, api, bridge, source_ref, "true", "supported", logical_id, original_member_id, record_property,
    )

    stopped = _role_json(f"{ENDPOINT_CONTROL}/xray/availability", method="POST",
                         payload={"available": False})
    assert stopped.get("available") is False, {"endpoint_xray_stop": stopped}
    unavailable_udp_phase = _probe_vpn_udp_contract_phase(
        stack, source_ref, logical_id, "supported", True, "vpn-endpoint-unavailable", record_property,
        expect_success=False, endpoint_unavailable=True,
    )
    restarted = _role_json(f"{ENDPOINT_CONTROL}/xray/availability", method="POST",
                           payload={"available": True})
    assert restarted.get("available") is True, {"endpoint_xray_restart": restarted}
    endpoint_restored = _role_json(f"{ENDPOINT_CONTROL}/ready")
    assert endpoint_restored.get("ready") is True and endpoint_restored.get("xray", {}).get("running") is True

    # Empty the eligible Auto pool through the public preference API while full-VPN
    # is applied. The native selector must select REJECT, and real UDP must fail
    # without reaching either direct endpoint path.
    preferences_before_empty_pool = _server_user_preferences(api, logical_id)
    assert preferences_before_empty_pool.get("vpn_auto") is True, {
        "empty_pool_requires_selected_provider_to_be_eligible": preferences_before_empty_pool,
    }
    fence_before_empty_pool = _read_packet_selection_fence()
    empty_pool_preference_change = _set_server_auto_eligibility(api, logical_id, False)
    empty_pool_native = _assert_native_empty_vpn_pool(
        stack, expected_fence=fence_before_empty_pool,
        preference_change=empty_pool_preference_change, record_property=record_property,
    )
    fence_before_empty_probe = _read_packet_selection_fence()
    empty_pool_phase = _probe_vpn_udp_contract_phase(
        stack, source_ref, logical_id, "supported", True, "vpn-empty-pool", record_property,
        expect_success=False, expect_active_server=False,
    )
    empty_pool_tcp_phase = _probe_vpn_tcp_empty_pool(stack, record_property)
    fence_after_empty_probe = _read_packet_selection_fence()
    assert (fence_before_empty_probe.get("active_server_id") == fence_after_empty_probe.get("active_server_id")
            and fence_before_empty_probe.get("provenance_server_id") == fence_after_empty_probe.get("provenance_server_id")
            and fence_before_empty_probe.get("revision") == fence_after_empty_probe.get("revision")), {
        "empty_pool_selection_fence_stable_during_packet_probes": {
            "active_matches": fence_before_empty_probe.get("active_server_id") == fence_after_empty_probe.get("active_server_id"),
            "provenance_matches": fence_before_empty_probe.get("provenance_server_id") == fence_after_empty_probe.get("provenance_server_id"),
            "revision_matches": fence_before_empty_probe.get("revision") == fence_after_empty_probe.get("revision"),
        }
    }
    _set_server_auto_eligibility(api, logical_id, True)
    preferences_after_empty_pool = _server_user_preferences(api, logical_id)
    assert preferences_after_empty_pool == preferences_before_empty_pool, {
        "provider_user_preferences_not_restored_exactly": {
            "before": preferences_before_empty_pool,
            "after": preferences_after_empty_pool,
        },
    }
    _assert_packet_runtime(stack, mode="vpn", policy_required=True)

    code, policy = http_json(f"{api}/subscription/sources/{source_ref}/provider/configuration",
                             method="POST", payload={"allow_automatic_member_switch": False})
    assert code == 200 and policy.get("ok") is True
    health_mode = _set_health_available(False)
    health_before_recovery = _recovery_health_snapshot(source_ref, logical_id)
    endpoint_health = _role_json(f"{ENDPOINT_CONTROL}/ready")
    bridge.calls.clear()
    code, first = http_json(f"{api}/__acceptance/provider/recovery", method="POST",
                            payload={"logical_server_id": logical_id, "decision_id": "packet-health-failure-1",
                                     "timeout_ms": 1000, "allow_switch": True}, timeout=90)
    provider_calls_after_first = bridge.snapshot_calls()
    health_after_first = _recovery_health_snapshot(source_ref, logical_id)
    assert code == 200 and first.get("provider_recovery") is True and first.get("provider_confirmation") == 1
    _record_phase_diagnostic(record_property, {
        "phase": "provider-recovery-confirmation-1",
        "decision": _recovery_decision_summary(first),
        "health": health_after_first,
        "provider_api_calls": len(provider_calls_after_first),
        "mihomo_path": _packet_refresh_native_proof(stack),
    })
    code, second = http_json(f"{api}/__acceptance/provider/recovery", method="POST",
                             payload={"logical_server_id": logical_id, "decision_id": "packet-health-failure-2",
                                      "timeout_ms": 1000, "allow_switch": True}, timeout=90)
    health_after_second = _recovery_health_snapshot(source_ref, logical_id)
    assert code == 200 and second.get("status") == "provider_auto_switch_disabled"
    code, direct = http_json(f"{api}/__acceptance/provider/recovery", method="POST",
                             payload={"logical_server_id": logical_id, "decision_id": "packet-health-failure-3",
                                      "timeout_ms": 1000, "allow_switch": True}, timeout=90)
    health_after_third = _recovery_health_snapshot(source_ref, logical_id)
    provider_calls_after_recovery = bridge.snapshot_calls()
    provider_call_summary = [
        {
            "method": method if method in {"GET", "PATCH", "POST"} else "other",
            "endpoint": "configs" if path == "/configs" else "other",
            "query_present": bool(query),
            "body_present": body is not None,
        }
        for method, path, query, body in provider_calls_after_recovery[:8]
    ]
    _record_phase_diagnostic(record_property, {
        "phase": "provider-recovery-confirmations",
        "health_fixture_available": health_mode.get("available") is True,
        "endpoint_sockets_ready": endpoint_health.get("ready") is True
        and all(value is True for value in (endpoint_health.get("sockets") or {}).values()),
        "endpoint_xray_running": (endpoint_health.get("xray") or {}).get("running") is True
        if isinstance(endpoint_health.get("xray"), dict) else False,
        "health_before": health_before_recovery,
        "health_after_confirmation_1": health_after_first,
        "health_after_confirmation_2": health_after_second,
        "health_after_confirmation_3": health_after_third,
        "decision_1": _recovery_decision_summary(first),
        "decision_2": _recovery_decision_summary(second),
        "decision_3": _recovery_decision_summary(direct),
        "mihomo_path": _packet_refresh_native_proof(stack),
        "provider_api_calls_after_confirmation_1": len(provider_calls_after_first),
        "provider_api_call_summary": provider_call_summary,
    })
    assert provider_calls_after_first == [("GET", "/configs", {}, None)], {
        "phase": "provider-recovery-confirmations",
        "provider_api_call_summary": provider_call_summary,
    }
    assert code == 200 and direct.get("status") == "emergency_direct"
    assert direct.get("effective_override") == "emergency_direct"
    assert provider_calls_after_recovery == provider_calls_after_first, {
        "phase": "provider-recovery-confirmations",
        "provider_api_call_summary": provider_call_summary,
    }
    _assert_packet_runtime(stack, mode="direct", policy_required=False)
    core_before_emergency = _core_counter_snapshot()
    before_emergency_direct = _packet_counts()
    emergency_phase = _probe_phase(stack, CLIENT_IP, "emergency-direct", core_before_emergency,
                                   record_property, source_ref)
    after_emergency_direct = _wait_packet_counts(lambda counts: all(
        _flow_delta(before_emergency_direct, counts, flow) > 0 for flow in (
            (CLIENT_IP, SERVICE_VIP, "tcp", 9080),
            (CLIENT_IP, SERVICE_VIP, "udp", 9081),
            (CLIENT_IP, SERVICE_VIP, "udp", 5353),
        )))
    core_emergency = emergency_phase["core_counters"]
    assert core_emergency["direct_lan_forward"] > 0, {
        "phase": "emergency-direct", "core_counters": core_emergency,
    }
    emergency_flows = {
        "tcp_9080": _flow_delta(before_emergency_direct, after_emergency_direct,
                                (CLIENT_IP, SERVICE_VIP, "tcp", 9080)),
        "udp_9081": _flow_delta(before_emergency_direct, after_emergency_direct,
                                (CLIENT_IP, SERVICE_VIP, "udp", 9081)),
        "dns_5353": _flow_delta(before_emergency_direct, after_emergency_direct,
                                (CLIENT_IP, SERVICE_VIP, "udp", 5353)),
    }
    assert all(count > 0 for count in emergency_flows.values()), {
        "phase": "emergency-direct", "flows": emergency_flows,
    }

    # The failed health refresh left an owner-bound verified generation while
    # Emergency Direct is active. Keep the exact binding as evidence; re-entry
    # must prove and close this checkpoint before its Mihomo apply changes the
    # native hash used by that proof.
    checkpoint_path = stack["state"] / "xray" / ".generation" / "generation-checkpoint.json"
    code, pre_reentry_inventory_response = http_json(
        f"{api}/__acceptance/subscription/native-generation",
    )
    pre_reentry_inventory = pre_reentry_inventory_response.get("state") or {}
    pre_reentry_checkpoint = (
        json.loads(checkpoint_path.read_text(encoding="utf-8")) if checkpoint_path.is_file() else {}
    )
    pre_reentry_binding = pre_reentry_checkpoint.get("subscription_inventory_generation")
    pre_reentry_digest = pre_reentry_inventory.get("candidate_digest")
    pre_reentry_revision = pre_reentry_inventory.get("candidate_revision")
    assert (
        code == 200 and pre_reentry_inventory_response.get("ok") is True
        and checkpoint_path.is_file()
        and pre_reentry_inventory.get("status") == "verified"
        and pre_reentry_checkpoint.get("phase") == "projections_cleaned"
        and isinstance(pre_reentry_digest, str) and len(pre_reentry_digest) == 64
        and pre_reentry_digest == pre_reentry_inventory.get("verified_digest")
        and type(pre_reentry_revision) is int
        and pre_reentry_revision == pre_reentry_inventory.get("verified_revision")
        and isinstance(pre_reentry_binding, dict)
        and pre_reentry_binding.get("digest") == pre_reentry_digest
        and pre_reentry_binding.get("revision") == pre_reentry_revision
    ), {
        "phase": "emergency-direct-checkpoint-before-reentry",
        "checkpoint_present": checkpoint_path.is_file(),
        "checkpoint_phase": pre_reentry_checkpoint.get("phase"),
        "inventory_status": pre_reentry_inventory.get("status"),
        "verified_generation_bound": bool(
            isinstance(pre_reentry_binding, dict)
            and pre_reentry_binding.get("digest") == pre_reentry_digest
            and pre_reentry_binding.get("revision") == pre_reentry_revision
        ),
    }
    identity_code, identity_before_reentry = http_json(f"{api}/__acceptance/xray/identity-context")
    identity_digest_before = identity_before_reentry.get("identity_digest")
    assert (
        identity_code == 200 and identity_before_reentry.get("ok") is True
        and isinstance(identity_digest_before, str) and len(identity_digest_before) == 64
        and all(char in "0123456789abcdef" for char in identity_digest_before)
    ), {"phase": "emergency-direct-checkpoint-before-reentry", "identity_digest_available": False}

    _set_health_available(True)
    provider_calls_before_reentry = bridge.snapshot_calls()
    code, reentered = http_json(f"{api}/__acceptance/provider/reentry", method="POST",
                                payload={"timeout_ms": 2000}, timeout=90)
    provider_calls_after_reentry = bridge.snapshot_calls()
    reentry_provider_calls = provider_calls_after_reentry[len(provider_calls_before_reentry):]
    reentry_provider_call_summary = [
        {
            "method": method if method in {"GET", "PATCH", "POST"} else "other",
            "endpoint": "configs" if path == "/configs" else "other",
            "query_present": bool(query),
            "body_present": body is not None,
        }
        for method, path, query, body in reentry_provider_calls[:4]
    ]
    reentry_routing_code, reentry_routing = http_json(f"{api}/routing/global")
    _record_phase_diagnostic(record_property, {
        "phase": "provider-reentry",
        "http_status": code if type(code) is int else None,
        "result": _provider_reentry_summary(reentered),
        "health": _recovery_health_snapshot(source_ref, logical_id),
        "routing": _provider_reentry_routing_summary(reentry_routing_code, reentry_routing),
        "mihomo_path": _packet_refresh_native_proof(stack),
        "provider_api_calls_before": len(provider_calls_before_reentry),
        "provider_api_calls_after": len(provider_calls_after_reentry),
        "provider_api_call_summary": reentry_provider_call_summary,
    })
    assert code == 200 and reentered.get("ok") is True
    assert reentered.get("effective_override") in {None, ""}
    assert provider_calls_after_reentry == provider_calls_before_reentry, {
        "phase": "provider-reentry", "provider_api_call_summary": reentry_provider_call_summary,
    }
    code, vpn_projection = http_json(f"{api}/routing/global")
    assert code == 200 and vpn_projection.get("ok") is True
    assert vpn_projection["data"]["routing"].get("desired_mode") == "vpn"
    assert vpn_projection["data"]["routing"].get("applied_mode") == "vpn"
    assert vpn_projection["data"]["runtime_enforcement"].get("live_global_mode") == "vpn"
    _assert_packet_runtime(stack, mode="vpn", policy_required=True)
    core_before_reentry = _core_counter_snapshot()
    before_reentry = _packet_counts()
    reentry_phase = _probe_phase(stack, SERVICE_VIP, "vpn-after-reentry", core_before_reentry,
                                 record_property, source_ref)
    after_reentry = _wait_packet_counts(lambda counts:
        _flow_delta(before_reentry, counts, (ROUTER_WAN_IP, ENDPOINT_IP, "tcp", 5301)) > 0)
    core_reentry = reentry_phase["core_counters"]
    assert (core_reentry["vpn_tcp_classified"] > 0 and core_reentry["vpn_udp_classified"] > 0
            and core_reentry["vpn_udp_handoff_packets"] > 0), {
        "phase": "vpn-after-reentry", "core_counters": core_reentry,
    }
    reentry_flow = _flow_delta(before_reentry, after_reentry, (ROUTER_WAN_IP, ENDPOINT_IP, "tcp", 5301))
    reentry_direct_leaks = {
        "tcp_9080": _flow_delta(before_reentry, after_reentry, (CLIENT_IP, SERVICE_VIP, "tcp", 9080)),
        "udp_9081": _flow_delta(before_reentry, after_reentry, (CLIENT_IP, SERVICE_VIP, "udp", 9081)),
        "dns_5353": _flow_delta(before_reentry, after_reentry, (CLIENT_IP, SERVICE_VIP, "udp", 5353)),
    }
    assert reentry_flow > 0 and all(count == 0 for count in reentry_direct_leaks.values()), {
        "phase": "vpn-after-reentry", "vless_endpoint_packets": reentry_flow,
        "direct_client_leaks": reentry_direct_leaks,
    }
    # Successful provider re-entry must have recovered the still-bound Xray
    # generation before the Mihomo reconcile changed its native checkpoint
    # hash. A post-reentry cleanup would be too late and must fail closed.
    assert not checkpoint_path.exists(), {
        "phase": "provider-reentry-checkpoint-recovery",
        "checkpoint_closed_by_reentry_preparation": not checkpoint_path.exists(),
    }
    code, verified_generation = http_json(f"{api}/__acceptance/subscription/native-generation")
    verified_state = verified_generation.get("state") or {}
    assert code == 200 and verified_generation.get("ok") is True
    assert (
        verified_state.get("status") == "verified"
        and verified_state.get("candidate_digest") == pre_reentry_digest == verified_state.get("verified_digest")
        and verified_state.get("candidate_revision") == pre_reentry_revision == verified_state.get("verified_revision")
    ), {
        "phase": "provider-reentry-checkpoint-recovery",
        "inventory_status": verified_state.get("status"),
        "candidate_revision": verified_state.get("candidate_revision"),
        "verified_revision": verified_state.get("verified_revision"),
        "candidate_digest_matches": verified_state.get("candidate_digest") == pre_reentry_digest,
    }
    identity_code, identity_after_reentry = http_json(f"{api}/__acceptance/xray/identity-context")
    assert (
        identity_code == 200
        and identity_after_reentry.get("ok") is True
        and identity_after_reentry.get("identity_digest") == identity_digest_before
    ), {
        "phase": "provider-reentry-checkpoint-recovery",
        "identity_context_status": identity_code,
        "identity_digest_preserved": (
            identity_after_reentry.get("identity_digest") == identity_digest_before
        ),
    }
    recovery_routing_code, recovery_routing = http_json(f"{api}/routing/global")
    assert (
        recovery_routing_code == 200
        and recovery_routing.get("data", {}).get("routing", {}).get("desired_mode") == "vpn"
        and recovery_routing.get("data", {}).get("routing", {}).get("applied_mode") == "vpn"
        and recovery_routing.get("data", {}).get("runtime_enforcement", {}).get("live_global_mode") == "vpn"
    ), {
        "phase": "provider-reentry-checkpoint-recovery",
        "routing_status": recovery_routing_code,
        "routing_mode": (recovery_routing.get("data", {}).get("routing") or {}).get("applied_mode"),
        "live_global_mode": (recovery_routing.get("data", {}).get("runtime_enforcement") or {}).get("live_global_mode"),
    }
    _record_phase_diagnostic(record_property, {
        "phase": "provider-reentry-checkpoint-recovery",
        "checkpoint_recovered_before_reentry_apply": not checkpoint_path.exists(),
        "inventory_verified": verified_state.get("status") == "verified",
        "inventory_digest_preserved": verified_state.get("candidate_digest") == pre_reentry_digest,
        "identity_digest_preserved": identity_after_reentry.get("identity_digest") == identity_digest_before,
        "routing_mode": (recovery_routing.get("data", {}).get("routing") or {}).get("applied_mode"),
        "live_global_mode": (recovery_routing.get("data", {}).get("runtime_enforcement") or {}).get("live_global_mode"),
    })
    dns53_phase = _probe_dns53_policy_phase(stack, source_ref, record_property)
    assert_mihomo_launch_matches_active(native, stack["state"] / "generated" / "mihomo" / "config.yaml")
    final_binding = binding_for(source_ref)
    assert final_binding["current_member_id"] == binding["current_member_id"]
    assert final_binding["applied_member_id"] == binding["applied_member_id"]
    assert get_vpn_auto_exclusive_source_ref() == source_ref
    code, vpn_state = http_json(f"{api}/state/vpn")
    assert code == 200 and vpn_state.get("ok") is True
    assert vpn_state["data"]["vpn"]["effective"]["server_health"]["active"]["server_id"] == logical_id

    # Add one ordinary native candidate only after the provider-specific
    # recovery/capability assertions. It targets the second owned Xray inbound
    # and is selected through the public Core selector API below.
    provider_binding_before_selector = binding_for(source_ref)
    provider_calls_before_selector = bridge.snapshot_calls()
    from fwrouter_api.adapters.subscription import parse_subscription_payload
    from fwrouter_api.services.logical_topology import get_logical_runtime_name
    from fwrouter_api.services.subscription import _upsert_subscription_servers

    selector_source_url = "https://selector-change.invalid/ordinary"
    ordinary_payload = (
        f"vless://{SELECTOR_XRAY_ID}@{ENDPOINT_IP}:{ENDPOINT_SELECTOR_PORT}"
        "?type=tcp&encryption=none#selector-packet-candidate"
    )
    parsed = parse_subscription_payload(ordinary_payload)
    assert parsed.status.value == "success" and len(parsed.servers) == 1, {
        "ordinary_candidate_parsed": parsed.status.value == "success",
        "ordinary_candidate_count": len(parsed.servers),
    }
    ordinary_server = parsed.servers[0]
    inventory = _upsert_subscription_servers(
        [ordinary_server], servers_by_url={selector_source_url: [ordinary_server]},
    )
    assert inventory.get("seen_count") == 1
    code, publication = http_json(
        f"{api}/__acceptance/xray/publish-pending-inventory", method="POST", payload={}, timeout=90,
    )
    assert code == 200 and publication.get("ok") is True, {
        "status": code, "error_code": publication.get("error_code"),
        "stage": publication.get("stage"),
        "native_generation_verified": publication.get("native_generation_verified"),
    }
    assert publication.get("native_generation_verified") is True
    code, ordinary_preference = http_json(
        f"{api}/servers/{ordinary_server.server_id}/preferences", method="PATCH",
        payload={"vpn_auto": True, "vpn_auto_priority": 1, "reconcile_mihomo": True,
                 "requested_by": "hosted-packet-selector-change"},
    )
    assert code == 200 and ordinary_preference.get("ok") is True, {
        "ordinary_candidate_preference_status": code,
    }
    preference_data = ordinary_preference.get("data", {}).get("server_preferences", {})
    preference_values = (preference_data.get("server") or {}).get("preferences") or {}
    assert preference_values.get("vpn_auto") is True and preference_values.get("vpn_auto_priority") == 1

    code, exclusive_disabled = http_json(
        f"{api}/subscription/sources/{source_ref}/vpn-auto-exclusive", method="POST",
        payload={"enabled": False},
    )
    disable_job = await_core_job(api, exclusive_disabled) if (
        code == 200 and exclusive_disabled.get("ok") is True
        and exclusive_disabled.get("data", {}).get("accepted") is True
    ) else None
    _record_job_stage(record_property, "vpn-auto-exclusive-disable-selector-packet", http_status=code,
                      accepted=exclusive_disabled, job=disable_job, stack=stack)
    assert code == 200 and exclusive_disabled.get("data", {}).get("accepted") is True, {
        "exclusive_disable_status": code,
    }
    assert disable_job is not None and disable_job.get("status") == "success", {
        "exclusive_disable_job_status": disable_job.get("status") if isinstance(disable_job, dict) else None,
    }
    assert get_vpn_auto_exclusive_source_ref() is None
    from fwrouter_api.services.selector import get_vpn_auto_state
    candidate_state = get_vpn_auto_state(read_only=True)
    candidate_ids = candidate_state.get("auto_selectable_candidate_ids")
    exact_candidate_pool = (isinstance(candidate_ids, list)
                            and set(candidate_ids) == {logical_id, ordinary_server.server_id})
    assert exact_candidate_pool and candidate_state.get("active_auto_server_id") == logical_id, {
        "candidate_pool_count": len(candidate_ids) if isinstance(candidate_ids, list) else None,
        "provider_candidate_present": isinstance(candidate_ids, list) and logical_id in candidate_ids,
        "ordinary_candidate_present": isinstance(candidate_ids, list) and ordinary_server.server_id in candidate_ids,
        "active_is_provider_candidate": candidate_state.get("active_auto_server_id") == logical_id,
    }
    selector_fence_before = _read_packet_selection_fence()
    assert selector_fence_before.get("active_server_id") == logical_id
    assert selector_fence_before.get("provenance_server_id") == logical_id

    code, switched = http_json(
        f"{api}/selector/vpn-auto/switch", method="POST",
        payload={"confirm_switch": True, "exclude_active": True, "update_ping_state": False,
                 "limit": 20, "timeout_ms": 1000, "reason": "hosted_packet_selector_change"},
        timeout=90,
    )
    selector = switched.get("data", {}).get("selector", {}) if isinstance(switched, dict) else {}
    switched_fence = _read_packet_selection_fence()
    selector_response_receipt = {
        "phase": "vpn-auto-selector-switch-response",
        "http_200": code == 200,
        "api_ok": isinstance(switched, dict) and switched.get("ok") is True,
        "applied": selector.get("applied") is True,
        "selected_ordinary_candidate": selector.get("selected_server_id") == ordinary_server.server_id,
        "ordinary_candidate_active": selector.get("active_after") == ordinary_server.server_id,
        "selection_revision": switched_fence.get("revision")
        if type(switched_fence.get("revision")) is int else None,
    }
    selector_response_json = json.dumps(selector_response_receipt, sort_keys=True,
                                        separators=(",", ":"), ensure_ascii=True)
    assert len(selector_response_json.encode("ascii")) <= MAX_PHASE_DIAGNOSTIC_BYTES
    record_property("packet_selector_switch_response", selector_response_json)
    assert code == 200 and switched.get("ok") is True and selector.get("ok") is True \
        and selector.get("applied") is True, {"selector_switch_status": code}
    assert selector.get("selected_server_id") == ordinary_server.server_id \
        and selector.get("active_after") == ordinary_server.server_id \
        and selector.get("selected_member_id") is None, {
            "selected_ordinary_candidate": selector.get("selected_server_id") == ordinary_server.server_id,
            "ordinary_candidate_active": selector.get("active_after") == ordinary_server.server_id,
            "no_member_selection": selector.get("selected_member_id") is None,
        }
    selector_fence_after = switched_fence
    core_selector_state = get_vpn_auto_state(read_only=True)
    ordinary_runtime_name = get_logical_runtime_name(ordinary_server.server_id)
    code, native_selector = http_json("http://127.0.0.1:5200/proxies/vpn-auto")
    assert code == 200 and native_selector.get("now") == ordinary_runtime_name, {
        "native_selector_status": code,
        "native_selector_matches_ordinary": isinstance(native_selector, dict)
        and native_selector.get("now") == ordinary_runtime_name,
    }
    config_path = stack["state"] / "generated" / "mihomo" / "config.yaml"
    assert_mihomo_launch_matches_active(native, config_path)
    loaded_config = yaml.safe_load(config_path.read_bytes())
    loaded_proxies = loaded_config.get("proxies") if isinstance(loaded_config, dict) else None
    ordinary_proxy = [row for row in loaded_proxies if isinstance(row, dict)
                      and row.get("name") == ordinary_runtime_name] if isinstance(loaded_proxies, list) else []
    native_target_matches = (len(ordinary_proxy) == 1 and ordinary_proxy[0].get("type") == "vless"
                             and ordinary_proxy[0].get("server") == ENDPOINT_IP
                             and ordinary_proxy[0].get("port") == ENDPOINT_SELECTOR_PORT)
    assert native_target_matches, {
        "native_candidate_count": len(ordinary_proxy),
        "native_candidate_target_matches": native_target_matches,
    }
    incarnation_before_probe = _mihomo_packet_path_proof(stack).get("native_incarnation_sha256")
    assert (selector_fence_after.get("active_server_id") == ordinary_server.server_id
            and selector_fence_after.get("provenance_server_id") == ordinary_server.server_id
            and type(selector_fence_after.get("revision")) is int
            and selector_fence_after["revision"] > selector_fence_before.get("revision", -1)
            and core_selector_state.get("active_auto_server_id") == ordinary_server.server_id
            and isinstance(incarnation_before_probe, str)), {
        "fence_revision_advanced": (type(selector_fence_after.get("revision")) is int
                                     and selector_fence_after.get("revision", 0)
                                     > selector_fence_before.get("revision", -1)),
        "core_active_matches_ordinary": core_selector_state.get("active_auto_server_id") == ordinary_server.server_id,
        "native_incarnation_available": isinstance(incarnation_before_probe, str),
    }
    binding_after_selector = binding_for(source_ref)
    provider_binding_unchanged = all(
        binding_after_selector.get(key) == provider_binding_before_selector.get(key)
        for key in ("logical_server_id", "current_member_id", "applied_member_id", "binding_revision")
    )
    assert provider_binding_unchanged and bridge.snapshot_calls() == provider_calls_before_selector, {
        "provider_binding_unchanged": provider_binding_unchanged,
        "provider_api_calls_unchanged": bridge.snapshot_calls() == provider_calls_before_selector,
    }

    packets_before_selector_probe = _packet_counts()
    core_before_selector_probe = _core_counter_snapshot()
    selector_phase = _probe_phase(stack, DNS_VPN_SERVICE_VIP, "vpn-after-selector-switch",
                                  core_before_selector_probe, record_property, source_ref)
    packets_after_selector_probe = _wait_packet_counts(lambda counts:
        _flow_delta(packets_before_selector_probe, counts,
                    (ROUTER_WAN_IP, ENDPOINT_IP, "tcp", ENDPOINT_SELECTOR_PORT)) > 0)
    selector_flow = _flow_delta(packets_before_selector_probe, packets_after_selector_probe,
                                (ROUTER_WAN_IP, ENDPOINT_IP, "tcp", ENDPOINT_SELECTOR_PORT))
    selector_core = selector_phase["core_counters"]
    selector_direct_leaks = {
        "tcp_9080": _flow_delta(packets_before_selector_probe, packets_after_selector_probe,
                                (CLIENT_IP, SERVICE_VIP, "tcp", 9080)),
        "client_dns_vpn_tcp_9080": _flow_delta(
            packets_before_selector_probe, packets_after_selector_probe,
            (CLIENT_IP, DNS_VPN_SERVICE_VIP, "tcp", 9080)),
        "udp_9081": _flow_delta(packets_before_selector_probe, packets_after_selector_probe,
                                (CLIENT_IP, SERVICE_VIP, "udp", 9081)),
        "client_dns_vpn_udp_9081": _flow_delta(
            packets_before_selector_probe, packets_after_selector_probe,
            (CLIENT_IP, DNS_VPN_SERVICE_VIP, "udp", 9081)),
        "dns_5353": _flow_delta(packets_before_selector_probe, packets_after_selector_probe,
                                (CLIENT_IP, SERVICE_VIP, "udp", 5353)),
        "router_tcp_9080": _flow_delta(packets_before_selector_probe, packets_after_selector_probe,
                                        (ROUTER_WAN_IP, SERVICE_VIP, "tcp", 9080)),
        "router_udp_9081": _flow_delta(packets_before_selector_probe, packets_after_selector_probe,
                                        (ROUTER_WAN_IP, SERVICE_VIP, "udp", 9081)),
        "router_dns_5353": _flow_delta(packets_before_selector_probe, packets_after_selector_probe,
                                       (ROUTER_WAN_IP, SERVICE_VIP, "udp", 5353)),
        "router_dns_vpn_tcp_9080": _flow_delta(
            packets_before_selector_probe, packets_after_selector_probe,
            (ROUTER_WAN_IP, DNS_VPN_SERVICE_VIP, "tcp", 9080)),
        "router_dns_vpn_udp_9081": _flow_delta(
            packets_before_selector_probe, packets_after_selector_probe,
            (ROUTER_WAN_IP, DNS_VPN_SERVICE_VIP, "udp", 9081)),
    }
    selector_observations = selector_phase["observations"]
    endpoint_peer_matches = all(selector_observations["last_peers"].get(kind) == DNS_VPN_SERVICE_VIP
                                for kind in ("http", "udp", "dns"))
    incarnation_after_probe = selector_phase["mihomo_path"].get("native_incarnation_sha256")
    selector_packet_diagnostic = {
        "phase": "vpn-auto-selector-change",
        "candidate_pool_exact_two": exact_candidate_pool,
        "public_selector_applied_ordinary": selector.get("applied") is True
        and selector.get("active_after") == ordinary_server.server_id,
        "fence_advanced_to_ordinary": selector_fence_after.get("active_server_id") == ordinary_server.server_id
        and selector_fence_after.get("revision", 0) > selector_fence_before.get("revision", -1),
        "native_now_matches_ordinary": native_selector.get("now") == ordinary_runtime_name,
        "native_target_5302": native_target_matches,
        "provider_binding_unchanged": provider_binding_unchanged,
        "provider_api_calls_unchanged": bridge.snapshot_calls() == provider_calls_before_selector,
        "native_incarnation_stable": incarnation_after_probe == incarnation_before_probe,
        "endpoint_peer_matches_vip_b": endpoint_peer_matches,
        "endpoint_observation_delta": selector_phase["endpoint_observation_delta"],
        "selector_vless_capture_delta": selector_flow,
        "core_counter_delta": selector_core,
        "direct_client_leak_delta": selector_direct_leaks,
    }
    _record_phase_diagnostic(record_property, selector_packet_diagnostic)
    assert endpoint_peer_matches and selector_flow > 0, selector_packet_diagnostic
    assert (selector_core.get("vpn_tcp_classified", 0) > 0
            and selector_core.get("vpn_udp_classified", 0) > 0
            and selector_core.get("vpn_udp_handoff_packets", 0) > 0), selector_packet_diagnostic
    assert all(count == 0 for count in selector_direct_leaks.values()), selector_packet_diagnostic
    assert incarnation_after_probe == incarnation_before_probe, selector_packet_diagnostic
    assert provider_binding_unchanged and bridge.snapshot_calls() == provider_calls_before_selector, selector_packet_diagnostic

    code, exclusive_restored = http_json(
        f"{api}/subscription/sources/{source_ref}/vpn-auto-exclusive", method="POST",
        payload={"enabled": True},
    )
    restore_job = await_core_job(api, exclusive_restored) if (
        code == 200 and exclusive_restored.get("ok") is True
        and exclusive_restored.get("data", {}).get("accepted") is True
    ) else None
    _record_job_stage(record_property, "vpn-auto-exclusive-restore-selector-packet", http_status=code,
                      accepted=exclusive_restored, job=restore_job, stack=stack)
    assert code == 200 and exclusive_restored.get("data", {}).get("accepted") is True
    assert restore_job is not None and restore_job.get("status") == "success"
    assert get_vpn_auto_exclusive_source_ref() == source_ref
    restored_candidate_state = get_vpn_auto_state(read_only=True)
    restored_candidate_ids = restored_candidate_state.get("auto_selectable_candidate_ids")
    assert (isinstance(restored_candidate_ids, list)
            and set(restored_candidate_ids) == {logical_id}
            and restored_candidate_state.get("active_auto_server_id") == logical_id), {
        "restored_candidate_pool_count": len(restored_candidate_ids)
        if isinstance(restored_candidate_ids, list) else None,
        "provider_candidate_only": isinstance(restored_candidate_ids, list)
        and set(restored_candidate_ids) == {logical_id},
        "provider_is_current_after_restore": restored_candidate_state.get("active_auto_server_id") == logical_id,
    }
    final_binding = binding_for(source_ref)
    assert all(final_binding.get(key) == provider_binding_before_selector.get(key)
               for key in ("logical_server_id", "current_member_id", "applied_member_id", "binding_revision"))
    assert bridge.snapshot_calls() == provider_calls_before_selector

    _qualify_docker_scoped_policy(stack, source_ref, logical_id, record_property)

    _write_capture_summary({
        "core_counter_mapping": {
            "direct_lan_forward": "unscoped inet fwrouter_v2 fwrouter_direct / global direct path counter; also counts host-output and host-local direct branches, diagnostic only (not flow-specific leak proof)",
            "vpn_lan_input": "sum of inet fwrouter_v2 chain fwrouter_vpn_full TCP 5204 REDIR and UDP 5205 TProxy mark counters selected by global vpn v1; Core has no input-hook packet counter",
            "vpn_udp_handoff_packets": "inet fwrouter_v2 chain prerouting / fwrouter full-vpn tproxy handoff udp:5205 counter; proves marked full-VPN UDP reached the Core TProxy handoff",
        },
        "direct_before_vpn": {"flows": direct_flows, "core_counters": core_direct,
                               "mihomo_path": direct_phase["mihomo_path"],
                               "observations": direct_phase["observations"],
                               "egress_guard": direct_phase["leak"],
                               "router_guard": direct_phase["router_guard"]},
        "vpn": {"vless_endpoint_packets": vpn_flow, "direct_client_leaks": vpn_direct_leaks,
                "core_counters": core_vpn,
                "mihomo_path": vpn_phase["mihomo_path"],
                "observations": vpn_phase["observations"],
                "egress_guard": vpn_phase["leak"], "router_guard": vpn_phase["router_guard"]},
        "protected_direct": protected_direct_phase,
        "native_dns53_policy": dns53_phase,
        "udp_capability_contract": {
            "supported": supported_capability["declared_capability"] == "supported",
            "supported_packet": {
                "endpoint_udp_observation": vpn_phase["observations"]["counts"].get("udp", 0),
                "core": core_vpn,
                "vless_tunnel_tcp_capture_packets": vpn_flow,
                "direct_udp_packets": vpn_direct_leaks["udp_9081"],
            },
            "unknown": unknown_capability["declared_capability"] == "unknown",
            "unknown_packet": {"endpoint_udp_delta": unknown_udp_phase["endpoint_udp_observation_delta"],
                               "core": unknown_udp_phase["core_counter_delta"],
                               "direct_udp_capture_delta": unknown_udp_phase["direct_udp_capture_delta"],
                               "tcp": unknown_tcp_phase["vpn_proxy_capture_delta"]},
            "unsupported": unsupported_capability["declared_capability"] == "unsupported",
            "unsupported_packet": {"endpoint_udp_delta": unsupported_udp_phase["endpoint_udp_observation_delta"],
                                   "core": unsupported_udp_phase["core_counter_delta"],
                                   "direct_udp_capture_delta": unsupported_udp_phase["direct_udp_capture_delta"],
                                   "tcp": unsupported_tcp_phase["vpn_proxy_capture_delta"]},
            "endpoint_unavailable": {"endpoint_udp_delta": unavailable_udp_phase["endpoint_udp_observation_delta"],
                                      "core": unavailable_udp_phase["core_counter_delta"],
                                      "direct_udp_capture_delta": unavailable_udp_phase["direct_udp_capture_delta"]},
            "empty_pool": {"native": empty_pool_native,
                               "endpoint_udp_delta": empty_pool_phase["endpoint_udp_observation_delta"],
                               "core": empty_pool_phase["core_counter_delta"],
                               "tcp": empty_pool_tcp_phase["core_counter_delta"],
                               "tcp_capture": empty_pool_tcp_phase["capture_delta"],
                               "direct_udp_capture_delta": empty_pool_phase["direct_udp_capture_delta"],
                               "selection_stable": (
                                   fence_before_empty_probe.get("active_server_id")
                                   == fence_after_empty_probe.get("active_server_id")
                                   and fence_before_empty_probe.get("provenance_server_id")
                                   == fence_after_empty_probe.get("provenance_server_id")
                                   and fence_before_empty_probe.get("revision")
                                   == fence_after_empty_probe.get("revision")),
                           },
        },
        "emergency_direct": {"flows": emergency_flows, "core_counters": core_emergency,
                              "mihomo_path": emergency_phase["mihomo_path"],
                              "observations": emergency_phase["observations"],
                              "egress_guard": emergency_phase["leak"],
                              "router_guard": emergency_phase["router_guard"]},
        "vpn_after_reentry": {"vless_endpoint_packets": reentry_flow,
                               "direct_client_leaks": reentry_direct_leaks,
                               "core_counters": core_reentry,
                               "mihomo_path": reentry_phase["mihomo_path"],
                               "observations": reentry_phase["observations"],
                               "egress_guard": reentry_phase["leak"],
                               "router_guard": reentry_phase["router_guard"]},
        "vpn_after_selector_change": selector_packet_diagnostic,
    })
