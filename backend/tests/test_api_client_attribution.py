from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import hashlib
import json
import os
import secrets

from fastapi.testclient import TestClient

from fwrouter_api.db.connection import db_session
from fwrouter_api.jobs.manager import JobManager
from fwrouter_api.main import create_app
from fwrouter_api.services import api_client_attribution as attribution
from fwrouter_api.services import api_client_registry as registry
from fwrouter_api.services import presentation_client_list_admission as admission
from fwrouter_api.services.api_client_attribution import (
    apply_attribution,
    attribution_from_snapshot,
    build_attribution_snapshot,
    current_attribution_snapshot,
    prepare_denial_event,
    redact_http_attribution,
    safe_display_tag,
)
from fwrouter_api.services.logs import list_technical_logs, write_technical_log
from fwrouter_api.services.ui_client_list_settings import read_client_list_settings
from fwrouter_api.services.event_contract import normalize_event_details


def _identity(*, kind: str = "integration", tag: str = "homeassistant", client_id: str = "client-1",
              credential_id: str = "credential-1"):
    return SimpleNamespace(
        assurance="credential_bound",
        principal="api-client",
        client_id=client_id,
        credential_id=credential_id,
        kind=kind,
        display_tag=tag,
        policy_revision=7,
    )


def _admin(capsys, command: list[str]) -> dict:
    from fwrouter_api.cli import main as cli_main

    assert cli_main(["api-clients", *command]) == 0
    captured = capsys.readouterr()
    assert not captured.err
    return json.loads(captured.out)


def _registry_clients(monkeypatch, tmp_path, capsys):
    with attribution._DENIAL_LOCK:
        attribution._DENIAL_BUCKETS.clear()
    host = tmp_path / "etc" / "fwrouter"
    host.mkdir(mode=0o700, parents=True)
    verifier = host / "presentation-client-list-verifier.json"
    registry_path = host / "api-clients.json"
    monkeypatch.setattr(registry, "REGISTRY_PATH", registry_path)
    monkeypatch.setattr(registry, "SELECTOR_PATH", verifier)
    monkeypatch.setattr(registry, "FENCE_PATH", host / "api-clients.required")
    monkeypatch.setattr(registry, "PENDING_PATH", host / "api-clients.pending")
    monkeypatch.setattr(registry, "LOCK_PATH", host / "api-clients.lock")
    monkeypatch.setattr(registry, "_trusted_uid", os.getuid)
    monkeypatch.setattr(admission, "VERIFIER_PATH", verifier)
    monkeypatch.setattr(admission, "_trusted_uid", os.getuid)
    legacy_token = secrets.token_hex(32)
    verifier.write_text(json.dumps({
        "schema_version": 1,
        "principal": "presentation-operator",
        "scopes": ["ui.client-list.write"],
        "token_sha256": hashlib.sha256(legacy_token.encode("ascii")).hexdigest(),
        "canonical_origin": "https://router.example.test",
    }), encoding="utf-8")
    verifier.chmod(0o600)
    _admin(capsys, ["init", "--canonical-origin", "https://router.example.test"])
    clients = {}
    specifications = (
        ("web_ui", "dashboard", "ui.client-list.write"),
        ("cli", "operator-cli", "ui.client-list.write"),
        ("integration", "homeassistant", "ui.client-list.write"),
    )
    for index, (kind, tag, scope) in enumerate(specifications, start=1):
        secret_path = host / f"{tag}.token"
        client = _admin(capsys, [
            "create", "--expected-revision", str(index), "--kind", kind,
            "--tag", tag, "--scope", scope, "--secret-file", str(secret_path),
        ])
        client["token"] = secret_path.read_text(encoding="ascii").strip()
        client.update(registry.authenticate(client["token"]))
        clients[tag] = client
    _admin(capsys, ["cutover", "--expected-revision", "4"])
    return host, clients


