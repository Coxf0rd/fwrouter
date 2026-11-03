from __future__ import annotations

import sqlite3
from collections import Counter
from collections.abc import Iterable as Iterable
from functools import lru_cache
from hashlib import sha256
from pathlib import Path
import re
from typing import Any


EXPECTED_SCHEMA_VERSION = "27"

_TABLE_EXPECTATIONS: dict[str, dict[str, Any]] = {
    "subscription_node_identities": {
        "columns": {"identity_id", "origin_server_id", "current_server_id", "revision", "provenance_sha256", "created_at"},
        "sql_contains": ("origin_server_id text not null unique", "current_server_id text not null unique"),
    },
    "subscription_node_aliases": {
        "columns": {"server_id", "identity_id"},
        "sql_contains": ("server_id text primary key", "references subscription_node_identities(identity_id)"),
    },
    "subscription_node_credentials": {
        "columns": {"scope", "identity_id", "client_uuid", "client_email", "epoch", "revoked"},
        "sql_contains": ("primary key (scope, identity_id)", "client_uuid text not null unique", "client_email text not null unique"),
    },
    "subscription_node_identity_transitions": {
        "columns": {"identity_id", "from_server_id", "to_server_id", "from_revision", "to_revision", "before_provenance_sha256", "after_provenance_sha256", "source_ownership_sha256", "operator_plan_sha256", "checkpoint_sha256", "created_at"},
        "sql_contains": ("primary key (identity_id, from_revision)", "to_revision = from_revision + 1"),
    },
    "subscription_node_credential_history": {
        "columns": {"client_uuid", "scope", "identity_id", "epoch", "operation_id_sha256", "retired_at"},
        "sql_contains": ("client_uuid text primary key", "operation_id_sha256 text not null"),
    },
    "provider_credentials": {"columns": {"source_ref", "api_key"}, "sql_contains": ("references provider_bindings(source_ref) on delete cascade",)},
    "provider_locations": {"columns": {"source_ref", "location_id", "label"}, "sql_contains": ("primary key (source_ref, location_id)",)},
    "provider_bindings": {
        "columns": {"source_ref", "provider_id", "resource_kind", "resource_id", "logical_server_id", "protocol", "enabled", "allow_automatic_member_switch", "binding_revision", "current_member_id", "current_location_id", "observed_protocol", "observed_at", "applied_member_id", "applied_protocol", "applied_at", "applied_revision", "last_outcome", "updated_at", "available_configs_json"},
        "sql_contains": ("create table provider_bindings", "source_ref text primary key", "check (enabled in (0, 1))"),
    },
    "provider_members": {
        "columns": {"source_ref", "provider_member_id", "location_id", "protocol", "ip", "available_slots", "provider_status", "provider_status_source", "auto_enabled", "priority", "advertised", "first_seen_at", "last_seen_at", "last_seen_revision"},
        "sql_contains": ("create table provider_members", "primary key (source_ref, provider_member_id, location_id, protocol)"),
    },
    "provider_evidence": {
        "columns": {"source_ref", "evidence_kind", "scope_key", "binding_revision", "observed_at", "safe_json", "outcome"},
        "sql_contains": ("create table provider_evidence", "safe_json text not null"),
    },
    "subscription_profile_snapshots": {
        "columns": {"token", "nodes_json", "runtime_verified_at", "updated_at"},
        "sql_contains": (
            "create table subscription_profile_snapshots",
            "token text primary key",
            "references subscription_clients(token) on delete cascade",
        ),
    },
    "schema_meta": {
        "columns": {"key", "value", "updated_at"},
        "sql_contains": (
            "create table schema_meta",
            "key text primary key",
            "value text not null",
        ),
    },
    "modules": {
        "columns": {
            "module_name",
            "desired_state",
            "lifecycle_mode",
            "runtime_state",
            "apply_state",
            "status_text",
            "error_code",
            "error_message",
            "updated_at",
        },
        "sql_contains": (
            "create table modules",
            "module_name text primary key",
            "lifecycle_mode text not null default 'none'",
            "check (lifecycle_mode in ('none', 'managed', 'external'))",
            "apply_state text not null default 'clean'",
            "check (runtime_state in ('not_configured', 'running', 'stopped', 'failed', 'degraded', 'paused'))",
        ),
    },
    "apply_versions": {
        "columns": {"apply_id", "manifest_path", "status", "created_at", "core_intent_revision"},
        "sql_contains": ("create table apply_versions", "apply_id text primary key"),
    },
    "external_connections": {
        "columns": {
            "connection_id",
            "system_id",
            "label",
            "connection_type",
            "runtime_type",
            "replacement_target",
            "location",
            "address",
            "integration_mode",
            "refresh_mode",
            "enabled",
            "value_json",
            "created_at",
            "updated_at",
            "last_seen_at",
        },
        "sql_contains": (
            "create table external_connections",
            "connection_id text primary key",
            "system_id text not null",
            "connection_type text not null",
            "check (connection_type in ('external_management', 'external_vpn_module', 'external_network_source', 'display_only'))",
        ),
    },
    "external_connection_generated_state": {
        "columns": {
            "connection_id",
            "state_json",
            "updated_at",
        },
        "sql_contains": (
            "create table external_connection_generated_state",
            "connection_id text primary key",
            "references external_connections(connection_id) on delete cascade",
        ),
    },
    "external_connection_migrations": {
        "columns": {
            "migration_key",
            "applied_at",
        },
        "sql_contains": (
            "create table external_connection_migrations",
            "migration_key text primary key",
        ),
    },
    "watchdog_state": {
        "columns": {
            "id",
            "path_key",
            "failure_candidate_json",
            "last_processed_decision_id",
            "last_successful_failover_at",
            "failover_path_key",
            "previous_target_id",
            "selected_target_id",
            "cooldown_until",
            "last_idle_probe_at",
            "last_idle_probe_server_id",
            "last_idle_probe_status",
            "updated_at",
        },
        "sql_contains": (
            "create table watchdog_state",
            "id integer primary key check (id = 1)",
            "failure_candidate_json text",
            "cooldown_until text",
        ),
    },
    "subjects": {
        "columns": {
            "subject_id",
            "subject_type",
            "subject_role",
            "implementation_kind",
            "stable_key",
            "display_name",
            "alias",
            "desired_mode",
            "applied_mode",
            "apply_state",
            "runtime_state",
            "is_active",
            "is_deleted",
            "first_seen_at",
            "last_seen_at",
            "last_traffic_at",
            "inactive_since",
            "deleted_at",
            "metadata_json",
            "created_at",
            "updated_at",
        },
        "sql_contains": (
            "create table subjects",
            "stable_key text not null",
            "subject_type text not null",
            "subject_role in ('unknown', 'lan_client', 'external_network_source', 'vless_client', 'docker_runtime', 'host_runtime', 'router_core')",
            "implementation_kind text not null default 'unknown'",
            "desired_mode in ('global', 'direct', 'selective', 'vpn', 'disabled', 'enabled', 'forced_vpn')",
            "is_deleted integer not null default 0",
        ),
    },
    "subject_server_overrides": {
        "columns": {
            "subject_id",
            "selected_server_id",
            "selected_target_kind",
            "selected_until",
            "apply_state",
            "error_code",
            "error_message",
            "updated_at",
        },
        "sql_contains": (
            "create table subject_server_overrides",
            "apply_state text not null default 'clean'",
            "selected_server_id text",
            "selected_target_kind text not null default 'server'",
        ),
    },
    "subscription_server_memberships": {
        "columns": {
            "source_id",
            "server_id",
            "source_url",
            "entry_identity_hash",
            "parser_format",
            "display_name",
            "first_seen_at",
            "last_seen_at",
            "is_active",
            "updated_at",
        },
        "sql_contains": (
            "create table subscription_server_memberships",
            "primary key (source_id, server_id)",
            "references servers(server_id) on delete cascade",
            "check (is_active in (0, 1))",
        ),
    },
    "subscription_accounts": {
        "columns": {
            "account_id",
            "slug",
            "display_name",
            "enabled",
            "created_at",
            "updated_at",
        },
        "sql_contains": (
            "create table subscription_accounts",
            "slug text not null unique",
            "check (enabled in (0, 1))",
        ),
    },
    "subscription_clients": {
        "columns": {
            "client_id",
            "account_id",
            "token",
            "app_type",
            "enabled",
            "display_name",
            "last_seen_at",
            "last_user_agent",
            "created_at",
            "updated_at",
        },
        "sql_contains": (
            "create table subscription_clients",
            "token text not null unique",
            "app_type text not null default 'auto'",
            "foreign key (account_id) references subscription_accounts(account_id) on delete cascade",
        ),
    },
    "traffic_counter_snapshots": {
        "columns": {
            "counter_key",
            "subject_id",
            "path",
            "rx_bytes",
            "tx_bytes",
            "collected_at",
            "metadata_json",
        },
        "sql_contains": (
            "create table traffic_counter_snapshots",
            "counter_key text primary key",
            "check (path in ('direct', 'vpn', 'blocked'))",
        ),
    },
}

