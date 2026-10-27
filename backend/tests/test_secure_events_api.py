from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Event, Thread
import uuid

import pytest
from fastapi.testclient import TestClient

from fwrouter_api.db.connection import db_session
from fwrouter_api.main import create_app
from fwrouter_api.services import api_client_registry as registry


def _admin(capsys, args: list[str]) -> dict:
    from fwrouter_api.cli import main as cli_main

    assert cli_main(["api-clients", *args]) == 0
    captured = capsys.readouterr()
    assert not captured.err
    return json.loads(captured.out)


def _clients(monkeypatch, tmp_path, capsys, *, bootstrap=False):
    host = tmp_path / "etc" / "fwrouter"
    host.mkdir(mode=0o700, parents=True)
    selector = host / "presentation-client-list-verifier.json"
    monkeypatch.setattr(registry, "SELECTOR_PATH", selector)
    monkeypatch.setattr(registry, "REGISTRY_PATH", host / "api-clients.json")
    monkeypatch.setattr(registry, "FENCE_PATH", host / "api-clients.required")
    monkeypatch.setattr(registry, "PENDING_PATH", host / "api-clients.pending")
    monkeypatch.setattr(registry, "LOCK_PATH", host / "api-clients.lock")
    monkeypatch.setattr(registry, "_trusted_uid", os.getuid)
    if not bootstrap:
        legacy_token = secrets.token_hex(32)
        selector.write_text(json.dumps({
            "schema_version": 1, "principal": "presentation-operator",
            "scopes": ["ui.client-list.write"],
            "token_sha256": hashlib.sha256(legacy_token.encode("ascii")).hexdigest(),
            "canonical_origin": "https://router.example.test",
        }), encoding="utf-8")
        selector.chmod(0o600)
    _admin(capsys, ["init", "--canonical-origin", "https://router.example.test"])
    if bootstrap:
        _admin(capsys, ["set-origin", "--expected-revision", "1", "--canonical-origin",
                        "https://new-router.example.test:8443"])
    secret_tokens = {}
    specifications = (
        ("web_ui", "presentation-admin", ("ui.client-list.write",)),
        ("cli", "cli-admin", ("ui.client-list.write",)),
        ("web_ui", "web-event-reader", ("events.read",)),
        ("cli", "cli-event-reader", ("events.read",)),
        ("integration", "event-reader", ("events.read", "ui.client-list.write")),
    )
    for revision, (kind, tag, scopes) in enumerate(specifications, start=2 if bootstrap else 1):
        secret_path = host / f"{tag}.token"
        command = [
            "create", "--expected-revision", str(revision), "--kind", kind,
            "--tag", tag,
        ]
        for scope in scopes:
            command.extend(("--scope", scope))
        command.extend(("--secret-file", str(secret_path)))
        record = _admin(capsys, command)
        token = secret_path.read_text(encoding="ascii").strip()
        for scope in scopes:
            secret_tokens[f"{scope}:{kind}"] = token
            secret_tokens[f"record:{scope}:{kind}"] = record
    from fwrouter_api.services import presentation_client_list_admission as presentation_admission
    monkeypatch.setattr(presentation_admission, "VERIFIER_PATH", selector)
    monkeypatch.setattr(presentation_admission, "_trusted_uid", os.getuid)
    _admin(capsys, ["cutover", "--expected-revision", "7" if bootstrap else "6"])
    return host, selector, secret_tokens


def _headers(token: str, *, origin: str | None = None, authority: str = "router.example.test") -> dict[str, str]:
    headers = {"Authorization": f"Bearer {token}", "Host": "127.0.0.1:5000"}
    if origin is not None:
        headers["Origin"] = origin
        headers["Host"] = authority
    return headers


def _client(peer: str = "127.0.0.1") -> TestClient:
    return TestClient(create_app(enable_startup_tasks=False), client=(peer, 45678))


