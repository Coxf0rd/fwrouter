"""Joined persistence/migration checks, executed only in qualified hosted Compose."""
from __future__ import annotations

import hashlib
import sqlite3

from .http_support import http_json
from .xray_support import loaded_identities


def _owned_database(stack):
    from fwrouter_api.core.config import get_settings
    path = get_settings().paths.db_path.resolve(strict=True)
    assert path.is_relative_to(stack["state"].resolve(strict=True))
    return path


def _database_rows(connection, table_names):
    """Return deterministic row snapshots for the selected existing tables."""
    snapshots = {}
    for table_name in sorted(table_names):
        quoted = '"' + table_name.replace('"', '""') + '"'
        columns = [row[1] for row in connection.execute(f"PRAGMA table_info({quoted})")]
        if not columns:
            continue
        projection = ", ".join('"' + name.replace('"', '""') + '"' for name in columns)
        snapshots[table_name] = (
            tuple(columns),
            [
                tuple(row)
                for row in connection.execute(
                    f"SELECT {projection} FROM {quoted} ORDER BY {projection}"
                ).fetchall()
            ],
        )
    return snapshots


def _rows_digest(rows):
    """Compare complete row values without putting persisted values in CI output."""
    return hashlib.sha256(repr(rows).encode("utf-8")).hexdigest()


def _assert_database_rows_exactly_preserved(before, after):
    """Current-schema initialization must preserve every table and row exactly."""
    assert set(before) == set(after)
    for table_name, (old_columns, old_rows) in before.items():
        new_columns, new_rows = after[table_name]
        assert new_columns == old_columns, f"{table_name} columns changed"
        assert _rows_digest(old_rows) == _rows_digest(new_rows), (
            f"{table_name} rows changed"
        )


