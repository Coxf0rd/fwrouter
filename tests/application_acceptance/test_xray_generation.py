from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import sqlite3
import stat
import threading
import urllib.error
from pathlib import Path
from queue import Queue
from typing import Any
from urllib.parse import urlsplit
from uuid import NAMESPACE_DNS, uuid5

import yaml

from .http_support import http_json
from .synthetic_subscription import SyntheticSubscriptionSource, synthetic_vless_profile
from .xray_support import (
    active_identities,
    assert_applied_bindings,
    assert_mihomo_launch_matches_active,
    assert_native_matches_active,
    await_job,
    enable_vpn_from_synthetic_subscription,
    loaded_identities,
    native_config_bytes,
    read_subscription_rows,
)


_PROVIDER_WORKER_LOG_PREFIX = "FWROUTER_ACCEPTANCE_PROVIDER_VERIFY_EXCEPTION "
_PROVIDER_WORKER_LOG_MAX_FILES = 16
_PROVIDER_WORKER_LOG_MAX_APPEND_BYTES = 64 * 1024


def current_xray_subject_identity_pairs(database_path: Path) -> set[tuple[str, str]]:
    """Read the canonical active Xray subject identities from a synthetic DB."""
    connection = sqlite3.connect(database_path, timeout=5)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT json_extract(metadata_json, '$.detail.client_uuid') AS client_uuid, "
            "json_extract(metadata_json, '$.detail.client_id') AS client_id, "
            "json_extract(metadata_json, '$.detail.email') AS email "
            "FROM subjects WHERE implementation_kind='xray' AND is_active=1 AND is_deleted=0 "
            "AND COALESCE(json_extract(metadata_json, '$.detail.enabled'), 1)=1 "
            "ORDER BY subject_id"
        ).fetchall()
    finally:
        connection.close()
    pairs = {
        (str(row["client_uuid"] or row["client_id"] or "").strip(), str(row["email"] or "").strip())
        for row in rows
    }
    assert len(pairs) == len(rows), "canonical Xray subject identities are incomplete or duplicated"
    assert all(client_id and email for client_id, email in pairs), "canonical Xray subject identity is incomplete"
    return pairs


def _synthetic_table_fingerprints(database_path: Path) -> dict[str, tuple[int, str]]:
    """Hash durable subscription identity/profile rows without exposing values."""
    tables = (
        "subscription_accounts", "subscription_clients", "subscription_profile_snapshots",
        "servers", "subscription_server_memberships", "subscription_node_identities",
        "subscription_node_aliases", "subscription_node_credentials",
        "subscription_node_identity_transitions", "subscription_node_credential_history",
    )

    def json_value(value):
        if isinstance(value, bytes):
            return {"blob_length": len(value), "sha256": hashlib.sha256(value).hexdigest()}
        return value

    connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True, timeout=3)
    try:
        connection.execute("PRAGMA query_only=ON")
        result = {}
        for table in tables:
            rows = connection.execute(f'SELECT * FROM "{table}"').fetchall()
            encoded = sorted(json.dumps([json_value(value) for value in row], ensure_ascii=False,
                                        sort_keys=True, separators=(",", ":")) for row in rows)
            payload = "\n".join(encoded).encode("utf-8")
            result[table] = (len(rows), hashlib.sha256(payload).hexdigest())
        return result
    finally:
        connection.close()


def _synthetic_file_tree_fingerprint(
    root: Path, *, exclude_relative_paths: tuple[str, ...] = (),
) -> tuple[tuple[str, int, str], ...]:
    """Fingerprint a bounded owned test tree; return no artifact contents."""
    try:
        root_info = root.lstat()
    except FileNotFoundError:
        return ()
    assert stat.S_ISDIR(root_info.st_mode) and not stat.S_ISLNK(root_info.st_mode)
    entries = []
    total_bytes = 0
    for path in sorted(root.rglob("*")):
        info = path.lstat()
        assert not stat.S_ISLNK(info.st_mode), f"synthetic state tree contains symlink: {path.name}"
        if stat.S_ISDIR(info.st_mode):
            continue
        assert stat.S_ISREG(info.st_mode), f"synthetic state tree contains non-file: {path.name}"
        relative_path = path.relative_to(root).as_posix()
        if relative_path in exclude_relative_paths:
            continue
        total_bytes += info.st_size
        assert len(entries) < 256 and total_bytes <= 32 * 1024 * 1024, "synthetic state tree exceeded audit bound"
        entries.append((relative_path, info.st_size,
                        hashlib.sha256(path.read_bytes()).hexdigest()))
    return tuple(entries)


