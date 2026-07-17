from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlparse

from fwrouter_api.db.connection import db_session


ALLOWED_SCHEMES = {"http", "https"}
PLACEHOLDER_HOSTS = {
    "subscription.example",
    "example.com",
    "example.net",
    "example.org",
    "localhost",
}


def _json_dumps(value: dict[str, Any] | None) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _json_loads(value: str | None) -> dict[str, Any] | None:
    if not value:
        return None

    loaded = json.loads(value)
    if isinstance(loaded, dict):
        return loaded

    return {"value": loaded}


def validate_subscription_url(url: str | None) -> dict[str, Any]:
    """Validate subscription URL without network access."""

    normalized_url = (url or "").strip()

    if not normalized_url:
        return {
            "valid": False,
            "normalized_url": "",
            "error": {
                "code": "SUBSCRIPTION_URL_EMPTY",
                "message": "Subscription URL is empty.",
            },
        }

    parsed = urlparse(normalized_url)

    if parsed.scheme not in ALLOWED_SCHEMES:
        return {
            "valid": False,
            "normalized_url": normalized_url,
            "error": {
                "code": "SUBSCRIPTION_URL_INVALID_SCHEME",
                "message": "Subscription URL must use http or https.",
            },
        }

    if not parsed.netloc:
        return {
            "valid": False,
            "normalized_url": normalized_url,
            "error": {
                "code": "SUBSCRIPTION_URL_INVALID_HOST",
                "message": "Subscription URL host is missing.",
            },
        }

    hostname = (parsed.hostname or "").strip().lower()
    if hostname in PLACEHOLDER_HOSTS:
        return {
            "valid": False,
            "normalized_url": normalized_url,
            "error": {
                "code": "SUBSCRIPTION_URL_PLACEHOLDER_HOST",
                "message": "Subscription URL host is a placeholder and must be replaced with a real provider URL.",
            },
        }

    return {
        "valid": True,
        "normalized_url": normalized_url,
        "error": None,
    }


def get_subscription_state() -> dict[str, Any]:
    """Return current subscription state from SQLite."""

    with db_session() as connection:
        row = connection.execute(
            """
            SELECT
                url,
                status,
                last_refresh_at,
                last_success_at,
                server_inventory_updated_at,
                error_code,
                error_message,
                metadata_json,
                updated_at
            FROM subscription_state
            WHERE id = 1
            """
        ).fetchone()

    if row is None:
        return {
            "url": None,
            "status": "not_configured",
            "last_refresh_at": None,
            "last_success_at": None,
            "server_inventory_updated_at": None,
            "error_code": None,
            "error_message": None,
            "metadata": None,
            "updated_at": None,
        }

    return {
        "url": row["url"],
        "status": row["status"],
        "last_refresh_at": row["last_refresh_at"],
        "last_success_at": row["last_success_at"],
        "server_inventory_updated_at": row["server_inventory_updated_at"],
        "error_code": row["error_code"],
        "error_message": row["error_message"],
        "metadata": _json_loads(row["metadata_json"]),
        "updated_at": row["updated_at"],
    }


def save_subscription_url(
    url: str,
    *,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Save subscription URL as desired/config state.

    This does not refresh provider inventory, does not update Mihomo and does not
    apply dataplane changes. Refresh will be implemented later as a job handler.
    """

    validation = validate_subscription_url(url)
    if not validation["valid"]:
        return {
            "saved": False,
            "validation": validation,
            "state": get_subscription_state(),
        }

    normalized_url = validation["normalized_url"]

    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO subscription_state (
                id,
                url,
                status,
                error_code,
                error_message,
                metadata_json
            )
            VALUES (1, ?, 'idle', NULL, NULL, ?)
            ON CONFLICT(id) DO UPDATE SET
                url = excluded.url,
                status = 'idle',
                error_code = NULL,
                error_message = NULL,
                metadata_json = excluded.metadata_json,
                updated_at = CURRENT_TIMESTAMP
            """,
            (normalized_url, _json_dumps(metadata)),
        )

    return {
        "saved": True,
        "validation": validation,
        "state": get_subscription_state(),
    }