def test_display_tags_and_denial_windows_are_bounded_and_identity_scoped():
    assert safe_display_tag("homeassistant", kind="integration") == "[API:homeassistant]"
    assert safe_display_tag("<b>[WEB_UI]</b>", kind="integration") == (
        "[API:&lt;b&gt;&#91;WEB_UI&#93;&lt;/b&gt;]"
    )
    assert safe_display_tag("web", kind="web_ui") == "[WEB_UI]"
    assert safe_display_tag("cli", kind="cli") == "[CLI]"
    assert safe_display_tag("integration", kind="web_ui") == "[WEB_UI]"
    assert safe_display_tag("bad\x1b[31m", kind="integration") is None
    assert safe_display_tag("tag", kind="other") is None

    with attribution._DENIAL_LOCK:
        attribution._DENIAL_BUCKETS.clear()
    first = build_attribution_snapshot(_identity())
    rename_candidate = _identity(tag="new-name")
    renamed_snapshot = build_attribution_snapshot(rename_candidate)
    rename_candidate.display_tag = "renamed-later"
    assert renamed_snapshot["tag_snapshot"] == "new-name"
    second = build_attribution_snapshot(_identity(client_id="client-2", credential_id="credential-2"))
    assert prepare_denial_event("PRESENTATION_SCOPE_FORBIDDEN", snapshot=first, now=10) == {
        "suppressed_count": 0, "aggregation_window_seconds": 30,
    }
    assert prepare_denial_event("PRESENTATION_SCOPE_FORBIDDEN", snapshot=first, now=11) is None
    assert prepare_denial_event("PRESENTATION_SCOPE_FORBIDDEN", snapshot=second, now=11) == {
        "suppressed_count": 0, "aggregation_window_seconds": 30,
    }
    assert prepare_denial_event("PRESENTATION_SCOPE_FORBIDDEN", snapshot=first, now=41) == {
        "suppressed_count": 1, "aggregation_window_seconds": 30,
    }
    for index in range(attribution._DENIAL_BUCKET_LIMIT + 30):
        prepare_denial_event(f"CODE-{index}", snapshot=None, now=100 + index)
    assert len(attribution._DENIAL_BUCKETS) <= attribution._DENIAL_BUCKET_LIMIT

    snapshot = build_attribution_snapshot(_identity(), api_request_id="trusted-request-id")
    normalized = normalize_event_details(
        {"api_identity": {"api_request_id": "forged", "api_credential_id": "x" * 400_000},
         "large": "x" * 300_000},
        event_id="event", timestamp="now", severity="info", component="test",
        event_category="audit", event_code="test", event_type="test",
        api_identity_snapshot=snapshot,
        server_api_request_id="trusted-request-id",
    )
    assert normalized["truncated_payload"] is True
    assert normalized["api_identity"]["api_request_id"] == "trusted-request-id"
    assert normalized["api_request_id"] == "trusted-request-id"
    assert len(json.dumps(normalized).encode("utf-8")) <= 256 * 1024
    untrusted = normalize_event_details(
        {"api_identity": {"api_request_id": "forged", "api_credential_id": "x" * 400_000}},
        event_id="event", timestamp="now", severity="info", component="test",
        event_category="audit", event_code="test", event_type="test",
    )
    assert "api_identity" not in untrusted