def _insert(*, event_type: str, details: dict, message: str = "private diagnostic marker") -> str:
    event_id = str(uuid.uuid4())
    with db_session() as connection:
        connection.execute(
            """INSERT INTO operational_logs
               (event_id, level, event_type, message, details_json, created_at)
               VALUES (?, 'warning', ?, ?, ?, '2026-10-28 10:00:00')""",
            (event_id, event_type, message, json.dumps(details)),
        )
    return event_id


def test_secure_event_read_requires_registry_scope_and_redacts_projection(monkeypatch, tmp_path, capsys):
    _, _, tokens = _clients(monkeypatch, tmp_path, capsys)
    client = _client()
    assert client.get("/api/v2/events/secure/recent", headers=_headers(tokens["ui.client-list.write:cli"])).status_code == 403
    assert client.get("/api/v2/events/secure/recent", headers={"Host": "127.0.0.1:5000"}).status_code == 401
    bad_token = client.get("/api/v2/events/secure/recent", headers=_headers("f" * 64))
    assert bad_token.status_code == 401
    duplicate_auth = client.get("/api/v2/events/secure/recent", headers=[
        ("authorization", f"Bearer {tokens['events.read:integration']}"),
        ("authorization", f"Bearer {tokens['events.read:integration']}"),
        ("host", "127.0.0.1:5000"),
    ])
    assert duplicate_auth.status_code == 401
    before_patch = client.get("/api/v2/ui/settings/client-list")
    before_value = before_patch.json()["data"]["client_list"]["show_inactive"]
    read_only_patch = client.patch(
        "/api/v2/ui/settings/client-list",
        json={"show_inactive": not before_value},
        headers={
            **_headers(tokens["events.read:web_ui"], origin="https://router.example.test"),
            "If-Match": before_patch.headers["etag"], "X-Role": "admin",
            "X-Forwarded-For": "127.0.0.1",
        },
    )
    assert read_only_patch.status_code == 403
    after_patch = client.get("/api/v2/ui/settings/client-list")
    assert after_patch.json()["data"]["client_list"]["show_inactive"] is before_value

    for kind, token in (
        ("web_ui", tokens["events.read:web_ui"]),
        ("cli", tokens["events.read:cli"]),
        ("integration", tokens["events.read:integration"]),
    ):
        request_headers = _headers(token, origin="https://router.example.test") if kind == "web_ui" else _headers(token)
        assert client.get("/api/v2/events/secure/recent", headers=request_headers).status_code == 200
    with db_session() as connection:
        before_browser_posts = connection.execute("SELECT COUNT(*) FROM operational_logs").fetchone()[0]
    browser_headers = _headers(tokens["events.read:web_ui"], origin="https://router.example.test")
    browser_read = client.post("/api/v2/events/secure/recent", headers=browser_headers)
    assert browser_read.status_code == 200
    assert browser_read.headers["cache-control"] == "no-store, private"
    assert client.post(
        "/api/v2/events/secure/recent",
        headers={**_headers(tokens["events.read:web_ui"]), "Origin": "https://attacker.test",
                 "Host": "router.example.test"},
    ).status_code == 403
    assert client.post(
        "/api/v2/events/secure/recent", headers=_headers(tokens["events.read:web_ui"]),
    ).status_code == 403
    assert client.post(
        "/api/v2/events/secure/recent",
        headers=_headers(tokens["ui.client-list.write:web_ui"], origin="https://router.example.test"),
    ).status_code == 403
    with db_session() as connection:
        after_browser_posts = connection.execute("SELECT COUNT(*) FROM operational_logs").fetchone()[0]
    assert before_browser_posts == after_browser_posts


    assert client.get("/api/v2/events/secure/recent", headers={
        **_headers(tokens["events.read:integration"]), "Origin": "https://attacker.test",
        "X-Forwarded-For": "127.0.0.1",
    }).status_code == 403
    assert _client("198.51.100.23").get(
        "/api/v2/events/secure/recent", headers=_headers(tokens["events.read:cli"]),
    ).status_code == 403
    token_identity = registry.authenticate(tokens["events.read:integration"])
    server_request_id = str(uuid.uuid4())
    event_id = _insert(event_type="client_list_mutation_denied", details={
        "event_category": "operational", "event_code": "client_list.mutation_denied",
        "outcome": "denied", "operation": "show_inactive_mutation",
        "api_request_id": server_request_id, "request_id": "token:SECRET",
        "job_id": "password:SECRET", "correlation_id": "c" * 64,
        "event_context": {"request_id": "verifier:CONTROL", "job_id": "control"},
        "api_identity": {
            "assurance": "credential_bound", "verified": True,
            "api_client_id": token_identity["client_id"],
            "api_credential_id": token_identity["credential_id"],
            "kind": "cli", "tag_snapshot": "event-reader",
            "policy_revision": 3, "api_request_id": server_request_id,
        },
        "raw_message": "DO_NOT_RETURN", "password": "DO_NOT_RETURN_EITHER",
    }, message="Bearer " + "a" * 64)
    malformed_request_event_id = _insert(event_type="client_list_mutation_denied", details={
        "event_category": "operational", "event_code": "client_list.mutation_denied",
        "api_identity": {
            "assurance": "credential_bound", "verified": True,
            "api_client_id": token_identity["client_id"],
            "api_credential_id": token_identity["credential_id"],
            "kind": "cli", "tag_snapshot": "event-reader",
            "api_request_id": "verifier:CONTROL",
        },
    })
    record = tokens["record:events.read:integration"]
    _admin(capsys, ["rename", record["client_id"], "--expected-revision", "6", "--tag", "renamed-reader"])
    with db_session() as connection:
        before_reads = connection.execute("SELECT COUNT(*) FROM operational_logs").fetchone()[0]
    response = client.get("/api/v2/events/secure/recent", headers=_headers(tokens["events.read:integration"]))
    with db_session() as connection:
        after_reads = connection.execute("SELECT COUNT(*) FROM operational_logs").fetchone()[0]
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store, private"
    payload = response.json()
    item = next(row for row in payload["events"] if row["event_id"] == event_id)
    assert item["category"] == "security"
    assert item["action"] == "client_list.mutation_denied"
    assert item["identity"] == {
        "assurance": "credential_bound", "client_id": token_identity["client_id"],
        "kind": "cli", "tag_snapshot": "event-reader", "display": "[CLI]",
    }
    assert before_reads == after_reads
    assert item["api_request_id"] == server_request_id
    assert str(uuid.UUID(item["api_request_id"])) == item["api_request_id"]
    malformed_item = next(row for row in payload["events"] if row["event_id"] == malformed_request_event_id)
    assert "api_request_id" not in malformed_item
    assert "policy_revision" not in json.dumps(item)
    assert all(canary not in response.text for canary in (
        "DO_NOT_RETURN", "Bearer ", "token:SECRET", "password:SECRET", "c" * 64,
        "verifier:CONTROL", "control", "caller_claims",
    ))


