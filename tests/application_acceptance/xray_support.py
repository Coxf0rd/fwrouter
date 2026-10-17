from __future__ import annotations

import hashlib
import ipaddress
import io
import json
import sqlite3
import tarfile
from pathlib import Path
from urllib.parse import urlsplit

from .http_support import http_json


def binding_identity_summary(bindings: list[dict], expected: set[tuple[str, str]], origin_lookup) -> dict:
    """Return bounded identity diagnostics without credentials or arbitrary metadata."""
    pairs = [(str(row.get("client_uuid") or row.get("client_id") or ""),
              str(row.get("client_email") or "")) for row in bindings]
    unique = set(pairs)
    origins = []
    for row, pair in zip(bindings, pairs):
        if pair in expected or len(origins) >= 4:
            continue
        origin = origin_lookup(row.get("subject_id")) or {}
        email = pair[1]
        origins.append({
            "subject_type": origin.get("subject_type") if origin.get("subject_type") in
                {"explicit_external_client", "core_module", "service", "lan_client", "tailscale_client"} else "other",
            "subject_role": origin.get("subject_role") if origin.get("subject_role") in
                {"vless_client", "module", "service", "lan_client", "tailscale_client"} else "other",
            "is_active": bool(origin.get("is_active")), "is_deleted": bool(origin.get("is_deleted")),
            "is_auxiliary_upstream_email": email == "acceptance-provider-upstream",
            "is_internal_auto_email": email.startswith("vpn-auto-"),
            "is_subscription_email": email.startswith("sub-"),
            "is_fixed_scale_email": email.startswith("acceptance-scale-fixed-"),
            "has_selected_server": bool(row.get("selected_server_id")),
        })
    return {"row_count": len(pairs), "unique_count": len(unique),
            "duplicate_count": len(pairs) - len(unique),
            "missing_count": len(expected - unique), "unexpected_count": len(unique - expected),
            "unexpected_origins": origins}


def await_job(api: str, response: dict, *, timeout: float = 75) -> dict:
    data = response.get("data") if isinstance(response.get("data"), dict) else {}
    job = data.get("job") if isinstance(data.get("job"), dict) else {}
    job_id = str(job.get("job_id") or "")
    assert job_id, f"API did not return the accepted job: {response!r}"
    timeout_seconds = max(1, min(90, int(timeout)))
    code, result = http_json(
        f"{api.rsplit('/api/v2', 1)[0]}/api/v2/__acceptance/jobs/{job_id}/wait",
        method="POST", payload={"timeout_seconds": timeout_seconds}, timeout=timeout_seconds + 5,
    )
    assert code == 200 and isinstance(result.get("job"), dict), result
    current_job = result["job"]
    assert current_job.get("status") in {"success", "failed", "cancelled"}, current_job
    return current_job