def test_registry_credentials_bind_identity_and_http_projections_redact(monkeypatch, tmp_path, capsys):
    _, clients = _registry_clients(monkeypatch, tmp_path, capsys)
    app = create_app(enable_startup_tasks=False)
    event_id = None
    stored_identities = []
    # Exercise three distinct registered principals over their admitted
    # transports; caller correlation remains separate from server request ID.
    for index, tag in enumerate(("dashboard", "operator-cli", "homeassistant")):
        registered = clients[tag]
        is_web = registered["kind"] == "web_ui"
        client = TestClient(
            app,
            client=(("198.51.100.12", 43210) if is_web else ("127.0.0.1", 45678)),
            headers={"Host": ("router.example.test" if is_web else "127.0.0.1:5000")},
        )
        with client:
            before = client.get("/api/v2/ui/settings/client-list")
            with db_session() as connection:
                audit_count_before_get = connection.execute(
                    "SELECT COUNT(*) FROM operational_logs WHERE event_type='ui_client_list_visibility_updated'"
                ).fetchone()[0]
            headers = {"Authorization": f"Bearer {registered['token']}",
                       "If-Match": before.headers["etag"], "X-Request-ID": f"caller-{index}"}
            if is_web:
                headers["Origin"] = "https://router.example.test"
            response = client.patch("/api/v2/ui/settings/client-list",
                                    json={"show_inactive": bool(index % 2 == 0)}, headers=headers)
            assert response.status_code == 200, response.text
            assert response.headers["x-request-id"] == f"caller-{index}"
            server_request_id = response.headers["x-fwrouter-request-id"]
            assert server_request_id != f"caller-{index}"
            assert response.json()["data"]["client_list"]["persistence_status"] in {"persisted", "unchanged"}
            assert client.get("/api/v2/ui/settings/client-list").status_code == 200
            with db_session() as connection:
                audit_count_after_get = connection.execute(
                    "SELECT COUNT(*) FROM operational_logs WHERE event_type='ui_client_list_visibility_updated'"
                ).fetchone()[0]
            expected_added = int(response.json()["data"]["client_list"]["audit_delivery_status"] == "delivered")
            assert audit_count_after_get == audit_count_before_get + expected_added
            # Model a lost successful response: retry with its original CAS
            # token and confirm the committed mutation is reported as conflict.
            if index == 0:
                retry = client.patch("/api/v2/ui/settings/client-list",
                                     json={"show_inactive": True}, headers=headers)
                assert retry.status_code == 412
                from fwrouter_api.services import logs as log_service
                original_operational_writer = log_service.write_operational_log
                monkeypatch.setattr(
                    log_service, "write_operational_log",
                    lambda **_kwargs: (_ for _ in ()).throw(OSError("synthetic denial log failure")),
                )
                malformed = client.patch(
                    "/api/v2/ui/settings/client-list", content=b'{"show_inactive":1}',
                    headers={**headers, "If-Match": response.headers["etag"],
                             "Content-Type": "application/json"},
                )
                monkeypatch.setattr(log_service, "write_operational_log", original_operational_writer)
                assert malformed.status_code == 422
                no_op = client.patch(
                    "/api/v2/ui/settings/client-list", json={"show_inactive": True},
                    headers={**headers, "If-Match": response.headers["etag"]},
                )
                assert no_op.status_code == 200
                assert no_op.json()["data"]["client_list"]["persistence_status"] == "unchanged"
                assert no_op.json()["data"]["client_list"]["audit_delivery_status"] == "not_attempted"
            with db_session() as connection:
                rows = connection.execute(
                    "SELECT details_json FROM operational_logs WHERE event_type='ui_client_list_visibility_updated'"
                ).fetchall()
            details = next(
                parsed for parsed in (json.loads(row["details_json"]) for row in rows)
                if parsed.get("api_identity", {}).get("api_request_id") == server_request_id
            )
            identity_data = details["api_identity"]
            assert identity_data["api_client_id"] == registered["client_id"]
            assert identity_data["api_credential_id"] == registered["credential_id"]
            assert identity_data["kind"] == registered["kind"]
            assert identity_data["tag_snapshot"] == registered["display_tag"]
            expected_label = {"web_ui": "[WEB_UI]", "cli": "[CLI]"}.get(
                registered["kind"], f"[API:{registered['display_tag']}]"
            )
            assert identity_data["display_tag"] == expected_label
            assert identity_data["api_request_id"] == server_request_id
            assert identity_data["caller_request_id"] == f"caller-{index}"
            event_id = details["event_id"]
            stored_identities.append(identity_data)

    assert len({identity_data["api_client_id"] for identity_data in stored_identities}) == 3
    with db_session() as connection:
        denial_rows = connection.execute(
            "SELECT details_json FROM operational_logs "
            "WHERE event_type IN ('client_list_mutation_denied', 'client_list_mutation_conflict')"
        ).fetchall()
    assert any(
        parsed.get("api_identity", {}).get("api_client_id") == clients["dashboard"]["client_id"]
        for parsed in (json.loads(row["details_json"]) for row in denial_rows)
    )
    # Renaming updates registry policy only; already written event snapshots
    # keep the original tag and label.
    original_snapshot = stored_identities[-1]
    _admin(capsys, ["rename", clients["homeassistant"]["client_id"],
                    "--expected-revision", "4", "--tag", "ha-renamed"])
    with db_session() as connection:
        historical = connection.execute(
            "SELECT details_json FROM operational_logs WHERE event_type='ui_client_list_visibility_updated'"
        ).fetchall()
    assert any(
        json.loads(row["details_json"]).get("api_identity", {}).get("api_request_id")
        == original_snapshot["api_request_id"]
        and json.loads(row["details_json"]).get("api_identity", {}).get("tag_snapshot") == "homeassistant"
        and json.loads(row["details_json"]).get("api_identity", {}).get("display_tag") == "[API:homeassistant]"
        for row in historical
    )
    # Revocation is checked against an actual registry credential. The registry
    # cannot issue unsupported scopes; legacy wrong-scope admission is covered
    # by the owner-path regression tests.
    local = TestClient(app, client=("127.0.0.1", 45678), headers={"Host": "127.0.0.1:5000"})
    with local:
        before = local.get("/api/v2/ui/settings/client-list")
        assert read_client_list_settings()["show_inactive"] is True

        integration = clients["homeassistant"]
        _admin(capsys, ["revoke-credential", integration["client_id"], integration["credential_id"],
                        "--expected-revision", "5"])
        revoked = local.patch("/api/v2/ui/settings/client-list", json={"show_inactive": False},
                              headers={"Authorization": f"Bearer {integration['token']}",
                                       "If-Match": before.headers["etag"]})
        assert revoked.status_code == 401
        revoked_id = revoked.headers["x-fwrouter-request-id"]
        with db_session() as connection:
            rejected_rows = connection.execute(
                "SELECT details_json FROM operational_logs WHERE event_type='client_list_mutation_denied'"
            ).fetchall()
        assert any(
            json.loads(row["details_json"]).get("error_code") == "PRESENTATION_CREDENTIAL_INVALID"
            and json.loads(row["details_json"]).get("api_request_id") == revoked_id
            and "api_identity" not in json.loads(row["details_json"])
            for row in rejected_rows
        )

        assert event_id is not None
        for path in (
            f"/api/v2/events/{event_id}", "/api/v2/events/recent?view=full",
            "/api/v2/events/recent?view=summary", "/api/v2/logs/operational?ui_only=false",
            "/api/v2/logs/technical?ui_only=false", "/api/v2/diagnose?view=full",
            "/api/v2/diagnose?view=summary", "/api/v2/ui/settings/workspace",
            "/api/v2/ui/router-summary", "/api/v2/jobs",
        ):
            body = local.get(path).text
            assert "api_identity" not in body, path
            assert "api_request_id" not in body, path
            assert "api_credential_id" not in body, path
            assert "[API:homeassistant]" not in body, path
            for registered in clients.values():
                assert registered["token"] not in body, path

    spoof_snapshot = build_attribution_snapshot(
        SimpleNamespace(assurance="credential_bound", client_id="real-id", credential_id="real-cred",
                       kind="integration", display_tag="homeassistant", policy_revision=5)
    )
    with attribution_from_snapshot(spoof_snapshot):
        spoofed = apply_attribution({
            "nested": [{"api_identity": {"client_id": "forged"}, "verified_api_client_id": "forged"}],
            "client_id": "business-object-id", "api_request_id": "forged-request-id",
        })
        assert spoofed["nested"] == [{}]
        assert spoofed["client_id"] == "business-object-id"
        assert spoofed["api_identity"]["api_client_id"] == "real-id"
        assert spoofed["api_request_id"] == spoof_snapshot["api_request_id"]
    identity = _identity()
    api_request_id = "synthetic-server-request"
    stored_identity = stored_identities[-1]
    client = TestClient(app, client=("127.0.0.1", 45678), headers={"Host": "127.0.0.1:5000"})
    safe = redact_http_attribution({
        "event": {"details": {"api_identity": stored_identity,
                               "verified_api_client_id": "private", "client_id": "domain-client"}},
        "job": {"result": {"nested": {"_api_identity": stored_identity}}},
    })
    encoded = json.dumps(safe)
    assert "credential-1" not in encoded and "api_identity" not in encoded
    assert safe["event"]["details"]["client_id"] == "domain-client"

    # A job receives an immutable snapshot, then restores identity in its
    # own worker context. It does not grant a presentation write capability.
    snapshot = build_attribution_snapshot(identity, api_request_id=api_request_id,
                                          caller_request_id="caller-correlation-7")
    manager = JobManager()
    observed: list[dict | None] = []

    def handler(job):
        admission_error = None
        try:
            admission.require_presentation_write_grant()
        except admission.PresentationAdmissionError as exc:
            admission_error = exc.code
        assert admission_error == "PRESENTATION_WRITE_ADMISSION_REQUIRED"
        observed.append(current_attribution_snapshot())
        write_technical_log(
            component="test-api-attribution",
            event_type="api_identity_job_success",
            message="Synthetic attribution worker completed.",
            details={"api_identity": {"client_id": "forged"}, "job_id": job["job_id"]},
        )
        return {"job_status": "success", "event_context": {"api_identity": {"client_id": "forged"}}}

    manager.register_handler("noop", handler)
    with attribution_from_snapshot(snapshot):
        job = manager.create("noop", requested_by="test")
    with client:
        raw_job = client.get(f"/api/v2/rules/jobs/{job['job_id']}").text
        assert "api_identity" not in raw_job
        assert "credential-1" not in raw_job
    assert "_api_identity" in job["event_context"]
    assert "api_identity" not in json.dumps(redact_http_attribution(job))
    with ThreadPoolExecutor(max_workers=1) as pool:
        completed = pool.submit(manager.run_job, job["job_id"]).result(timeout=5)
        assert completed and completed["status"] == "success"
        with db_session() as connection:
            terminal = connection.execute(
                "SELECT details_json FROM operational_logs WHERE event_type = ? ORDER BY rowid DESC LIMIT 1",
                ("job.terminal_outcome",),
            ).fetchone()
        terminal_payload = json.loads(terminal["details_json"])
        assert terminal_payload["job_id"] == job["job_id"]
        assert terminal_payload["api_identity"]["api_client_id"] == "client-1"
        assert terminal_payload["api_identity"]["api_request_id"] == snapshot["api_request_id"]
        assert terminal_payload["execution_status"] == "success"
        assert pool.submit(current_attribution_snapshot).result() is None
        untrusted_job = manager.create("plain", requested_by="test")
        manager.register_handler(
            "plain",
            lambda _job: write_technical_log(
                component="test-api-attribution",
                event_type="api_identity_job_plain",
                message="Synthetic unattributed worker completed.",
                details={},
            ) and {"job_status": "success"},
        )
        plain_result = pool.submit(manager.run_job, untrusted_job["job_id"]).result(timeout=5)
        assert plain_result and plain_result["status"] == "success"
        assert pool.submit(current_attribution_snapshot).result() is None
        manager.register_handler("explode", lambda _job: (_ for _ in ()).throw(RuntimeError("synthetic")))
        with attribution_from_snapshot(snapshot):
            failed_job = manager.create("explode", requested_by="test")
        failed_result = pool.submit(manager.run_job, failed_job["job_id"]).result(timeout=5)
        assert failed_result and failed_result["status"] == "failed"
        assert pool.submit(current_attribution_snapshot).result() is None
    assert observed[0]["client_id"] == "client-1"
    assert current_attribution_snapshot() is None
    technical = list_technical_logs(limit=100)
    attributed = next(item for item in technical if item["event_type"] == "api_identity_job_success")
    assert attributed["details"]["api_identity"]["api_client_id"] == "client-1"
    worker_failure = next(item for item in technical if item["event_type"] == "job_handler_exception")
    assert worker_failure["details"]["api_identity"]["api_credential_id"] == "credential-1"
    assert not next(item for item in technical if item["event_type"] == "api_identity_job_plain")["details"].get("api_identity")
    listed_jobs = client.get("/api/v2/jobs").text
    assert "credential-1" not in listed_jobs
    assert "api_identity" not in listed_jobs
    public_technical = client.get(
        "/api/v2/logs/technical?ui_only=false&component=test-api-attribution"
    ).text
    assert "credential-1" not in public_technical
    def contains_reserved_key(value):
        if isinstance(value, dict):
            return any(
                str(key).lower() in {"api_identity", "_api_identity"}
                or str(key).lower().startswith("verified_api_")
                or contains_reserved_key(item)
                for key, item in value.items()
            )
        if isinstance(value, list):
            return any(contains_reserved_key(item) for item in value)
        return False
    assert not contains_reserved_key(json.loads(public_technical))