def test_secure_event_reader_uses_bootstrapped_origin_and_rejects_old_origin_scope_and_revocation(
        monkeypatch, tmp_path, capsys):
    _, _, tokens = _clients(monkeypatch, tmp_path, capsys, bootstrap=True)
    client = _client()
    new_headers = _headers(tokens["events.read:web_ui"],
                           origin="https://new-router.example.test:8443",
                           authority="new-router.example.test:8443")
    assert client.get("/api/v2/events/secure/recent", headers=new_headers).status_code == 200
    old_origin = _headers(tokens["events.read:web_ui"], origin="https://router.example.test")
    assert client.get("/api/v2/events/secure/recent", headers=old_origin).status_code == 403
    wrong_scope = _headers(tokens["ui.client-list.write:web_ui"],
                           origin="https://new-router.example.test:8443",
                           authority="new-router.example.test:8443")
    assert client.get("/api/v2/events/secure/recent", headers=wrong_scope).status_code == 403
    record = tokens["record:events.read:web_ui"]
    _admin(capsys, ["revoke-credential", record["client_id"], record["credential_id"],
                    "--expected-revision", "7"])
    assert client.get("/api/v2/events/secure/recent", headers=new_headers).status_code == 401


def test_secure_event_read_projects_real_owner_writes_for_all_client_kinds(monkeypatch, tmp_path, capsys):
    _, _, tokens = _clients(monkeypatch, tmp_path, capsys)
    app = create_app(enable_startup_tasks=False)
    expected = {}
    for kind, peer, host in (
        ("web_ui", "198.51.100.12", "router.example.test"),
        ("cli", "127.0.0.1", "127.0.0.1:5000"),
        ("integration", "127.0.0.1", "127.0.0.1:5000"),
    ):
        writer = TestClient(app, client=(peer, 45678), headers={"Host": host})
        etag = writer.get("/api/v2/ui/settings/client-list").headers["etag"]
        current = writer.get("/api/v2/ui/settings/client-list").json()["data"]["client_list"]["show_inactive"]
        headers = {
            "Authorization": f"Bearer {tokens[f'ui.client-list.write:{kind}']}",
            "If-Match": etag,
        }
        if kind == "web_ui":
            headers["Origin"] = "https://router.example.test"
        changed = writer.patch(
            "/api/v2/ui/settings/client-list", json={"show_inactive": not current}, headers=headers,
        )
        assert changed.status_code == 200, changed.text
        record = tokens[f"record:ui.client-list.write:{kind}"]
        expected[kind] = {
            "client_id": record["client_id"], "kind": kind,
            "tag_snapshot": {"web_ui": "presentation-admin", "cli": "cli-admin", "integration": "event-reader"}[kind],
        }

    reader = _client()
    response = reader.get(
        "/api/v2/events/secure/recent?category=audit&event_code=client_list.show_inactive_changed",
        headers=_headers(tokens["events.read:integration"]),
    )
    assert response.status_code == 200
    projected = response.json()["events"]
    for kind, identity in expected.items():
        matching = [row for row in projected if row["identity"].get("client_id") == identity["client_id"]]
        assert matching
        assert str(uuid.UUID(matching[0]["api_request_id"])) == matching[0]["api_request_id"]
        assert matching[0]["identity"] == {
            "assurance": "credential_bound", **identity,
            "display": {"web_ui": "[WEB_UI]", "cli": "[CLI]", "integration": "[API:event-reader]"}[kind],
        }
    integration = tokens["record:ui.client-list.write:integration"]
    _admin(capsys, [
        "rename", integration["client_id"], "--expected-revision", "6", "--tag", "renamed-reader",
    ])
    after_rename = reader.get(
        "/api/v2/events/secure/recent?category=audit&event_code=client_list.show_inactive_changed",
        headers=_headers(tokens["events.read:integration"]),
    )
    historical = [row for row in after_rename.json()["events"]
                  if row["identity"].get("client_id") == integration["client_id"]]
    assert historical and historical[0]["identity"]["tag_snapshot"] == "event-reader"
    assert "credential_id" not in json.dumps(historical[0]["identity"])


