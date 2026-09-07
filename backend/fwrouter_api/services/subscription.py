from __future__ import annotations

import json
from datetime import datetime, timezone
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


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _server_to_metadata(server: Any) -> dict[str, Any]:
    return {
        "server_id": server.server_id,
        "server_name": server.server_name,
        "provider_name": server.provider_name,
        "country_code": server.country_code,
        "region": server.region,
        "raw": server.raw,
    }


def _server_from_metadata(payload: dict[str, Any]) -> Any | None:
    if not isinstance(payload, dict):
        return None

    server_id = str(payload.get("server_id") or "").strip()
    server_name = str(payload.get("server_name") or server_id).strip()
    if not server_id or not server_name:
        return None

    from fwrouter_api.adapters.subscription import SubscriptionServer

    raw = payload.get("raw")
    return SubscriptionServer(
        server_id=server_id,
        server_name=server_name,
        provider_name=payload.get("provider_name"),
        country_code=payload.get("country_code"),
        region=payload.get("region"),
        raw=raw if isinstance(raw, dict) else {},
    )


def _subscription_sources(metadata: dict[str, Any] | None) -> list[dict[str, Any]]:
    subscription = metadata.get("subscriptions") if isinstance(metadata, dict) else None
    items = subscription.get("items") if isinstance(subscription, dict) else None
    if not isinstance(items, list):
        return []

    sources: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        sources.append({**item, "url": url, "enabled": bool(item.get("enabled", True))})
    return sources


def _saved_subscription_urls(state: dict[str, Any] | None = None) -> list[str]:
    state = state or get_subscription_state()
    urls: list[Any] = [
        source.get("url")
        for source in _subscription_sources(state.get("metadata") if isinstance(state, dict) else None)
        if bool(source.get("enabled", True))
    ]
    if isinstance(state, dict) and state.get("url"):
        urls.append(state.get("url"))
    return normalize_subscription_urls(urls)["urls"]


def _merge_source_metadata(
    *,
    base_metadata: dict[str, Any] | None,
    urls: list[str],
    items: list[dict[str, Any]],
    servers_by_url: dict[str, list[Any]],
    now: str,
    batch: dict[str, Any],
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        key: value
        for key, value in (base_metadata or {}).items()
        if key != "subscriptions"
    }

    existing_by_url = {
        source["url"]: source
        for source in _subscription_sources(base_metadata)
    }
    item_by_url = {
        str(item.get("url") or "").strip(): item
        for item in items
        if str(item.get("url") or "").strip()
    }

    source_items: list[dict[str, Any]] = []
    for url in urls:
        previous = existing_by_url.get(url) or {}
        item = item_by_url.get(url) or {}
        ok = bool(item.get("ok"))
        refresh = item.get("refresh") if isinstance(item.get("refresh"), dict) else {}
        previous_servers = previous.get("servers") if isinstance(previous.get("servers"), list) else []
        servers = (
            [
                _server_to_metadata(server)
                for server in servers_by_url.get(url, [])
            ]
            if ok
            else previous_servers
        )

        error = item.get("error") if isinstance(item.get("error"), dict) else None
        source_items.append(
            {
                "url": url,
                "enabled": True,
                "status": "success" if ok else (previous.get("status") or ("failed" if item else "idle")),
                "last_refresh_at": now if item else previous.get("last_refresh_at"),
                "last_success_at": now if ok else previous.get("last_success_at"),
                "last_error_code": None if ok else ((error or {}).get("code") or previous.get("last_error_code")),
                "last_error_message": None if ok else ((error or {}).get("message") or previous.get("last_error_message")),
                "servers_count": len(servers),
                "servers": servers,
                "metadata": refresh.get("metadata") if ok and isinstance(refresh, dict) else previous.get("metadata"),
                "used_last_good": bool((not ok) and previous_servers),
                "last_refresh_servers_count": int(item.get("servers_count") or 0),
            }
        )

    metadata["batch"] = batch
    metadata["subscriptions"] = {
        "version": 1,
        "updated_at": now,
        "items": source_items,
    }
    return metadata


