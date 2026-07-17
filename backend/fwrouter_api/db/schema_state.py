from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from typing import Any


EXPECTED_SCHEMA_VERSION = "7"

_TABLE_EXPECTATIONS: dict[str, dict[str, Any]] = {
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
            "apply_state text not null default 'clean'",
            "check (runtime_state in ('not_configured', 'running', 'stopped', 'failed', 'degraded', 'paused'))",
        ),
    },
    "subjects": {
        "columns": {
            "subject_id",
            "subject_type",
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
            "subject_type in ('lan', 'tailscale', 'tailscale_node', 'xray', 'host', 'docker', 'fwrouter')",
            "desired_mode in ('global', 'direct', 'selective', 'vpn', 'disabled', 'enabled', 'forced_vpn')",
            "is_deleted integer not null default 0",
        ),
    },
    "subject_tailscale": {
        "columns": {
            "subject_id",
            "node_id",
            "tailscale_ip",
            "hostname",
            "user_name",
            "online",
            "source_json",
            "updated_at",
        },
        "sql_contains": (
            "create table subject_tailscale",
            "tailscale_ip text",
            "check (online in (0, 1))",
        ),
    },
    "subject_server_overrides": {
        "columns": {
            "subject_id",
            "selected_server_id",
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


def _normalize_sql(value: str | None) -> str:
    if not value:
        return ""
    return " ".join(value.strip().lower().split())


def _load_schema_version(connection: sqlite3.Connection) -> str | None:
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


def inspect_database_schema(connection: sqlite3.Connection) -> dict[str, Any]:
    schema_version = _load_schema_version(connection)
    tables: dict[str, Any] = {}
    problems: list[dict[str, Any]] = []

    for table_name, expected in _TABLE_EXPECTATIONS.items():
        sql = _table_sql(connection, table_name)
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

    return {
        "ok": version_ok and not problems,
        "status": "ok" if version_ok and not problems else "drift",
        "expected_schema_version": EXPECTED_SCHEMA_VERSION,
        "actual_schema_version": schema_version,
        "rebuild_required": not (version_ok and not problems),
        "problems": problems,
        "tables": tables,
    }


def summarize_schema_state(schema_state: dict[str, Any]) -> dict[str, Any]:
    return {
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