def test_secure_event_cursor_bounds_scan_and_detects_replaced_anchor(monkeypatch, tmp_path, capsys):
    _, _, tokens = _clients(monkeypatch, tmp_path, capsys)
    client = _client()
    target_id = _insert(event_type="user_action", details={"event_category": "audit", "event_code": "user_action"})
    for _ in range(251):
        _insert(event_type="routine_status", details={"event_category": "operational", "event_code": "routine_status"})

    response = client.get("/api/v2/events/secure/recent?limit=1", headers=_headers(tokens["events.read:integration"]))
    assert response.status_code == 200
    first = response.json()
    assert first["events"] == [] and first["scanned"] == 250
    assert first["next_cursor"]
    late_id = _insert(event_type="user_action", details={"event_category": "audit", "event_code": "user_action"})
    filter_mismatch = client.get(
        "/api/v2/events/secure/recent?limit=1&category=audit&cursor=" + first["next_cursor"],
        headers=_headers(tokens["events.read:integration"]),
    )
    assert filter_mismatch.status_code == 400
    second = client.get(
        "/api/v2/events/secure/recent?limit=1&cursor=" + first["next_cursor"],
        headers=_headers(tokens["events.read:integration"]),
    )
    assert second.status_code == 200
    assert second.json()["events"][0]["event_id"] == target_id
    assert late_id not in {row["event_id"] for row in second.json()["events"]}
    assert second.json()["next_cursor"] is None

    _insert(event_type="user_action", details={"event_category": "audit", "event_code": "user_action"})
    page = client.get("/api/v2/events/secure/recent?limit=1", headers=_headers(tokens["events.read:integration"])).json()
    assert page["events"] and page["next_cursor"]
    with db_session() as connection:
        connection.execute("DELETE FROM operational_logs WHERE event_id = ?", (page["events"][0]["event_id"],))
    _insert(event_type="user_action", details={"event_category": "audit", "event_code": "user_action"})
    stale = client.get(
        "/api/v2/events/secure/recent?limit=1&cursor=" + page["next_cursor"],
        headers=_headers(tokens["events.read:integration"]),
    )
    assert stale.status_code == 409
    assert stale.json() == {"error_code": "EVENTS_CURSOR_STALE"}


