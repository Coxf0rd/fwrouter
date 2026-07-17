from __future__ import annotations

import json
import re
import secrets
from typing import Any

from fwrouter_api.db.connection import db_session
from fwrouter_api.services.servers import get_server, list_servers


CUSTOM_HTTPS_PROXY_PROVIDER = "custom proxy"
CUSTOM_HTTPS_PROXY_KIND = "custom_https_proxy"
VIRTUAL_XRAY_VPN_AUTO_SERVER_ID = "virtual:xray:vpn-auto"
VIRTUAL_XRAY_VPN_AUTO_SERVER_NAME = "Автоматический выбор"
VIRTUAL_XRAY_VPN_AUTO_KIND = "xray_vpn_auto"


def _json_dumps(value: dict[str, Any] | None) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.strip().lower())
    slug = slug.strip("-")
    return slug or "custom-proxy"


def _sanitize_custom_https_proxy_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": CUSTOM_HTTPS_PROXY_KIND,
        "proxy_type": row.get("proxy_type") or "http",
        "host": row["host"],
        "port": row["port"],
        "username": row["username"],
        "password_configured": bool(row["password"]),
        "tls": bool(row["tls"]),
        "sni": row["sni"],
        "skip_cert_verify": bool(row["skip_cert_verify"]),
        "path": row["path"],
        "updated_at": row["updated_at"],
    }


def _reconcile_custom_proxy_runtime(*, enabled: bool) -> dict[str, Any] | None:
    if not enabled:
        return None

    from fwrouter_api.services.servers import _reconcile_mihomo_after_server_preferences

    return _reconcile_mihomo_after_server_preferences(enabled=True)