def test_raw_job_and_conflict_http_paths_redact_identity(monkeypatch):
    from fwrouter_api.routes import core
    from fwrouter_api.services.jobs import JobLockConflictError

    manager = JobManager()
    snapshot = build_attribution_snapshot(_identity())
    with attribution_from_snapshot(snapshot):
        job = manager.create("attribution-test", requested_by="test")
    active_job = dict(job)
    active_job["event_context"]["_api_identity"] = snapshot
    monkeypatch.setattr(
        core, "submit_core_bypass_job",
        lambda **_kwargs: (_ for _ in ()).throw(JobLockConflictError("test-lock", active_job)),
    )
    client = TestClient(create_app(enable_startup_tasks=False))
    with client:
        detail = client.get(f"/api/v2/rules/jobs/{job['job_id']}").text
        assert "api_identity" not in detail and "credential-1" not in detail
        conflict = client.post("/api/v2/core/bypass/enable", json={"confirm_apply": True}).text
        assert '"active_job"' in conflict
        assert "_api_identity" not in conflict and "credential-1" not in conflict


def test_concurrent_registry_requests_keep_attribution_isolated(monkeypatch, tmp_path, capsys):
    _, clients = _registry_clients(monkeypatch, tmp_path, capsys)
    app = create_app(enable_startup_tasks=False)
    request_specs = (("dashboard", True), ("operator-cli", False))
    prepared = []
    for tag, is_web in request_specs:
        registered = clients[tag]
        transport = TestClient(
            app,
            client=(("198.51.100.12", 43210) if is_web else ("127.0.0.1", 45678)),
            headers={"Host": ("router.example.test" if is_web else "127.0.0.1:5000")},
        )
        with transport:
            etag = transport.get("/api/v2/ui/settings/client-list").headers["etag"]
        prepared.append((registered, is_web, etag))

    def mutate(index, item):
        registered, is_web, etag = item
        client = TestClient(
            app,
            client=(("198.51.100.12", 43210) if is_web else ("127.0.0.1", 45678)),
            headers={"Host": ("router.example.test" if is_web else "127.0.0.1:5000")},
        )
        headers = {"Authorization": f"Bearer {registered['token']}", "If-Match": etag,
                   "X-Request-ID": f"parallel-caller-{index}"}
        if is_web:
            headers["Origin"] = "https://router.example.test"
        with client:
            response = client.patch("/api/v2/ui/settings/client-list",
                                    json={"show_inactive": bool(index)}, headers=headers)
        return registered, response

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = [future.result(timeout=10) for future in (
            pool.submit(mutate, index, item) for index, item in enumerate(prepared)
        )]
    assert sorted(response.status_code for _, response in outcomes) == [200, 412]
    with db_session() as connection:
        committed = connection.execute(
            "SELECT details_json FROM operational_logs WHERE event_type='ui_client_list_visibility_updated'"
        ).fetchall()
        denials = connection.execute(
            "SELECT details_json FROM operational_logs WHERE event_type='client_list_mutation_conflict'"
        ).fetchall()
    committed_identity = next(
        json.loads(row["details_json"])["api_identity"] for row in committed
        if json.loads(row["details_json"]).get("api_identity", {}).get("api_request_id")
        in {response.headers["x-fwrouter-request-id"] for _, response in outcomes if response.status_code == 200}
    )
    winner = next(registered for registered, response in outcomes if response.status_code == 200)
    loser = next(registered for registered, response in outcomes if response.status_code == 412)
    assert committed_identity["api_client_id"] == winner["client_id"]
    assert any(json.loads(row["details_json"]).get("api_identity", {}).get("api_client_id") == loser["client_id"]
               for row in denials)