def test_current_schema_initialization_preserves_control_plane_and_native_state(acceptance_stack):
    """Repeated schema-27 initialization preserves LAN, Core, provider, and native state."""
    stack = acceptance_stack
    from fwrouter_api.db.connection import initialize_database
    from fwrouter_api.db.schema_state import EXPECTED_SCHEMA_VERSION

    expected_schema_version = str(EXPECTED_SCHEMA_VERSION)
    assert expected_schema_version == "27"
    before_native = loaded_identities(stack["native"])
    path = _owned_database(stack)
    stack["worker"].terminate()
    stack["worker"].wait(timeout=5)
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        core_before = connection.execute(
            "SELECT desired_state,apply_state FROM modules WHERE module_name='core'",
        ).fetchone()
        assert core_before is not None and tuple(core_before) == ("enabled", "clean")
        connection.execute(
            "UPDATE modules SET desired_state='disabled',apply_state='pending',"
            "status_text='Acceptance fixture records pending Core intent' "
            "WHERE module_name='core'",
        )
        from fwrouter_api.services.core_intent import advance_core_intent_revision
        core_revision = advance_core_intent_revision(connection)
        assert core_revision > 0

        source = "acceptance:current-schema-provider"
        connection.execute(
            "INSERT INTO provider_bindings (source_ref,provider_id,resource_id,logical_server_id,"
            "protocol,enabled,current_member_id,updated_at) VALUES (?,?,?,?,?,0,?,0)",
            (source, "stealthsurf", "1", "current-schema-logical", "hysteria2", "member-1"),
        )
        connection.execute(
            "INSERT INTO provider_credentials (source_ref,api_key) VALUES (?,?)",
            (source, "synthetic-current-schema-secret"),
        )
        connection.execute(
            "INSERT INTO servers(server_id,server_name,provider_name,inventory_state) "
            "VALUES ('acceptance-server-27','Acceptance fixture server','acceptance','active')",
        )
        subject_id = "lan:acceptance-preserve"
        connection.execute(
            "INSERT INTO subjects(subject_id,subject_type,subject_role,implementation_kind,"
            "stable_key,display_name,desired_mode,applied_mode,runtime_state,is_active) "
            "VALUES (?, 'lan', 'lan_client', 'lan', ?, 'Acceptance fixture LAN client', "
            "'direct', 'direct', 'active', 1)",
            (subject_id, "acceptance-preserve"),
        )
        connection.execute(
            "INSERT INTO subject_lan(subject_id,mac_address,ip_address,hostname,dhcp_hostname) "
            "VALUES (?, '02:00:00:00:00:27', '192.0.2.27', 'acceptance-fixture', 'acceptance')",
            (subject_id,),
        )
        connection.execute(
            "INSERT INTO subject_server_overrides(subject_id,selected_server_id) VALUES (?,?)",
            (subject_id, "acceptance-server-27"),
        )
        connection.execute(
            "INSERT INTO subject_user_overrides(subject_id,override_mode,created_by) "
            "VALUES (?, 'direct', 'acceptance') ON CONFLICT(subject_id) DO UPDATE SET "
            "override_mode='direct',created_by='acceptance'",
            (subject_id,),
        )
        connection.execute(
            "INSERT INTO external_connections(connection_id,system_id,label,connection_type,"
            "runtime_type,value_json) VALUES (?,?,?,?,?,?)",
            ("acceptance-generated-state", "acceptance-system", "Synthetic", "external_management",
             "acceptance-provider", '{"source":"test"}'),
        )
        connection.execute(
            "INSERT INTO external_connection_generated_state(connection_id,state_json) VALUES (?,?)",
            ("acceptance-generated-state", '{"artifact":"preserve"}'),
        )
        identity_id = "acceptance-current-schema-identity"
        connection.execute(
            "INSERT INTO subscription_node_identities(identity_id,origin_server_id,current_server_id,"
            "provenance_sha256) VALUES (?,?,?,?)",
            (identity_id, "acceptance-origin", "acceptance-current", "a" * 64),
        )
        connection.execute(
            "INSERT INTO subscription_node_aliases(server_id,identity_id) VALUES (?,?)",
            ("acceptance-alias", identity_id),
        )
        connection.execute(
            "INSERT INTO subscription_node_credentials(scope,identity_id,client_uuid,client_email) "
            "VALUES (?,?,?,?)",
            ("xray", identity_id, "00000000-0000-4000-8000-000000000027", "acceptance-identity@example.invalid"),
        )
        connection.commit()
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert [tuple(row) for row in connection.execute("PRAGMA integrity_check").fetchall()] == [
            ("ok",),
        ]
        table_names = {
            str(row[0]) for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name",
            )
        }
        before_rows = _database_rows(connection, table_names)

    first = initialize_database()
    assert first["ok"] is True and first["actual_schema_version"] == expected_schema_version, first
    with sqlite3.connect(path) as connection:
        after_first_rows = _database_rows(connection, table_names)
        revision = connection.execute(
            "SELECT value_json FROM settings WHERE key='core.intent_revision'",
        ).fetchone()[0]
        core_module = connection.execute(
            "SELECT desired_state,apply_state FROM modules WHERE module_name='core'",
        ).fetchone()
        assert revision == str(core_revision)
        assert core_module == ("disabled", "pending")
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    _assert_database_rows_exactly_preserved(before_rows, after_first_rows)

    second = initialize_database()
    assert second["ok"] is True and second["actual_schema_version"] == expected_schema_version, second
    with sqlite3.connect(path) as connection:
        after_second_rows = _database_rows(connection, table_names)
    _assert_database_rows_exactly_preserved(after_first_rows, after_second_rows)

    stack["worker"], stack["api"] = stack["start_worker"]()
    code, response = http_json(stack["api"] + "/xray/clients")
    assert code == 200 and response.get("ok") is True, response
    assert loaded_identities(stack["native"]) == before_native
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT enabled,current_member_id,allow_automatic_member_switch FROM provider_bindings WHERE source_ref=?",
            (source,),
        ).fetchone() == (0, "member-1", 0)
        assert connection.execute(
            "SELECT api_key FROM provider_credentials WHERE source_ref=?", (source,),
        ).fetchone() == ("synthetic-current-schema-secret",)


def test_application_database_transaction_rollback_is_visible_after_restart(acceptance_stack):
    """The product transaction context must roll back intent; API/native state stays unchanged."""
    stack = acceptance_stack
    from fwrouter_api.db.connection import db_session
    before_native = loaded_identities(stack["native"])
    path = _owned_database(stack)
    source = "acceptance:rolled-back-binding"
    try:
        with db_session() as connection:
            connection.execute("INSERT INTO provider_bindings (source_ref,provider_id,resource_id,logical_server_id,protocol,updated_at) VALUES (?,?,?,?,?,0)",
                               (source, "stealthsurf", "2", "rollback-logical", "hysteria2"))
            assert connection.execute("SELECT source_ref FROM provider_bindings WHERE source_ref=?", (source,)).fetchone() is not None
            raise RuntimeError("intentional isolated transaction abort")
    except RuntimeError as exc:
        assert str(exc) == "intentional isolated transaction abort"
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT source_ref FROM provider_bindings WHERE source_ref=?", (source,)).fetchone() is None
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    stack["worker"].terminate()
    stack["worker"].wait(timeout=5)
    stack["worker"], stack["api"] = stack["start_worker"]()
    code, response = http_json(stack["api"] + "/xray/clients")
    assert code == 200 and response.get("ok") is True, response
    assert loaded_identities(stack["native"]) == before_native
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT source_ref FROM provider_bindings WHERE source_ref=?", (source,)).fetchone() is None