# Exact full-schema exceptions observed from the baseline's supported
# migration-produced schema-27 fixtures.  The canonical inspector still
# validates every other object; only these complete mismatch sets can match.
# Fingerprints are SHA-256(repr(_table_semantics(...))) from disposable
# f7847a39 schema-7/10/11 -> 27 fixtures.  They retain known historical
# constraint omissions without permitting arbitrary missing constraints.
_LEGACY_SCHEMA27_PROFILES: tuple[tuple[str, dict[str, str]], ...] = (
    ("legacy_migrated_schema27_from_7", {
        "modules": "9394895782232a6655c0c13a88d9cb10c1f10e55eb9ead6422786323ce0bd4f9",
        "routing_global_state": "5ac8a8c06df945a1eb7ab0b45adcd23e1357569d512bd4bfbd7a59eb8ca219e8",
        "server_custom_https_proxy": "fdc6c4c6f5f04d5df60e46d34316b510724c433aae350e6dd638a49a9ad685aa",
        "server_preferences": "f5ac5c1db04b6858c11e86c69861099b0b07588185f453bb361165929b303fd8",
    }),
    ("legacy_migrated_schema27_from_10_or_11", {
        "routing_global_state": "5ac8a8c06df945a1eb7ab0b45adcd23e1357569d512bd4bfbd7a59eb8ca219e8",
        "server_preferences": "8c0085efdc69f58e2c8d948b2cb6bc01e4aa3b52a9cb0b327cc7aa0118f3a49a",
    }),
)
_LEGACY_SCHEMA27_PROFILE_TABLES = {
    name: frozenset(fingerprints) for name, fingerprints in _LEGACY_SCHEMA27_PROFILES
}
_LEGACY_SCHEMA27_PROFILE_NOTES = {
    "legacy_migrated_schema27_from_7": (
        "Preserves the baseline 7-to-27 table declarations, including three absent "
        "routing_global_state server foreign keys and historical inline CHECK placement."
    ),
    "legacy_migrated_schema27_from_10_or_11": (
        "Preserves the baseline 10/11-to-27 table declarations, including three absent "
        "routing_global_state server foreign keys and historical inline CHECK placement."
    ),
}