def enable_vpn_from_synthetic_subscription(api: str, state: Path, expected_url: str) -> None:
    """Enable the managed VPN owner through its public lifecycle using only this fixture's loopback source."""
    api_url = urlsplit(api)
    assert (api_url.scheme == "http" and api_url.path == "/api/v2"
            and not api_url.username and not api_url.password and not api_url.query and not api_url.fragment
            and api_url.hostname is not None and ipaddress.ip_address(api_url.hostname).is_loopback), (
                "acceptance API is not the owned loopback worker"
            )
    database = state / "fwrouter.db"
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=2)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        row = connection.execute(
            "SELECT url, metadata_json FROM subscription_state WHERE id=1"
        ).fetchone()
        assert row is not None, "synthetic subscription state is missing"
        metadata = json.loads(row["metadata_json"] or "{}")
        registry = metadata.get("subscriptions") if isinstance(metadata, dict) else None
        items = registry.get("items") if isinstance(registry, dict) else None
        assert isinstance(items, list), "synthetic subscription registry is missing"
        assert all(isinstance(item, dict) and str(item.get("url") or "").strip() for item in items), (
            "subscription registry contains an unclassifiable source"
        )
        saved_urls = {str(item["url"]).strip() for item in items}
        if row["url"]:
            saved_urls.add(str(row["url"]).strip())
        assert saved_urls == {expected_url}, "subscription registry is not limited to the owned synthetic source"
        parsed = urlsplit(expected_url)
        assert parsed.scheme == "http" and parsed.path == "/synthetic-subscription"
        assert not parsed.username and not parsed.password and not parsed.query and not parsed.fragment
        assert parsed.hostname is not None and ipaddress.ip_address(parsed.hostname).is_loopback
        enabled_bindings = connection.execute(
            "SELECT COUNT(*) FROM provider_bindings WHERE enabled=1"
        ).fetchone()[0]
        assert enabled_bindings == 0, "enabled provider binding could introduce an external refresh source"
    finally:
        connection.close()

    code, response = http_json(f"{api}/modules/vpn")
    initial_module = ((response.get("data") or {}).get("module")
                      if isinstance(response.get("data"), dict) else None)
    assert code == 200 and response.get("ok") is True and isinstance(initial_module, dict), {
        "status": code,
        "ok": response.get("ok"),
        "error_code": (response.get("error") or {}).get("code")
        if isinstance(response.get("error"), dict) else None,
    }
    assert (initial_module.get("module_name"), initial_module.get("desired_state"),
            initial_module.get("lifecycle_mode")) == ("vpn", "disabled", "managed"), (
                "synthetic acceptance fixture is not in the expected disabled managed state"
            )
    code, response = http_json(
        f"{api}/modules/vpn/desired-state",
        method="POST",
        payload={
            "desired_state": "enabled",
            "requested_by": "hosted-acceptance-synthetic",
            "run_now": True,
        },
        timeout=90,
    )
    assert code == 200 and response.get("ok") is True, {
        "status": code,
        "ok": response.get("ok"),
        "error_code": (response.get("error") or {}).get("code")
        if isinstance(response.get("error"), dict) else None,
    }
    job = await_job(api, response)
    assert job.get("status") == "success", {
        "job_status": job.get("status"),
        "error_code": job.get("error_code"),
    }
    code, response = http_json(f"{api}/modules/vpn")
    module = ((response.get("data") or {}).get("module")
              if isinstance(response.get("data"), dict) else None)
    assert code == 200 and response.get("ok") is True and isinstance(module, dict), {
        "status": code,
        "ok": response.get("ok"),
        "error_code": (response.get("error") or {}).get("code")
        if isinstance(response.get("error"), dict) else None,
    }
    assert (module.get("module_name"), module.get("desired_state"), module.get("lifecycle_mode"),
            module.get("runtime_state"), module.get("apply_state")) == (
                "vpn", "enabled", "managed", "running", "clean",
            ), "VPN owner did not converge through its public lifecycle"


def parse_inbound_users_reply(reply: dict) -> list[dict]:
    """Parse the successful native protobuf JSON response, including its empty form."""
    assert isinstance(reply, dict) and reply.get("ok") is True, reply
    details = reply.get("details")
    assert isinstance(details, dict), reply
    stdout = details.get("stdout")
    assert isinstance(stdout, str), reply
    payload = json.loads(stdout)
    assert isinstance(payload, dict), payload
    if payload == {}:
        users = []
    else:
        assert set(payload) == {"users"}, payload
        users = payload["users"]
    assert isinstance(users, list), payload
    return users


def loaded_identities(native) -> list[tuple[str, str]]:
    reply = native.rpc("api_inbound_users", {"tag": "vless-ws"})
    users = parse_inbound_users_reply(reply)
    result = []
    for user in users:
        account = user.get("account") if isinstance(user, dict) else None
        assert isinstance(account, dict), user
        assert account.get("_TypedMessage_") == "xray.proxy.vless.Account", account
        result.append((str(account.get("id") or ""), str(user.get("email") or "")))
    return sorted(result)


def active_identities(config_path: Path) -> list[tuple[str, str]]:
    config = json.loads(config_path.read_text(encoding="utf-8"))
    inbounds = config.get("inbounds") if isinstance(config, dict) else None
    inbound = next((item for item in inbounds or [] if isinstance(item, dict) and item.get("tag") == "vless-ws"), None)
    clients = ((inbound or {}).get("settings") or {}).get("clients", [])
    return sorted((str(item.get("id") or ""), str(item.get("email") or ""))
                  for item in clients if isinstance(item, dict))