def test_denial_events_keep_server_request_id_and_authenticated_actor(monkeypatch, tmp_path, capsys):
    _, clients = _registry_clients(monkeypatch, tmp_path, capsys)
    app = create_app(enable_startup_tasks=False)
    registered = clients["operator-cli"]
    client = TestClient(app, client=("127.0.0.1", 45678), headers={"Host": "127.0.0.1:5000"})

    def find_denial(request_id):
        with db_session() as connection:
            rows = connection.execute(
                "SELECT details_json FROM operational_logs "
                "WHERE event_type IN ('client_list_mutation_denied', 'client_list_mutation_conflict')"
            ).fetchall()
        return next(
            parsed for parsed in (json.loads(row["details_json"]) for row in rows)
            if parsed.get("api_request_id") == request_id
        )

    def clear_aggregates():
        with attribution._DENIAL_LOCK:
            attribution._DENIAL_BUCKETS.clear()

    with client:
        etag = client.get("/api/v2/ui/settings/client-list").headers["etag"]
        requests = (
            ("anonymous-invalid", b'{', None, None),
            ("authenticated-json", b'{', registered["token"], None),
            ("authenticated-bool", b'{"show_inactive":1}', registered["token"], etag),
            ("authenticated-if-match", b'{"show_inactive":true}', registered["token"], "invalid-etag"),
            ("authenticated-no-if-match", b'{"show_inactive":true}', registered["token"], None),
        )
        for label, body, token, precondition in requests:
            clear_aggregates()
            headers = {"Content-Type": "application/json", "X-Request-ID": f"caller-{label}"}
            if token:
                headers["Authorization"] = f"Bearer {token}"
            if precondition:
                headers["If-Match"] = precondition
            response = client.patch("/api/v2/ui/settings/client-list", content=body, headers=headers)
            assert response.status_code in {400, 401, 422, 428}
            server_id = response.headers["x-fwrouter-request-id"]
            assert server_id != f"caller-{label}"
            event = find_denial(server_id)
            assert event["api_request_id"] == server_id
            assert event["request_id"] == f"caller-{label}"
            if token:
                assert event["api_identity"]["api_client_id"] == registered["client_id"]
                assert event["api_identity"]["api_request_id"] == server_id
            else:
                assert "api_identity" not in event

        legacy_put_requests = (
            ("put-authenticated-json", b'{', None),
            ("put-authenticated-bool", b'{"show_inactive":1}', None),
            ("put-authenticated-if-match", b'{"show_inactive":true}', "invalid-etag"),
        )
        for label, body, precondition in legacy_put_requests:
            clear_aggregates()
            headers = {"Content-Type": "application/json",
                       "Authorization": f"Bearer {registered['token']}",
                       "X-Request-ID": f"caller-{label}"}
            if precondition:
                headers["If-Match"] = precondition
            response = client.put("/api/v2/ui/settings/display", content=body, headers=headers)
            assert response.status_code in {400, 422}
            server_id = response.headers["x-fwrouter-request-id"]
            event = find_denial(server_id)
            assert event["api_request_id"] == server_id
            assert event["api_identity"]["api_client_id"] == registered["client_id"]
            assert event["api_identity"]["api_request_id"] == server_id

        # Invalid credential remains anonymous for identity, while keeping
        # server correlation. The caller's correlation header is not reused.
        clear_aggregates()
        invalid = client.patch(
            "/api/v2/ui/settings/client-list", content=b'{"show_inactive":true}',
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {'a' * 64}",
                     "X-Request-ID": "caller-invalid-credential"},
        )
        assert invalid.status_code == 401
        invalid_id = invalid.headers["x-fwrouter-request-id"]
        invalid_event = find_denial(invalid_id)
        assert invalid_event["api_request_id"] == invalid_id
        assert "api_identity" not in invalid_event