def _normalize_sql(value: str | None) -> str:
    if not value:
        return ""
    result: list[str] = []
    pending_space = False
    index = 0
    while index < len(value):
        char = value[index]
        if char.isspace():
            pending_space = True
            index += 1
            continue
        if pending_space and result:
            result.append(" ")
        pending_space = False
        if char == "'":
            result.append(char)
            index += 1
            while index < len(value):
                result.append(value[index])
                if value[index] == "'":
                    if index + 1 < len(value) and value[index + 1] == "'":
                        result.append(value[index + 1])
                        index += 2
                        continue
                    index += 1
                    break
                index += 1
            continue
        if char in ('"', "`", "["):
            closing = "]" if char == "[" else char
            index += 1
            identifier: list[str] = []
            while index < len(value):
                if value[index] == closing:
                    if index + 1 < len(value) and value[index + 1] == closing:
                        identifier.append(closing)
                        index += 2
                        continue
                    index += 1
                    break
                identifier.append(value[index])
                index += 1
            result.append("".join(identifier).lower())
            continue
        result.append(char.lower())
        index += 1
    return "".join(result).strip()


def _check_expressions(sql: str | None) -> tuple[str, ...]:
    """Return CHECK expressions while respecting nested and quoted parentheses."""
    if not sql:
        return ()
    result: list[str] = []
    for match in re.finditer(r"\bcheck\s*\(", sql, re.IGNORECASE):
        opening = sql.find("(", match.start())
        depth = 0
        quote: str | None = None
        index = opening
        while index < len(sql):
            char = sql[index]
            if quote is not None:
                if char == quote:
                    if index + 1 < len(sql) and sql[index + 1] == quote:
                        index += 2
                        continue
                    quote = None
            elif char in ("'", '"', "`", "["):
                quote = "]" if char == "[" else char
            elif char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    result.append(_normalize_sql(sql[opening + 1:index]))
                    break
            index += 1
        else:
            return ("<malformed-check>",)
    return tuple(sorted(result))