def _presentation_policy(stack, token, *, origin="https://presentation.test"):
    """Install only an owned synthetic verifier; never touch a host trust file."""
    import json
    path = stack["state"] / "presentation-verifier.json"
    assert path.parent.resolve().is_relative_to(stack["root"].resolve())
    document = {"schema_version": 1, "principal": "presentation-operator",
                "scopes": ["ui.client-list.write"],
                "token_sha256": hashlib.sha256(token.encode("ascii")).hexdigest(),
                "canonical_origin": origin}
    temporary = path.with_suffix(".pending")
    temporary.write_text(json.dumps(document), encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)
    return path


def _presentation_http(stack, *, method="GET", token=None, etag=None, value=None, origin=None):
    import json
    import urllib.error
    import urllib.request
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    if etag is not None:
        headers["If-Match"] = etag
    if origin is not None:
        headers["Origin"] = origin
    body = json.dumps({"show_inactive": value}).encode() if method == "PATCH" else None
    request = urllib.request.Request(stack["api"] + "/ui/settings/client-list", data=body,
                                     headers=headers, method=method)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        response = opener.open(request, timeout=5)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        data = response.read(8193)
        assert len(data) <= 8192
        return response.status if hasattr(response, "status") else response.code, json.loads(data)


def test_presentation_http_cli_cas_restart_and_native_unchanged(acceptance_stack):
    """Real isolated HTTP/CLI/persistence; native routing must remain untouched."""
    import secrets
    import subprocess
    import sys

    stack = acceptance_stack
    token = secrets.token_hex(32)
    _presentation_policy(stack, token)
    native_before = loaded_identities(stack["native"])

    # Warm read models before taking the mutation baseline. Workspace reads Xray
    # runtime status; its existing owner can refresh the observed `modules`
    # projection from the fixture's seeded status to live-derived state.
    code, response = _presentation_http(stack)
    assert code == 200
    current = response["data"]["client_list"]
    initial = current["show_inactive"]
    for _ in range(2):
        code, warm_workspace = http_json(stack["api"] + "/ui/settings/workspace")
        assert code == 200 and warm_workspace["data"]["workspace"]["display_settings"]["show_inactive"] is initial
        code, warm_panel = http_json(stack["api"] + "/ui/clients")
        assert code == 200 and warm_panel["data"]["display_settings"]["show_inactive"] is initial

    def routing_snapshot():
        with sqlite3.connect(_owned_database(stack)) as db:
            return (
                _database_rows(db, {"routing_global_state", "modules", "subject_server_overrides", "subject_user_overrides"}),
                db.execute("SELECT key,value_json FROM settings WHERE key IN ('core.intent_revision','subscriptions.native_generation.v1') ORDER BY key").fetchall(),
            )

    def assert_no_presentation_routing_side_effect(expected_rows, expected_generation):
        actual_rows, actual_generation = routing_snapshot()
        assert actual_rows == expected_rows, "presentation mutation changed routing/modules/override rows"
        assert actual_generation == expected_generation, "presentation mutation changed Core/native generation"

    # Freeze only after supported observation readers have settled. Every
    # presentation write/rejected request after this point must leave the full
    # routing, module, override, and generation snapshots equal.
    routing_before, generation_before = routing_snapshot()
    module_columns, module_rows = routing_before["modules"]
    xray_row = next(row for row in module_rows if row[module_columns.index("module_name")] == "xray")
    assert xray_row[module_columns.index("desired_state")] == "enabled"
    assert xray_row[module_columns.index("lifecycle_mode")] == "managed"
    assert xray_row[module_columns.index("runtime_state")] == "running"
    assert xray_row[module_columns.index("apply_state")] == "pending"
    assert xray_row[module_columns.index("status_text")] == (
        "Xray runtime and egress are ready, but no clients are configured."
    )
    code, _ = http_json(stack["api"] + "/ui/settings/workspace")
    assert code == 200
    code, _ = http_json(stack["api"] + "/ui/clients")
    assert code == 200
    assert_no_presentation_routing_side_effect(routing_before, generation_before)

    code, _ = _presentation_http(stack, method="PATCH", etag=current["etag"], value=not initial)
    assert code == 401
    assert_no_presentation_routing_side_effect(routing_before, generation_before)
    code, _ = _presentation_http(stack, method="PATCH", token=token,
                                  etag=current["etag"], value=not initial,
                                  origin="https://untrusted-origin.test")
    assert code == 403
    assert_no_presentation_routing_side_effect(routing_before, generation_before)
    code, changed = _presentation_http(stack, method="PATCH", token=token,
                                       etag=current["etag"], value=not initial)
    assert code == 200
    changed = changed["data"]["client_list"]
    assert changed["show_inactive"] is not initial
    assert_no_presentation_routing_side_effect(routing_before, generation_before)
    code, changed_workspace = http_json(stack["api"] + "/ui/settings/workspace")
    assert code == 200 and changed_workspace["data"]["workspace"]["display_settings"]["show_inactive"] is not initial
    code, panel = http_json(stack["api"] + "/ui/clients")
    assert code == 200 and panel["data"]["display_settings"]["show_inactive"] is not initial
    assert_no_presentation_routing_side_effect(routing_before, generation_before)
    code, _ = _presentation_http(stack, method="PATCH", token=token,
                                 etag=current["etag"], value=initial)
    assert code == 412
    assert_no_presentation_routing_side_effect(routing_before, generation_before)

    credential = stack["state"] / "presentation-token"
    credential.write_text(token, encoding="ascii")
    credential.chmod(0o600)
    # Only this owned child client receives the dynamic test API address. The
    # production CLI has no endpoint/env override, SQL path or fallback writer.
    client_script = (
        "import sys;from fwrouter_api.services import config_cli;"
        "config_cli.API_URL=sys.argv[1];"
        "from fwrouter_api.cli import main;raise SystemExit(main(sys.argv[2:]))"
    )
    env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "PYTHONPATH": "/workspace/backend",
           "PYTHONDONTWRITEBYTECODE": "1", "HOME": str(stack["state"])}
    result = subprocess.run(
        [sys.executable, "-c", client_script, stack["api"] + "/ui/settings/client-list",
         "config", "set", "ui-client-list", "--show-inactive", str(initial).lower(),
         "--expected-revision", changed["etag"], "--credential-file", str(credential)],
        cwd=stack["state"], env=env, capture_output=True, text=True, timeout=10,
    )
    credential_leaked = token in result.stdout + result.stderr
    assert credential_leaked is False
    assert result.returncode == 0, "owned API-first CLI conditional mutation failed"
    assert_no_presentation_routing_side_effect(routing_before, generation_before)
    code, final = _presentation_http(stack)
    assert code == 200 and final["data"]["client_list"]["show_inactive"] is initial
    final_etag = final["data"]["client_list"]["etag"]
    assert_no_presentation_routing_side_effect(routing_before, generation_before)

    stack["worker"].terminate()
    stack["worker"].wait(timeout=5)
    stack["worker"], stack["api"] = stack["start_worker"]()
    code, restarted = _presentation_http(stack)
    assert code == 200 and restarted["data"]["client_list"]["etag"] == final_etag
    # Revocation is observed from the actual verifier file on the next request.
    _presentation_policy(stack, secrets.token_hex(32))
    code, _ = _presentation_http(stack, method="PATCH", token=token,
                                 etag=final_etag, value=not initial)
    assert code in {401, 403}
    assert loaded_identities(stack["native"]) == native_before
    after_restart, generation_after_restart = routing_snapshot()
    assert generation_after_restart == generation_before
    for table_name in ("routing_global_state", "subject_server_overrides", "subject_user_overrides"):
        assert after_restart[table_name] == routing_before[table_name], (
            f"presentation mutation changed {table_name} across worker restart"
        )

    before_columns, before_modules = routing_before["modules"]
    after_columns, after_modules = after_restart["modules"]
    assert after_columns == before_columns
    before_by_name = {row[before_columns.index("module_name")]: row for row in before_modules}
    after_by_name = {row[after_columns.index("module_name")]: row for row in after_modules}
    assert after_by_name.keys() == before_by_name.keys()
    observable_columns = {
        "runtime_state", "apply_state", "status_text", "error_code", "error_message", "updated_at",
    }
    for module_name, before_row in before_by_name.items():
        after_row = after_by_name[module_name]
        for column in ("desired_state", "lifecycle_mode"):
            index = before_columns.index(column)
            assert after_row[index] == before_row[index], (
                f"presentation mutation changed {module_name} module intent {column} across restart"
            )
        changed_columns = {
            column for index, column in enumerate(before_columns)
            if before_row[index] != after_row[index]
        }
        if changed_columns:
            assert module_name == "xray", (
                f"unexpected observed module-row change across restart: {module_name} {sorted(changed_columns)}"
            )
            assert changed_columns <= observable_columns
            # The isolated fixture starts a real Xray process with no clients.
            # Its status owner truthfully derives the pending apply projection.
            assert after_row[after_columns.index("runtime_state")] == "running"
            assert after_row[after_columns.index("apply_state")] == "pending"
            assert after_row[after_columns.index("status_text")] == (
                "Xray runtime and egress are ready, but no clients are configured."
            )
            assert after_row[after_columns.index("error_code")] is None
            assert after_row[after_columns.index("error_message")] is None