def _upsert_subscription_servers(
    servers: list[Any],
) -> dict[str, Any]:
    """Store parsed subscription servers in SQLite inventory tables."""

    seen_ids = {server.server_id for server in servers}

    with db_session() as connection:
        for server in servers:
            connection.execute(
                """
                INSERT INTO servers (
                    server_id,
                    server_name,
                    provider_name,
                    country_code,
                    region,
                    raw_json,
                    inventory_state,
                    missing_since
                )
                VALUES (?, ?, ?, ?, ?, ?, 'active', NULL)
                ON CONFLICT(server_id) DO UPDATE SET
                    server_name = excluded.server_name,
                    provider_name = excluded.provider_name,
                    country_code = excluded.country_code,
                    region = excluded.region,
                    raw_json = excluded.raw_json,
                    inventory_state = 'active',
                    last_seen_at = CURRENT_TIMESTAMP,
                    missing_since = NULL,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    server.server_id,
                    server.server_name,
                    server.provider_name,
                    server.country_code,
                    server.region,
                    _json_dumps(server.raw),
                ),
            )

            connection.execute(
                "INSERT OR IGNORE INTO server_preferences (server_id) VALUES (?)",
                (server.server_id,),
            )
            connection.execute(
                "INSERT OR IGNORE INTO server_ping_state (server_id) VALUES (?)",
                (server.server_id,),
            )

        if seen_ids:
            placeholders = ", ".join("?" for _ in seen_ids)
            connection.execute(
                f"""
                UPDATE servers
                SET
                    inventory_state = 'missing',
                    missing_since = COALESCE(missing_since, CURRENT_TIMESTAMP),
                    updated_at = CURRENT_TIMESTAMP
                WHERE inventory_state = 'active'
                  AND server_id NOT IN ({placeholders})
                  AND server_id NOT IN (
                      SELECT server_id FROM server_custom_https_proxy
                  )
                """,
                tuple(sorted(seen_ids)),
            )
        else:
            connection.execute(
                """
                UPDATE servers
                SET
                    inventory_state = 'missing',
                    missing_since = COALESCE(missing_since, CURRENT_TIMESTAMP),
                    updated_at = CURRENT_TIMESTAMP
                WHERE inventory_state = 'active'
                  AND server_id NOT IN (
                      SELECT server_id FROM server_custom_https_proxy
                  )
                """
            )

        active_count = connection.execute(
            "SELECT COUNT(*) FROM servers WHERE inventory_state = 'active'"
        ).fetchone()[0]
        missing_count = connection.execute(
            "SELECT COUNT(*) FROM servers WHERE inventory_state = 'missing'"
        ).fetchone()[0]
        existing_vpn_auto_count = connection.execute(
            """
            SELECT COUNT(*)
            FROM server_preferences
            WHERE COALESCE(vpn_auto, 0) = 1
              AND COALESCE(manually_deleted_at, '') = ''
            """
        ).fetchone()[0]
        vpn_auto_seeded_count = 0
        if existing_vpn_auto_count == 0 and seen_ids:
            placeholders = ", ".join("?" for _ in seen_ids)
            vpn_auto_seeded_count = connection.execute(
                f"""
                UPDATE server_preferences
                SET
                    vpn_auto = 1,
                    updated_at = CURRENT_TIMESTAMP
                WHERE server_id IN ({placeholders})
                  AND COALESCE(manually_deleted_at, '') = ''
                """,
                tuple(sorted(seen_ids)),
            ).rowcount

    return {
        "seen_count": len(seen_ids),
        "active_count": active_count,
        "missing_count": missing_count,
        "vpn_auto_seeded_count": vpn_auto_seeded_count,
    }


def refresh_subscription_inventory(
    url: str | None = None,
) -> dict[str, Any]:
    """Download subscription and sync parsed servers into SQLite.

    This does not generate Mihomo config, does not restart Mihomo and does not
    apply dataplane changes.
    """

    from fwrouter_api.adapters.subscription import DEFAULT_SUBSCRIPTION_ADAPTER

    state = get_subscription_state()
    refresh_url = (url or state.get("url") or "").strip()

    validation = validate_subscription_url(refresh_url)
    if not validation["valid"]:
        with db_session() as connection:
            connection.execute(
                """
                INSERT INTO subscription_state (
                    id,
                    status,
                    error_code,
                    error_message,
                    metadata_json
                )
                VALUES (1, 'failed', ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    status = 'failed',
                    error_code = excluded.error_code,
                    error_message = excluded.error_message,
                    metadata_json = excluded.metadata_json,
                    last_refresh_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    validation["error"]["code"],
                    validation["error"]["message"],
                    _json_dumps({"stage": "validate"}),
                ),
            )

        return {
            "ok": False,
            "stage": "validate",
            "validation": validation,
            "state": get_subscription_state(),
            "inventory": None,
            "error": validation["error"],
            "diagnostics": {
                "kind": "saved_subscription_url_invalid",
                "url_saved": bool(refresh_url),
                "saved_url_invalid": True,
                "saved_url_reason": validation["error"]["code"],
            },
        }

    refresh_result = DEFAULT_SUBSCRIPTION_ADAPTER.refresh(validation["normalized_url"])

    if not refresh_result.ok:
        with db_session() as connection:
            connection.execute(
                """
                INSERT INTO subscription_state (
                    id,
                    url,
                    status,
                    error_code,
                    error_message,
                    metadata_json
                )
                VALUES (1, ?, 'failed', ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    url = excluded.url,
                    status = 'failed',
                    error_code = excluded.error_code,
                    error_message = excluded.error_message,
                    metadata_json = excluded.metadata_json,
                    last_refresh_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    validation["normalized_url"],
                    refresh_result.error_code,
                    refresh_result.error_message,
                    _json_dumps(refresh_result.metadata),
                ),
            )

        return {
            "ok": False,
            "stage": "download_parse",
            "validation": validation,
            "state": get_subscription_state(),
            "inventory": None,
            "error": {
                "code": refresh_result.error_code,
                "message": refresh_result.error_message,
            },
            "refresh": refresh_result.to_dict(),
            "diagnostics": {
                "kind": "subscription_provider_refresh_failed",
                "url_saved": True,
                "saved_url_invalid": False,
                "saved_url_reason": None,
            },
        }

    inventory = _upsert_subscription_servers(refresh_result.servers)

    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO subscription_state (
                id,
                url,
                status,
                error_code,
                error_message,
                metadata_json,
                last_refresh_at,
                last_success_at,
                server_inventory_updated_at
            )
            VALUES (
                1,
                ?,
                'success',
                NULL,
                NULL,
                ?,
                CURRENT_TIMESTAMP,
                CURRENT_TIMESTAMP,
                CURRENT_TIMESTAMP
            )
            ON CONFLICT(id) DO UPDATE SET
                url = excluded.url,
                status = 'success',
                error_code = NULL,
                error_message = NULL,
                metadata_json = excluded.metadata_json,
                last_refresh_at = CURRENT_TIMESTAMP,
                last_success_at = CURRENT_TIMESTAMP,
                server_inventory_updated_at = CURRENT_TIMESTAMP,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                validation["normalized_url"],
                _json_dumps(refresh_result.metadata),
            ),
        )

    return {
        "ok": True,
        "stage": "inventory_synced",
        "validation": validation,
        "state": get_subscription_state(),
        "inventory": inventory,
        "refresh": refresh_result.to_dict(),
    }