def _table_declaration_contract(sql: str | None) -> tuple[str, ...]:
    """Normalize CREATE TABLE column/constraint declarations independent of order."""
    if not sql:
        return ("<missing-table-ddl>",)
    opening = sql.find("(")
    if opening < 0:
        return ("<malformed-table-ddl>",)
    parts: list[str] = []
    start = opening + 1
    depth = 1
    quote: str | None = None
    index = start
    while index < len(sql):
        char = sql[index]
        if quote is not None:
            if char == quote:
                if index + 1 < len(sql) and sql[index + 1] == quote:
                    index += 2
                    continue
                quote = None
        elif char in ("'", '"', "`", "["):
            quote = "]" if char == "[" else char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                part = _normalize_sql(sql[start:index])
                if part:
                    parts.append(part)
                return tuple(sorted(parts))
        elif char == "," and depth == 1:
            part = _normalize_sql(sql[start:index])
            if part:
                parts.append(part)
            start = index + 1
        index += 1
    return ("<malformed-table-ddl>",)


def _index_semantics(connection: sqlite3.Connection, table_name: str) -> tuple[tuple[Any, ...], ...]:
    result: list[tuple[Any, ...]] = []
    for index in connection.execute(f'PRAGMA index_list("{table_name}")'):
        name = str(index["name"])
        quoted = '"' + name.replace('"', '""') + '"'
        key_columns = tuple(
            (int(row["seqno"]),
             str(row["name"]) if row["name"] is not None else None,
             int(row["desc"]), str(row["coll"] or "BINARY").upper())
            for row in connection.execute(f"PRAGMA index_xinfo({quoted})")
            if int(row["key"])
        )
        result.append((int(index["unique"]), int(index["partial"]),
                       str(index["origin"]), key_columns))
    return tuple(sorted(result, key=repr))


def _table_semantics(
    connection: sqlite3.Connection,
    table_name: str,
    *,
    sql: str | None = None,
    table_options: tuple[int, int] | None = None,
) -> tuple[Any, ...]:
    quoted = '"' + table_name.replace('"', '""') + '"'
    columns = tuple(sorted(
        (str(row["name"]), " ".join(str(row["type"] or "").upper().split()),
         int(row["notnull"]), _normalize_sql(row["dflt_value"]), int(row["pk"]), int(row["hidden"]))
        for row in connection.execute(f"PRAGMA table_xinfo({quoted})")
    ))
    if sql is None:
        sql = _table_sql(connection, table_name)
    if table_options is None:
        table_options = next(
            ((int(row["wr"]), int(row["strict"]))
             for row in connection.execute("PRAGMA table_list") if str(row["name"]) == table_name),
            None,
        )
    return (
        columns,
        _check_expressions(sql),
        _table_declaration_contract(sql),
        table_options,
        _index_semantics(connection, table_name),
    )


def _objects(connection: sqlite3.Connection) -> dict[str, dict[str, str | None]]:
    result: dict[str, dict[str, str | None]] = {kind: {} for kind in ("table", "index", "view", "trigger")}
    for row in connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
    ):
        result[str(row["type"])][str(row["name"])] = str(row["sql"]) if row["sql"] is not None else None
    return result


@lru_cache(maxsize=1)
def _canonical_schema_contract() -> tuple[dict[str, dict[str, str | None]], dict[str, tuple[Any, ...]]]:
    """Compile the complete expected contract from canonical schema.sql in memory."""
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    try:
        connection.executescript(Path(__file__).with_name("schema.sql").read_text(encoding="utf-8"))
        objects = _objects(connection)
        tables = {
            name: _table_semantics(connection, name)
            for name in objects["table"]
        }
        return objects, tables
    finally:
        connection.close()


def _load_schema_version(connection: sqlite3.Connection) -> str | None:
    if connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_meta'"
    ).fetchone() is None:
        return None
    row = connection.execute(
        "SELECT value FROM schema_meta WHERE key = 'schema_version'"
    ).fetchone()
    if row is None:
        return None
    return str(row["value"])


def _table_columns(connection: sqlite3.Connection, table_name: str) -> set[str]:
    rows = connection.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {str(row["name"]) for row in rows}


def _table_sql(connection: sqlite3.Connection, table_name: str) -> str:
    row = connection.execute(
        """
        SELECT sql
        FROM sqlite_master
        WHERE type = 'table'
          AND name = ?
        """,
        (table_name,),
    ).fetchone()
    if row is None:
        return ""
    return str(row["sql"] or "")