def native_config_bytes(native) -> tuple[bytes, dict]:
    reply = native.rpc("runtime_container_id", {})
    assert reply.get("ok") is True, reply
    identity = str((reply.get("details") or {}).get("stdout") or "").strip()
    archive = native.rpc("runtime_config_archive", {"container_id": identity})
    assert archive.get("ok") is True, archive
    payload = archive["details"]["archive_bytes"]
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:") as tar:
        members = tar.getmembers()
        assert len(members) == 1 and members[0].name == "config.json" and members[0].isfile(), members
        stream = tar.extractfile(members[0])
        return (stream.read() if stream is not None else b""), archive["details"]


def read_subscription_rows(state: Path, token: str) -> dict[str, list[dict]]:
    """Read persisted desired identity and promoted snapshot without write-capable app imports."""
    database = state / "fwrouter.db"
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=2)
    connection.row_factory = sqlite3.Row
    try:
        clients = [dict(row) for row in connection.execute(
            "SELECT account_id, token, app_type, enabled, display_name FROM subscription_clients WHERE token = ?", (token,))]
        accounts = [dict(row) for row in connection.execute(
            """SELECT sa.account_id, sa.slug, sa.display_name, sa.enabled
               FROM subscription_accounts sa JOIN subscription_clients sc ON sc.account_id=sa.account_id
               WHERE sc.token = ?""", (token,))]
        snapshots = [dict(row) for row in connection.execute(
            "SELECT token, nodes_json, runtime_verified_at FROM subscription_profile_snapshots WHERE token = ?", (token,))]
        return {"accounts": accounts, "clients": clients, "snapshots": snapshots}
    finally:
        connection.close()


def assert_native_matches_active(native, config_path: Path) -> dict[str, str]:
    native_bytes, launch = native_config_bytes(native)
    active_bytes = config_path.read_bytes()
    launch_path = Path(str(launch.get("native_config_path") or "")).resolve(strict=True)
    assert launch_path.is_relative_to((native.root / "native-configs").resolve(strict=True))
    assert launch_path.read_bytes() == native_bytes
    assert native_bytes == active_bytes, {
        "active_sha256": hashlib.sha256(active_bytes).hexdigest(),
        "native_sha256": hashlib.sha256(native_bytes).hexdigest(),
        "launch": launch,
    }
    assert loaded_identities(native) == active_identities(config_path)
    return {"sha256": hashlib.sha256(active_bytes).hexdigest(),
            "native_config_path": str(launch.get("native_config_path") or "")}


def assert_applied_bindings(state: Path, expected: set[tuple[str, str]]) -> dict:
    """Read actual applied Xray binding projection and require generated identities."""
    path = state / "xray" / "fwrouter-bindings.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    bindings = payload.get("bindings") if isinstance(payload, dict) else None
    assert isinstance(bindings, list), payload
    applied_pairs = {
        (str(row.get("client_uuid") or row.get("client_id") or ""), str(row.get("client_email") or ""))
        for row in bindings if isinstance(row, dict) and row.get("status") == "applied"
    }
    assert payload.get("bindings_count") == payload.get("applied_count"), payload
    assert expected.issubset(applied_pairs), {
        "expected": sorted(expected), "applied": sorted(applied_pairs), "state": payload,
    }
    return payload


def assert_mihomo_launch_matches_active(native, config_path: Path) -> dict[str, str]:
    """Require the owned Mihomo child to have launched from the active bytes."""
    reply = native.rpc("mihomo_config_snapshot", {})
    assert reply.get("ok") is True, reply
    details = reply.get("details") if isinstance(reply.get("details"), dict) else {}
    snapshot = Path(str(details.get("native_config_path") or "")).resolve(strict=True)
    assert snapshot.is_relative_to((native.root / "native-mihomo-configs").resolve(strict=True)), snapshot
    snapshot_bytes = details.get("config_bytes")
    assert isinstance(snapshot_bytes, bytes) and snapshot.read_bytes() == snapshot_bytes
    active_bytes = config_path.read_bytes()
    assert snapshot_bytes == active_bytes, {
        "active_sha256": hashlib.sha256(active_bytes).hexdigest(),
        "native_sha256": hashlib.sha256(snapshot_bytes).hexdigest(),
        "native_config_path": str(snapshot),
    }
    return {"sha256": hashlib.sha256(snapshot_bytes).hexdigest(), "native_config_path": str(snapshot)}