def _synthetic_file_fingerprint(path: Path) -> str | None:
    """Hash one known suite-owned state artifact without exposing its contents."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    assert stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode)
    assert info.st_size <= 8 * 1024 * 1024, f"synthetic artifact exceeded bound: {path.name}"
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _snapshot_provider_worker_logs(root):
    """Capture identity and byte offsets for this suite-owned worker log set."""
    root_info = root.lstat()
    assert not stat.S_ISLNK(root_info.st_mode) and stat.S_ISDIR(root_info.st_mode)
    paths = sorted(root.glob("uvicorn-*.log"))
    assert len(paths) <= _PROVIDER_WORKER_LOG_MAX_FILES
    snapshot = {}
    for path in paths:
        assert re.fullmatch(r"uvicorn-[0-9]{1,5}\.log", path.name), path.name
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            info = os.fstat(fd)
            assert stat.S_ISREG(info.st_mode), path.name
            snapshot[path.name] = (info.st_dev, info.st_ino, info.st_size)
        finally:
            os.close(fd)
    return (root_info.st_dev, root_info.st_ino), snapshot


def _provider_worker_records_since(root, snapshot):
    """Read only bounded bytes appended since the pre-action log snapshot."""
    root_identity, prior_logs = snapshot
    root_info = root.lstat()
    assert (not stat.S_ISLNK(root_info.st_mode) and stat.S_ISDIR(root_info.st_mode)
            and (root_info.st_dev, root_info.st_ino) == root_identity)
    paths = sorted(root.glob("uvicorn-*.log"))
    assert len(paths) <= _PROVIDER_WORKER_LOG_MAX_FILES
    current_names = {path.name for path in paths}
    assert set(prior_logs) <= current_names, "an existing worker log disappeared or rotated"
    records = []
    for path in paths:
        assert re.fullmatch(r"uvicorn-[0-9]{1,5}\.log", path.name), path.name
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            info = os.fstat(fd)
            assert stat.S_ISREG(info.st_mode), path.name
            prior = prior_logs.get(path.name)
            if prior is None:
                offset = 0  # A newly created suite-owned worker log belongs to this interval.
            else:
                assert (info.st_dev, info.st_ino) == prior[:2], "an existing worker log was replaced"
                offset = prior[2]
                assert info.st_size >= offset, "an existing worker log was truncated"
            append_size = info.st_size - offset
            assert append_size <= _PROVIDER_WORKER_LOG_MAX_APPEND_BYTES, "worker log append exceeded its bound"
            payload = os.pread(fd, append_size, offset)
            assert len(payload) == append_size, "worker log append changed during bounded read"
        finally:
            os.close(fd)
        for line in payload.splitlines():
            if not line.startswith(_PROVIDER_WORKER_LOG_PREFIX.encode("ascii")):
                continue
            try:
                record = json.loads(line[len(_PROVIDER_WORKER_LOG_PREFIX):])
            except (UnicodeError, json.JSONDecodeError):
                continue
            if isinstance(record, dict):
                records.append(record)
    return records


def test_email_client_generation_persists_and_matches_native_after_worker_restart(acceptance_stack):
    """Email client creation must persist profile intent and converge generated/native state."""
    stack = acceptance_stack
    token = "phase-c-profile@acceptance.invalid"
    active_path = stack["state"] / "xray" / "config.json"
    code, response = http_json(
        f"{stack['api']}/xray/clients", method="POST",
        payload={"alias": "phase-c-profile", "email": token, "requested_by": "hosted-acceptance", "allow_blocked_egress": True},
    )
    assert code == 200 and response.get("ok") is True, response
    job = await_job(stack["api"], response)
    assert job.get("status") == "success", job
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    client = (result.get("xray_client") or {}).get("client") or {}
    client_id = str(client.get("client_id") or "")
    assert client_id and client.get("email") == token, job

    rows = read_subscription_rows(stack["state"], token)
    assert len(rows["accounts"]) == 1 and rows["accounts"][0]["enabled"] == 1, rows
    assert len(rows["clients"]) == 1 and rows["clients"][0]["enabled"] == 1, rows
    assert rows["snapshots"] and rows["snapshots"][0]["runtime_verified_at"], rows
    persisted_nodes = json.loads(rows["snapshots"][0]["nodes_json"])
    assert isinstance(persisted_nodes, list) and persisted_nodes, rows
    generated_pairs = {
        (str(node.get("client_uuid") or ""), str(node.get("client_email") or ""))
        for node in persisted_nodes if isinstance(node, dict)
    }
    assert generated_pairs and generated_pairs.issubset(set(active_identities(active_path))), {
        "generated": sorted(generated_pairs), "active": active_identities(active_path),
    }
    assert generated_pairs.issubset(set(loaded_identities(stack["native"])))
    assert_applied_bindings(stack["state"], generated_pairs)
    assert_native_matches_active(stack["native"], active_path)
    assert_mihomo_launch_matches_active(stack["native"], stack["state"] / "generated" / "mihomo" / "config.yaml")

    # A repeated API request resolves the same email identity and must not queue
    # a second create or change the generated/native generation.
    before = active_path.read_bytes()
    reload_count_before_repeat = stack["native"].reload_calls
    again_code, again = http_json(
        f"{stack['api']}/xray/clients", method="POST",
        payload={"alias": "phase-c-profile", "email": token, "requested_by": "hosted-acceptance", "allow_blocked_egress": True},
    )
    assert again_code == 200 and again.get("ok") is True, again
    assert (again.get("data", {}).get("xray_client") or {}).get("stage") == "existing", again
    assert active_path.read_bytes() == before
    assert stack["native"].reload_calls == reload_count_before_repeat

    stack["worker"].terminate()
    stack["worker"].wait(timeout=5)
    restarted_worker, restarted_api = stack["start_worker"]()
    stack["worker"], stack["api"] = restarted_worker, restarted_api
    reload_code, reload_response = http_json(
        f"{restarted_api}/xray/reload", method="POST", payload={"requested_by": "hosted-acceptance"},
    )
    assert reload_code == 200 and reload_response.get("ok") is True, reload_response
    assert_native_matches_active(stack["native"], active_path)


def test_email_generation_reload_failure_keeps_desired_candidate_and_last_good_loaded_identity(acceptance_stack):
    """A reload fault remains failed while desired bytes and loaded bytes stay distinguishable."""
    stack = acceptance_stack
    active_path = stack["state"] / "xray" / "config.json"
    before = active_path.read_bytes()
    from .xray_support import loaded_identities
    before_loaded = loaded_identities(stack["native"])
    stack["native"].fail_next_reload()
    code, response = http_json(
        f"{stack['api']}/xray/clients", method="POST",
        payload={"alias": "phase-c-reload-failure", "email": "reload-failure@acceptance.invalid",
                 "requested_by": "hosted-acceptance", "allow_blocked_egress": True},
    )
    job_ref = response.get("data", {}).get("job") if isinstance(response.get("data"), dict) else None
    assert code == 200 and isinstance(job_ref, dict) and bool(str(job_ref.get("job_id") or "")), response
    job = await_job(stack["api"], response)
    assert job.get("status") == "failed", job
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    client_result = result.get("xray_client") if isinstance(result.get("xray_client"), dict) else {}
    assert client_result.get("ok") is False, job
    desired = json.loads(active_path.read_text(encoding="utf-8"))
    desired_clients = next(item for item in desired["inbounds"] if item.get("tag") == "vless-ws")["settings"]["clients"]
    assert any(item.get("email") == "reload-failure@acceptance.invalid" for item in desired_clients), {
        "before_sha256": __import__("hashlib").sha256(before).hexdigest(),
        "desired_clients": desired_clients,
    }
    assert loaded_identities(stack["native"]) == before_loaded, "failed generation changed actual HandlerService identities"
    assert (token_uuid(job), "reload-failure@acceptance.invalid") not in set(loaded_identities(stack["native"])), job


def test_unverified_subscription_inventory_stays_pending_across_startup_until_native_retry(acceptance_stack):
    """Pending inventory stays blocked across startup; proof-only legacy admission resumes only by explicit owner action after fresh isolated Xray HandlerService and Mihomo proof, preserving 78 synthetic identities, durable mappings, dataplane state and native incarnations. Self-checking tripwires block Xray/Mihomo/Core dataplane mutation and unexpected HTTP; provider and synthetic-source request counts stay unchanged."""
    stack = acceptance_stack
    state, native = stack["state"], stack["native"]

    # Keep this generation/recovery scenario independent of external VPN
    # reachability. Establish the supported public routing intent before any
    # native baselines or reload counters are captured: direct traffic remains
    # the applied global policy, while fixed server selection without a target
    # explicitly skips post-refresh VPN auto-selection.
    for routing_payload in (
        {"mode": "direct", "requested_by": "hosted-acceptance", "run_now": True},
        {"server_mode": "fixed", "requested_by": "hosted-acceptance", "run_now": True},
    ):
        code, response = http_json(
            f"{stack['api']}/routing/global", method="POST", payload=routing_payload,
        )
        assert code == 200 and response.get("ok") is True, {
            "status": code, "ok": response.get("ok"),
            "error_code": (response.get("error") or {}).get("code")
            if isinstance(response.get("error"), dict) else None,
        }
        job = await_job(stack["api"], response)
        assert job.get("status") == "success", job

    def assert_generation_routing(api: str) -> None:
        code, response = http_json(f"{api}/routing/global")
        assert code == 200 and response.get("ok") is True, {
            "status": code, "ok": response.get("ok"),
        }
        routing = ((response.get("data") or {}).get("routing") or {})
        assert routing.get("desired_mode") == "direct", routing
        assert routing.get("applied_mode") == "direct", routing
        assert routing.get("server_mode") == "fixed", routing
        assert routing.get("desired_fixed_server_id") is None, routing
        assert routing.get("applied_fixed_server_id") is None, routing

    def assert_no_fixed_target_auto_select(result: dict) -> None:
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        auto_select = data.get("auto_select")
        assert isinstance(auto_select, dict), {
            "data_keys": sorted(data), "auto_select_type": type(auto_select).__name__,
        }
        assert auto_select.get("status") == "skipped_no_fixed_target", auto_select

    assert_generation_routing(stack["api"])
    active_xray = state / "xray" / "config.json"
    active_mihomo = state / "generated" / "mihomo" / "config.yaml"

    # Seed an exact, isolated 78-identity native baseline. These deterministic
    # fixtures are local to this acceptance worker and never sourced from prod.
    xray_config = json.loads(active_xray.read_text(encoding="utf-8"))
    inbound = next(item for item in xray_config["inbounds"] if item.get("tag") == "vless-ws")
    baseline_clients = [
        {
            "id": str(uuid5(NAMESPACE_DNS, f"pending-inventory-preserved-{index}")),
            "email": f"pending-inventory-preserved-{index}@acceptance.invalid",
            "level": 0,
        }
        for index in range(78)
    ]
    inbound["settings"]["clients"] = baseline_clients
    active_xray.write_text(json.dumps(xray_config, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    native.restart_xray_child()
    assert_native_matches_active(native, active_xray)
    baseline_pairs = {(str(row["id"]), str(row["email"])) for row in baseline_clients}
    baseline_fingerprint = hashlib.sha256(
        json.dumps(sorted(baseline_pairs), separators=(",", ":")).encode()
    ).hexdigest()
    assert set(loaded_identities(native)) == baseline_pairs
    assert len(loaded_identities(native)) == 78

    def profile(*, changed_short_ids: bool, add_member: bool) -> dict:
        suffix = "a1b2c3d4e5f607" if changed_short_ids else ""
        return synthetic_vless_profile(short_id_suffix=suffix, include_third_member=add_member)

    def add_profile_member(value: dict, index: int) -> dict:
        """Create strict ordered growth while preserving every existing outbound."""
        grown = json.loads(json.dumps(value))
        prior = next(
            item for item in grown["outbounds"]
            if isinstance(item, dict) and item.get("tag") == "synthetic-member-2"
        )
        added = json.loads(json.dumps(prior))
        added["tag"] = f"synthetic-member-{index}"
        vnext = added["settings"]["vnext"][0]
        vnext["address"] = f"synthetic-member-{index}.example.invalid"
        vnext["users"][0]["id"] = str(uuid5(NAMESPACE_DNS, f"pending-generation-member-{index}"))
        reality = added["streamSettings"]["realitySettings"]
        reality["serverName"] = f"synthetic-member-{index}.example.invalid"
        reality["shortId"] = f"{index + 1:02x}" * 8
        service_index = next(
            index for index, outbound in enumerate(grown["outbounds"])
            if isinstance(outbound, dict) and outbound.get("protocol") in {"freedom", "blackhole", "dns"}
        )
        grown["outbounds"].insert(service_index, added)
        return grown

    def content_id(value: dict) -> str:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        return "sub:" + hashlib.sha256(raw).hexdigest()

    def import_source(url: str) -> dict:
        code, result = http_json(
            f"{stack['api']}/subscription", method="POST",
            payload={"urls": [url], "requested_by": "hosted-acceptance-synthetic"},
            timeout=90,
        )
        assert code == 200, {"status": code, "ok": result.get("ok"), "error": result.get("error")}
        return result

    def generation_state(api: str) -> dict:
        code, result = http_json(f"{api}/__acceptance/subscription/native-generation")
        assert code == 200 and result.get("ok") is True, {
            "status": code, "ok": result.get("ok"), "error_code": result.get("error_code"),
        }
        value = result.get("state")
        assert isinstance(value, dict), result
        return value

    def active_profile_runtime_name() -> str:
        # Read only this acceptance worker's synthetic database. This avoids
        # depending on API DTO redaction/shape for internal runtime metadata.
        connection = sqlite3.connect(state / "fwrouter.db", timeout=5)
        try:
            rows = connection.execute(
                "SELECT raw_json FROM servers WHERE server_name=? AND inventory_state='active'",
                ("acceptance-pending-generation-profile",),
            ).fetchall()
        finally:
            connection.close()
        matches = []
        for row in rows:
            raw = json.loads(row[0])
            topology = raw.get("_fwrouter_topology") if isinstance(raw, dict) else None
            if isinstance(topology, dict) and topology.get("kind") == "logical_profile":
                name = str(raw.get("_fwrouter_runtime_name") or "").strip()
                if name:
                    matches.append(name)
        assert len(matches) == 1, {"owned_logical_profile_count": len(matches)}
        return matches[0]

    def logical_profile_group(config: dict, runtime_name: str, expected_probe_url: str) -> dict:
        groups = [
            item for item in config.get("proxy-groups", [])
            if isinstance(item, dict) and item.get("name") == runtime_name
        ]
        assert len(groups) == 1, {"owned_logical_group_count": len(groups)}
        group = groups[0]
        assert group.get("type") == "fallback"
        probe = urlsplit(str(group.get("url") or ""))
        expected_probe = urlsplit(expected_probe_url)
        assert (expected_probe.scheme == "http" and expected_probe.hostname is not None
                and expected_probe.port is not None and expected_probe.path == "/generate_204"
                and not expected_probe.username and not expected_probe.password
                and not expected_probe.query and not expected_probe.fragment), {
            "expected_probe_is_owned": False,
        }
        assert probe == expected_probe, {"probe_matches_owned_stack_endpoint": probe == expected_probe}
        return group

    def expected_logical_probe_url() -> str:
        profile = stack.get("profile")
        assert isinstance(profile, dict)
        if profile.get("profile") == "hosted-kernel-packet":
            return "http://203.0.113.53:9080/generate_204"
        if profile.get("profile") == "hosted-kernel-dataplane":
            probe_url = stack.get("mihomo_probe_url")
            assert isinstance(probe_url, str) and probe_url
            return probe_url
        bridge = stack.get("provider_bridge")
        assert bridge is not None
        return f"{bridge.base_url}/generate_204"

    def last_good_mihomo_snapshot() -> dict[str, str]:
        directory = state / "last-good" / "mihomo"
        if not directory.is_dir():
            return {}
        return {
            item.name: hashlib.sha256(item.read_bytes()).hexdigest()
            for item in sorted(directory.iterdir()) if item.is_file() and not item.is_symlink()
        }

    source = SyntheticSubscriptionSource(profile(changed_short_ids=False, add_member=False))
    try:
        initial = import_source(source.url)
        assert initial.get("ok") is True, {
            "ok": initial.get("ok"),
            "error_code": (initial.get("error") or {}).get("code")
            if isinstance(initial.get("error"), dict) else None,
        }
        # The harness globally keeps VPN disabled so unrelated tests preserve
        # blocked-egress semantics. This admission contract specifically
        # requires a managed native owner, so establish it through the normal
        # public lifecycle using only the saved suite-owned loopback source.
        enable_vpn_from_synthetic_subscription(stack["api"], state, source.url)
        assert_generation_routing(stack["api"])
        assert_native_matches_active(native, active_xray)
        assert_mihomo_launch_matches_active(native, active_mihomo)
        enabled_pairs = set(loaded_identities(native))
        assert enabled_pairs >= baseline_pairs and sum(pair in baseline_pairs for pair in enabled_pairs) == 78
        assert_no_fixed_target_auto_select(initial)
        applied = generation_state(stack["api"])
        assert applied.get("status") == "verified"
        assert applied.get("candidate_digest") == applied.get("verified_digest")
        assert set(loaded_identities(native)) >= baseline_pairs
        assert sum(1 for pair in loaded_identities(native) if pair in baseline_pairs) == 78

        runtime_name_a = active_profile_runtime_name()
        owned_probe_url = expected_logical_probe_url()
        mihomo_before = active_mihomo.read_bytes()
        xray_before = active_xray.read_bytes()
        config_a = yaml.safe_load(mihomo_before)
        group_a = logical_profile_group(config_a, runtime_name_a, owned_probe_url)
        members_a = {str(item) for item in group_a.get("proxies", [])}
        assert len(members_a) == 2
        assert_mihomo_launch_matches_active(native, active_mihomo)
        last_good_before_failure = last_good_mihomo_snapshot()
        mihomo_incarnation = (native.mihomo_process.pid, native.mihomo_started_at)
        reloads_before_failure = native.reload_calls

        source.replace(profile(changed_short_ids=True, add_member=True))
        # The first observation persists B but must stop at the reviewed
        # identity-migration boundary. It is not the injected native-stage
        # failure below; assert the distinct reason so the one-shot hook cannot
        # be accidentally consumed by an earlier lifecycle guard.
        identity_blocked = import_source(source.url)
        identity_error = identity_blocked.get("error")
        assert identity_blocked.get("ok") is False
        assert (identity_error or {}).get("code") == "XRAY_VPN_AUTO_RECONCILE_EXCEPTION"
        assert (identity_error or {}).get("message") == "IDENTITY_MIGRATION_REQUIRED"
        pending = generation_state(stack["api"])
        assert pending.get("status") == "pending"
        assert pending.get("candidate_digest") and pending.get("verified_digest")
        assert pending.get("candidate_digest") != pending.get("verified_digest")
        assert type(pending.get("candidate_revision")) is int
        assert type(pending.get("verified_revision")) is int
        assert active_mihomo.read_bytes() == mihomo_before
        assert active_xray.read_bytes() == xray_before
        assert native.reload_calls == reloads_before_failure
        assert (native.mihomo_process.pid, native.mihomo_started_at) == mihomo_incarnation
        assert last_good_mihomo_snapshot() == last_good_before_failure
        assert_native_matches_active(native, active_xray)
        assert set(loaded_identities(native)) >= baseline_pairs
        assert sum(1 for pair in loaded_identities(native) if pair in baseline_pairs) == 78

        old_worker = stack["worker"]
        old_worker.terminate()
        old_worker.wait(timeout=5)
        restarted_worker, restarted_api = stack["start_worker"]()
        stack["worker"], stack["api"] = restarted_worker, restarted_api
        assert_generation_routing(restarted_api)

        assert generation_state(restarted_api) == pending
        recovery_code, recovery = http_json(
            f"{restarted_api}/__acceptance/xray/startup-generation-recovery",
            method="POST", timeout=75,
        )
        assert recovery_code == 200 and recovery.get("ok") is False, {
            "status": recovery_code, "ok": recovery.get("ok"),
            "status_name": recovery.get("status"), "error_code": recovery.get("error_code"),
        }
        assert recovery.get("native_generation", {}).get("status") == "pending"
        assert generation_state(restarted_api) == pending
        assert active_mihomo.read_bytes() == mihomo_before
        assert active_xray.read_bytes() == xray_before
        assert native.reload_calls == reloads_before_failure
        assert (native.mihomo_process.pid, native.mihomo_started_at) == mihomo_incarnation
        assert last_good_mihomo_snapshot() == last_good_before_failure
        assert_native_matches_active(native, active_xray)
        assert_mihomo_launch_matches_active(native, active_mihomo)
        assert set(loaded_identities(native)) >= baseline_pairs
        assert sum(1 for pair in loaded_identities(native) if pair in baseline_pairs) == 78

        # A changed Reality short-id is a transport-identity change, not
        # compatible profile growth. Keep the failed candidate fenced across
        # restart, then use the existing exact-context identity review flow;
        # it must not write native state or claim the inventory verified.
        assert not (state / "xray" / ".generation" / "generation-checkpoint.json").exists(), (
            "pre-stage identity-migration refusal unexpectedly created a generation checkpoint"
        )
        context_code, context = http_json(
            f"{restarted_api}/__acceptance/xray/identity-context",
        )
        assert context_code == 200 and context.get("ok") is True
        old_server_id = content_id(profile(changed_short_ids=False, add_member=False))
        new_profile = profile(changed_short_ids=True, add_member=True)
        new_server_id = content_id(new_profile)
        connection = sqlite3.connect(state / "fwrouter.db", timeout=5)
        try:
            identity_row = connection.execute(
                "SELECT i.revision FROM subscription_node_aliases a "
                "JOIN subscription_node_identities i ON i.identity_id=a.identity_id "
                "WHERE a.server_id=?", (old_server_id,),
            ).fetchone()
        finally:
            connection.close()
        assert identity_row is not None, "synthetic source identity was not durably registered"
        manifest = {
            "version": 1,
            "expected_selection_revision": context["selection_revision"],
            "expected_identity_digest": context["identity_digest"],
            "mappings": [{
                "old_server_id": old_server_id,
                "new_server_id": new_server_id,
                "expected_identity_revision": int(identity_row[0]),
                "old_profile": profile(changed_short_ids=False, add_member=False),
                "new_profile": new_profile,
            }],
        }

        def identity_cli(payload: dict) -> dict:
            code, result = http_json(
                f"{restarted_api}/__acceptance/xray/identity-cli", method="POST",
                payload=payload, timeout=90,
            )
            assert code == 200 and result.get("stderr_empty") is True, {
                "status": code, "ok": result.get("ok"), "error_code": result.get("error_code"),
            }
            assert result.get("ok") is True, {
                "exit_code": result.get("exit_code"),
                "error_code": (result.get("receipt") or {}).get("error_code"),
            }
            return result.get("receipt") or {}

        dry = identity_cli({"action": "dry-run", "manifest": manifest})
        assert dry.get("fresh_generation_required") is True
        assert dry.get("native_applied") is False and dry.get("changed_count") == 1
        apply = identity_cli({
            "action": "apply", "manifest": manifest,
            "expected_plan_sha256": dry.get("plan_sha256"),
        })
        assert apply.get("fresh_generation_required") is True
        assert apply.get("native_applied") is False and apply.get("changed_count") == 1
        reviewed_pending = generation_state(restarted_api)
        assert reviewed_pending.get("status") == "pending"
        assert reviewed_pending.get("candidate_digest") != reviewed_pending.get("verified_digest")
        assert active_mihomo.read_bytes() == mihomo_before
        assert active_xray.read_bytes() == xray_before
        assert native.reload_calls == reloads_before_failure
        assert set(loaded_identities(native)) >= baseline_pairs
        assert sum(1 for pair in loaded_identities(native) if pair in baseline_pairs) == 78

        # Now the reviewed candidate can reach the native staging boundary.
        # The one-shot fault must be consumed there, and report its own exact
        # error rather than inheriting the earlier identity guard failure.
        armed_code, armed = http_json(
            f"{restarted_api}/__acceptance/xray/pre-native-stage-failure/arm", method="POST",
        )
        assert armed_code == 200 and armed.get("ok") is True and armed.get("armed") is True, {
            "status": armed_code, "ok": armed.get("ok"),
        }
        stage_failed = import_source(source.url)
        stage_data = stage_failed.get("data") if isinstance(stage_failed.get("data"), dict) else {}
        stage_apply = stage_data.get("refresh") if isinstance(stage_data.get("refresh"), dict) else {}
        stage_error = stage_apply.get("error") if isinstance(stage_apply.get("error"), dict) else {}
        stage_reconcile = stage_apply.get("reconcile") if isinstance(stage_apply.get("reconcile"), dict) else {}
        assert stage_failed.get("ok") is False
        assert stage_error.get("code") == "ACCEPTANCE_PRE_NATIVE_STAGE_FAILURE"
        assert stage_reconcile.get("error_code") == "ACCEPTANCE_PRE_NATIVE_STAGE_FAILURE"
        assert not (state / "run" / "fail-before-native-stage-once").exists(), (
            "pre-native-stage fault hook was not consumed at the staging boundary"
        )
        assert not (state / "xray" / ".generation" / "generation-checkpoint.json").exists(), (
            "pre-native-stage failure unexpectedly created a generation checkpoint"
        )
        pending_after_stage_failure = generation_state(restarted_api)
        assert pending_after_stage_failure.get("status") == "pending"
        assert pending_after_stage_failure.get("candidate_digest") != pending_after_stage_failure.get("verified_digest")
        assert active_mihomo.read_bytes() == mihomo_before
        assert active_xray.read_bytes() == xray_before
        assert native.reload_calls == reloads_before_failure
        assert (native.mihomo_process.pid, native.mihomo_started_at) == mihomo_incarnation
        assert last_good_mihomo_snapshot() == last_good_before_failure
        assert_native_matches_active(native, active_xray)
        assert_mihomo_launch_matches_active(native, active_mihomo)
        assert set(loaded_identities(native)) >= baseline_pairs
        assert sum(1 for pair in loaded_identities(native) if pair in baseline_pairs) == 78

        # A real native-stage failure remains pending across a worker restart;
        # automatic startup reconciliation must decline without changing native.
        old_worker = stack["worker"]
        old_worker.terminate()
        old_worker.wait(timeout=5)
        restarted_worker, restarted_api = stack["start_worker"]()
        stack["worker"], stack["api"] = restarted_worker, restarted_api
        assert generation_state(restarted_api) == pending_after_stage_failure
        recovery_code, recovery = http_json(
            f"{restarted_api}/__acceptance/xray/startup-generation-recovery",
            method="POST", timeout=75,
        )
        assert recovery_code == 200 and recovery.get("ok") is False, {
            "status": recovery_code, "ok": recovery.get("ok"),
            "status_name": recovery.get("status"), "error_code": recovery.get("error_code"),
        }
        assert recovery.get("native_generation", {}).get("status") == "pending"
        assert generation_state(restarted_api) == pending_after_stage_failure
        assert active_mihomo.read_bytes() == mihomo_before
        assert active_xray.read_bytes() == xray_before
        assert native.reload_calls == reloads_before_failure
        assert (native.mihomo_process.pid, native.mihomo_started_at) == mihomo_incarnation
        assert last_good_mihomo_snapshot() == last_good_before_failure
        assert_native_matches_active(native, active_xray)
        assert_mihomo_launch_matches_active(native, active_mihomo)
        assert set(loaded_identities(native)) >= baseline_pairs
        assert sum(1 for pair in loaded_identities(native) if pair in baseline_pairs) == 78

        # Re-fetch the same synthetic source through the normal public import
        # and native publication path after the reviewed identity transition.
        # The explicit review authorizes the identity continuity; this fresh
        # operation still has to prove the candidate through native readback.
        retried = import_source(source.url)
        assert retried.get("ok") is True, {
            "ok": retried.get("ok"),
            "error_code": (retried.get("error") or {}).get("code")
            if isinstance(retried.get("error"), dict) else None,
        }
        assert_no_fixed_target_auto_select(retried)
        verified = generation_state(restarted_api)
        assert verified.get("status") == "verified"
        assert verified.get("candidate_digest") == verified.get("verified_digest")
        assert verified.get("verified_digest") == pending_after_stage_failure.get("candidate_digest")
        runtime_name_b = active_profile_runtime_name()
        config_b = yaml.safe_load(active_mihomo.read_bytes())
        group_b = logical_profile_group(config_b, runtime_name_b, owned_probe_url)
        members_b = {str(item) for item in group_b.get("proxies", [])}
        assert len(members_b) == 3
        assert members_b != members_a and members_a - members_b and members_b - members_a
        proxies = {
            str(item.get("name") or ""): item
            for item in config_b.get("proxies", []) if isinstance(item, dict)
        }
        assert members_b <= set(proxies)
        observed_short_ids = {
            str((proxy.get("reality-opts") or {}).get("short-id") or "")
            for name, proxy in proxies.items() if name in members_b
        }
        expected_short_ids = {
            f"{index + 1:02x}a1b2c3d4e5f607" for index in range(3)
        }
        assert expected_short_ids <= observed_short_ids, {
            "expected_count": len(expected_short_ids),
            "observed_count": len(observed_short_ids),
        }
        final_identities = loaded_identities(native)
        assert set(final_identities) >= baseline_pairs
        assert sum(1 for pair in final_identities if pair in baseline_pairs) == 78
        assert_native_matches_active(native, active_xray)
        assert_mihomo_launch_matches_active(native, active_mihomo)
        assert hashlib.sha256(json.dumps(sorted(baseline_pairs), separators=(",", ":")).encode()).hexdigest() == baseline_fingerprint

        # Exercise a crash-window after real native apply/readback and profile
        # publication, but before the durable inventory marker is promoted.
        before_checkpoint_mihomo = active_mihomo.read_bytes()
        profile_c = add_profile_member(new_profile, 3)
        source.replace(profile_c)
        armed_code, armed = http_json(
            f"{restarted_api}/__acceptance/xray/inventory-promotion-failure/arm", method="POST",
        )
        assert armed_code == 200 and armed.get("armed") is True, {
            "status": armed_code, "ok": armed.get("ok"),
        }
        promotion_failed = import_source(source.url)
        assert promotion_failed.get("ok") is False, {
            "ok": promotion_failed.get("ok"),
            "stage": (promotion_failed.get("data") or {}).get("stage")
            if isinstance(promotion_failed.get("data"), dict) else None,
        }
        checkpoint_path = state / "xray" / ".generation" / "generation-checkpoint.json"
        assert checkpoint_path.is_file(), "promotion failure closed the native generation checkpoint"
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        pending_after_apply = generation_state(restarted_api)
        binding = checkpoint.get("subscription_inventory_generation")
        assert pending_after_apply.get("status") == "pending"
        assert isinstance(binding, dict)
        assert binding.get("digest") == pending_after_apply.get("candidate_digest")
        assert binding.get("revision") == pending_after_apply.get("candidate_revision")
        assert type(binding.get("revision")) is int
        assert checkpoint.get("phase") == "snapshots_published", checkpoint.get("phase")
        assert active_mihomo.read_bytes() != before_checkpoint_mihomo
        assert_native_matches_active(native, active_xray)
        assert_mihomo_launch_matches_active(native, active_mihomo)
        assert set(loaded_identities(native)) >= baseline_pairs
        assert sum(1 for pair in loaded_identities(native) if pair in baseline_pairs) == 78

        old_worker = stack["worker"]
        old_worker.terminate()
        old_worker.wait(timeout=5)
        restarted_worker, restarted_api = stack["start_worker"]()
        stack["worker"], stack["api"] = restarted_worker, restarted_api
        assert generation_state(restarted_api).get("status") == "pending"
        recovery_reloads = native.reload_calls
        recovery_incarnation = (native.mihomo_process.pid, native.mihomo_started_at)
        checkpoint_recovery_code, checkpoint_recovery = http_json(
            f"{restarted_api}/__acceptance/xray/reconcile",
            method="POST", payload={"direct_checkpoint": True}, timeout=75,
        )
        assert checkpoint_recovery_code == 200 and checkpoint_recovery.get("ok") is True, {
            "status": checkpoint_recovery_code,
            "ok": checkpoint_recovery.get("ok"),
            "recovered": checkpoint_recovery.get("recovered"),
            "reason": checkpoint_recovery.get("reason"),
        }
        recovered_generation = generation_state(restarted_api)
        assert recovered_generation.get("status") == "verified"
        assert recovered_generation.get("candidate_digest") == recovered_generation.get("verified_digest")
        assert recovered_generation.get("candidate_digest") == pending_after_apply.get("candidate_digest")
        assert not checkpoint_path.exists(), "freshly proven recovery left the generation checkpoint open"
        assert native.reload_calls == recovery_reloads + 1, (
            "checkpoint recovery must reload the owned Xray projection once for fresh native proof"
        )
        assert (native.mihomo_process.pid, native.mihomo_started_at) == recovery_incarnation
        assert_native_matches_active(native, active_xray)
        assert_mihomo_launch_matches_active(native, active_mihomo)
        assert set(loaded_identities(native)) >= baseline_pairs
        assert sum(1 for pair in loaded_identities(native) if pair in baseline_pairs) == 78

        # A newer persisted candidate invalidates the checkpoint's bound
        # digest/revision. Recovery must decline without touching native state.
        profile_d = add_profile_member(profile_c, 4)
        source.replace(profile_d)
        armed_code, armed = http_json(
            f"{restarted_api}/__acceptance/xray/inventory-promotion-failure/arm", method="POST",
        )
        assert armed_code == 200 and armed.get("armed") is True, {
            "status": armed_code, "ok": armed.get("ok"),
        }
        stale_candidate_apply = import_source(source.url)
        assert stale_candidate_apply.get("ok") is False
        assert checkpoint_path.is_file()
        stale_checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        stale_native_xray = active_xray.read_bytes()
        stale_native_mihomo = active_mihomo.read_bytes()
        stale_reload_count = native.reload_calls
        stale_incarnation = (native.mihomo_process.pid, native.mihomo_started_at)
        stale_binding = stale_checkpoint.get("subscription_inventory_generation")
        stale_pending = generation_state(restarted_api)
        assert isinstance(stale_binding, dict) and stale_binding.get("digest") == stale_pending.get("candidate_digest")
        stale_checkpoint_bytes = checkpoint_path.read_bytes()

        blocked_code, blocked_refresh = http_json(
            f"{restarted_api}/__acceptance/xray/identity-refresh", method="POST",
            payload={"profile": add_profile_member(profile_d, 5)},
        )
        assert blocked_code == 200 and blocked_refresh.get("ok") is False, {
            "status": blocked_code, "ok": blocked_refresh.get("ok"),
            "error_code": blocked_refresh.get("error_code"),
        }
        assert blocked_refresh.get("error_code") == "XRAY_GENERATION_CHECKPOINT_RECOVERY_REQUIRED"
        assert blocked_refresh.get("last_good_retained") is True
        assert generation_state(restarted_api) == stale_pending
        assert checkpoint_path.read_bytes() == stale_checkpoint_bytes
        assert active_xray.read_bytes() == stale_native_xray
        assert active_mihomo.read_bytes() == stale_native_mihomo
        assert native.reload_calls == stale_reload_count
        assert (native.mihomo_process.pid, native.mihomo_started_at) == stale_incarnation
        assert sum(1 for pair in loaded_identities(native) if pair in baseline_pairs) == 78

        # Resolve D through the owner while its durable checkpoint still matches.
        # Only after fresh native proof and promotion may ordinary refresh E run.
        recovery_code, recovered_d = http_json(
            f"{restarted_api}/__acceptance/xray/reconcile",
            method="POST", payload={"direct_checkpoint": True}, timeout=75,
        )
        assert recovery_code == 200 and recovered_d.get("ok") is True, {
            "status": recovery_code, "ok": recovered_d.get("ok"),
            "recovered": recovered_d.get("recovered"), "reason": recovered_d.get("reason"),
        }
        assert not checkpoint_path.exists()
        verified_d = generation_state(restarted_api)
        assert verified_d.get("status") == "verified"
        assert verified_d.get("candidate_digest") == stale_binding.get("digest")
        assert verified_d.get("verified_digest") == stale_binding.get("digest")
        assert_native_matches_active(native, active_xray)
        assert sum(1 for pair in loaded_identities(native) if pair in baseline_pairs) == 78

        profile_e = add_profile_member(profile_d, 5)
        refresh_code, refreshed = http_json(
            f"{restarted_api}/__acceptance/xray/identity-refresh", method="POST",
            payload={"profile": profile_e},
        )
        assert refresh_code == 200 and refreshed.get("ok") is True, {
            "status": refresh_code, "ok": refreshed.get("ok"),
            "error_code": refreshed.get("error_code"),
        }
        candidate_e = generation_state(restarted_api)
        assert candidate_e.get("status") == "pending"
        assert candidate_e.get("candidate_digest") != verified_d.get("candidate_digest")
        publish_code, published_e = http_json(
            f"{restarted_api}/__acceptance/xray/publish-pending-inventory",
            method="POST", payload={"include_vpn_auto": True}, timeout=75,
        )
        assert publish_code == 200 and published_e.get("ok") is True, {
            "status": publish_code, "ok": published_e.get("ok"),
            "error_code": published_e.get("error_code"),
        }
        verified_e = generation_state(restarted_api)
        assert verified_e.get("status") == "verified"
        assert verified_e.get("candidate_digest") == candidate_e.get("candidate_digest")
        assert verified_e.get("verified_digest") == candidate_e.get("candidate_digest")
        assert_native_matches_active(native, active_xray)
        assert set(loaded_identities(native)) >= baseline_pairs
        assert sum(1 for pair in loaded_identities(native) if pair in baseline_pairs) == 78
        identities_e = current_xray_subject_identity_pairs(state / "fwrouter.db")
        expected_e_identities = baseline_pairs | identities_e
        assert set(loaded_identities(native)) == expected_e_identities, {
            "expected_count": len(expected_e_identities),
            "actual_count": len(set(loaded_identities(native))),
        }
        assert set(active_identities(active_xray)) == expected_e_identities

        # Exercise the distinct crash window after durable inventory promotion
        # but before the generation checkpoint is closed. This must recover from
        # the committed current projection, never restore the old preimage.
        profile_f = add_profile_member(profile_e, 6)
        profile_tags_f = [
            str(item.get("tag") or "") for item in profile_f["outbounds"]
            if isinstance(item, dict) and str(item.get("tag") or "").startswith("synthetic-member-")
        ]
        assert profile_tags_f == [f"synthetic-member-{index}" for index in range(7)]
        source.replace(profile_f)
        armed_code, armed = http_json(
            f"{restarted_api}/__acceptance/xray/post-promotion-crash/arm", method="POST",
        )
        assert armed_code == 200 and armed.get("armed") is True, {
            "status": armed_code, "ok": armed.get("ok"),
        }
        xray_before_promotion_crash = active_xray.read_bytes()
        mihomo_before_promotion_crash = active_mihomo.read_bytes()
        reloads_before_promotion_crash = native.reload_calls
        try:
            http_json(
                f"{restarted_api}/subscription", method="POST",
                payload={"urls": [source.url], "requested_by": "hosted-acceptance-synthetic"},
                timeout=90,
            )
        except urllib.error.URLError as exc:
            assert isinstance(exc.reason, (
                ConnectionError, TimeoutError, http.client.RemoteDisconnected,
            )), {"transport_error_type": type(exc.reason).__name__}
        except http.client.RemoteDisconnected:
            pass
        else:
            raise AssertionError("post-promotion crash injection unexpectedly returned an HTTP response")
        old_worker = stack["worker"]
        assert old_worker.wait(timeout=5) == 86, "worker did not exit at the injected post-promotion crash"
        assert checkpoint_path.is_file(), "post-promotion interruption closed the generation checkpoint"
        committed_checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        committed_binding = committed_checkpoint.get("subscription_inventory_generation")
        assert committed_checkpoint.get("phase") == "snapshots_published"
        connection = sqlite3.connect(state / "fwrouter.db", timeout=5)
        try:
            rows = connection.execute(
                "SELECT value_json FROM settings WHERE key='subscriptions.native_generation.v1'"
            ).fetchall()
        finally:
            connection.close()
        assert len(rows) == 1
        committed_inventory = json.loads(rows[0][0])
        identities_f = current_xray_subject_identity_pairs(state / "fwrouter.db")
        expected_f_identities = baseline_pairs | identities_f
        assert committed_inventory.get("status") == "verified"
        assert committed_inventory.get("candidate_digest") == committed_inventory.get("verified_digest")
        assert isinstance(committed_binding, dict)
        assert committed_binding.get("digest") == committed_inventory.get("candidate_digest")
        assert committed_binding.get("revision") == committed_inventory.get("candidate_revision")
        assert active_xray.read_bytes() != xray_before_promotion_crash
        assert active_mihomo.read_bytes() != mihomo_before_promotion_crash
        assert_native_matches_active(native, active_xray)
        assert_mihomo_launch_matches_active(native, active_mihomo)
        assert identities_e <= identities_f
        assert set(active_identities(active_xray)) == expected_f_identities
        assert set(loaded_identities(native)) == expected_f_identities, {
            "expected_count": len(expected_f_identities),
            "actual_count": len(set(loaded_identities(native))),
        }
        assert set(loaded_identities(native)) >= baseline_pairs
        assert sum(1 for pair in loaded_identities(native) if pair in baseline_pairs) == 78
        assert native.reload_calls > reloads_before_promotion_crash

        restarted_worker, restarted_api = stack["start_worker"]()
        stack["worker"], stack["api"] = restarted_worker, restarted_api
        assert checkpoint_path.is_file(), "restart discarded the committed generation checkpoint"
        assert generation_state(restarted_api) == committed_inventory
        recovery_code, recovered_committed = http_json(
            f"{restarted_api}/__acceptance/xray/reconcile",
            method="POST", payload={"direct_checkpoint": True}, timeout=75,
        )
        assert recovery_code == 200 and recovered_committed.get("ok") is True, {
            "status": recovery_code, "ok": recovered_committed.get("ok"),
            "recovered": recovered_committed.get("recovered"),
            "reason": recovered_committed.get("reason"),
        }
        assert recovered_committed.get("recovered") == "current_projection_verified"
        assert not checkpoint_path.exists()
        verified_after_recovery = generation_state(restarted_api)
        assert verified_after_recovery == committed_inventory
        assert_native_matches_active(native, active_xray)
        assert_mihomo_launch_matches_active(native, active_mihomo)
        assert set(loaded_identities(native)) == expected_f_identities
        assert set(loaded_identities(native)) >= baseline_pairs
        assert sum(1 for pair in loaded_identities(native) if pair in baseline_pairs) == 78

        def controller_state_fingerprint() -> dict[str, tuple[int, str]]:
            observed = {}
            for path in ("/proxies/vpn-global", "/proxies/vpn-auto"):
                status, body = http_json(f"http://127.0.0.1:5200{path}")
                encoded = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
                observed[path] = (status, hashlib.sha256(encoded).hexdigest())
            return observed

        def selective_native_fingerprint() -> dict[str, Any]:
            mounted_xray, _launch = native_config_bytes(native)
            bindings_path = state / "xray" / "fwrouter-bindings.json"
            return {
                "xray_active": hashlib.sha256(active_xray.read_bytes()).hexdigest(),
                "xray_mounted": hashlib.sha256(mounted_xray).hexdigest(),
                "xray_loaded": hashlib.sha256(
                    json.dumps(loaded_identities(native), separators=(",", ":")).encode()
                ).hexdigest(),
                "xray_bindings": hashlib.sha256(bindings_path.read_bytes()).hexdigest(),
                "xray_last_good": _synthetic_file_tree_fingerprint(state / "last-good" / "xray"),
                "mihomo_active": hashlib.sha256(active_mihomo.read_bytes()).hexdigest(),
                "mihomo_generated": _synthetic_file_tree_fingerprint(
                    state / "generated" / "mihomo", exclude_relative_paths=("reconcile-state.json",),
                ),
                "mihomo_last_good": last_good_mihomo_snapshot(),
                "xray_incarnation": (native.process.pid, native.started_at),
                "mihomo_incarnation": (native.mihomo_process.pid, native.mihomo_started_at),
                "xray_reload_calls": native.reload_calls,
                "mihomo_controller": controller_state_fingerprint(),
            }

        # A verified subscription is not a rules fixture. Seed a small, bounded
        # synthetic IP-only policy through the real rules owner so selective
        # mode has both a compiled artifact and freshly applied owned chains.
        # These documentation-range destinations do not assert external VPN
        # reachability; the latter remains an explicit UNKNOWN below.
        synthetic_rules = "VPN 198.51.100.77\nDIRECT 203.0.113.53"
        native_before_rules_fixture = selective_native_fingerprint()
        rules_code, rules_saved = http_json(
            f"{restarted_api}/rules/manual", method="POST", payload={"text": synthetic_rules},
        )
        assert rules_code == 200 and rules_saved.get("ok") is True, rules_saved
        rules_apply_code, rules_apply = http_json(
            f"{restarted_api}/rules/manual/apply", method="POST",
            payload={"requested_by": "hosted-acceptance-selective-fixture", "run_now": True},
            timeout=90,
        )
        rules_job = await_job(restarted_api, rules_apply)
        assert rules_apply_code == 200 and rules_apply.get("ok") is True, rules_apply
        assert rules_job.get("status") == "success", rules_job
        rules_artifact_path = state / "generated" / "rules" / "effective-rules.json"
        rules_artifact = json.loads(rules_artifact_path.read_text(encoding="utf-8"))
        effective_rules = rules_artifact.get("rules") if isinstance(rules_artifact, dict) else None
        assert isinstance(effective_rules, list) and effective_rules, rules_artifact
        assert any(
            item.get("action") == "VPN" and item.get("value") == "198.51.100.77"
            for item in effective_rules if isinstance(item, dict)
        ), rules_artifact
        assert any(
            item.get("action") == "DIRECT" and item.get("value") == "203.0.113.53"
            for item in effective_rules if isinstance(item, dict)
        ), rules_artifact
        assert any(
            item.get("source") == "protected" and item.get("action") == "DIRECT"
            for item in effective_rules if isinstance(item, dict)
        ), rules_artifact
        xray_native_fields = {
            "xray_active", "xray_mounted", "xray_loaded", "xray_bindings", "xray_last_good",
            "xray_incarnation", "xray_reload_calls",
        }
        native_after_rules_fixture = selective_native_fingerprint()
        assert all(
            native_after_rules_fixture.get(key) == native_before_rules_fixture.get(key)
            for key in xray_native_fields
        ), {
            key: (native_before_rules_fixture.get(key), native_after_rules_fixture.get(key))
            for key in sorted(xray_native_fields)
            if native_after_rules_fixture.get(key) != native_before_rules_fixture.get(key)
        }
        assert_mihomo_launch_matches_active(native, active_mihomo)

        # Build the historical legacy preimage only after pending-generation
        # recovery has completed and verified inventory is settled. Keep the
        # selective/VPN policy applied before disabling Core or removing its
        # verified-generation marker.
        native_before_selective_default = selective_native_fingerprint()
        selective_code, selective_result = http_json(
            f"{restarted_api}/routing/global", method="POST",
            payload={"selective_default": "vpn", "requested_by": "hosted-acceptance", "run_now": True},
            timeout=45,
        )
        assert selective_code == 200 and selective_result.get("ok") is True, selective_result
        selective_job = await_job(restarted_api, selective_result)
        assert selective_job.get("status") == "success", selective_job
        assert selective_native_fingerprint() == native_before_selective_default, {
            "before": native_before_selective_default,
            "after": selective_native_fingerprint(),
            "contract": "global direct selective-default update leaves runtime unchanged",
        }

        # Switching from global direct to selective with VPN default legitimately
        # changes Mihomo's transparent fallback from MATCH,DIRECT to
        # MATCH,vpn-global. Keep Xray, bindings, identities, credentials and its
        # native incarnation byte-for-byte stable while qualifying that exact
        # Mihomo policy transition through native readback.
        native_before_selective_mode = selective_native_fingerprint()
        direct_mihomo_config = yaml.safe_load(active_mihomo.read_text(encoding="utf-8"))
        direct_subrules = direct_mihomo_config.get("sub-rules") if isinstance(direct_mihomo_config, dict) else None
        direct_transparent_rules = direct_subrules.get("fwrouter-transparent") if isinstance(direct_subrules, dict) else None
        assert isinstance(direct_transparent_rules, list) and direct_transparent_rules[-1] == "MATCH,DIRECT", {
            "transparent_fallback": (
                direct_transparent_rules[-1]
                if isinstance(direct_transparent_rules, list) and direct_transparent_rules else None
            ),
            "mihomo_config_sha256": hashlib.sha256(active_mihomo.read_bytes()).hexdigest(),
        }
        mihomo_proxy_surface_before = hashlib.sha256(json.dumps(
            {
                "proxies": direct_mihomo_config.get("proxies"),
                "proxy-groups": direct_mihomo_config.get("proxy-groups"),
            }, sort_keys=True, separators=(",", ":"),
        ).encode()).hexdigest()
        selective_code, selective_result = http_json(
            f"{restarted_api}/routing/global", method="POST",
            payload={"mode": "selective", "requested_by": "hosted-acceptance", "run_now": True},
            timeout=45,
        )
        assert selective_code == 200 and selective_result.get("ok") is True, selective_result
        selective_job = await_job(restarted_api, selective_result)
        assert selective_job.get("status") == "success", selective_job
        native_after_selective_apply = selective_native_fingerprint()
        assert all(
            native_after_selective_apply.get(key) == native_before_selective_mode.get(key)
            for key in xray_native_fields
        ), {
            key: (native_before_selective_mode.get(key), native_after_selective_apply.get(key))
            for key in sorted(xray_native_fields)
            if native_after_selective_apply.get(key) != native_before_selective_mode.get(key)
        }
        assert_mihomo_launch_matches_active(native, active_mihomo)
        selective_mihomo_config = yaml.safe_load(active_mihomo.read_text(encoding="utf-8"))
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
        assert isinstance(transparent_rules, list) and transparent_rules[-1] == "MATCH,vpn-global", {
            "transparent_fallback": transparent_rules[-1] if isinstance(transparent_rules, list) and transparent_rules else None,
            "mihomo_config_sha256": hashlib.sha256(active_mihomo.read_bytes()).hexdigest(),
        }
        selective_fwrouter_metadata = (
            selective_mihomo_config.get("fwrouter")
            if isinstance(selective_mihomo_config, dict) else None
        )
        assert isinstance(selective_fwrouter_metadata, dict)
        assert selective_fwrouter_metadata.get("transparent_final_match_rule") == "MATCH,vpn-global", {
            "transparent_final_match_rule": selective_fwrouter_metadata.get("transparent_final_match_rule"),
            "selective_default": selective_fwrouter_metadata.get("resolved_selective_default"),
        }
        selective_code, selective_state = http_json(f"{restarted_api}/routing/global")
        assert selective_code == 200 and selective_state.get("ok") is True, selective_state
        selective_data = selective_state.get("data") if isinstance(selective_state.get("data"), dict) else {}
        selective_routing = selective_data.get("routing") if isinstance(selective_data.get("routing"), dict) else {}
        selective_enforcement = (
            selective_data.get("runtime_enforcement")
            if isinstance(selective_data.get("runtime_enforcement"), dict) else {}
        )
        assert selective_routing.get("desired_mode") == "selective", selective_routing
        assert selective_routing.get("applied_mode") == "selective", selective_routing
        assert selective_routing.get("selective_default") == "vpn", selective_routing
        assert selective_enforcement.get("active_mode_matches_intent") is True, selective_enforcement
        assert selective_enforcement.get("traffic_enforcement_guaranteed") is True, selective_enforcement
        assert selective_enforcement.get("enforcement_level") == "global_selective_enforced", selective_enforcement
        assert selective_enforcement.get("selective_vpn_policy_ready") is True, selective_enforcement
        assert selective_enforcement.get("vpn_egress_ready") is False, selective_enforcement
        # Synthetic .invalid provider nodes do not prove an external VPN path.
        # Permit only the two closed egress observations; any missing runtime,
        # adapter, policy, kernel, or owned-table requirement remains a failure.
        egress_unknown_codes = {
            "mihomo_global_egress_binding_unknown",
            "vpn_external_path_not_verified",
        }
        missing_requirements = selective_enforcement.get("missing_runtime_requirements")
        assert isinstance(missing_requirements, list), selective_enforcement
        assert "vpn_external_path_not_verified" in missing_requirements, selective_enforcement
        assert "vpn_udp_fail_closed_policy_unconfirmed" not in missing_requirements, selective_enforcement
        assert set(missing_requirements) <= egress_unknown_codes, selective_enforcement
        assert selective_enforcement.get("supported_modes", {}).get("vpn") is True, selective_enforcement
        runtime_code, runtime_response = http_json(f"{restarted_api}/runtime")
        runtime = ((runtime_response.get("data") or {}).get("runtime") or {})
        dataplane = runtime.get("dataplane") if isinstance(runtime.get("dataplane"), dict) else {}
        live_payload = dataplane.get("details") if isinstance(dataplane.get("details"), dict) else {}
        live_chains = live_payload.get("required_chains") if isinstance(live_payload.get("required_chains"), dict) else {}
        assert runtime_code == 200 and runtime_response.get("ok") is True, runtime_response
        assert live_payload.get("ok") is True and live_payload.get("table_exists") is True, live_payload
        assert live_chains and all(value is True for value in live_chains.values()), live_chains
        mihomo_code, mihomo_response = http_json(f"{restarted_api}/mihomo")
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
        assert selectors.get("vpn_global_now") == "vpn-auto", selectors
        applied_manifest = json.loads(
            (state / "generated" / "dataplane" / "applied-manifest.json").read_text(encoding="utf-8")
        )
        applied_routing = applied_manifest.get("routing_global_state")
        assert isinstance(applied_routing, dict), applied_manifest
        assert applied_routing.get("desired_mode") == "selective", applied_routing
        assert applied_routing.get("selective_default") == "vpn", applied_routing
        applied_rules = ((applied_manifest.get("extra") or {}).get("rules_effective")
                         if isinstance(applied_manifest.get("extra"), dict) else None)
        applied_rule_rows = applied_rules.get("rules") if isinstance(applied_rules, dict) else None
        assert isinstance(applied_rules, dict) and applied_rules.get("selective_default") == "vpn", applied_rules
        assert isinstance(applied_rule_rows, list) and all(any(
            row.get("action") == action and row.get("value") == value
            for row in applied_rule_rows if isinstance(row, dict)
        ) for action, value in (("VPN", "198.51.100.77"), ("DIRECT", "203.0.113.53"))), applied_rules
        assert any(row.get("source") == "protected" and row.get("action") == "DIRECT"
                   for row in applied_rule_rows if isinstance(row, dict)), applied_rules
        assert selective_native_fingerprint() == native_after_selective_apply, {
            "before": native_after_selective_apply,
            "after": selective_native_fingerprint(),
        }

        # Model the historical 69ec419 state by removing only the generation
        # marker from this suite-owned DB. Inventory, identity mappings,
        # credentials, snapshots and native processes remain the real isolated
        # outputs of the preceding successful synthetic refreshes.
        assert not checkpoint_path.exists()
        connection = sqlite3.connect(state / "fwrouter.db", timeout=5)
        try:
            deleted = connection.execute(
                "DELETE FROM settings WHERE key=?",
                ("subscriptions.native_generation.v1",),
            ).rowcount
            connection.commit()
        finally:
            connection.close()
        assert deleted == 1, "legacy fixture did not remove exactly the synthetic generation marker"

        code, generation_digest = http_json(
            f"{restarted_api}/__acceptance/subscription/legacy-inventory-digest",
        )
        assert code == 200 and generation_digest.get("ok") is True
        expected_digest = generation_digest.get("digest")
        assert isinstance(expected_digest, str) and re.fullmatch(r"[0-9a-f]{64}", expected_digest)
        assert type(generation_digest.get("inventory_count")) is int
        state_before_admission = generation_state(restarted_api)
        assert state_before_admission.get("status") == "unknown"
        assert state_before_admission.get("reason") == "verified_generation_missing"
        assert state_before_admission.get("inventory_count") == generation_digest["inventory_count"]

        def native_state_fingerprint() -> dict[str, Any]:
            xray_native, _xray_launch = native_config_bytes(native)
            return {
                "xray_active": hashlib.sha256(active_xray.read_bytes()).hexdigest(),
                "xray_mounted": hashlib.sha256(xray_native).hexdigest(),
                "xray_loaded": hashlib.sha256(
                    json.dumps(loaded_identities(native), separators=(",", ":")).encode()
                ).hexdigest(),
                "xray_bindings": hashlib.sha256(
                    (state / "xray" / "fwrouter-bindings.json").read_bytes()
                ).hexdigest() if (state / "xray" / "fwrouter-bindings.json").is_file() else None,
                "dataplane_applied_manifest": _synthetic_file_fingerprint(
                    state / "generated" / "dataplane" / "applied-manifest.json",
                ),
                "dataplane_last_good_manifest": _synthetic_file_fingerprint(
                    state / "last-good" / "dataplane" / "last-good-manifest.json",
                ),
                "dataplane_last_good_nft": _synthetic_file_fingerprint(
                    state / "last-good" / "dataplane" / "last-good.nft",
                ),
                "mihomo_active": hashlib.sha256(active_mihomo.read_bytes()).hexdigest(),
                # The exact reconcile receipt may be refreshed by successful
                # proof-only admission. Check its semantics separately below;
                # every other generated artifact remains in this fingerprint.
                "mihomo_generated": _synthetic_file_tree_fingerprint(
                    state / "generated" / "mihomo",
                    exclude_relative_paths=("reconcile-state.json",),
                ),
                "mihomo_last_good": last_good_mihomo_snapshot(),
                "xray_last_good": _synthetic_file_tree_fingerprint(state / "last-good" / "xray"),
                "xray_incarnation": (native.process.pid, native.started_at),
                "mihomo_incarnation": (native.mihomo_process.pid, native.mihomo_started_at),
                "xray_reload_calls": native.reload_calls,
                "mihomo_controller": controller_state_fingerprint(),
            }

        def mihomo_proof_receipt_state() -> dict[str, Any]:
            receipt_path = state / "generated" / "mihomo" / "reconcile-state.json"
            receipt_info = receipt_path.lstat()
            assert stat.S_ISREG(receipt_info.st_mode) and not stat.S_ISLNK(receipt_info.st_mode)
            assert receipt_info.st_size <= 262144, "Mihomo proof receipt exceeded its contract bound"
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            assert isinstance(receipt, dict)
            incarnation_result = native.rpc("mihomo_incarnation", {})
            incarnation = (
                incarnation_result.get("details", {}).get("stdout")
                if isinstance(incarnation_result, dict)
                and isinstance(incarnation_result.get("details"), dict) else None
            )
            active_hash = hashlib.sha256(active_mihomo.read_bytes()).hexdigest()
            return {
                "active_hash_matches": receipt.get("active_config_hash") == active_hash,
                "runtime_incarnation_matches": (
                    isinstance(incarnation, str) and bool(incarnation.strip())
                    and receipt.get("runtime_incarnation") == incarnation.strip()
                ),
                "version": receipt.get("version"),
                "udp_policy_version": receipt.get("udp_policy_version"),
                "native_proof": (
                    receipt.get("native_top_level_rules_match"),
                    receipt.get("native_selector_membership_match"),
                    receipt.get("subrule_config_guard_match"),
                ),
            }

        native_before_admission = native_state_fingerprint()
        mihomo_receipt_before_admission = mihomo_proof_receipt_state()
        assert mihomo_receipt_before_admission["active_hash_matches"] is True
        assert mihomo_receipt_before_admission["runtime_incarnation_matches"] is True
        persistence_before_admission = _synthetic_table_fingerprints(state / "fwrouter.db")
        assert persistence_before_admission["subscription_node_credentials"][0] > 0
        assert persistence_before_admission["subscription_node_identities"][0] > 0
        assert len(set(loaded_identities(native)) & baseline_pairs) == 78
        provider_calls_before_admission = stack["provider_bridge"].snapshot_calls()
        subscription_source_requests_before_admission = source.snapshot_requests()
        checkpoint_path = state / "xray" / ".generation" / "generation-checkpoint.json"
        assert not checkpoint_path.exists()

        # Prove each tripwire catches the forbidden attempt before it reaches
        # a native process or external source. The .invalid HTTP request is
        # rejected by the wrapper and must never leave the worker.
        tripwire_code, tripwire_check = http_json(
            f"{restarted_api}/__acceptance/subscription/proof-only-tripwire-self-check",
            method="POST",
        )
        assert tripwire_code == 200 and tripwire_check.get("ok") is True
        assert tripwire_check.get("intercepts") == {
            "xray_reload": True, "mihomo_restart": True,
            "core_dataplane_apply": True, "core_dataplane_rollback": True,
            "core_routing_hot_swap": True, "unexpected_http": True,
        }
        tripwire_attempts = tripwire_check.get("attempts")
        assert isinstance(tripwire_attempts, dict)
        assert tripwire_attempts["xray_reload"] == {
            "xray_apply_or_reload": 1, "mihomo_apply_or_reload": 0,
            "core_dataplane_apply_or_rollback": 0, "unexpected_http_request": 0,
        }
        assert tripwire_attempts["mihomo_restart"] == {
            "xray_apply_or_reload": 0, "mihomo_apply_or_reload": 1,
            "core_dataplane_apply_or_rollback": 0, "unexpected_http_request": 0,
        }
        for name in ("core_dataplane_apply", "core_dataplane_rollback", "core_routing_hot_swap"):
            assert tripwire_attempts[name] == {
                "xray_apply_or_reload": 0, "mihomo_apply_or_reload": 0,
                "core_dataplane_apply_or_rollback": 1, "unexpected_http_request": 0,
            }
        assert tripwire_attempts["unexpected_http"] == {
            "xray_apply_or_reload": 0, "mihomo_apply_or_reload": 0,
            "core_dataplane_apply_or_rollback": 0, "unexpected_http_request": 1,
        }
        assert native_state_fingerprint() == native_before_admission
        assert mihomo_proof_receipt_state() == mihomo_receipt_before_admission
        assert source.snapshot_requests() == subscription_source_requests_before_admission

        # Requalify Core against this already populated, marker-missing legacy
        # inventory. The request must remain pending until the canonical owner
        # performs a fresh same-mode apply; the apply may update Core-owned
        # manifests, but it must not touch Xray/Mihomo, inventory or providers.
        disabled_code, disabled = http_json(
            f"{stack['api']}/modules/core/desired-state", method="POST",
            payload={"desired_state": "disabled", "requested_by": "hosted-acceptance", "run_now": False},
        )
        assert disabled_code == 200 and disabled.get("ok") is True, disabled
        enabled_code, enabled = http_json(
            f"{stack['api']}/modules/core/desired-state", method="POST",
            payload={"desired_state": "enabled", "requested_by": "hosted-acceptance", "run_now": False},
        )
        assert enabled_code == 200 and enabled.get("ok") is True, enabled
        module_result = enabled.get("data") if isinstance(enabled.get("data"), dict) else {}
        pending_module = module_result.get("module") if isinstance(module_result.get("module"), dict) else {}
        assert pending_module.get("desired_state") == "enabled", pending_module
        assert pending_module.get("apply_state") == "pending", pending_module
        assert generation_state(restarted_api) == state_before_admission
        assert stack["provider_bridge"].snapshot_calls() == provider_calls_before_admission
        assert source.snapshot_requests() == subscription_source_requests_before_admission

        core_apply_code, core_apply = http_json(
            f"{stack['api']}/routing/global", method="POST",
            payload={"mode": "selective", "requested_by": "hosted-acceptance", "run_now": True},
            timeout=30,
        )
        assert core_apply_code == 200 and core_apply.get("ok") is True, core_apply
        core_apply_job = await_job(stack["api"], core_apply)
        assert core_apply_job.get("status") == "success", core_apply_job
        core_apply_receipt = sqlite3.connect(state / "fwrouter.db")
        try:
            core_revision = core_apply_receipt.execute(
                "SELECT value_json FROM settings WHERE key='core.intent_revision'",
            ).fetchone()
            latest_core_apply = core_apply_receipt.execute(
                "SELECT core_intent_revision FROM apply_versions "
                "ORDER BY created_at DESC, apply_id DESC LIMIT 1",
            ).fetchone()
            settled_module = core_apply_receipt.execute(
                "SELECT apply_state FROM modules WHERE module_name='core'",
            ).fetchone()
        finally:
            core_apply_receipt.close()
        assert core_revision and latest_core_apply
        assert int(latest_core_apply[0]) == int(json.loads(core_revision[0]))
        assert settled_module == ("clean",)
        assert generation_state(restarted_api) == state_before_admission
        native_after_core_apply = native_state_fingerprint()
        for untouched_runtime in (
            "xray_active", "xray_mounted", "xray_loaded", "xray_bindings",
            "mihomo_active", "mihomo_generated", "mihomo_last_good", "xray_last_good",
            "xray_incarnation", "mihomo_incarnation", "xray_reload_calls", "mihomo_controller",
        ):
            assert native_after_core_apply[untouched_runtime] == native_before_admission[untouched_runtime], {
                "runtime": untouched_runtime,
                "before": native_before_admission[untouched_runtime],
                "after": native_after_core_apply[untouched_runtime],
            }
        assert _synthetic_table_fingerprints(state / "fwrouter.db") == persistence_before_admission
        assert mihomo_proof_receipt_state() == mihomo_receipt_before_admission
        assert stack["provider_bridge"].snapshot_calls() == provider_calls_before_admission
        assert source.snapshot_requests() == subscription_source_requests_before_admission
        # Proof-only admission below starts from the post-apply baseline. The
        # only permissible runtime delta was the Core-owned apply receipt.
        native_before_admission = native_after_core_apply
        persistence_before_admission = _synthetic_table_fingerprints(state / "fwrouter.db")

        def admit_legacy_inventory(api: str) -> tuple[int, dict]:
            return http_json(
                f"{api}/__acceptance/subscription/admit-legacy-inventory",
                method="POST", payload={"expected_digest": expected_digest}, timeout=90,
            )

        # An actual HandlerService transport error after the proof-only
        # checkpoint is durable must leave the inventory unknown. It must not
        # run the ordinary checkpoint restore/reload path.
        native.fail_next_action("api_inbound_users")
        failed_admission_code, failed_admission = admit_legacy_inventory(restarted_api)
        assert failed_admission_code == 200 and failed_admission.get("ok") is False, {
            "status": failed_admission_code,
            "error_code": failed_admission.get("error_code"),
            "status_name": failed_admission.get("status"),
            "reason": failed_admission.get("reason"),
            "entry_facts": failed_admission.get("entry_facts"),
        }
        # The injected HandlerService failure must be consumed inside the
        # admission proof itself. Assert its closed exception class before any
        # later native read can consume a still-pending one-shot fault.
        assert failed_admission.get("reason") == "XrayAdapterError", {
            "reason": failed_admission.get("reason"),
            "entry_facts": failed_admission.get("entry_facts"),
        }
        assert failed_admission.get("mutation_attempts") == {
            "xray_apply_or_reload": 0, "mihomo_apply_or_reload": 0,
            "core_dataplane_apply_or_rollback": 0, "unexpected_http_request": 0,
        }
        assert checkpoint_path.is_file(), {
            "reason": failed_admission.get("reason"),
            "entry_facts": failed_admission.get("entry_facts"),
            "checkpoint_exists": checkpoint_path.exists(),
        }
        failed_checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        assert failed_checkpoint.get("operation_mode") == "proof_only_legacy_admission"
        assert failed_checkpoint.get("phase") == "proof_started"
        assert generation_state(restarted_api) == state_before_admission
        assert _synthetic_table_fingerprints(state / "fwrouter.db") == persistence_before_admission
        assert native_state_fingerprint() == native_before_admission
        assert mihomo_proof_receipt_state() == mihomo_receipt_before_admission
        assert stack["provider_bridge"].snapshot_calls() == provider_calls_before_admission
        assert source.snapshot_requests() == subscription_source_requests_before_admission

        # A generic startup recovery attempt must recognize the proof-only
        # checkpoint and refuse it without restoring or reloading native state.
        old_worker = stack["worker"]
        old_worker.terminate()
        old_worker.wait(timeout=5)
        restarted_worker, restarted_api = stack["start_worker"]()
        stack["worker"], stack["api"] = restarted_worker, restarted_api
        assert native_state_fingerprint() == native_before_admission
        startup_code, startup_recovery = http_json(
            f"{restarted_api}/__acceptance/xray/startup-generation-recovery",
            method="POST", timeout=75,
        )
        assert startup_code == 200 and startup_recovery.get("ok") is False
        assert startup_recovery.get("tripwire_armed") is True
        assert startup_recovery.get("mutation_attempts") == {
            "xray_apply_or_reload": 0, "mihomo_apply_or_reload": 0,
            "core_dataplane_apply_or_rollback": 0, "unexpected_http_request": 0,
        }
        assert "proof" in str(startup_recovery.get("error_code") or "").lower() or (
            "proof" in str(startup_recovery.get("inventory_authority") or "").lower()
        ), {"error_code": startup_recovery.get("error_code"),
            "inventory_authority": startup_recovery.get("inventory_authority")}
        assert checkpoint_path.is_file()
        assert json.loads(checkpoint_path.read_text(encoding="utf-8")).get("phase") == "proof_started"
        assert generation_state(restarted_api) == state_before_admission
        assert _synthetic_table_fingerprints(state / "fwrouter.db") == persistence_before_admission
        assert native_state_fingerprint() == native_before_admission
        assert mihomo_proof_receipt_state() == mihomo_receipt_before_admission
        assert stack["provider_bridge"].snapshot_calls() == provider_calls_before_admission
        assert source.snapshot_requests() == subscription_source_requests_before_admission

        # Explicitly repeat the owner operation with the same exact digest and
        # confirmation. It must re-read the actual native processes, then
        # atomically promote the legacy generation without changing runtime.
        resumed_code, resumed_admission = admit_legacy_inventory(restarted_api)
        assert resumed_code == 200 and resumed_admission.get("ok") is True, {
            "status": resumed_code,
            "status_name": resumed_admission.get("status"),
            "reason": resumed_admission.get("reason"),
            "error_code": resumed_admission.get("error_code"),
        }
        assert resumed_admission.get("status") == "verified"
        assert resumed_admission.get("digest") == expected_digest
        assert resumed_admission.get("proof_receipt_written") is True
        mihomo_receipt_after_admission = mihomo_proof_receipt_state()
        assert mihomo_receipt_after_admission["active_hash_matches"] is True
        assert mihomo_receipt_after_admission["runtime_incarnation_matches"] is True
        assert mihomo_receipt_after_admission["native_proof"] == (True, True, True)
        assert resumed_admission.get("mutation_attempts") == {
            "xray_apply_or_reload": 0, "mihomo_apply_or_reload": 0,
            "core_dataplane_apply_or_rollback": 0, "unexpected_http_request": 0,
        }
        admitted = generation_state(restarted_api)
        assert admitted.get("status") == "verified"
        assert admitted.get("candidate_digest") == expected_digest
        assert admitted.get("verified_digest") == expected_digest
        assert admitted.get("candidate_revision") == admitted.get("verified_revision")
        assert not checkpoint_path.exists(), "successful proof-only admission left its checkpoint open"
        assert _synthetic_table_fingerprints(state / "fwrouter.db") == persistence_before_admission
        assert native_state_fingerprint() == native_before_admission
        assert mihomo_proof_receipt_state() == mihomo_receipt_after_admission
        assert stack["provider_bridge"].snapshot_calls() == provider_calls_before_admission
        assert source.snapshot_requests() == subscription_source_requests_before_admission
        assert set(loaded_identities(native)) == expected_f_identities
        assert len(set(loaded_identities(native)) & baseline_pairs) == 78

        # Repeating admission is idempotent: it may collect fresh proof, but it
        # cannot alter inventory, credentials, projections or native processes.
        repeat_code, repeated_admission = admit_legacy_inventory(restarted_api)
        assert repeat_code == 200 and repeated_admission.get("ok") is True
        assert repeated_admission.get("already_admitted") is True
        assert repeated_admission.get("proof_receipt_written") is True
        assert repeated_admission.get("mutation_attempts") == {
            "xray_apply_or_reload": 0, "mihomo_apply_or_reload": 0,
            "core_dataplane_apply_or_rollback": 0, "unexpected_http_request": 0,
        }
        assert generation_state(restarted_api) == admitted
        assert _synthetic_table_fingerprints(state / "fwrouter.db") == persistence_before_admission
        assert native_state_fingerprint() == native_before_admission
        assert mihomo_proof_receipt_state() == mihomo_receipt_after_admission
        assert stack["provider_bridge"].snapshot_calls() == provider_calls_before_admission
        assert source.snapshot_requests() == subscription_source_requests_before_admission
    finally:
        source.close()


def test_xray_reviewed_identity_migration_preserves_credentials_through_refresh_restart_recovery(acceptance_stack):
    """Reviewed profile continuity reaches native Xray only through a fresh fenced generation."""
    stack = acceptance_stack
    state, native = stack["state"], stack["native"]
    tokens = [f"identity-public-{index}@acceptance.invalid" for index in range(7)]
    active_path = state / "xray" / "config.json"

    # Core vpn-auto admission requires VLESS over TCP/gRPC with Reality and a
    # public key. Keep this profile eligible through both revisions; plain TLS
    # would correctly exclude it and make the seeded profile-owned internal
    # identity impossible to expect in native output.
    # These 62 unmanaged identities are loaded by the actual native child before
    # FWRouter generates managed credentials; they must remain untouched.
    baseline_config = json.loads(active_path.read_text(encoding="utf-8"))
    inbound = next(item for item in baseline_config["inbounds"] if item.get("tag") == "vless-ws")
    unrelated_native = [
        {"id": str(uuid5(NAMESPACE_DNS, f"identity-native-unrelated-{index}")),
         "email": f"unrelated-native-{index}@acceptance.invalid", "level": 0}
        for index in range(62)
    ]
    unrelated_native_pairs = {
        (str(item["id"]), str(item["email"])) for item in unrelated_native
    }
    inbound["settings"]["clients"].extend(unrelated_native)
    active_path.write_text(json.dumps(baseline_config, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    native.restart_xray_child()
    assert_native_matches_active(native, active_path)

    def outbound(index: int) -> dict:
        return {
            "tag": f"member-{index}", "protocol": "vless",
            "settings": {"vnext": [{
                "address": f"member-{index}.example.invalid", "port": 443,
                "users": [{"id": str(uuid5(NAMESPACE_DNS, f"identity-member-{index}")),
                           "encryption": "none", "flow": "xtls-rprx-vision"}],
            }]},
            "streamSettings": {
                "network": "tcp", "security": "reality",
                "realitySettings": {
                    "serverName": f"member-{index}.example.invalid",
                    # Valid deterministic X25519 test public key (RFC 7748
                    # basepoint), not a provider credential or endpoint key.
                    "publicKey": "CQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
                    "shortId": "0123456789abcdef",
                    "fingerprint": "chrome",
                },
            },
        }

    old_profile = {
        "remarks": "identity profile before review",
        "outbounds": [outbound(index) for index in range(7)] + [
            {"tag": "direct", "protocol": "freedom"},
            {"tag": "reject", "protocol": "blackhole"},
            {"tag": "dns", "protocol": "dns"},
        ],
        "routing": {"rules": [{"type": "field", "outboundTag": "direct"}]},
    }
    new_profile = json.loads(json.dumps(old_profile))
    new_profile["outbounds"][7:7] = [outbound(index) for index in range(7, 10)]
    # Policy/root change makes this a reviewed mapping, never automatic growth.
    new_profile["routing"]["rules"].append({"type": "field", "outboundTag": "reject"})

    def content_id(profile: dict) -> str:
        raw = json.dumps(profile, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        return "sub:" + hashlib.sha256(raw).hexdigest()

    def identity_fingerprint(values) -> str:
        payload = json.dumps(sorted(values), ensure_ascii=False, separators=(",", ":")).encode()
        return hashlib.sha256(payload).hexdigest()

    old_id, new_id = content_id(old_profile), content_id(new_profile)
    code, seeded = http_json(
        f"{stack['api']}/__acceptance/xray/identity-refresh", method="POST",
        payload={"profile": old_profile},
    )
    assert code == 200 and seeded.get("ok") is True and seeded.get("server_ids") == [old_id], {
        "status": code, "ok": seeded.get("ok"), "refreshed_count": seeded.get("refreshed_count"),
    }

    def profile_member_ids(server_id: str) -> set[str]:
        connection = sqlite3.connect(state / "fwrouter.db", timeout=5)
        try:
            row = connection.execute("SELECT raw_json FROM servers WHERE server_id=?", (server_id,)).fetchone()
            assert row is not None
            raw = json.loads(row[0])
            endpoints = raw.get("_fwrouter_topology", {}).get("endpoints", [])
            return {str(item.get("identity") or "") for item in endpoints if isinstance(item, dict)}
        finally:
            connection.close()

    def profile_credentials(server_id: str):
        connection = sqlite3.connect(state / "fwrouter.db", timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            alias = connection.execute(
                "SELECT identity_id FROM subscription_node_aliases WHERE server_id=?", (server_id,),
            ).fetchone()
            if alias is None:
                return None, set()
            identity_id = str(alias["identity_id"])
            rows = {
                (str(row["scope"]), str(row["client_uuid"]), str(row["client_email"]), int(row["revoked"]))
                for row in connection.execute(
                    "SELECT scope, client_uuid, client_email, revoked "
                    "FROM subscription_node_credentials WHERE identity_id=?",
                    (identity_id,),
                )
            }
            return identity_id, rows
        finally:
            connection.close()

    original_member_ids = profile_member_ids(old_id)
    assert len(original_member_ids) == 7

    # Seed seven public subscription subjects and the one internal auto subject.
    def digest(value: str, length: int) -> str:
        return hashlib.sha1(value.encode()).hexdigest()[:length]

    def subject(connection, client_uuid: str, email: str) -> None:
        connection.execute(
            """INSERT INTO subjects (subject_id, subject_type, subject_role, implementation_kind,
               stable_key, display_name, desired_mode, runtime_state, is_active, is_deleted, metadata_json)
               VALUES (?, 'explicit_external_client', 'vless_client', 'xray', ?, ?, 'enabled', 'active', 1, 0, ?)""",
            (f"xray:{client_uuid}", f"xray:{client_uuid}", email,
             json.dumps({"detail": {"email": email, "client_uuid": client_uuid, "enabled": True}})),
        )

    connection = sqlite3.connect(state / "fwrouter.db", timeout=5)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("UPDATE server_preferences SET vpn_auto=1 WHERE server_id=?", (old_id,))
        for index, token in enumerate(tokens):
            cursor = connection.execute(
                "INSERT INTO subscription_accounts (slug, display_name, enabled) VALUES (?, ?, 1)",
                (f"identity-account-{index}", f"Identity account {index}"),
            )
            connection.execute(
                "INSERT INTO subscription_clients (account_id, token, app_type, enabled) VALUES (?, ?, 'xray', 1)",
                (cursor.lastrowid, token),
            )
            email = f"sub-{digest(token, 10)}-{digest(old_id, 12)}@fwrouter.local"
            client_uuid = str(uuid5(NAMESPACE_DNS, f"fwrouter-subscription:{token}:{old_id}"))
            subject(connection, client_uuid, email)
        internal_email = f"vpn-auto-{digest(old_id, 12)}@fwrouter.local"
        internal_uuid = str(uuid5(NAMESPACE_DNS, "identity-acceptance-vpn-auto"))
        subject(connection, internal_uuid, internal_email)
        connection.commit()
    finally:
        connection.close()

    code, baseline = http_json(
        f"{stack['api']}/__acceptance/xray/publish-pending-inventory", method="POST",
        payload={"include_vpn_auto": True}, timeout=90,
    )
    assert code == 200 and baseline.get("ok") is True, {
        "status": code, "ok": baseline.get("ok"), "stage": baseline.get("stage"),
        "error_code": baseline.get("error_code"),
    }
    assert baseline.get("native_generation_verified") is True
    assert_native_matches_active(native, active_path)
    baseline_pairs = set(loaded_identities(native))
    affected = {
        (str(uuid5(NAMESPACE_DNS, f"fwrouter-subscription:{token}:{old_id}")),
         f"sub-{digest(token, 10)}-{digest(old_id, 12)}@fwrouter.local")
        for token in tokens
    }
    affected.add((internal_uuid, internal_email))
    assert len(affected) == 8
    virtual_id = "virtual:xray:vpn-auto"
    public_virtual = {
        (str(uuid5(NAMESPACE_DNS, f"fwrouter-subscription:{token}:{virtual_id}")),
         f"sub-{digest(token, 10)}-{digest(virtual_id, 12)}@fwrouter.local")
        for token in tokens
    }
    virtual_auto_email = f"vpn-auto-{digest(virtual_id, 12)}@fwrouter.local"
    virtual_auto = {pair for pair in baseline_pairs if pair[1] == virtual_auto_email}
    expected_categories = {
        "preloaded_native": unrelated_native_pairs,
        "public_profile_server": {
            (str(uuid5(NAMESPACE_DNS, f"fwrouter-subscription:{token}:{old_id}")),
             f"sub-{digest(token, 10)}-{digest(old_id, 12)}@fwrouter.local")
            for token in tokens
        },
        "internal_profile_server": {(internal_uuid, internal_email)},
        "public_virtual_selector": public_virtual,
        "internal_virtual_selector": virtual_auto,
    }
    category_counts = {
        name: len(pairs & baseline_pairs) for name, pairs in expected_categories.items()
    }
    expected_counts = {
        "preloaded_native": 62,
        "public_profile_server": 7,
        "internal_profile_server": 1,
        "public_virtual_selector": 7,
        "internal_virtual_selector": 1,
    }
    assert category_counts == expected_counts and len(baseline_pairs) == 78, {
        "category_counts": category_counts,
        "expected_category_counts": expected_counts,
        "baseline_count": len(baseline_pairs),
        "baseline_sha256": identity_fingerprint(baseline_pairs),
    }
    expected_baseline = set().union(*expected_categories.values())
    assert baseline_pairs == expected_baseline, {
        "unexpected_count": len(baseline_pairs - expected_baseline),
        "missing_count": len(expected_baseline - baseline_pairs),
        "baseline_sha256": identity_fingerprint(baseline_pairs),
        "expected_sha256": identity_fingerprint(expected_baseline),
    }
    assert affected <= baseline_pairs
    expected_migration_credentials = {
        ("subscription:" + token,
         str(uuid5(NAMESPACE_DNS, f"fwrouter-subscription:{token}:{old_id}")),
         f"sub-{digest(token, 10)}-{digest(old_id, 12)}@fwrouter.local", 0)
        for token in tokens
    }
    expected_migration_credentials.add(("vpn-auto", internal_uuid, internal_email, 0))
    old_identity_id, old_migration_credentials = profile_credentials(old_id)
    assert (old_identity_id is not None and len(old_migration_credentials) == 8
            and old_migration_credentials == expected_migration_credentials), {
        "identity_present": old_identity_id is not None,
        "credential_count": len(old_migration_credentials),
        "expected_credential_count": len(expected_migration_credentials),
        "credential_sha256": identity_fingerprint(old_migration_credentials),
        "expected_sha256": identity_fingerprint(expected_migration_credentials),
    }
    unaffected = baseline_pairs - affected
    assert len(unaffected) == 70
    assert unrelated_native_pairs <= baseline_pairs, {
        "unrelated_native_count": len(unrelated_native_pairs & baseline_pairs),
        "baseline_count": len(baseline_pairs),
        "baseline_sha256": identity_fingerprint(baseline_pairs),
    }
    assert public_virtual <= unaffected and virtual_auto <= unaffected
    assert len(unrelated_native_pairs & unaffected) == 62, {
        "unrelated_native_count": len(unrelated_native_pairs & unaffected),
        "unaffected_count": len(unaffected),
        "baseline_sha256": identity_fingerprint(baseline_pairs),
    }
    before_snapshots = {
        token: read_subscription_rows(state, token)["snapshots"] for token in tokens
    }
    # Leave a genuine prepared checkpoint by injecting a controlled exception
    # immediately after the production writer persists it. This qualifies the
    # durable pre-native boundary across restart; it is not a SIGKILL test.
    checkpoint = state / "xray" / ".generation" / "generation-checkpoint.json"
    interrupted_email = "identity-interrupted-candidate@acceptance.invalid"
    interrupted_uuid = str(uuid5(NAMESPACE_DNS, "identity-interrupted-candidate"))
    connection = sqlite3.connect(state / "fwrouter.db", timeout=5)
    try:
        subject(connection, interrupted_uuid, interrupted_email)
        connection.commit()
    finally:
        connection.close()
    arm_code, armed = http_json(
        f"{stack['api']}/__acceptance/xray/checkpoint-failure/arm", method="POST",
    )
    assert arm_code == 200 and armed.get("ok") is True and armed.get("armed") is True
    reloads_before_failure = native.reload_calls
    failed_code, failed_generation = http_json(
        f"{stack['api']}/__acceptance/xray/reconcile", method="POST",
        payload={"include_vpn_auto": True}, timeout=75,
    )
    assert failed_code == 200 and failed_generation.get("ok") is False, {
        "status": failed_code, "ok": failed_generation.get("ok"),
        "stage": failed_generation.get("stage"), "error_code": failed_generation.get("error_code"),
    }
    assert failed_generation.get("stage") == "checkpoint_prepare"
    assert failed_generation.get("error_code") == "XRAY_GENERATION_CHECKPOINT_FAILED"
    assert failed_generation.get("error_message") == "ACCEPTANCE_PREPARED_CHECKPOINT_POSTWRITE_FAILURE"
    assert not (state / "run" / "fail-after-prepared-checkpoint-once").exists()
    checkpoint_data = json.loads(checkpoint.read_text(encoding="utf-8"))
    assert checkpoint_data.get("phase") == "prepared"
    old_checkpoint_bytes = checkpoint.read_bytes()
    old_checkpoint_sha = hashlib.sha256(old_checkpoint_bytes).hexdigest()
    assert native.reload_calls == reloads_before_failure
    active_before_checkpoint_failure = active_path.read_bytes()
    mihomo_active_path = state / "generated" / "mihomo" / "config.yaml"
    mihomo_before_checkpoint_failure = mihomo_active_path.read_bytes()
    loaded_before_restart = set(loaded_identities(native))
    assert len(loaded_before_restart) == len(baseline_pairs) and identity_fingerprint(loaded_before_restart) == identity_fingerprint(baseline_pairs), {
        "loaded_count": len(loaded_before_restart), "loaded_sha256": identity_fingerprint(loaded_before_restart),
        "expected_count": len(baseline_pairs), "expected_sha256": identity_fingerprint(baseline_pairs),
    }
    # Restart the owned worker gracefully after the controlled exception has
    # returned. No recovery action is requested, so the marker must remain.
    prior_worker = stack["worker"]
    prior_worker.terminate()
    prior_worker.wait(timeout=5)
    restarted_worker, restarted_api = stack["start_worker"]()
    stack["worker"], stack["api"] = restarted_worker, restarted_api
    assert checkpoint.is_file() and hashlib.sha256(checkpoint.read_bytes()).hexdigest() == old_checkpoint_sha
    assert native.reload_calls == reloads_before_failure
    assert active_path.read_bytes() == active_before_checkpoint_failure
    assert mihomo_active_path.read_bytes() == mihomo_before_checkpoint_failure
    loaded_after_restart = set(loaded_identities(native))
    assert len(loaded_after_restart) == len(baseline_pairs) and identity_fingerprint(loaded_after_restart) == identity_fingerprint(baseline_pairs), {
        "loaded_count": len(loaded_after_restart), "loaded_sha256": identity_fingerprint(loaded_after_restart),
        "expected_count": len(baseline_pairs), "expected_sha256": identity_fingerprint(baseline_pairs),
    }
    inventory_code, inventory_response = http_json(
        f"{restarted_api}/__acceptance/subscription/native-generation",
    )
    assert inventory_code == 200 and inventory_response.get("ok") is True, {
        "status": inventory_code, "error_code": inventory_response.get("error_code"),
    }
    verified_inventory = inventory_response.get("state")
    assert isinstance(verified_inventory, dict)
    checkpoint_binding = checkpoint_data.get("subscription_inventory_generation")
    assert (verified_inventory.get("status") == "verified"
            and isinstance(checkpoint_binding, dict)
            and checkpoint_binding.get("digest") == verified_inventory.get("candidate_digest")
            and checkpoint_binding.get("digest") == verified_inventory.get("verified_digest")
            and checkpoint_binding.get("revision") == verified_inventory.get("candidate_revision")
            and checkpoint_binding.get("revision") == verified_inventory.get("verified_revision")), {
        "inventory_status": verified_inventory.get("status"),
        "checkpoint_inventory_bound": isinstance(checkpoint_binding, dict),
        "candidate_revision": verified_inventory.get("candidate_revision"),
        "verified_revision": verified_inventory.get("verified_revision"),
    }

    # Resolve the still-bound checkpoint through the production recovery owner
    # before allowing the ordinary identity refresh to persist a new candidate.
    recovery_code, recovered_checkpoint = http_json(
        f"{restarted_api}/__acceptance/xray/subscription-refresh-preflight",
        method="POST", timeout=75,
    )
    assert recovery_code == 200 and recovered_checkpoint.get("ok") is True, {
        "status": recovery_code, "error_code": recovered_checkpoint.get("error_code"),
        "stage": recovered_checkpoint.get("stage"),
    }
    assert not checkpoint.exists()
    connection = sqlite3.connect(state / "fwrouter.db", timeout=5)
    try:
        connection.execute(
            "UPDATE subjects SET is_active=0, is_deleted=1, deleted_at=CURRENT_TIMESTAMP "
            "WHERE subject_id=?", (f"xray:{interrupted_uuid}",),
        )
        connection.commit()
    finally:
        connection.close()
    # The actual source refresh now replaces the old content-addressed profile;
    # its root/policy change cannot silently migrate identity.
    code, refreshed = http_json(
        f"{restarted_api}/__acceptance/xray/identity-refresh", method="POST",
        payload={"profile": new_profile},
    )
    assert code == 200 and refreshed.get("ok") is True and refreshed.get("server_ids") == [new_id], {
        "status": code, "ok": refreshed.get("ok"), "refreshed_count": refreshed.get("refreshed_count"),
    }
    refreshed_member_ids = profile_member_ids(new_id)
    assert len(refreshed_member_ids) == 10
    assert original_member_ids <= refreshed_member_ids
    assert len(refreshed_member_ids - original_member_ids) == 3

    # Reproduce the production partial-apply shape: old Xray bindings still
    # point at the old profile, while the current Mihomo base has already lost
    # that generated group and its member proxies. The native process remains
    # on its previously loaded config until the candidate is applied.
    connection = sqlite3.connect(state / "fwrouter.db", timeout=5)
    try:
        old_raw_row = connection.execute(
            "SELECT raw_json FROM servers WHERE server_id=?", (old_id,),
        ).fetchone()
        assert old_raw_row is not None
        old_runtime_name = str(json.loads(old_raw_row[0]).get("_fwrouter_runtime_name") or "")
        assert old_runtime_name
    finally:
        connection.close()
    mihomo_active_path = state / "generated" / "mihomo" / "config.yaml"
    mihomo_partial = yaml.safe_load(mihomo_active_path.read_text(encoding="utf-8"))
    old_group_row = next(
        (group for group in mihomo_partial.get("proxy-groups", [])
         if isinstance(group, dict) and group.get("name") == old_runtime_name),
        None,
    )
    assert old_group_row is not None
    old_member_names = set(str(name) for name in old_group_row.get("proxies", []))
    assert old_member_names
    mihomo_partial["proxy-groups"] = [
        group for group in mihomo_partial.get("proxy-groups", [])
        if not isinstance(group, dict) or group.get("name") != old_runtime_name
    ]
    mihomo_partial["proxies"] = [
        proxy for proxy in mihomo_partial.get("proxies", [])
        if not isinstance(proxy, dict) or proxy.get("name") not in old_member_names
    ]
    mihomo_active_path.write_text(
        yaml.safe_dump(mihomo_partial, allow_unicode=True, sort_keys=False), encoding="utf-8",
    )

    # Simulate a target written by the previous parser: it retains the exact
    # raw/profile identity fields but has no durable provenance witness.
    connection = sqlite3.connect(state / "fwrouter.db", timeout=5)
    try:
        row = connection.execute("SELECT raw_json FROM servers WHERE server_id=?", (new_id,)).fetchone()
        assert row is not None
        legacy_raw = json.loads(row[0])
        assert isinstance(legacy_raw.get("_fwrouter_profile_provenance"), dict)
        legacy_raw.pop("_fwrouter_profile_provenance")
        connection.execute("UPDATE servers SET raw_json=? WHERE server_id=?",
                           (json.dumps(legacy_raw, sort_keys=True), new_id))
        connection.commit()
    finally:
        connection.close()

    code, context = http_json(f"{restarted_api}/__acceptance/xray/identity-context")
    assert code == 200 and context.get("ok") is True
    mapping = {
        "old_server_id": old_id, "new_server_id": new_id,
        "expected_identity_revision": 1,
        "old_profile": old_profile, "new_profile": new_profile,
    }
    manifest = {
        "version": 1,
        "expected_selection_revision": context["selection_revision"],
        "expected_identity_digest": context["identity_digest"],
        "checkpoint_sha256": old_checkpoint_sha,
        "mappings": [mapping],
    }
    def cli(payload: dict) -> tuple[int, dict]:
        code, result = http_json(
            f"{restarted_api}/__acceptance/xray/identity-cli", method="POST",
            payload=payload, timeout=90,
        )
        assert code == 200 and result.get("stderr_empty") is True, {
            "status": code, "ok": result.get("ok"), "error_code": result.get("error_code"),
        }
        return int(result.get("exit_code", 1)), result.get("receipt", {})

    def reconcile_after_identity_refresh(api: str) -> dict:
        code, generation_response = http_json(
            f"{api}/__acceptance/subscription/native-generation",
        )
        assert code == 200 and generation_response.get("ok") is True, {
            "status": code,
            "error_code": generation_response.get("error_code"),
        }
        inventory = generation_response.get("state")
        assert isinstance(inventory, dict)
        if inventory.get("status") == "pending":
            code, result = http_json(
                f"{api}/__acceptance/xray/publish-pending-inventory", method="POST",
                payload={"include_vpn_auto": True}, timeout=75,
            )
            assert code == 200 and result.get("ok") is True, {
                "status": code, "ok": result.get("ok"), "stage": result.get("stage"),
                "error_code": result.get("error_code"),
                "native_generation_verified": result.get("native_generation_verified"),
            }
            assert result.get("native_generation_verified") is True
            return result
        code, result = http_json(
            f"{api}/__acceptance/xray/reconcile", method="POST",
            payload={"include_vpn_auto": True}, timeout=75,
        )
        assert code == 200 and result.get("ok") is True, {
            "status": code, "ok": result.get("ok"), "stage": result.get("stage"),
            "error_code": result.get("error_code"),
        }
        return result

    # Reconstruct the exact pre-upgrade durable state before the CLI dry-run:
    # its manifest is explicitly bound to this checkpoint SHA. The owned
    # worker is stopped while this synthetic historical snapshot is restored.
    prior_worker = stack["worker"]
    prior_worker.terminate()
    prior_worker.wait(timeout=5)
    checkpoint.write_bytes(old_checkpoint_bytes)
    checkpoint.chmod(0o600)
    restarted_worker, restarted_api = stack["start_worker"]()
    stack["worker"], stack["api"] = restarted_worker, restarted_api
    assert checkpoint.is_file() and hashlib.sha256(checkpoint.read_bytes()).hexdigest() == old_checkpoint_sha

    reloads_before_intent = native.reload_calls
    active_before_intent = active_path.read_bytes()
    dry_code, dry = cli({"action": "dry-run", "manifest": manifest})
    assert dry_code == 0 and dry.get("ok") is True and dry.get("native_applied") is False, {
        "exit_code": dry_code, "receipt": {key: dry.get(key) for key in
            ("ok", "error_code", "status", "mappings_count", "changed_count", "native_applied")},
    }
    assert (dry.get("mappings_count") == 1 and dry.get("changed_count") == 1
            and dry.get("preserved_identity_count") == len(baseline_pairs)
            and dry.get("native_applied") is False
            and dry.get("fresh_generation_required") is True), {
        "mappings_count": dry.get("mappings_count"),
        "changed_count": dry.get("changed_count"),
        "preserved_identity_count": dry.get("preserved_identity_count"),
        "expected_preimage_count": len(baseline_pairs),
        "native_applied": dry.get("native_applied"),
        "fresh_generation_required": dry.get("fresh_generation_required"),
    }
    connection = sqlite3.connect(state / "fwrouter.db", timeout=5)
    try:
        current_raw = json.loads(connection.execute(
            "SELECT raw_json FROM servers WHERE server_id=?", (new_id,),
        ).fetchone()[0])
        assert "_fwrouter_profile_provenance" not in current_raw
        assert current_raw.get("_fwrouter_identity_migration_required") is True
    finally:
        connection.close()

    assert native.reload_calls == reloads_before_intent
    apply_code, applied = cli({
        "action": "apply", "manifest": manifest,
        "expected_plan_sha256": str(dry.get("plan_sha256") or ""),
    })
    assert apply_code == 0 and applied.get("ok") is True, {
        "exit_code": apply_code, "receipt": {key: applied.get(key) for key in
            ("ok", "error_code", "status", "changed_count", "native_applied", "public_snapshots_changed")},
    }
    assert applied.get("native_applied") is False and applied.get("public_snapshots_changed") is False
    assert (applied.get("mappings_count") == 1 and applied.get("changed_count") == 1
            and applied.get("preserved_identity_count") == len(baseline_pairs)
            and applied.get("native_applied") is False
            and applied.get("public_snapshots_changed") is False
            and applied.get("fresh_generation_required") is True), {
        "mappings_count": applied.get("mappings_count"),
        "changed_count": applied.get("changed_count"),
        "preserved_identity_count": applied.get("preserved_identity_count"),
        "expected_preimage_count": len(baseline_pairs),
        "native_applied": applied.get("native_applied"),
        "public_snapshots_changed": applied.get("public_snapshots_changed"),
        "fresh_generation_required": applied.get("fresh_generation_required"),
    }
    new_identity_id, migrated_credentials = profile_credentials(new_id)
    assert (new_identity_id == old_identity_id and migrated_credentials == old_migration_credentials
            and len(migrated_credentials) == 8), {
        "same_logical_identity": new_identity_id == old_identity_id,
        "credential_count": len(migrated_credentials),
        "expected_credential_count": 8,
        "credential_sha256": identity_fingerprint(migrated_credentials),
        "expected_sha256": identity_fingerprint(old_migration_credentials),
    }
    connection = sqlite3.connect(state / "fwrouter.db", timeout=5)
    try:
        persisted_raw = json.loads(connection.execute(
            "SELECT raw_json FROM servers WHERE server_id=?", (new_id,),
        ).fetchone()[0])
        persisted_witness = persisted_raw.pop("_fwrouter_profile_provenance")
        assert persisted_raw.pop("_fwrouter_identity_migration_required", None) is None
        assert legacy_raw.pop("_fwrouter_identity_migration_required", None) is True
        assert persisted_raw == legacy_raw, {
            "migration_marker_cleared": True,
            "profile_projection_preserved": persisted_raw == legacy_raw,
            "persisted_projection_sha256": hashlib.sha256(
                json.dumps(persisted_raw, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
            "expected_projection_sha256": hashlib.sha256(
                json.dumps(legacy_raw, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
        }
        assert persisted_witness.get("content") == new_id.removeprefix("sub:")
        assert persisted_witness.get("version") == 1
    finally:
        connection.close()
    assert native.reload_calls == reloads_before_intent
    assert active_path.read_bytes() == active_before_intent
    assert {token: read_subscription_rows(state, token)["snapshots"] for token in tokens} == before_snapshots

    # Old checkpoint recovery is fenced after migration intent and performs no
    # native write; the only authorized path is exact-SHA fresh supersession.
    code, declined = http_json(
        f"{stack['api']}/__acceptance/xray/reconcile", method="POST",
        payload={"direct_checkpoint": True}, timeout=75,
    )
    assert code == 200 and declined.get("ok") is False, {
        "status": code, "ok": declined.get("ok"), "recovered": declined.get("recovered"),
        "reason": declined.get("reason"),
    }
    assert native.reload_calls == reloads_before_intent
    supersede_code, superseded = cli({
        "action": "supersede", "checkpoint_sha256": old_checkpoint_sha,
        "migration_plan_sha256": str(applied["plan_sha256"]),
    })
    assert supersede_code == 0 and superseded.get("ok") is True and superseded.get("native_applied") is True, {
        "exit_code": supersede_code, "receipt": {key: superseded.get(key) for key in
            ("ok", "status", "stage", "error_code", "native_applied", "recovery_status", "recovery_reason")},
    }
    assert_native_matches_active(native, active_path)
    loaded_after_supersession = set(loaded_identities(native))
    assert len(loaded_after_supersession) == 78 and identity_fingerprint(loaded_after_supersession) == identity_fingerprint(unaffected | affected), {
        "loaded_count": len(loaded_after_supersession),
        "loaded_sha256": identity_fingerprint(loaded_after_supersession),
    }
    # Successful publication deliberately removes the active checkpoint. Prove
    # supersession through its retained CLI receipt, the deterministic durable
    # archive, and the migration ledger instead of expecting a pending marker.
    assert superseded.get("supersedes_checkpoint_sha256") == old_checkpoint_sha
    assert superseded.get("migration_plan_sha256") == applied.get("plan_sha256")
    transition_candidate_path = state / "xray" / ".generation" / "mihomo-transition.candidate.yaml"
    transition_candidate = yaml.safe_load(transition_candidate_path.read_text(encoding="utf-8"))
    transition_groups = {
        group.get("name"): group for group in transition_candidate.get("proxy-groups", [])
        if isinstance(group, dict)
    }
    transition_proxies = {
        proxy.get("name") for proxy in transition_candidate.get("proxies", [])
        if isinstance(proxy, dict)
    }
    assert old_runtime_name in transition_groups
    assert old_member_names <= transition_proxies
    final_mihomo = yaml.safe_load(mihomo_active_path.read_text(encoding="utf-8"))
    final_group_names = {
        group.get("name") for group in final_mihomo.get("proxy-groups", [])
        if isinstance(group, dict)
    }
    final_proxy_names = {
        proxy.get("name") for proxy in final_mihomo.get("proxies", [])
        if isinstance(proxy, dict)
    }
    assert old_runtime_name not in final_group_names
    assert not (old_member_names & final_proxy_names)
    assert not checkpoint.exists(), "successful supersession left an active generation checkpoint"
    archive_path = checkpoint.parent / "checkpoint-archive" / f"generation-checkpoint-{old_checkpoint_sha}.json"
    assert archive_path.is_file() and not archive_path.is_symlink()
    assert archive_path.read_bytes() == old_checkpoint_bytes
    connection = sqlite3.connect(state / "fwrouter.db", timeout=5)
    try:
        transition_count = connection.execute(
            "SELECT COUNT(*) FROM subscription_node_identity_transitions "
            "WHERE operator_plan_sha256=? AND checkpoint_sha256=?",
            (applied.get("plan_sha256"), old_checkpoint_sha),
        ).fetchone()[0]
    finally:
        connection.close()
    assert transition_count == 1, {"migration_transition_count": transition_count}
    assert all(read_subscription_rows(state, token)["snapshots"] for token in tokens)

    def snapshot_pairs(token: str) -> list[tuple[str, str, str]]:
        rows = read_subscription_rows(state, token)["snapshots"]
        nodes = json.loads(rows[0]["nodes_json"]) if rows else []
        return sorted(
            (str(node.get("server_id") or ""), str(node.get("client_uuid") or ""),
             str(node.get("client_email") or ""))
            for node in nodes if isinstance(node, dict)
        )

    snapshots_after_migration = {token: snapshot_pairs(token) for token in tokens}

    # Worker restart and another source observation retain the exact published
    # credential set and native proof.
    stack["worker"].terminate()
    stack["worker"].wait(timeout=5)
    restarted_worker, restarted_api = stack["start_worker"]()
    stack["worker"], stack["api"] = restarted_worker, restarted_api
    code, repeated_refresh = http_json(
        f"{restarted_api}/__acceptance/xray/identity-refresh", method="POST",
        payload={"profile": new_profile},
    )
    assert code == 200 and repeated_refresh.get("ok") is True and repeated_refresh.get("server_ids") == [new_id]
    reconcile_after_identity_refresh(restarted_api)
    assert_native_matches_active(native, active_path)
    loaded_after_restart = set(loaded_identities(native))
    assert len(loaded_after_restart) == 78 and identity_fingerprint(loaded_after_restart) == identity_fingerprint(unaffected | affected), {
        "loaded_count": len(loaded_after_restart),
        "loaded_sha256": identity_fingerprint(loaded_after_restart),
    }
    assert {token: snapshot_pairs(token) for token in tokens} == snapshots_after_migration

    # Grow the reviewed logical profile through the real parser/upsert path.
    # This makes the subsequent native generation materially different while
    # requiring all eight credentials to remain attached to the same logical
    # profile identity.
    grown_profile = json.loads(json.dumps(new_profile))
    grown_profile["outbounds"].insert(10, outbound(10))
    grown_id = content_id(grown_profile)
    identity_id_before_growth, credentials_before_growth = profile_credentials(new_id)
    growth_code, growth = http_json(
        f"{restarted_api}/__acceptance/xray/identity-refresh", method="POST",
        payload={"profile": grown_profile},
    )
    assert growth_code == 200 and growth.get("ok") is True and growth.get("server_ids") == [grown_id], {
        "status": growth_code, "ok": growth.get("ok"), "refreshed_count": growth.get("refreshed_count"),
    }
    grown_member_ids = profile_member_ids(grown_id)
    assert len(grown_member_ids) == 11 and refreshed_member_ids <= grown_member_ids
    identity_id_after_growth, credentials_after_growth = profile_credentials(grown_id)
    assert (identity_id_after_growth == identity_id_before_growth
            and credentials_after_growth == credentials_before_growth
            and len(credentials_after_growth) == 8), {
        "same_logical_identity": identity_id_after_growth == identity_id_before_growth,
        "credential_count": len(credentials_after_growth),
        "expected_credential_count": 8,
        "credential_sha256": identity_fingerprint(credentials_after_growth),
        "expected_sha256": identity_fingerprint(credentials_before_growth),
    }

    # Fault the validated private Mihomo final candidate only after the real
    # Xray apply and durable xray_applied checkpoint. This drives the production
    # owned-generation restore path, including CAS and native preimage proof,
    # instead of resuming the older stale checkpoint directly.
    before_integrity_fault = set(loaded_identities(native))
    assert len(before_integrity_fault) == 78
    assert before_integrity_fault == set(loaded_after_restart)
    mihomo_active_path = state / "generated" / "mihomo" / "config.yaml"
    mihomo_preimage_bytes = mihomo_active_path.read_bytes()
    snapshots_before_integrity_fault = {token: snapshot_pairs(token) for token in tokens}
    fence_code, fence_response = http_json(f"{restarted_api}/__acceptance/selection/fence")
    assert fence_code == 200 and fence_response.get("ok") is True
    fence_before_integrity_fault = fence_response.get("fence")
    assert isinstance(fence_before_integrity_fault, dict)

    native.hold_checkpoint_phase("xray_applied")
    reconcile_result: Queue = Queue(maxsize=1)

    def reconcile_with_candidate_integrity_fault() -> None:
        try:
            reconcile_result.put(("result", http_json(
                f"{restarted_api}/__acceptance/xray/publish-pending-inventory", method="POST",
                payload={"include_vpn_auto": True}, timeout=110,
            )), timeout=2)
        except Exception as exc:
            reconcile_result.put(("exception", exc), timeout=2)

    generation_thread = threading.Thread(
        target=reconcile_with_candidate_integrity_fault,
        name="acceptance-xray-applied-recovery", daemon=True,
    )
    generation_thread.start()
    try:
        assert native.checkpoint_entered.wait(25), (
            "real generation did not durably reach xray_applied before candidate integrity fault"
        )
        checkpoint_data = json.loads(checkpoint.read_text(encoding="utf-8"))
        staged = checkpoint_data.get("staged_generation")
        assert checkpoint_data.get("phase") == "xray_applied" and isinstance(staged, dict), {
            "phase": checkpoint_data.get("phase"), "staged_generation_present": isinstance(staged, dict),
        }
        assert (checkpoint_data.get("xray_runtime_incarnation_after")
                and checkpoint_data.get("mihomo_runtime_incarnation")), {
            "xray_incarnation_present": bool(checkpoint_data.get("xray_runtime_incarnation_after")),
            "mihomo_incarnation_present": bool(checkpoint_data.get("mihomo_runtime_incarnation")),
        }
        assert hashlib.sha256(mihomo_active_path.read_bytes()).hexdigest() == staged.get(
            "mihomo_transition_sha256"
        ), "active Mihomo config is not the checkpointed transition generation"
        assert_mihomo_launch_matches_active(native, mihomo_active_path)
        recorded_before_selection = checkpoint_data.get("auto_selection_before")
        assert isinstance(recorded_before_selection, dict)
        initial_revision = fence_before_integrity_fault.get("revision")
        checkpoint_revision = checkpoint_data.get("selection_revision")
        assert (type(initial_revision) is int
                and recorded_before_selection.get("selection_revision") == initial_revision
                and type(checkpoint_revision) is int
                and checkpoint_revision > initial_revision), {
            "initial_revision": initial_revision,
            "checkpoint_preimage_revision": recorded_before_selection.get("selection_revision"),
            "checkpoint_owned_revision": checkpoint_revision,
        }
        barrier_fence_code, barrier_fence_response = http_json(
            f"{restarted_api}/__acceptance/selection/fence",
        )
        assert barrier_fence_code == 200 and barrier_fence_response.get("ok") is True
        barrier_fence = barrier_fence_response.get("fence")
        assert (isinstance(barrier_fence, dict)
                and barrier_fence.get("revision") == checkpoint_revision
                and barrier_fence.get("active_server_id") == fence_before_integrity_fault.get("active_server_id")
                and barrier_fence.get("decision_id") == fence_before_integrity_fault.get("decision_id")), {
            "checkpoint_revision": checkpoint_revision,
            "barrier_fence": {key: barrier_fence.get(key) for key in
                ("revision", "active_server_id", "decision_id")} if isinstance(barrier_fence, dict) else None,
            "pre_operation_fence": {key: fence_before_integrity_fault.get(key) for key in
                ("revision", "active_server_id", "decision_id")},
        }
        after_selection = checkpoint_data.get("auto_selection_after")
        if isinstance(after_selection, dict):
            assert after_selection.get("selection_revision") == checkpoint_revision
            assert after_selection.get("selection_decision_id") == barrier_fence.get("decision_id")
        loaded_at_checkpoint = set(loaded_identities(native))
        assert sorted(loaded_at_checkpoint) == sorted(
            tuple(pair) for pair in checkpoint_data.get("expected_client_identities", [])
        )
        assert len(loaded_at_checkpoint) == 78
        candidate_value = staged.get("mihomo_final_path")
        assert isinstance(candidate_value, str) and candidate_value
        final_candidate = Path(candidate_value)
        generation_dir = state / "xray" / ".generation"
        assert final_candidate.parent.resolve() == generation_dir.resolve()
        candidate_info = final_candidate.lstat()
        assert stat.S_ISREG(candidate_info.st_mode) and candidate_info.st_size < 32 * 1024 * 1024
        expected_candidate_sha = str(staged.get("mihomo_final_sha256") or "")
        assert len(expected_candidate_sha) == 64
        assert hashlib.sha256(final_candidate.read_bytes()).hexdigest() == expected_candidate_sha
        assert expected_candidate_sha != hashlib.sha256(mihomo_preimage_bytes).hexdigest(), (
            "11-member source refresh did not produce a distinct Mihomo final generation"
        )
        integrity_marker = b"\n# acceptance post-validation candidate integrity fault\n"
        fd = os.open(final_candidate, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            written = os.write(fd, integrity_marker)
            assert written == len(integrity_marker), "short write while injecting bounded candidate integrity fault"
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        native.release_checkpoint()
        generation_thread.join(timeout=115)

    assert not generation_thread.is_alive(), "bounded native checkpoint recovery did not finish"
    result_kind, reconcile_value = reconcile_result.get(timeout=2)
    assert result_kind == "result", {"thread_result": result_kind, "exception_type": type(reconcile_value).__name__}
    fault_code, fault = reconcile_value
    assert fault_code == 200 and fault.get("ok") is False, {
        "status": fault_code, "ok": fault.get("ok"), "stage": fault.get("stage"),
        "error_code": fault.get("error_code"),
    }
    assert fault.get("native_generation_verified") is False
    assert fault.get("stage") == "mihomo_final_apply"
    assert fault.get("error_code") == "XRAY_GENERATION_FINAL_APPLY_FAILED"
    recovery = fault.get("generation_recovery")
    assert isinstance(recovery, dict) and recovery.get("ok") is True
    assert recovery.get("recovered") == "restored_last_good"
    assert recovery.get("mihomo_preimage_proof", {}).get("ok") is True
    assert recovery.get("mihomo_preimage_proof", {}).get("policy_verified") is True
    assert recovery.get("mihomo_preimage_proof", {}).get("native_config_validated") is True
    assert recovery.get("xray_reload") is True and recovery.get("mihomo_restart") is True
    assert recovery.get("derived_rows_restored") is True
    assert recovery.get("xray_runtime_readback", {}).get("ok") is True
    assert recovery.get("mihomo_runtime_readback", {}).get("ok") is True
    assert not checkpoint.exists(), "successful last-good restore left an active generation checkpoint"
    assert mihomo_active_path.read_bytes() == mihomo_preimage_bytes
    assert_native_matches_active(native, active_path)
    assert_mihomo_launch_matches_active(native, mihomo_active_path)
    loaded_after_integrity_fault = set(loaded_identities(native))
    assert loaded_after_integrity_fault == before_integrity_fault and len(loaded_after_integrity_fault) == 78
    assert {token: snapshot_pairs(token) for token in tokens} == snapshots_before_integrity_fault
    restored_fence_code, restored_fence_response = http_json(
        f"{restarted_api}/__acceptance/selection/fence",
    )
    assert restored_fence_code == 200 and restored_fence_response.get("ok") is True
    restored_fence = restored_fence_response.get("fence")
    assert isinstance(restored_fence, dict)
    assert (type(fence_before_integrity_fault.get("revision")) is int
            and type(restored_fence.get("revision")) is int
            and restored_fence["revision"] > checkpoint_revision)
    assert restored_fence.get("active_server_id") == fence_before_integrity_fault.get("active_server_id")
    assert restored_fence.get("decision_id") == fence_before_integrity_fault.get("decision_id")

    # Restart the worker and prove the source refresh and native reconcile still
    # converge on the grown topology without changing any of the 78 identities.
    stack["worker"].terminate()
    stack["worker"].wait(timeout=5)
    restarted_worker, restarted_api = stack["start_worker"]()
    stack["worker"], stack["api"] = restarted_worker, restarted_api
    code, recovered_refresh = http_json(
        f"{restarted_api}/__acceptance/xray/identity-refresh", method="POST",
        payload={"profile": grown_profile},
    )
    assert code == 200 and recovered_refresh.get("ok") is True
    assert recovered_refresh.get("server_ids") == [grown_id]
    reconcile_after_identity_refresh(restarted_api)
    assert_native_matches_active(native, active_path)
    assert_mihomo_launch_matches_active(native, mihomo_active_path)
    loaded_after_fault_restart = set(loaded_identities(native))
    assert loaded_after_fault_restart == before_integrity_fault and len(loaded_after_fault_restart) == 78
    identity_id_after_restart, credentials_after_restart = profile_credentials(grown_id)
    assert (identity_id_after_restart == identity_id_before_growth
            and credentials_after_restart == credentials_before_growth)

    # Tombstone one migrated public credential, then prove stale snapshots and
    # repeated source refresh/restart cannot put it back into native Xray.
    revoked_token = tokens[0]
    revoked_email = f"sub-{digest(revoked_token, 10)}-{digest(old_id, 12)}@fwrouter.local"
    revoked_uuid = str(uuid5(NAMESPACE_DNS, f"fwrouter-subscription:{revoked_token}:{old_id}"))
    revoke_code, revoke_result = http_json(
        f"{restarted_api}/__acceptance/xray/identity-revoke", method="POST",
        payload={"server_id": grown_id},
    )
    assert revoke_code == 200 and revoke_result.get("ok") is True and revoke_result.get("revoked") is True, {
        "status": revoke_code, "ok": revoke_result.get("ok"), "revoked": revoke_result.get("revoked"),
    }
    from urllib.parse import quote, urlsplit, urlunsplit
    from urllib.request import urlopen
    api_parts = urlsplit(restarted_api)
    assert api_parts.scheme == "http" and api_parts.netloc and api_parts.path.endswith("/api/v2")
    public_url = urlunsplit((
        api_parts.scheme, api_parts.netloc, f"/s/{quote(revoked_token, safe='')}",
        "format=raw-vless", "",
    ))
    with urlopen(public_url, timeout=10) as response:
        assert response.status == 200, {"public_export_status": response.status}
        public_export = response.read(1024 * 1024).decode("utf-8", "replace")
    assert revoked_email not in public_export and revoked_uuid not in public_export, {
        "export_length": len(public_export), "export_sha256": hashlib.sha256(public_export.encode()).hexdigest(),
    }
    stack["worker"].terminate()
    stack["worker"].wait(timeout=5)
    restarted_worker, restarted_api = stack["start_worker"]()
    stack["worker"], stack["api"] = restarted_worker, restarted_api
    code, post_revoke_refresh = http_json(
        f"{restarted_api}/__acceptance/xray/identity-refresh", method="POST",
        payload={"profile": grown_profile},
    )
    assert code == 200 and post_revoke_refresh.get("ok") is True
    assert post_revoke_refresh.get("server_ids") == [grown_id]
    reconcile_after_identity_refresh(restarted_api)
    loaded_after_revocation = set(loaded_identities(native))
    expected_after_revocation = (unaffected | affected) - {(revoked_uuid, revoked_email)}
    assert len(loaded_after_revocation) == 77 and identity_fingerprint(loaded_after_revocation) == identity_fingerprint(expected_after_revocation), {
        "loaded_count": len(loaded_after_revocation),
        "loaded_sha256": identity_fingerprint(loaded_after_revocation),
        "expected_count": len(expected_after_revocation),
        "expected_sha256": identity_fingerprint(expected_after_revocation),
    }
    assert all(
        revoked_uuid not in {pair[1] for pair in snapshot_pairs(token)}
        and revoked_email not in {pair[2] for pair in snapshot_pairs(token)}
        for token in tokens
    )

    # A subsequent real API create with an injected native reload fault retains
    # the proven native preimage while leaving the new desired client visible.
    probe_email = "identity-rollback-probe@acceptance.invalid"
    native.fail_next_reload()
    reload_count_before_fault = native.reload_calls
    code, accepted = http_json(
        f"{restarted_api}/xray/clients", method="POST",
        payload={"alias": "identity-rollback-probe", "email": probe_email,
                 "requested_by": "hosted-acceptance", "allow_blocked_egress": True},
        timeout=75,
    )
    accepted_data = accepted.get("data") if isinstance(accepted.get("data"), dict) else {}
    accepted_error = accepted_data.get("error") if isinstance(accepted_data.get("error"), dict) else {}
    job_ref = accepted_data.get("job") if isinstance(accepted_data.get("job"), dict) else {}
    job_id = str(job_ref.get("job_id") or "")
    immediate_failure = bool(
        accepted.get("ok") is False
        and accepted_error.get("code") == "HARNESS_XRAY_RELOAD_FAILURE"
        and accepted_error.get("job_id") == job_id
    )
    accepted_for_processing = bool(
        accepted.get("ok") is True
        and accepted_data.get("status") in {"queued", "running"}
        and accepted_data.get("error") is None
    )
    assert code == 200 and job_id and (immediate_failure or accepted_for_processing), {
        "status": code, "ok": accepted.get("ok"),
        "error_code": accepted_error.get("code"), "job_id_present": bool(job_id),
        "error_job_id_matches": accepted_error.get("job_id") == job_id,
        "accepted_status": accepted_data.get("status"),
    }
    # Poll only this returned job id; never treat an arbitrary API failure as
    # evidence that the harness-injected native reload boundary was reached.
    terminal_job = await_job(restarted_api, {"data": {"job": {"job_id": job_id}}})
    terminal_result = terminal_job.get("result") if isinstance(terminal_job.get("result"), dict) else {}
    terminal_client = terminal_result.get("xray_client") if isinstance(terminal_result.get("xray_client"), dict) else {}
    terminal_client_result = terminal_client.get("result") if isinstance(terminal_client.get("result"), dict) else {}
    assert (terminal_job.get("job_id") == job_id and terminal_job.get("status") == "failed"
            and terminal_job.get("error_code") == "HARNESS_XRAY_RELOAD_FAILURE"
            and terminal_result.get("error_code") == "HARNESS_XRAY_RELOAD_FAILURE"
            and terminal_client_result.get("error_code") == "HARNESS_XRAY_RELOAD_FAILURE"), {
        "same_job": terminal_job.get("job_id") == job_id,
        "job_status": terminal_job.get("status"),
        "job_error_code": terminal_job.get("error_code"),
        "result_error_code": terminal_result.get("error_code"),
        "native_error_code": terminal_client_result.get("error_code"),
    }
    assert native.reload_calls > reload_count_before_fault, {
        "reload_calls_before": reload_count_before_fault,
        "reload_calls_after": native.reload_calls,
    }
    loaded_after_rollback = set(loaded_identities(native))
    assert len(loaded_after_rollback) == 77 and identity_fingerprint(loaded_after_rollback) == identity_fingerprint(expected_after_revocation), {
        "loaded_count": len(loaded_after_rollback),
        "loaded_sha256": identity_fingerprint(loaded_after_rollback),
    }
    desired = json.loads(active_path.read_text(encoding="utf-8"))
    desired_clients = next(item for item in desired["inbounds"] if item.get("tag") == "vless-ws")["settings"]["clients"]
    assert any(item.get("email") == probe_email for item in desired_clients)


def test_xray_generation_fence_rejects_replaced_native_incarnation(acceptance_stack):
    """A process replacement after a durable generation fence cannot pass publication."""
    stack = acceptance_stack
    native = stack["native"]
    from .test_core_provider_mihomo import (
        _configure_provider, _enable_owned_mihomo_runtime_state, _seed_source,
    )
    _enable_owned_mihomo_runtime_state()
    source_ref = _seed_source(stack)
    _configure_provider(stack["api"], source_ref)
    token = "incarnation-checkpoint@acceptance.invalid"
    code, response = http_json(
        f"{stack['api']}/xray/clients", method="POST",
        payload={"alias": "phase-c-incarnation-fence", "email": token,
                 "requested_by": "hosted-acceptance", "allow_blocked_egress": True},
    )
    assert code == 200 and response.get("ok") is True, response
    seeded = await_job(stack["api"], response)
    assert seeded.get("status") == "success", seeded
    prior_snapshots = read_subscription_rows(stack["state"], token)["snapshots"]
    prior_loaded = set(loaded_identities(native))
    assert prior_snapshots and prior_loaded, "seeded generation did not establish prior published/native state"
    checkpoint = stack["state"] / "xray" / ".generation" / "generation-checkpoint.json"
    native.hold_checkpoint_phase("xray_applied")
    worker_log_snapshot = _snapshot_provider_worker_logs(stack["root"])
    enable_code, enable = http_json(
        f"{stack['api']}/subscription/sources/{source_ref}/provider",
        method="POST", payload={"action": "enable"}, timeout=15,
    )
    assert enable_code == 200 and enable.get("ok") is True and enable["data"].get("accepted") is True, enable
    assert native.checkpoint_entered.wait(25), "generation did not durably reach xray_applied checkpoint"
    checkpoint_data = json.loads(checkpoint.read_text(encoding="utf-8"))
    expected_incarnation = str(checkpoint_data.get("xray_runtime_incarnation_after") or "")
    assert checkpoint_data.get("phase") == "xray_applied" and expected_incarnation, checkpoint_data
    prior_pid = native.process.pid if native.process is not None else None
    try:
        native.restart_xray_child()
        assert native.process is not None and native.process.pid != prior_pid
        current_incarnation = native.rpc("runtime_inspect", {"container_id": f"{native.process.pid:x}"})
        assert current_incarnation.get("ok") is True, current_incarnation
        observed_incarnation = str((current_incarnation.get("details") or {}).get("stdout") or "")
        assert observed_incarnation and observed_incarnation != expected_incarnation
    finally:
        native.release_checkpoint()
    job = await_job(stack["api"], enable)
    assert job.get("status") == "failed", job
    assert job.get("error_code") == "PROVIDER_LOCAL_VERIFICATION_FAILED", "unexpected public failure taxonomy"
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    assert result.get("last_good_retained") is True, "failed generation did not report last-good retention"
    worker_records = _provider_worker_records_since(stack["root"], worker_log_snapshot)
    readback_failures = [
        record for record in worker_records
        if record.get("event") == "refresh_result"
        and record.get("targeted_source_match") is True
        and isinstance(record.get("summary"), dict)
        and isinstance(record["summary"].get("error"), dict)
        and record["summary"]["error"].get("code") == "XRAY_GENERATION_RUNTIME_READBACK_FAILED"
    ]
    assert readback_failures, "worker did not record the expected native-incarnation readback failure"
    assert readback_failures[-1]["summary"].get("last_good_retained") is True
    assert read_subscription_rows(stack["state"], token)["snapshots"] == prior_snapshots, (
        "failed generation published a new subscription snapshot"
    )
    assert prior_loaded.issubset(set(loaded_identities(native))), (
        "failed generation replaced a previously loaded native identity"
    )
    assert_native_matches_active(native, stack["state"] / "xray" / "config.json")
    assert_mihomo_launch_matches_active(native, stack["state"] / "generated" / "mihomo" / "config.yaml")


def test_xray_create_job_cannot_succeed_before_actual_native_readback_completes(acceptance_stack):
    """Hold the real HandlerService readback and inspect the still-running API job."""
    stack = acceptance_stack
    native = stack["native"]
    token = "readback-barrier@acceptance.invalid"
    native.hold_action("api_inbound_users")
    code, response = http_json(
        f"{stack['api']}/xray/clients", method="POST",
        payload={"alias": "readback-barrier", "email": token,
                 "requested_by": "hosted-acceptance", "allow_blocked_egress": True},
    )
    assert code == 200 and response.get("ok") is True, response
    assert native.action_entered.wait(25), "create job did not reach actual HandlerService readback"
    job_id = str(((response.get("data") or {}).get("job") or {}).get("job_id") or "")
    assert job_id, response
    try:
        pending_code, pending = http_json(f"{stack['api']}/jobs/{job_id}")
        assert pending_code == 200 and pending.get("ok") is True, pending
        pending_job = (pending.get("data") or {}).get("job") or {}
        assert pending_job.get("status") not in {"success", "failed", "cancelled"}, pending_job
        duplicate_code, duplicate = http_json(
            f"{stack['api']}/xray/clients", method="POST",
            payload={"alias": "readback-barrier", "email": token,
                     "requested_by": "hosted-acceptance", "allow_blocked_egress": True},
        )
        assert duplicate_code == 200 and duplicate.get("ok") is True, duplicate
        duplicate_job = (duplicate.get("data") or {}).get("job") or {}
        assert duplicate_job.get("job_id") == job_id, duplicate
    finally:
        native.release_action()
    job = await_job(stack["api"], response)
    assert job.get("status") == "success", job
    assert (token_uuid(job), token) in set(loaded_identities(native))
    assert_native_matches_active(native, stack["state"] / "xray" / "config.json")
    assert_mihomo_launch_matches_active(native, stack["state"] / "generated" / "mihomo" / "config.yaml")


def test_xray_create_job_fails_on_native_readback_transport_error(acceptance_stack):
    """A real HandlerService readback transport error cannot be reported as success."""
    stack = acceptance_stack
    native = stack["native"]
    native.fail_next_action("api_inbound_users")
    code, response = http_json(
        f"{stack['api']}/xray/clients", method="POST",
        payload={"alias": "readback-error", "email": "readback-error@acceptance.invalid",
                 "requested_by": "hosted-acceptance", "allow_blocked_egress": True},
    )
    assert code == 200 and response.get("ok") is True, response
    job = await_job(stack["api"], response)
    assert job.get("status") == "failed", job
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    client_result = result.get("xray_client") if isinstance(result.get("xray_client"), dict) else {}
    assert client_result.get("ok") is False, job
    assert_native_matches_active(native, stack["state"] / "xray" / "config.json")
    assert_mihomo_launch_matches_active(native, stack["state"] / "generated" / "mihomo" / "config.yaml")


def test_real_core_selection_cas_miss_reconciles_after_native_readback_without_provider_retry(acceptance_stack):
    """Advance Core's real fence after a real runtime switch and verify fresh reconciliation."""
    stack = acceptance_stack
    api, native, bridge = stack["api"], stack["native"], stack["provider_bridge"]
    from .test_core_provider_mihomo import (
        _configure_provider, _enable_owned_mihomo_runtime_state, _seed_source,
    )
    _enable_owned_mihomo_runtime_state()
    source_ref = _seed_source(stack)
    _configure_provider(api, source_ref)
    token = "selection-cas-miss@acceptance.invalid"
    code, created = http_json(
        f"{api}/xray/clients", method="POST",
        payload={"alias": "selection-cas-miss", "email": token,
                 "requested_by": "hosted-acceptance", "allow_blocked_egress": True},
    )
    assert code == 200 and created.get("ok") is True, created
    assert await_job(api, created).get("status") == "success"

    code, accepted = http_json(
        f"{api}/subscription/sources/{source_ref}/provider", method="POST",
        payload={"action": "enable"}, timeout=15,
    )
    assert code == 200 and accepted.get("ok") is True and accepted["data"].get("accepted") is True, accepted
    enabled = await_job(api, accepted)
    assert enabled.get("status") == "success", enabled

    fence_code, fence_before_fault = http_json(f"{api}/__acceptance/selection/fence")
    assert fence_code == 200 and fence_before_fault.get("ok") is True, fence_before_fault
    competing_code, competing = http_json(
        f"{api}/__acceptance/selection/runtime-reject", method="POST", payload={},
    )
    assert competing_code == 200 and competing.get("ok") is True, competing
    assert competing.get("selector") == "vpn-auto" and competing.get("selected_target") == "REJECT", competing
    assert competing.get("membership_confirmed") is True and competing.get("readback_confirmed") is True, competing
    fence_code, fence_before_switch = http_json(f"{api}/__acceptance/selection/fence")
    assert fence_code == 200 and fence_before_switch.get("ok") is True, fence_before_switch
    assert fence_before_switch["fence"] == fence_before_fault["fence"], {
        "before_fault": fence_before_fault, "after_runtime_fault": fence_before_switch,
    }

    native.hold_action("selection_commit_barrier")
    switch_result: Queue = Queue(maxsize=1)

    def switch_vpn_auto() -> None:
        try:
            switch_result.put(http_json(
                f"{api}/selector/vpn-auto/switch", method="POST",
                payload={
                    "confirm_switch": True, "exclude_active": False, "update_ping_state": False,
                    "limit": 20, "timeout_ms": 1000, "reason": "acceptance_selection_cas_miss",
                    "requested_by": "external_client",
                    "management_context": {"client_name": "hosted-acceptance",
                                           "action": "selection_cas_miss_reconcile"},
                },
                timeout=90,
            ))
        except BaseException as exc:
            switch_result.put(exc)

    switch_thread = threading.Thread(target=switch_vpn_auto, daemon=True)
    switch_thread.start()
    try:
        assert native.action_entered.wait(30), "real selector did not reach its persistent CAS after native apply/readback"
        commit = native.last_selection_commit
        actual_probe = native.last_mihomo_probe_result
        assert commit and commit.get("server_id"), commit
        assert actual_probe and actual_probe.get("probe_ok") is True, actual_probe
        assert str(actual_probe.get("logical_runtime_target") or ""), actual_probe
        pause_fence = fence_before_switch["fence"]
        assert commit.get("expected_revision") == pause_fence["revision"], {"commit": commit, "fence": pause_fence}

        # This explicit test fault uses the application's monotonic revision helper
        # in its real transaction; it does not invent a selector result or edit the
        # active selection/provenance fields.
        advance_code, advanced = http_json(
            f"{api}/__acceptance/selection/revision/advance", method="POST",
            payload={"expected_revision": commit["expected_revision"]},
        )
        assert advance_code == 200 and advanced.get("ok") is True, advanced
        assert advanced["revision"] == commit["expected_revision"] + 1, advanced
        assert advanced["fence"]["active_server_id"] == pause_fence["active_server_id"], advanced
        assert advanced["fence"]["decision_id"] == pause_fence["decision_id"], advanced
        provider_calls_at_cas = bridge.snapshot_calls()
        native.release_action()
    finally:
        native.release_action()
        switch_thread.join(timeout=95)

    assert not switch_thread.is_alive(), "public Core selector request did not finish after CAS barrier release"
    switched_result = switch_result.get_nowait()
    if isinstance(switched_result, BaseException):
        raise switched_result
    switched_code, switched = switched_result

    assert switched_code == 200 and switched.get("ok") is True, switched
    selector_result = switched.get("data", {}).get("selector", {})
    assert selector_result.get("applied") is True, selector_result
    assert selector_result.get("apply_result", {}).get("ok") is True, selector_result
    transition = selector_result.get("auto_transition") if isinstance(selector_result.get("auto_transition"), dict) else {}
    selected_runtime_target = str(transition.get("selected_runtime_target") or "")
    assert selected_runtime_target, selector_result
    assert selector_result.get("active_after_runtime_target") == selected_runtime_target, selector_result
    assert selector_result.get("selector_readback_matches") is True, selector_result

    job = switched

    def nested_dicts(value):
        if isinstance(value, dict):
            yield value
            for child in value.values():
                yield from nested_dicts(child)
        elif isinstance(value, list):
            for child in value:
                yield from nested_dicts(child)

    reconcile_proof = next((item for item in nested_dicts(job)
                            if item.get("reconciled_after_cas_miss") is True
                            and item.get("reconciliation_after_cas_miss", {}).get("outcome") == "repaired"), None)
    assert reconcile_proof is not None, job
    assert reconcile_proof.get("selection_outcome") == "selected", reconcile_proof
    assert reconcile_proof.get("error_code") is None, reconcile_proof
    assert bridge.snapshot_calls() == provider_calls_at_cas, "CAS repair issued a second provider HTTP request"

    fence_code, final_fence = http_json(f"{api}/__acceptance/selection/fence")
    assert fence_code == 200 and final_fence.get("ok") is True, final_fence
    fence = final_fence["fence"]
    assert fence["revision"] == advanced["revision"] + 1, fence
    assert fence["active_server_id"] == commit["server_id"], {"commit": commit, "fence": fence}
    state_code, state = http_json(f"{api}/state/vpn")
    assert state_code == 200 and state.get("ok") is True, state
    actual_active = state["data"]["vpn"]["effective"]["server_health"]["active"]["server_id"]
    assert actual_active == commit["server_id"], {"expected": commit["server_id"], "state": state}
    assert_native_matches_active(native, stack["state"] / "xray" / "config.json")
    assert_mihomo_launch_matches_active(native, stack["state"] / "generated" / "mihomo" / "config.yaml")