def test_snapshot_request_id_takes_precedence_over_ambient_request_context():
    from fwrouter_api.services import api_client_attribution as attribution_context

    snapshot = build_attribution_snapshot(_identity(), api_request_id="job-request-A")
    marker = attribution_context.set_api_request_id("ambient-request-B")
    try:
        with attribution_from_snapshot(snapshot):
            enriched = attribution_context.apply_attribution({
                "api_request_id": "caller-forged-id",
                "api_identity": {"api_request_id": "caller-forged-id"},
            })
            assert enriched["api_request_id"] == "job-request-A"
            assert enriched["api_identity"]["api_request_id"] == "job-request-A"
    finally:
        attribution_context.reset_api_request_id(marker)


def test_stale_cas_emits_one_identity_bound_attempt_and_exact_suppression(monkeypatch, tmp_path, capsys):
    _, clients = _registry_clients(monkeypatch, tmp_path, capsys)
    registered = clients["operator-cli"]
    app = create_app(enable_startup_tasks=False)
    client = TestClient(app, client=("127.0.0.1", 45678), headers={"Host": "127.0.0.1:5000"})
    clock = [100.0]
    monkeypatch.setattr(attribution, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    with attribution._DENIAL_LOCK:
        attribution._DENIAL_BUCKETS.clear()

    with client:
        stale_etag = client.get("/api/v2/ui/settings/client-list").headers["etag"]
        current_etag = client.patch(
            "/api/v2/ui/settings/client-list", json={"show_inactive": True},
            headers={"Authorization": f"Bearer {registered['token']}", "If-Match": stale_etag},
        ).headers["etag"]
        assert current_etag != stale_etag

        emitted = []
        for attempt, tick in ((1, 100.0), (2, 101.0), (3, 131.0)):
            clock[0] = tick
            response = client.patch(
                "/api/v2/ui/settings/client-list", json={"show_inactive": False},
                headers={"Authorization": f"Bearer {registered['token']}",
                         "If-Match": stale_etag, "X-Request-ID": f"cas-caller-{attempt}"},
            )
            assert response.status_code == 412
            request_id = response.headers["x-fwrouter-request-id"]
            with db_session() as connection:
                rows = connection.execute(
                    "SELECT details_json FROM operational_logs WHERE event_type='client_list_mutation_conflict'"
                ).fetchall()
            matches = [json.loads(row["details_json"]) for row in rows
                       if json.loads(row["details_json"]).get("api_request_id") == request_id]
            if attempt == 2:
                assert matches == []
            else:
                assert len(matches) == 1
                assert matches[0]["api_identity"]["api_client_id"] == registered["client_id"]
                emitted.append(matches[0])
            with attribution._DENIAL_LOCK:
                bucket = next(value for key, value in attribution._DENIAL_BUCKETS.items()
                              if key[1] == f"{registered['client_id']}:{registered['credential_id']}"
                              and key[2] == "CLIENT_LIST_REVISION_STALE")
            assert bucket[1] == attempt - 1 if attempt < 3 else bucket[1] == 0

        assert emitted[0]["suppressed_count"] == 0
        assert emitted[1]["suppressed_count"] == 1


def test_api_response_serializer_redacts_reserved_fields_only():
    from fwrouter_api.schemas import ApiResponse

    identity = {"api_client_id": "private-client", "api_credential_id": "private-credential"}
    serialized = ApiResponse(ok=True, data={
        "client_id": "business-client-id",
        "nested": [{"api_identity": identity, "_api_identity": identity,
                    "verified_api_client_id": "private", "client_id": "business-nested-id"}],
    }).model_dump(mode="json")
    rendered = json.dumps(serialized)
    assert "private-client" not in rendered and "private-credential" not in rendered
    assert "api_identity" not in rendered and "verified_api_client_id" not in rendered
    assert serialized["data"]["client_id"] == "business-client-id"
    assert serialized["data"]["nested"] == [{"client_id": "business-nested-id"}]


def test_legacy_capability_keeps_unverified_identity_and_nested_redaction():
    legacy = SimpleNamespace(assurance="legacy_capability", principal="presentation-operator")
    snapshot = build_attribution_snapshot(legacy, api_request_id="server-id", caller_request_id="client-id")
    assert snapshot == {
        "assurance": "legacy_capability",
        "verified": False,
        "api_request_id": "server-id",
        "caller_request_id": "client-id",
    }
    rendered = redact_http_attribution({"nested": {"api_identity": snapshot, "ok": True}})
    assert rendered == {"nested": {"ok": True}}