def _known_legacy_schema27_profile(
    connection: sqlite3.Connection,
    problems: list[dict[str, Any]],
    expected_tables: dict[str, tuple[Any, ...]],
    actual_objects: dict[str, dict[str, str | None]],
    actual_table_options: dict[str, tuple[int, int]],
) -> str | None:
    if not problems or any(problem.get("code") != "SCHEMA_TABLE_CONTRACT_MISMATCH"
                            for problem in problems):
        return None
    mismatched_tables = {str(problem.get("table")) for problem in problems}
    if not mismatched_tables or any(table not in expected_tables for table in mismatched_tables):
        return None

    fingerprints: dict[str, str] = {}
    for table in mismatched_tables:
        actual = _table_semantics(
            connection, table, sql=actual_objects["table"].get(table),
            table_options=actual_table_options.get(table),
        )
        # Preserve the historical additive non-unique performance-index
        # contract while keeping every canonical index definition required.
        expected_indexes = Counter(expected_tables[table][4])
        actual_indexes = Counter(actual[4])
        missing_indexes = expected_indexes - actual_indexes
        extra_indexes = actual_indexes - expected_indexes
        if missing_indexes or any(index[0] for index in extra_indexes):
            return None
        filtered_indexes = tuple(sorted(
            (actual_indexes - extra_indexes).elements(), key=repr,
        ))
        normalized = (*actual[:4], filtered_indexes)
        fingerprints[table] = sha256(repr(normalized).encode("utf-8")).hexdigest()

    for profile_name, profile_fingerprints in _LEGACY_SCHEMA27_PROFILES:
        if fingerprints == profile_fingerprints:
            return profile_name
    return None