def _last_good_union_from_sources(sources: list[dict[str, Any]]) -> list[Any]:
    merged: dict[str, Any] = {}
    for source in sources:
        if not bool(source.get("enabled", True)):
            continue
        for server_payload in source.get("servers") or []:
            server = _server_from_metadata(server_payload)
            if server is not None:
                merged.setdefault(server.server_id, server)
    return list(merged.values())


def compact_subscription_metadata(
    metadata: dict[str, Any] | None,
    *,
    redact_urls: bool = False,
) -> dict[str, Any] | None:
    """Return subscription metadata without embedded last-good server snapshots."""

    if not isinstance(metadata, dict):
        return metadata

    public = {
        key: value
        for key, value in metadata.items()
        if key != "subscriptions"
    }
    subscription = metadata.get("subscriptions")
    items = subscription.get("items") if isinstance(subscription, dict) else None
    if isinstance(subscription, dict) and isinstance(items, list):
        public_items: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            item_public = {
                key: value
                for key, value in item.items()
                if key not in {"servers"}
            }
            if redact_urls:
                item_public["url_saved"] = bool(item_public.get("url"))
                item_public.pop("url", None)
            item_metadata = item_public.get("metadata")
            if isinstance(item_metadata, dict):
                item_metadata_public = dict(item_metadata)
                item_metadata_public.pop("url", None)
                item_public["metadata"] = item_metadata_public
            public_items.append(item_public)
        public["subscriptions"] = {
            key: value
            for key, value in subscription.items()
            if key != "items"
        }
        public["subscriptions"]["items"] = public_items
    return public


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


