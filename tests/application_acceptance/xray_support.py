from __future__ import annotations

import json
import hashlib
import io
import sqlite3
import tarfile
from pathlib import Path

from .http_support import http_json


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


def loaded_identities(native) -> list[tuple[str, str]]:
    reply = native.rpc("api_inbound_users", {"tag": "vless-ws"})
    assert reply.get("ok") is True, reply
    payload = json.loads(reply["details"]["stdout"])
    users = payload.get("users")
    assert isinstance(users, list), payload
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