def _custom_proxy_result(
    *,
    requested_by: str,
    server_id: str | None = None,
    server: dict[str, Any] | None = None,
    deleted_server_id: str | None = None,
    mihomo_reconcile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if mihomo_reconcile is not None and not bool(mihomo_reconcile.get("ok")):
        return {
            "ok": False,
            "requested_by": requested_by,
            "deleted_server_id": deleted_server_id,
            "server": server if server is not None else get_server_api(str(server_id)),
            "mihomo_reconcile": mihomo_reconcile,
            "error_code": mihomo_reconcile.get("error_code") or "CUSTOM_PROXY_RECONCILE_FAILED",
            "error_message": mihomo_reconcile.get("error_message") or "Custom proxy runtime reconcile failed.",
        }

    return {
        "ok": True,
        "requested_by": requested_by,
        "deleted_server_id": deleted_server_id,
        "server": server if server is not None else get_server_api(str(server_id)),
        "mihomo_reconcile": mihomo_reconcile,
        "error_code": None,
        "error_message": None,
    }


def _build_runtime_proxy_entry(server_name: str, row: dict[str, Any]) -> dict[str, Any]:
    proxy_type = str(row.get("proxy_type") or "http").strip().lower()
    proxy: dict[str, Any] = {
        "name": server_name,
        "type": proxy_type,
        "server": row["host"],
        "port": int(row["port"]),
    }
    username = str(row.get("username") or "").strip()
    password = str(row.get("password") or "").strip()
    sni = str(row.get("sni") or "").strip()
    path = str(row.get("path") or "").strip()

    if username:
        proxy["username"] = username
    if password:
        proxy["password"] = password
    if proxy_type == "http":
        proxy["tls"] = bool(row["tls"])
        if sni:
            proxy["sni"] = sni
        if path:
            proxy["path"] = path
        if bool(row.get("skip_cert_verify")):
            proxy["skip-cert-verify"] = True

    return proxy


def _fetch_custom_https_proxy_rows() -> dict[str, dict[str, Any]]:
    with db_session() as connection:
        rows = connection.execute(
            """
            SELECT
                server_id,
                proxy_type,
                host,
                port,
                username,
                password,
                tls,
                sni,
                skip_cert_verify,
                path,
                updated_at
            FROM server_custom_https_proxy
            ORDER BY server_id
            """
        ).fetchall()

    return {str(row["server_id"]): dict(row) for row in rows}


def _server_name_exists(server_name: str, *, exclude_server_id: str | None = None) -> bool:
    with db_session() as connection:
        if exclude_server_id:
            row = connection.execute(
                """
                SELECT 1
                FROM servers
                WHERE server_name = ?
                  AND server_id <> ?
                """,
                (server_name, exclude_server_id),
            ).fetchone()
        else:
            row = connection.execute(
                """
                SELECT 1
                FROM servers
                WHERE server_name = ?
                """,
                (server_name,),
            ).fetchone()
    return row is not None


def _validate_custom_https_proxy_input(
    *,
    server_name: str,
    proxy_type: str,
    host: str,
    port: int,
    exclude_server_id: str | None = None,
) -> dict[str, Any]:
    normalized_name = server_name.strip()
    normalized_proxy_type = str(proxy_type or "http").strip().lower()
    normalized_host = host.strip()
    if not normalized_name:
        return {
            "ok": False,
            "error_code": "CUSTOM_SERVER_NAME_EMPTY",
            "error_message": "Custom HTTPS proxy server name is empty.",
        }
    if not normalized_host:
        return {
            "ok": False,
            "error_code": "CUSTOM_SERVER_HOST_EMPTY",
            "error_message": "Custom HTTPS proxy host is empty.",
        }
    if port < 1 or port > 65535:
        return {
            "ok": False,
            "error_code": "CUSTOM_SERVER_PORT_INVALID",
            "error_message": "Custom HTTPS proxy port must be between 1 and 65535.",
        }
    if normalized_proxy_type not in {"http", "socks5"}:
        return {
            "ok": False,
            "error_code": "CUSTOM_SERVER_PROXY_TYPE_INVALID",
            "error_message": "Custom proxy type must be 'http' or 'socks5'.",
        }
    if _server_name_exists(normalized_name, exclude_server_id=exclude_server_id):
        return {
            "ok": False,
            "error_code": "CUSTOM_SERVER_NAME_ALREADY_EXISTS",
            "error_message": f"Server name already exists: {normalized_name}",
        }
    return {
        "ok": True,
        "server_name": normalized_name,
        "proxy_type": normalized_proxy_type,
        "host": normalized_host,
        "port": port,
    }


def _generate_custom_server_id(server_name: str) -> str:
    return f"custom-https:{_slugify(server_name)}:{secrets.token_hex(4)}"


def is_virtual_xray_vpn_auto_server_id(server_id: str | None) -> bool:
    return str(server_id or "").strip() == VIRTUAL_XRAY_VPN_AUTO_SERVER_ID


def build_virtual_xray_vpn_auto_server() -> dict[str, Any]:
    return {
        "server_id": VIRTUAL_XRAY_VPN_AUTO_SERVER_ID,
        "server_name": VIRTUAL_XRAY_VPN_AUTO_SERVER_NAME,
        "kind": VIRTUAL_XRAY_VPN_AUTO_KIND,
        "provider_name": "fwrouter virtual",
        "country_code": None,
        "region": None,
        "inventory_state": "active",
        "raw": {
            "kind": VIRTUAL_XRAY_VPN_AUTO_KIND,
            "selector": "vpn-global",
            "resolution": "server_side_auto",
        },
        "first_seen_at": None,
        "last_seen_at": None,
        "missing_since": None,
        "updated_at": None,
        "preferences": {
            "vpn_auto": True,
            "global_list": True,
            "remembered_until": None,
            "manually_deleted_at": None,
        },
        "ping": {
            "status": "virtual",
            "last_ping_ms": None,
            "checked_at": None,
            "checked_by": None,
            "error_code": None,
            "error_message": None,
            "metadata": {
                "kind": VIRTUAL_XRAY_VPN_AUTO_KIND,
            },
        },
        "origin": {
            "kind": VIRTUAL_XRAY_VPN_AUTO_KIND,
            "label": "Xray vpn-auto",
            "managed_by": "fwrouter",
        },
    }


def enrich_server_with_custom_metadata(server: dict[str, Any]) -> dict[str, Any]:
    custom_rows = _fetch_custom_https_proxy_rows()
    row = custom_rows.get(str(server["server_id"]))
    if row is None:
        return server

    enriched = dict(server)
    enriched["kind"] = CUSTOM_HTTPS_PROXY_KIND
    enriched["origin"] = {
        "kind": CUSTOM_HTTPS_PROXY_KIND,
        "label": "Custom proxy",
        "managed_by": "fwrouter",
    }
    enriched["custom_proxy"] = _sanitize_custom_https_proxy_row(row)
    return enriched


def list_servers_api(
    *,
    inventory_state: str | None = None,
    vpn_auto: bool | None = None,
    global_list: bool | None = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    servers = [
        enrich_server_with_custom_metadata(server)
        for server in list_servers(
            inventory_state=inventory_state,
            vpn_auto=vpn_auto,
            global_list=global_list,
            limit=limit,
        )
    ]
    servers.sort(
        key=lambda item: (
            0 if str(item.get("kind") or "") == CUSTOM_HTTPS_PROXY_KIND else 1,
            1 if str(item.get("kind") or "") == VIRTUAL_XRAY_VPN_AUTO_KIND else 0,
            str(item.get("server_name") or "").lower(),
        )
    )
    if (
        (inventory_state is None or inventory_state == "active")
        and (vpn_auto is None or vpn_auto)
        and (global_list is None or global_list)
        and len(servers) < max(1, limit)
    ):
        servers.append(build_virtual_xray_vpn_auto_server())
    return servers


def get_server_api(server_id: str) -> dict[str, Any] | None:
    if is_virtual_xray_vpn_auto_server_id(server_id):
        return build_virtual_xray_vpn_auto_server()
    server = get_server(server_id)
    if server is None:
        return None
    return enrich_server_with_custom_metadata(server)


def resolve_runtime_proxy_rows(
    *,
    inventory_state: str | None = None,
    vpn_auto: bool | None = None,
    global_list: bool | None = None,
    limit: int = 1000,
) -> list[dict[str, Any]]:
    custom_rows = _fetch_custom_https_proxy_rows()
    resolved: list[dict[str, Any]] = []
    for server in list_servers(
        inventory_state=inventory_state,
        vpn_auto=vpn_auto,
        global_list=global_list,
        limit=limit,
    ):
        item = dict(server)
        custom_row = custom_rows.get(str(server["server_id"]))
        if custom_row is not None:
            item["raw"] = _build_runtime_proxy_entry(str(server["server_name"]), custom_row)
        resolved.append(item)
    return resolved


def resolve_mihomo_runtime_proxy_rows(
    *,
    inventory_state: str | None = "active",
    limit: int = 1000,
) -> list[dict[str, Any]]:
    """Return proxy rows that must exist in generated Mihomo runtime config."""

    custom_rows = _fetch_custom_https_proxy_rows()
    resolved: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for server in list_servers(
        inventory_state=inventory_state,
        limit=limit,
    ):
        preferences = server.get("preferences") if isinstance(server.get("preferences"), dict) else {}
        include = bool(preferences.get("global_list", True)) or bool(preferences.get("vpn_auto"))
        if not include:
            continue
        server_id = str(server.get("server_id") or "").strip()
        if not server_id or server_id in seen_ids:
            continue
        seen_ids.add(server_id)
        item = dict(server)
        custom_row = custom_rows.get(server_id)
        if custom_row is not None:
            item["raw"] = _build_runtime_proxy_entry(str(server["server_name"]), custom_row)
        resolved.append(item)
    return resolved


def create_custom_https_proxy_server(
    *,
    server_name: str,
    proxy_type: str = "http",
    host: str,
    port: int,
    username: str | None = None,
    password: str | None = None,
    tls: bool = True,
    sni: str | None = None,
    skip_cert_verify: bool = False,
    path: str | None = None,
    vpn_auto: bool = False,
    global_list: bool = True,
    requested_by: str = "api",
) -> dict[str, Any]:
    validation = _validate_custom_https_proxy_input(
        server_name=server_name,
        proxy_type=proxy_type,
        host=host,
        port=port,
    )
    if not validation["ok"]:
        return {
            "ok": False,
            "error_code": validation["error_code"],
            "error_message": validation["error_message"],
            "server": None,
        }

    server_id = _generate_custom_server_id(validation["server_name"])
    sanitized_raw = {
        "name": validation["server_name"],
        "type": validation["proxy_type"],
        "server": validation["host"],
        "port": validation["port"],
        "fwrouter_custom": {
            "kind": CUSTOM_HTTPS_PROXY_KIND,
            "password_configured": bool((password or "").strip()),
        },
    }
    if validation["proxy_type"] == "http":
        sanitized_raw["tls"] = bool(tls)
        if sni:
            sanitized_raw["sni"] = sni.strip()
        if path:
            sanitized_raw["path"] = path.strip()
        if bool(skip_cert_verify):
            sanitized_raw["skip-cert-verify"] = True
    if username:
        sanitized_raw["username"] = username.strip()

    with db_session() as connection:
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
            VALUES (?, ?, ?, NULL, NULL, ?, 'active', NULL)
            """,
            (
                server_id,
                validation["server_name"],
                CUSTOM_HTTPS_PROXY_PROVIDER,
                _json_dumps(sanitized_raw),
            ),
        )
        connection.execute(
            """
            INSERT INTO server_preferences (
                server_id,
                vpn_auto,
                global_list
            )
            VALUES (?, ?, ?)
            """,
            (
                server_id,
                1 if vpn_auto else 0,
                1 if global_list else 0,
            ),
        )
        connection.execute(
            """
            INSERT INTO server_ping_state (server_id, status)
            VALUES (?, 'unknown')
            """,
            (server_id,),
        )
        connection.execute(
            """
            INSERT INTO server_custom_https_proxy (
                server_id,
                proxy_type,
                host,
                port,
                username,
                password,
                tls,
                sni,
                skip_cert_verify,
                path
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                server_id,
                validation["proxy_type"],
                validation["host"],
                validation["port"],
                (username or "").strip() or None,
                (password or "").strip() or None,
                1 if tls else 0,
                (sni or "").strip() or None,
                1 if skip_cert_verify else 0,
                (path or "").strip() or None,
            ),
        )

    mihomo_reconcile = _reconcile_custom_proxy_runtime(
        enabled=bool(vpn_auto) or bool(global_list)
    )
    return _custom_proxy_result(
        requested_by=requested_by,
        server_id=server_id,
        mihomo_reconcile=mihomo_reconcile,
    )


def update_custom_https_proxy_server(
    server_id: str,
    *,
    server_name: str,
    proxy_type: str = "http",
    host: str,
    port: int,
    username: str | None = None,
    password: str | None = None,
    tls: bool = True,
    sni: str | None = None,
    skip_cert_verify: bool = False,
    path: str | None = None,
    vpn_auto: bool = False,
    global_list: bool = True,
    requested_by: str = "api",
) -> dict[str, Any]:
    with db_session() as connection:
        existing = connection.execute(
            """
            SELECT s.server_id
            FROM servers s
            INNER JOIN server_custom_https_proxy c ON c.server_id = s.server_id
            WHERE s.server_id = ?
            """,
            (server_id,),
        ).fetchone()
    if existing is None:
        return {
            "ok": False,
            "requested_by": requested_by,
            "server": None,
            "error_code": "CUSTOM_SERVER_NOT_FOUND",
            "error_message": f"Custom HTTPS proxy server not found: {server_id}",
        }

    validation = _validate_custom_https_proxy_input(
        server_name=server_name,
        proxy_type=proxy_type,
        host=host,
        port=port,
        exclude_server_id=server_id,
    )
    if not validation["ok"]:
        return {
            "ok": False,
            "requested_by": requested_by,
            "server": get_server_api(server_id),
            "error_code": validation["error_code"],
            "error_message": validation["error_message"],
        }

    sanitized_raw = {
        "name": validation["server_name"],
        "type": validation["proxy_type"],
        "server": validation["host"],
        "port": validation["port"],
        "fwrouter_custom": {
            "kind": CUSTOM_HTTPS_PROXY_KIND,
            "password_configured": bool((password or "").strip()),
        },
    }
    if validation["proxy_type"] == "http":
        sanitized_raw["tls"] = bool(tls)
        if sni:
            sanitized_raw["sni"] = sni.strip()
        if path:
            sanitized_raw["path"] = path.strip()
        if bool(skip_cert_verify):
            sanitized_raw["skip-cert-verify"] = True
    if username:
        sanitized_raw["username"] = username.strip()

    with db_session() as connection:
        current_preferences = connection.execute(
            """
            SELECT vpn_auto, global_list
            FROM server_preferences
            WHERE server_id = ?
            """,
            (server_id,),
        ).fetchone()
        connection.execute(
            """
            UPDATE servers
            SET
                server_name = ?,
                provider_name = ?,
                raw_json = ?,
                inventory_state = 'active',
                missing_since = NULL,
                updated_at = CURRENT_TIMESTAMP,
                last_seen_at = CURRENT_TIMESTAMP
            WHERE server_id = ?
            """,
            (
                validation["server_name"],
                CUSTOM_HTTPS_PROXY_PROVIDER,
                _json_dumps(sanitized_raw),
                server_id,
            ),
        )
        connection.execute(
            """
            UPDATE server_preferences
            SET
                vpn_auto = ?,
                global_list = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE server_id = ?
            """,
            (
                1 if vpn_auto else 0,
                1 if global_list else 0,
                server_id,
            ),
        )
        connection.execute(
            """
            UPDATE server_custom_https_proxy
            SET
                host = ?,
                proxy_type = ?,
                port = ?,
                username = ?,
                password = ?,
                tls = ?,
                sni = ?,
                skip_cert_verify = ?,
                path = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE server_id = ?
            """,
            (
                validation["host"],
                validation["proxy_type"],
                validation["port"],
                (username or "").strip() or None,
                (password or "").strip() or None,
                1 if tls else 0,
                (sni or "").strip() or None,
                1 if skip_cert_verify else 0,
                (path or "").strip() or None,
                server_id,
            ),
        )

    mihomo_reconcile = _reconcile_custom_proxy_runtime(
        enabled=bool(vpn_auto)
        or bool(global_list)
        or bool(current_preferences and current_preferences["vpn_auto"])
        or bool(current_preferences and current_preferences["global_list"])
    )
    return _custom_proxy_result(
        requested_by=requested_by,
        server_id=server_id,
        mihomo_reconcile=mihomo_reconcile,
    )


def delete_custom_https_proxy_server(
    server_id: str,
    *,
    requested_by: str = "api",
) -> dict[str, Any]:
    server = get_server_api(server_id)
    preferences = (server or {}).get("preferences") if isinstance(server, dict) else {}
    with db_session() as connection:
        existing = connection.execute(
            """
            SELECT 1
            FROM server_custom_https_proxy
            WHERE server_id = ?
            """,
            (server_id,),
        ).fetchone()
        if existing is None:
            return {
                "ok": False,
                "requested_by": requested_by,
                "server": server,
                "error_code": "CUSTOM_SERVER_NOT_FOUND",
                "error_message": f"Custom HTTPS proxy server not found: {server_id}",
            }
        connection.execute(
            "DELETE FROM servers WHERE server_id = ?",
            (server_id,),
        )

    mihomo_reconcile = _reconcile_custom_proxy_runtime(
        enabled=bool(preferences.get("vpn_auto")) or bool(preferences.get("global_list", True))
    )
    return _custom_proxy_result(
        requested_by=requested_by,
        server=server,
        deleted_server_id=server_id,
        mihomo_reconcile=mihomo_reconcile,
    )