def normalize_subscription_urls(urls: list[Any] | None) -> dict[str, Any]:
    """Normalize a user-submitted batch without network access."""

    normalized: list[str] = []
    seen: set[str] = set()
    empty_count = 0
    duplicate_count = 0

    for item in urls or []:
        value = str(item or "").strip()
        if not value:
            empty_count += 1
            continue
        if value in seen:
            duplicate_count += 1
            continue
        seen.add(value)
        normalized.append(value)

    return {
        "urls": normalized,
        "empty_count": empty_count,
        "duplicate_count": duplicate_count,
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
    existing_state = get_subscription_state()
    existing_metadata = existing_state.get("metadata") if isinstance(existing_state, dict) else None
    urls = normalize_subscription_urls([*_saved_subscription_urls(existing_state), normalized_url])["urls"]
    now = _utc_timestamp()
    next_metadata = _merge_source_metadata(
        base_metadata=existing_metadata if isinstance(existing_metadata, dict) else metadata,
        urls=urls,
        items=[],
        servers_by_url={},
        now=now,
        batch={
            "submitted_count": len(urls),
            "duplicate_urls": 0,
            "empty_urls": 0,
            "errors": 0,
        },
    )
    if metadata:
        next_metadata.update({key: value for key, value in metadata.items() if key != "subscriptions"})

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
            (normalized_url, _json_dumps(next_metadata)),
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


def _existing_server_ids(server_ids: set[str]) -> set[str]:
    if not server_ids:
        return set()

    placeholders = ", ".join("?" for _ in server_ids)
    with db_session() as connection:
        rows = connection.execute(
            f"SELECT server_id FROM servers WHERE server_id IN ({placeholders})",
            tuple(sorted(server_ids)),
        ).fetchall()

    return {str(row["server_id"]) for row in rows}


def refresh_subscription_inventory_batch(
    urls: list[Any],
    *,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Download several subscriptions and sync their union into SQLite once.

    This does not generate Mihomo config, does not restart Mihomo and does not
    apply dataplane changes.
    """

    from fwrouter_api.adapters.subscription import DEFAULT_SUBSCRIPTION_ADAPTER

    state_before = get_subscription_state()
    existing_metadata = state_before.get("metadata") if isinstance(state_before, dict) else None
    normalized = normalize_subscription_urls(urls)
    submitted_urls: list[str] = normalized["urls"]
    if not submitted_urls:
        return {
            "ok": False,
            "stage": "validate",
            "validation": {
                "valid": False,
                "normalized_url": "",
                "error": {
                    "code": "SUBSCRIPTION_URL_EMPTY",
                    "message": "Subscription URL is empty.",
                },
            },
            "state": get_subscription_state(),
            "inventory": None,
            "batch": {
                "submitted_count": 0,
                "requested_count": len(urls or []),
                "added_subscriptions": 0,
                "imported_servers": 0,
                "already_existing": normalized["duplicate_count"],
                "duplicate_urls": normalized["duplicate_count"],
                "empty_urls": normalized["empty_count"],
                "errors": 1,
                "items": [],
            },
            "error": {
                "code": "SUBSCRIPTION_URL_EMPTY",
                "message": "Subscription URL is empty.",
            },
        }

    batch_urls = normalize_subscription_urls([*_saved_subscription_urls(state_before), *submitted_urls])["urls"]
    items: list[dict[str, Any]] = []
    servers_by_url: dict[str, list[Any]] = {}
    merged_servers_by_id: dict[str, Any] = {}
    last_successful_url: str | None = None
    errors = 0

    for refresh_url in batch_urls:
        validation = validate_subscription_url(refresh_url)
        if not validation["valid"]:
            errors += 1
            items.append(
                {
                    "url": refresh_url,
                    "ok": False,
                    "stage": "validate",
                    "servers_count": 0,
                    "error": validation["error"],
                }
            )
            continue

        refresh_result = DEFAULT_SUBSCRIPTION_ADAPTER.refresh(validation["normalized_url"])
        if not refresh_result.ok:
            errors += 1
            items.append(
                {
                    "url": validation["normalized_url"],
                    "ok": False,
                    "stage": "download_parse",
                    "servers_count": 0,
                    "error": {
                        "code": refresh_result.error_code,
                        "message": refresh_result.error_message,
                    },
                    "refresh": refresh_result.to_dict(),
                }
            )
            continue

        servers_by_url[validation["normalized_url"]] = list(refresh_result.servers)
        for server in refresh_result.servers:
            merged_servers_by_id.setdefault(server.server_id, server)
        last_successful_url = validation["normalized_url"]
        items.append(
            {
                "url": validation["normalized_url"],
                "ok": True,
                "stage": "download_parse",
                "servers_count": len(refresh_result.servers),
                "error": None,
                "refresh": refresh_result.to_dict(),
            }
        )

    now = _utc_timestamp()
    batch_summary = {
        "submitted_count": len(batch_urls),
        "requested_count": len(urls or []),
        "new_urls_count": len(submitted_urls),
        "duplicate_urls": normalized["duplicate_count"],
        "empty_urls": normalized["empty_count"],
        "errors": errors,
    }
    next_metadata = _merge_source_metadata(
        base_metadata=existing_metadata if isinstance(existing_metadata, dict) else metadata,
        urls=batch_urls,
        items=items,
        servers_by_url=servers_by_url,
        now=now,
        batch=batch_summary,
    )
    if metadata:
        next_metadata.update({key: value for key, value in metadata.items() if key != "subscriptions"})
        next_metadata["batch"] = batch_summary

    merged_servers = list(merged_servers_by_id.values())
    if errors:
        for server in _last_good_union_from_sources(_subscription_sources(next_metadata)):
            merged_servers_by_id.setdefault(server.server_id, server)
        merged_servers = list(merged_servers_by_id.values())

    existing_server_ids = _existing_server_ids(set(merged_servers_by_id.keys()))
    inventory = _upsert_subscription_servers(merged_servers) if merged_servers else None
    imported_servers = max(0, len(merged_servers_by_id) - len(existing_server_ids))
    added_subscriptions = sum(1 for item in items if item.get("ok"))
    already_existing = (
        normalized["duplicate_count"]
        + len(existing_server_ids)
        + sum(max(0, int(item.get("servers_count") or 0)) for item in items if item.get("ok"))
        - len(merged_servers_by_id)
    )

    if last_successful_url:
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
                (last_successful_url, _json_dumps(next_metadata)),
            )
    else:
        first_error = next((item.get("error") for item in items if item.get("error")), None)
        if inventory is None:
            last_good_servers = _last_good_union_from_sources(_subscription_sources(next_metadata))
            inventory = _upsert_subscription_servers(last_good_servers) if last_good_servers else None
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
                    (first_error or {}).get("code") or "SUBSCRIPTION_BATCH_FAILED",
                    (first_error or {}).get("message") or "Subscription batch failed.",
                    _json_dumps(next_metadata),
                ),
            )

    return {
        "ok": bool(last_successful_url),
        "stage": "inventory_synced" if last_successful_url else "download_parse",
        "validation": None,
        "state": get_subscription_state(),
        "inventory": inventory,
        "batch": {
            "submitted_count": len(batch_urls),
            "requested_count": len(urls or []),
            "added_subscriptions": added_subscriptions,
            "imported_servers": imported_servers,
            "already_existing": already_existing,
            "duplicate_urls": normalized["duplicate_count"],
            "empty_urls": normalized["empty_count"],
            "errors": errors,
            "items": items,
        },
        "error": None if last_successful_url else {
            "code": "SUBSCRIPTION_BATCH_FAILED",
            "message": "All subscription URLs failed.",
        },
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
    requested_urls = normalize_subscription_urls([*_saved_subscription_urls(state), url] if url else _saved_subscription_urls(state))["urls"]
    if len(requested_urls) > 1:
        return refresh_subscription_inventory_batch([url] if url else requested_urls)

    refresh_url = (url or state.get("url") or "").strip()
    existing_metadata = state.get("metadata") if isinstance(state.get("metadata"), dict) else None

    validation = validate_subscription_url(refresh_url)
    if not validation["valid"]:
        next_metadata = {
            **(existing_metadata or {}),
            "stage": "validate",
        }
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
                    _json_dumps(next_metadata),
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
    now = _utc_timestamp()

    if not refresh_result.ok:
        item = {
            "url": validation["normalized_url"],
            "ok": False,
            "stage": "download_parse",
            "servers_count": 0,
            "error": {
                "code": refresh_result.error_code,
                "message": refresh_result.error_message,
            },
            "refresh": refresh_result.to_dict(),
        }
        next_metadata = _merge_source_metadata(
            base_metadata=existing_metadata,
            urls=[validation["normalized_url"]],
            items=[item],
            servers_by_url={},
            now=now,
            batch={
                "submitted_count": 1,
                "requested_count": 1,
                "new_urls_count": 0,
                "duplicate_urls": 0,
                "empty_urls": 0,
                "errors": 1,
            },
        )
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
                    _json_dumps(next_metadata),
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
    item = {
        "url": validation["normalized_url"],
        "ok": True,
        "stage": "download_parse",
        "servers_count": len(refresh_result.servers),
        "error": None,
        "refresh": refresh_result.to_dict(),
    }
    next_metadata = _merge_source_metadata(
        base_metadata=existing_metadata,
        urls=[validation["normalized_url"]],
        items=[item],
        servers_by_url={validation["normalized_url"]: list(refresh_result.servers)},
        now=now,
        batch={
            "submitted_count": 1,
            "requested_count": 1,
            "new_urls_count": 0,
            "duplicate_urls": 0,
            "empty_urls": 0,
            "errors": 0,
        },
    )

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
                _json_dumps(next_metadata),
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