def test_secure_event_route_rejects_legacy_selector_and_bad_filters(monkeypatch, tmp_path, capsys):
    host, selector, tokens = _clients(monkeypatch, tmp_path, capsys)
    selector.write_text(json.dumps({
        "schema_version": 1, "principal": "presentation-operator", "scopes": ["events.read"],
        "token_sha256": hashlib.sha256(tokens["events.read:integration"].encode("ascii")).hexdigest(),
        "canonical_origin": "https://router.example.test",
    }), encoding="utf-8")
    selector.chmod(0o600)
    client = _client()
    denied = client.get("/api/v2/events/secure/recent", headers=_headers(tokens["events.read:integration"]))
    assert denied.status_code == 503
    assert denied.json() == {"error_code": "EVENTS_AUTH_CONFIG_UNAVAILABLE"}
    post_denied = client.post(
        "/api/v2/events/secure/recent", headers=_headers(tokens["events.read:integration"]),
    )
    assert post_denied.status_code == 503
    assert post_denied.json() == {"error_code": "EVENTS_AUTH_CONFIG_UNAVAILABLE"}
    assert "events.read" in registry.SUPPORTED_SCOPES
    assert host.exists()


def test_secure_event_projection_uses_fixed_codes_and_survives_malformed_history(monkeypatch, tmp_path, capsys):
    _, _, tokens = _clients(monkeypatch, tmp_path, capsys)
    connection_client = _client()
    token = tokens["events.read:integration"]
    _insert(event_type="user_action", details={
        "event_category": "audit", "event_code": "password=topsecret",
        "action": "Authorization: Bearer " + "b" * 64,
        "api_identity": {"assurance": [], "verified": True, "api_client_id": [],
                         "kind": [], "tag_snapshot": []},
    })
    deep_json = "[" * 1100 + "]" * 1100
    oversized = json.dumps({"event_category": "audit", "event_code": "user_action"}) + " " * 70000
    with db_session() as connection:
        connection.execute(
            """INSERT INTO operational_logs(event_id, level, event_type, message, details_json, created_at)
               VALUES (?, 'info', 'user_action', ?, ?, '2026-10-28 10:00:00')""",
            (str(uuid.uuid4()), "do not project", deep_json),
        )
        connection.execute(
            """INSERT INTO operational_logs(event_id, level, event_type, message, details_json, created_at)
               VALUES (?, 'info', 'user_action', ?, ?, '2026-10-28 10:00:00')""",
            (str(uuid.uuid4()), "do not project", oversized),
        )
        connection.execute(
            """INSERT INTO operational_logs(event_id, level, event_type, message, details_json, created_at)
               VALUES (?, 'info', 'user_action', ?, ?, '2026-10-28 10:00:00')""",
            (str(uuid.uuid4()), "do not project", '{"event_category":"audit","event_code":"leak","event_code":"user_action"}'),
        )
    response = connection_client.get("/api/v2/events/secure/recent", headers=_headers(token))
    assert response.status_code == 200
    assert "topsecret" not in response.text and "Authorization" not in response.text
    projected = response.json()["events"]
    assert projected and all(row["event_code"] == "user_action" for row in projected)
    assert all(row["identity"]["assurance"] == "unverified" for row in projected)