def inspect_database_schema(
    connection: sqlite3.Connection,
    *,
    allow_legacy_schema27_profiles: bool = True,
) -> dict[str, Any]:
    schema_version = _load_schema_version(connection)
    tables: dict[str, Any] = {}
    problems: list[dict[str, Any]] = []

    expected_objects, expected_tables = _canonical_schema_contract()
    actual_objects = _objects(connection)
    actual_table_options = {
        str(row["name"]): (int(row["wr"]), int(row["strict"]))
        for row in connection.execute("PRAGMA table_list")
    }
    for object_type in ("table", "index", "view", "trigger"):
        expected_names = set(expected_objects[object_type])
        actual_names = set(actual_objects[object_type])
        missing = sorted(expected_names - actual_names)
        extra = sorted(actual_names - expected_names)
        if object_type == "index" and extra:
            # Historic CLI upgrade admission explicitly allowed additive
            # non-unique performance indexes. Keep that narrow schema-27
            # compatibility while rejecting indexes that change write rules.
            unexpected: list[str] = []
            for name in extra:
                row = connection.execute(
                    "SELECT tbl_name FROM sqlite_master WHERE type='index' AND name=?", (name,)
                ).fetchone()
                if row is None:
                    unexpected.append(name)
                    continue
                table = '"' + str(row["tbl_name"]).replace('"', '""') + '"'
                index = next((item for item in connection.execute(f"PRAGMA index_list({table})")
                              if str(item["name"]) == name), None)
                if index is None or int(index["unique"]):
                    unexpected.append(name)
            extra = unexpected
        if missing or extra:
            problems.append({
                "code": "SCHEMA_OBJECT_SET_MISMATCH",
                "object_type": object_type,
                "missing": missing,
                "unexpected": extra,
            })

    actual_table_names = set(actual_objects["table"])
    for table_name in sorted(set(expected_tables) & actual_table_names):
        actual = _table_semantics(
            connection, table_name, sql=actual_objects["table"][table_name],
            table_options=actual_table_options.get(table_name),
        )
        expected = expected_tables[table_name]
        expected_indexes, actual_indexes = Counter(expected[4]), Counter(actual[4])
        missing_indexes = expected_indexes - actual_indexes
        extra_indexes = actual_indexes - expected_indexes
        if (actual[:4] != expected[:4] or missing_indexes
                or any(index[0] for index, count in extra_indexes.items() if count)):
            problems.append({"code": "SCHEMA_TABLE_CONTRACT_MISMATCH", "table": table_name})

    # Table indexes are compared semantically above, so harmless DDL formatting
    # and ALTER-added column positions do not matter. Partial/expression index
    # predicates do matter and retain an exact normalized SQL check.
    for name in sorted(set(expected_objects["index"]) & set(actual_objects["index"])):
        row = connection.execute(
            "SELECT tbl_name FROM sqlite_master WHERE type='index' AND name=?", (name,)
        ).fetchone()
        table = '"' + str(row["tbl_name"]).replace('"', '""') + '"'
        metadata = next(item for item in connection.execute(f"PRAGMA index_list({table})")
                        if str(item["name"]) == name)
        quoted = '"' + name.replace('"', '""') + '"'
        expression_index = any(
            int(item["key"]) and item["name"] is None
            for item in connection.execute(f"PRAGMA index_xinfo({quoted})")
        )
        if (int(metadata["partial"]) or expression_index) and (
                _normalize_sql(expected_objects["index"][name])
                != _normalize_sql(actual_objects["index"][name])):
            problems.append({"code": "SCHEMA_OBJECT_DEFINITION_MISMATCH",
                             "object_type": "index", "name": name})

    # Views and triggers have no equivalent PRAGMA semantics.
    for object_type in ("view", "trigger"):
        for name in sorted(set(expected_objects[object_type]) & set(actual_objects[object_type])):
            if (_normalize_sql(expected_objects[object_type][name])
                    != _normalize_sql(actual_objects[object_type][name])):
                problems.append({"code": "SCHEMA_OBJECT_DEFINITION_MISMATCH",
                                 "object_type": object_type, "name": name})

    for table_name, expected in _TABLE_EXPECTATIONS.items():
        sql = actual_objects["table"].get(table_name) or ""
        columns = _table_columns(connection, table_name) if sql else set()
        normalized_sql = _normalize_sql(sql)
        expected_columns = set(expected.get("columns") or set())
        missing_columns = sorted(expected_columns - columns)
        missing_sql_snippets = sorted(
            snippet
            for snippet in expected.get("sql_contains") or ()
            if snippet not in normalized_sql
        )
        table_ok = bool(sql) and not missing_columns and not missing_sql_snippets
        tables[table_name] = {
            "exists": bool(sql),
            "ok": table_ok,
            "columns": sorted(columns),
            "missing_columns": missing_columns,
            "missing_sql_snippets": missing_sql_snippets,
        }
        if not table_ok:
            problems.append(
                {
                    "code": "TABLE_SCHEMA_MISMATCH",
                    "table": table_name,
                    "missing_columns": missing_columns,
                    "missing_sql_snippets": missing_sql_snippets,
                }
            )

    version_ok = schema_version == EXPECTED_SCHEMA_VERSION
    if not version_ok:
        problems.append(
            {
                "code": "SCHEMA_VERSION_MISMATCH",
                "expected": EXPECTED_SCHEMA_VERSION,
                "actual": schema_version,
            }
        )

    legacy_profile = None
    if allow_legacy_schema27_profiles and schema_version == EXPECTED_SCHEMA_VERSION:
        legacy_profile = _known_legacy_schema27_profile(
            connection, problems, expected_tables, actual_objects, actual_table_options,
        )
        if legacy_profile is not None:
            for table_name in _LEGACY_SCHEMA27_PROFILE_TABLES[legacy_profile]:
                if table_name in tables:
                    tables[table_name] = {**tables[table_name], "ok": True}
            problems = []

    ok = version_ok and not problems
    result = {
        "ok": ok,
        "status": "ok" if ok else "drift",
        "expected_schema_version": EXPECTED_SCHEMA_VERSION,
        "actual_schema_version": schema_version,
        "rebuild_required": not ok,
        "problems": problems,
        "tables": tables,
    }
    if legacy_profile is not None:
        result["compatibility_profile"] = legacy_profile
        result["compatibility_notes"] = [_LEGACY_SCHEMA27_PROFILE_NOTES[legacy_profile]]
    return result


def summarize_schema_state(schema_state: dict[str, Any]) -> dict[str, Any]:
    summary = {
        "ok": bool(schema_state.get("ok")),
        "status": str(schema_state.get("status") or "unknown"),
        "expected_schema_version": schema_state.get("expected_schema_version"),
        "actual_schema_version": schema_state.get("actual_schema_version"),
        "rebuild_required": bool(schema_state.get("rebuild_required")),
        "problem_count": len(list(schema_state.get("problems") or [])),
        "drifted_tables": [
            table_name
            for table_name, table_state in (schema_state.get("tables") or {}).items()
            if isinstance(table_state, dict) and not bool(table_state.get("ok"))
        ],
    }
    profile = schema_state.get("compatibility_profile")
    if isinstance(profile, str):
        summary["compatibility_profile"] = profile
        notes = schema_state.get("compatibility_notes")
        if isinstance(notes, list):
            summary["compatibility_notes"] = [str(note) for note in notes]
    return summary