def token_uuid(job: dict) -> str:
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    client = (result.get("xray_client") or {}).get("client") or {}
    return str(client.get("client_id") or "")


def test_concurrent_xray_create_alias_update_and_delete_preserve_committed_clients(acceptance_stack):
    """Concurrent public CRUD intents serialize through the real Xray writer guard."""
    stack = acceptance_stack
    native = stack["native"]
    first_email = "crud-existing@acceptance.invalid"
    first_code, first_response = http_json(
        f"{stack['api']}/xray/clients", method="POST",
        payload={"alias": "crud-existing", "email": first_email,
                 "requested_by": "hosted-acceptance", "allow_blocked_egress": True},
    )
    assert first_code == 200 and first_response.get("ok") is True, first_response
    first_job = await_job(stack["api"], first_response)
    assert first_job.get("status") == "success", first_job
    first_id = token_uuid(first_job)
    assert first_id

    second_email = "crud-concurrent@acceptance.invalid"
    native.hold_next_reload()
    create_code, create_response = http_json(
        f"{stack['api']}/xray/clients", method="POST",
        payload={"alias": "crud-concurrent", "email": second_email,
                 "requested_by": "hosted-acceptance", "allow_blocked_egress": True},
    )
    assert create_code == 200 and create_response.get("ok") is True, create_response
    assert native.reload_entered.wait(25), "concurrent create did not reach actual Xray reload"
    staged_second_id = next((client_id for client_id, email in active_identities(
        stack["state"] / "xray" / "config.json") if email == second_email), "")
    assert staged_second_id, "active generation did not expose the real pending client identity"

    update_result: Queue = Queue(maxsize=1)

    def update_existing_alias() -> None:
        try:
            update_result.put(http_json(
                f"{stack['api']}/xray/clients/{first_id}", method="PATCH",
                payload={"alias": "crud-existing-updated", "requested_by": "hosted-acceptance"}, timeout=45,
            ))
        except BaseException as exc:
            update_result.put(exc)

    updater = threading.Thread(target=update_existing_alias, daemon=True)
    updater.start()
    delete_code, delete_response = http_json(
        f"{stack['api']}/xray/clients/{staged_second_id}",
        method="DELETE", payload={"requested_by": "hosted-acceptance"}, timeout=15,
    )
    native.release_reload()
    create_job = await_job(stack["api"], create_response)
    assert create_job.get("status") == "success", create_job
    created_id = token_uuid(create_job)
    assert created_id
    assert delete_code == 200 and delete_response.get("ok") is True, delete_response
    delete_job = await_job(stack["api"], delete_response)
    assert delete_job.get("status") == "success", delete_job
    updater.join(timeout=10)
    assert not updater.is_alive(), "alias update remained blocked after Xray create completed"
    update_reply = update_result.get_nowait()
    assert not isinstance(update_reply, BaseException), repr(update_reply)
    update_code, update_response = update_reply
    assert update_code == 200 and update_response.get("ok") is True, update_response

    list_code, listing = http_json(f"{stack['api']}/xray/clients")
    assert list_code == 200 and listing.get("ok") is True, listing
    clients = (listing.get("data") or {}).get("clients") or []
    by_id = {str(item.get("client_id") or item.get("id") or ""): item for item in clients if isinstance(item, dict)}
    assert first_id in by_id and by_id[first_id].get("alias") == "crud-existing-updated", by_id
    assert created_id not in by_id, by_id
    identities = set(loaded_identities(native))
    assert any(email == first_email for _client_id, email in identities), identities
    assert all(email != second_email for _client_id, email in identities), identities
    first_identity = next(pair for pair in identities if pair[1] == first_email)
    assert_applied_bindings(stack["state"], {first_identity})
    assert_native_matches_active(native, stack["state"] / "xray" / "config.json")
    assert_mihomo_launch_matches_active(native, stack["state"] / "generated" / "mihomo" / "config.yaml")


def test_xray_forced_reload_failure_restores_last_good_native_and_active_config(acceptance_stack):
    """An application-requested reload failure must restore and re-read last-good state."""
    stack = acceptance_stack
    active_path = stack["state"] / "xray" / "config.json"
    before = active_path.read_bytes()
    before_loaded = loaded_identities(stack["native"])
    stack["native"].fail_next_reload()
    code, response = http_json(
        f"{stack['api']}/xray/reload", method="POST", payload={"requested_by": "hosted-acceptance"},
    )
    assert code == 200 and response.get("ok") is False, response
    assert active_path.read_bytes() == before
    assert loaded_identities(stack["native"]) == before_loaded
    assert_native_matches_active(stack["native"], active_path)