@pytest.mark.parametrize("malformation", [
    "alias_only", "display_tag_only", "missing_api_client_id", "missing_api_credential_id",
    "missing_tag_snapshot", "missing_policy_revision", "missing_api_request_id", "missing_kind",
    "policy_revision_bool", "policy_revision_zero", "policy_revision_string", "invalid_api_request_id",
])
def test_secure_event_malformed_identity_snapshots_stay_unverified(
    monkeypatch, tmp_path, capsys, malformation,
):
    _, _, tokens = _clients(monkeypatch, tmp_path, capsys)
    identity = {
        "assurance": "credential_bound", "verified": True,
        "api_client_id": "a" * 32, "api_credential_id": "b" * 32,
        "kind": "integration", "tag_snapshot": "plausible-tag",
        "policy_revision": 1, "api_request_id": str(uuid.uuid4()),
    }
    if malformation == "alias_only":
        identity["client_id"] = identity.pop("api_client_id")
        identity["credential_id"] = identity.pop("api_credential_id")
    elif malformation == "display_tag_only":
        identity["display_tag"] = identity.pop("tag_snapshot")
    elif malformation.startswith("missing_"):
        field = malformation.removeprefix("missing_")
        field = {"api_credential_id": "api_credential_id"}.get(field, field)
        identity.pop(field)
    elif malformation == "policy_revision_bool":
        identity["policy_revision"] = True
    elif malformation == "policy_revision_zero":
        identity["policy_revision"] = 0
    elif malformation == "policy_revision_string":
        identity["policy_revision"] = "1"
    elif malformation == "invalid_api_request_id":
        identity["api_request_id"] = "verifier:CONTROL"
    event_id = _insert(event_type="user_action", details={
        "event_category": "audit", "event_code": "user_action", "api_identity": identity,
        "api_request_id": "token:SECRET", "request_id": "password:SECRET",
    })
    response = _client().get(
        "/api/v2/events/secure/recent?event_code=user_action",
        headers=_headers(tokens["events.read:integration"]),
    )
    assert response.status_code == 200
    row = next(item for item in response.json()["events"] if item["event_id"] == event_id)
    assert row["identity"] == {"assurance": "unverified"}
    assert "api_request_id" not in row
    assert all(secret not in response.text for secret in (
        "a" * 32, "b" * 32, "plausible-tag", "verifier:CONTROL", "token:SECRET", "password:SECRET",
    ))


def test_secure_event_legacy_null_and_empty_ids_remain_unverified_across_cursor(monkeypatch, tmp_path, capsys):
    _, _, tokens = _clients(monkeypatch, tmp_path, capsys)
    client = _client()
    details = json.dumps({
        "event_category": "audit", "event_code": "user_action",
        "api_identity": {
            "assurance": "credential_bound", "verified": True,
            "api_client_id": "a" * 32, "api_credential_id": "b" * 32,
            "kind": "integration", "tag_snapshot": "historic-tag",
        },
    })
    with db_session() as connection:
        for event_id in (None, ""):
            connection.execute(
                """INSERT INTO operational_logs(event_id, level, event_type, message, details_json,
                   created_at) VALUES (?, 'info', 'user_action', 'private', ?, '2026-10-28 10:00:00')""",
                (event_id, details),
            )

    first = client.get(
        "/api/v2/events/secure/recent?limit=1&category=audit",
        headers=_headers(tokens["events.read:integration"]),
    )
    assert first.status_code == 200
    assert first.json()["events"] == [{
        "event_id": None, "timestamp": "2026-10-28 10:00:00", "category": "audit",
        "event_code": "user_action", "action": "user_action", "outcome": "unknown",
        "identity": {"assurance": "unverified"},
    }]
    assert first.json()["next_cursor"]
    second = client.get(
        "/api/v2/events/secure/recent?limit=1&category=audit&cursor=" + first.json()["next_cursor"],
        headers=_headers(tokens["events.read:integration"]),
    )
    assert second.status_code == 200
    assert second.json()["events"] == first.json()["events"]
    assert second.json()["next_cursor"] is None


def test_secure_event_registry_failures_and_revocation_drain_admitted_read(monkeypatch, tmp_path, capsys):
    host, _, tokens = _clients(monkeypatch, tmp_path, capsys)
    client = _client()
    token = tokens["events.read:integration"]
    request_url = "/api/v2/events/secure/recent"
    registry_path = host / "api-clients.json"
    original_policy = registry_path.read_bytes()
    registry_path.unlink()
    missing = client.get(request_url, headers=_headers(token))
    assert missing.status_code == 503
    registry_path.write_bytes(original_policy)
    registry_path.chmod(0o600)
    registry_path.write_text("not-json", encoding="ascii")
    unavailable = client.get(request_url, headers=_headers(token))
    assert unavailable.status_code == 503
    assert unavailable.json() == {"error_code": "EVENTS_AUTH_CONFIG_UNAVAILABLE"}
    registry_path.write_bytes(original_policy)
    registry_path.chmod(0o600)
    (host / "api-clients.pending").write_text("pending", encoding="ascii")
    pending = client.get(request_url, headers=_headers(token))
    assert pending.status_code == 503
    (host / "api-clients.pending").unlink()

    from fwrouter_api.services import secure_events
    original_connect = secure_events.connect
    monkeypatch.setattr(secure_events, "connect", lambda: (_ for _ in ()).throw(sqlite3.OperationalError("private path")))
    db_failure = client.get(request_url, headers=_headers(token))
    assert db_failure.status_code == 503
    assert db_failure.json() == {"error_code": "EVENTS_READ_UNAVAILABLE"}, db_failure.text
    assert "private path" not in db_failure.text
    monkeypatch.setattr(secure_events, "connect", original_connect)
    huge_cursor = secure_events._encode_cursor({
        "v": 1, "ceiling": 10**80, "anchor": str(uuid.uuid4()), "last": 10**80,
        "filters": "0" * 64,
    })
    malformed = client.get(
        request_url + "?cursor=" + huge_cursor, headers=_headers(token),
    )
    assert malformed.status_code == 400
    assert str(10**80) not in malformed.text

    _insert(event_type="user_action", details={"event_category": "audit", "event_code": "user_action"})
    with db_session() as connection:
        connection.execute("UPDATE operational_logs SET event_id = ? WHERE rowid = (SELECT MAX(rowid) FROM operational_logs)", ("x" * 36,))
    malformed_head = client.get(request_url, headers=_headers(token))
    assert malformed_head.status_code == 503
    assert malformed_head.json() == {"error_code": "EVENTS_READ_UNAVAILABLE"}
    with db_session() as connection:
        connection.execute("UPDATE operational_logs SET event_id = ? WHERE rowid = (SELECT MAX(rowid) FROM operational_logs)", (str(uuid.uuid4()),))

    record = tokens["record:events.read:integration"]
    entered, release = Event(), Event()
    original_read = secure_events.read_secure_recent

    def blocked_read(**kwargs):
        entered.set()
        assert release.wait(3)
        return original_read(**kwargs)

    monkeypatch.setattr(secure_events, "read_secure_recent", blocked_read)
    with ThreadPoolExecutor(max_workers=2) as pool:
        page = pool.submit(client.get, request_url, headers=_headers(token))
        assert entered.wait(3)
        revoked = Event()

        def revoke():
            _admin(capsys, [
                "revoke-credential", record["client_id"], record["credential_id"],
                "--expected-revision", "6",
            ])
            revoked.set()

        revoker = Thread(target=revoke)
        revoker.start()
        assert not revoked.wait(0.1)
        release.set()
        assert page.result(timeout=4).status_code == 200
        revoker.join(timeout=4)
        assert revoked.is_set()
    assert client.get(request_url, headers=_headers(token)).status_code == 401
