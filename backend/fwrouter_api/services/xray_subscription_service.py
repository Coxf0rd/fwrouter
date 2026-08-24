from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

from fwrouter_api.adapters.xray import XRAY_PUBLIC_HOST, XRAY_PUBLIC_PATH, XRAY_PUBLIC_PORT, XrayClient
from fwrouter_api.db.connection import db_session
from fwrouter_api.services.custom_servers import (
    VIRTUAL_CUSTOM_HTTPS_PROXY_SERVER_NAME,
    VIRTUAL_XRAY_VPN_AUTO_SERVER_ID,
    VIRTUAL_XRAY_VPN_AUTO_SERVER_NAME,
)
from fwrouter_api.services.subscription_profiles import list_desired_subscription_xray_clients, render_subscription_profile
from fwrouter_api.services.xray_client_state import _client_alias_map, _set_local_alias, _sync_xray_inventory, _xray_subject_for_client
from fwrouter_api.services.xray_common import (
    _materialize_xray_runtime_bindings,
    _strip_raw_payload,
    _xray_adapter,
    _xray_managed_runtime_blocked,
)
from fwrouter_api.services.xray_runtime_state import _is_xray_supported_server_config, _module_state
from fwrouter_api.services.xray_subscription import build_xray_vless_uri


def _full_xray_client_uri(client: XrayClient, *, display_name: str | None = None) -> str:
    label = display_name or client.alias or client.email or client.client_id
    return build_xray_vless_uri(
        client_uuid=client.client_uuid,
        label=label,
    )


def _vpn_auto_xray_client_email(server_id: str) -> str:
    digest = hashlib.sha1(server_id.encode("utf-8")).hexdigest()[:12]
    return f"vpn-auto-{digest}@fwrouter.local"


def _is_subscription_profile_email(email: str) -> bool:
    return str(email or "").startswith("sub-")


def _vpn_auto_servers_for_xray_subscription() -> list[dict[str, Any]]:
    with db_session() as connection:
        vpn_auto_rows = connection.execute(
            """
            SELECT s.server_id, s.server_name, s.raw_json, ps.status AS ping_status, ps.last_ping_ms
            FROM servers AS s
            JOIN server_preferences AS p ON p.server_id = s.server_id
            LEFT JOIN server_ping_state AS ps ON ps.server_id = s.server_id
            WHERE COALESCE(p.vpn_auto, 0) = 1
              AND s.inventory_state = 'active'
              AND COALESCE(p.manually_deleted_at, '') = ''
              AND s.server_id NOT IN (
                  SELECT server_id FROM server_custom_https_proxy
              )
            ORDER BY
              CASE WHEN ps.status = 'success' THEN 0 ELSE 1 END,
              ps.last_ping_ms,
              s.server_id
            """
        ).fetchall()
        proxy_rows = connection.execute(
            """
            SELECT s.server_id, s.server_name, s.raw_json, ps.status AS ping_status, ps.last_ping_ms
            FROM servers AS s
            JOIN server_preferences AS p ON p.server_id = s.server_id
            JOIN server_custom_https_proxy AS c ON c.server_id = s.server_id
            LEFT JOIN server_ping_state AS ps ON ps.server_id = s.server_id
            WHERE s.inventory_state = 'active'
              AND COALESCE(p.vpn_auto, 0) = 1
              AND COALESCE(p.manually_deleted_at, '') = ''
            ORDER BY s.server_name, s.server_id
            """
        ).fetchall()

    normal_servers: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for row in vpn_auto_rows:
        try:
            raw = json.loads(row["raw_json"] or "{}")
        except json.JSONDecodeError:
            continue
        supported, reason = _is_xray_supported_server_config(raw if isinstance(raw, dict) else None)
        if not supported:
            continue
        normal_servers.append(
            {
                "server_id": row["server_id"],
                "server_name": row["server_name"],
                "raw": raw,
                "ping_status": row["ping_status"],
                "last_ping_ms": row["last_ping_ms"],
                "support_reason": reason,
            }
        )
        seen_ids.add(str(row["server_id"]))

    proxy_server: dict[str, Any] | None = None
    for row in proxy_rows:
        server_id = str(row["server_id"])
        if server_id in seen_ids:
            continue
        proxy_server = {
            "server_id": server_id,
            "server_name": VIRTUAL_CUSTOM_HTTPS_PROXY_SERVER_NAME,
            "raw": {"kind": "custom_https_proxy"},
            "ping_status": row["ping_status"],
            "last_ping_ms": row["last_ping_ms"],
            "support_reason": "custom_https_proxy",
        }
        seen_ids.add(server_id)

    result: list[dict[str, Any]] = [
        {
            "server_id": VIRTUAL_XRAY_VPN_AUTO_SERVER_ID,
            "server_name": VIRTUAL_XRAY_VPN_AUTO_SERVER_NAME,
            "raw": {"kind": "xray_vpn_auto"},
            "ping_status": "virtual",
            "last_ping_ms": None,
            "support_reason": "virtual_xray_vpn_auto",
        }
    ]
    if proxy_server is not None:
        result.append(proxy_server)
    result.extend(normal_servers)
    return result


def _upsert_xray_subject_server_override(
    *,
    subject_id: str,
    selected_server_id: str,
    requested_by: str,
) -> None:
    selected_until = "2099-12-31 23:59:59"

    with db_session() as connection:
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(subject_server_overrides)").fetchall()
        }

        subject_exists = connection.execute(
            "SELECT 1 FROM subjects WHERE subject_id = ? AND is_deleted = 0 LIMIT 1",
            (subject_id,),
        ).fetchone()
        if subject_exists is None:
            return

        server_exists = connection.execute(
            "SELECT 1 FROM servers WHERE server_id = ? LIMIT 1",
            (selected_server_id,),
        ).fetchone()
        if server_exists is None:
            return

        existing = connection.execute(
            "SELECT 1 FROM subject_server_overrides WHERE subject_id = ? LIMIT 1",
            (subject_id,),
        ).fetchone()

        if existing is None:
            insert_values: dict[str, Any] = {}
            if "subject_id" in columns:
                insert_values["subject_id"] = subject_id
            if "selected_server_id" in columns:
                insert_values["selected_server_id"] = selected_server_id
            if "selected_until" in columns:
                insert_values["selected_until"] = selected_until
            if "requested_by" in columns:
                insert_values["requested_by"] = requested_by
            if "created_by" in columns:
                insert_values["created_by"] = requested_by
            if "updated_by" in columns:
                insert_values["updated_by"] = requested_by

            literal_columns: list[str] = []
            literal_values: list[str] = []
            if "created_at" in columns:
                literal_columns.append("created_at")
                literal_values.append("CURRENT_TIMESTAMP")
            if "updated_at" in columns:
                literal_columns.append("updated_at")
                literal_values.append("CURRENT_TIMESTAMP")

            names = list(insert_values.keys()) + literal_columns
            placeholders = ["?"] * len(insert_values) + literal_values

            connection.execute(
                f"""
                INSERT INTO subject_server_overrides ({", ".join(names)})
                VALUES ({", ".join(placeholders)})
                """,
                tuple(insert_values.values()),
            )
        else:
            assignments: list[str] = []
            params: list[Any] = []

            if "selected_server_id" in columns:
                assignments.append("selected_server_id = ?")
                params.append(selected_server_id)
            if "selected_until" in columns:
                assignments.append("selected_until = ?")
                params.append(selected_until)
            if "requested_by" in columns:
                assignments.append("requested_by = ?")
                params.append(requested_by)
            if "updated_by" in columns:
                assignments.append("updated_by = ?")
                params.append(requested_by)
            if "updated_at" in columns:
                assignments.append("updated_at = CURRENT_TIMESTAMP")

            params.append(subject_id)
            connection.execute(
                f"""
                UPDATE subject_server_overrides
                SET {", ".join(assignments)}
                WHERE subject_id = ?
                """,
                tuple(params),
            )


def reconcile_xray_vpn_auto_subscription(
    *,
    requested_by: str = "api",
) -> dict[str, Any]:
    module = _module_state("xray") or {}
    if str(module.get("desired_state") or "") != "enabled":
        return {
            "ok": True,
            "status": "skipped",
            "reason": "xray_module_disabled",
            "created_count": 0,
            "deleted_count": 0,
            "nodes_count": 0,
        }

    servers = _vpn_auto_servers_for_xray_subscription()
    desired_by_email: dict[str, dict[str, Any]] = {
        _vpn_auto_xray_client_email(str(server["server_id"])): server
        for server in servers
    }

    existing_clients = {
        str(client.email or ""): client
        for client in _xray_adapter().list_clients()
        if str(client.email or "")
    }

    created: list[dict[str, Any]] = []
    deleted: list[dict[str, Any]] = []

    for email, client in list(existing_clients.items()):
        if not email.startswith("vpn-auto-"):
            continue
        if email in desired_by_email:
            continue

        result = _xray_adapter().delete_client(client.client_id or client.client_uuid)
        if not result.ok:
            return {
                "ok": False,
                "status": "failed",
                "stage": "delete_stale_client",
                "error_code": result.error_code or "XRAY_VPN_AUTO_STALE_DELETE_FAILED",
                "error_message": result.message,
                "client_id": client.client_id,
                "email": email,
                "details": _strip_raw_payload(result.details),
            }

        deleted.append(
            {
                "client_id": client.client_id,
                "client_uuid": client.client_uuid,
                "email": email,
            }
        )
        existing_clients.pop(email, None)

    nodes: list[dict[str, Any]] = []

    for email, server in desired_by_email.items():
        server_id = str(server["server_id"])
        server_name = str(server["server_name"] or server_id)

        client = existing_clients.get(email)
        if client is None:
            result = _xray_adapter().create_client(alias=server_name, email=email)
            if not result.ok:
                return {
                    "ok": False,
                    "status": "failed",
                    "stage": "create_client",
                    "error_code": result.error_code or "XRAY_VPN_AUTO_CLIENT_CREATE_FAILED",
                    "error_message": result.message,
                    "server_id": server_id,
                    "email": email,
                    "details": _strip_raw_payload(result.details),
                }

            client_payload = dict((result.details or {}).get("client") or {})
            client = XrayClient(
                client_id=str(client_payload.get("client_id") or client_payload.get("client_uuid") or ""),
                client_uuid=str(client_payload.get("client_uuid") or client_payload.get("client_id") or ""),
                email=email,
                alias=server_name,
                enabled=True,
                raw=dict(client_payload.get("raw") or {}),
            )
            existing_clients[email] = client
            created.append(
                {
                    "client_id": client.client_id,
                    "client_uuid": client.client_uuid,
                    "email": email,
                    "server_id": server_id,
                    "server_name": server_name,
                }
            )

        nodes.append(
            {
                "server_id": server_id,
                "server_name": server_name,
                "email": email,
                "client": client,
            }
        )

    _sync_xray_inventory(requested_by)

    for node in nodes:
        client = node["client"]
        server_id = str(node["server_id"])
        server_name = str(node["server_name"])

        _set_local_alias(client.client_id or client.client_uuid, server_name)

        subject = _xray_subject_for_client(client.client_uuid) or _xray_subject_for_client(client.client_id)
        if subject is None:
            return {
                "ok": False,
                "status": "failed",
                "stage": "subject_lookup",
                "error_code": "XRAY_VPN_AUTO_SUBJECT_MISSING",
                "error_message": f"Xray subject was not created for client {client.client_uuid or client.client_id}.",
                "server_id": server_id,
                "email": node["email"],
            }

        _upsert_xray_subject_server_override(
            subject_id=str(subject["subject_id"]),
            selected_server_id=server_id,
            requested_by=requested_by,
        )

    profile_reconcile = reconcile_xray_subscription_profile_nodes(
        requested_by=requested_by,
        materialize=False,
    )
    if not profile_reconcile.get("ok"):
        return {
            "ok": False,
            "status": "failed",
            "stage": "subscription_profiles",
            "error_code": profile_reconcile.get("error_code") or "XRAY_SUBSCRIPTION_PROFILE_RECONCILE_FAILED",
            "error_message": profile_reconcile.get("error_message") or "Failed to reconcile subscription profile nodes.",
            "profile_reconcile": profile_reconcile,
            "created": created,
            "deleted": deleted,
        }

    from fwrouter_api.services.mihomo_config import reconcile_mihomo_runtime

    mihomo_reconcile = reconcile_mihomo_runtime()
    if not mihomo_reconcile.get("ok"):
        return {
            "ok": False,
            "status": "failed",
            "stage": "mihomo_handoff_prepare",
            "error_code": "XRAY_VPN_AUTO_MIHOMO_RECONCILE_FAILED",
            "error_message": "Failed to prepare Mihomo Xray handoff listeners.",
            "mihomo_reconcile": mihomo_reconcile,
            "created": created,
            "deleted": deleted,
        }

    materialize = _materialize_xray_runtime_bindings(
        requested_by=requested_by,
        prepare_mihomo_handoff=False,
    )
    if not materialize.get("ok"):
        return {
            "ok": False,
            "status": "failed",
            "stage": "materialize",
            "error_code": "XRAY_VPN_AUTO_MATERIALIZE_FAILED",
            "error_message": "Failed to materialize Xray vpn-auto bindings.",
            "mihomo_reconcile": mihomo_reconcile,
            "materialize": materialize,
            "created": created,
            "deleted": deleted,
        }

    return {
        "ok": True,
        "status": "success",
        "created_count": len(created),
        "deleted_count": len(deleted),
        "nodes_count": len(nodes),
        "created": created,
        "deleted": deleted,
        "profile_reconcile": profile_reconcile,
        "mihomo_reconcile": mihomo_reconcile,
        "nodes": [
            {
                "server_id": node["server_id"],
                "server_name": node["server_name"],
                "email": node["email"],
                "client_id": node["client"].client_id,
                "client_uuid": node["client"].client_uuid,
            }
            for node in nodes
        ],
        "materialize": materialize,
    }


def reconcile_xray_subscription_profile_nodes(
    *,
    requested_by: str = "api",
    materialize: bool = True,
    token_or_slug: str | None = None,
) -> dict[str, Any]:
    blocked = _xray_managed_runtime_blocked("xray_subscription_profile_reconcile")
    if blocked is not None:
        return {
            **blocked,
            "ok": True,
            "status": "skipped",
            "reason": "managed_runtime_required",
            "nodes_count": 0,
        }

    module = _module_state("xray") or {}
    if str(module.get("desired_state") or "") != "enabled":
        return {
            "ok": True,
            "status": "skipped",
            "reason": "xray_module_disabled",
            "nodes_count": 0,
        }

    desired_nodes = list_desired_subscription_xray_clients(token_or_slug)
    desired_by_email = {
        str(node["client_email"]): node
        for node in desired_nodes
        if str(node.get("client_email") or "").strip()
    }
    existing_clients = {
        str(client.email or ""): client
        for client in _xray_adapter().list_clients()
        if str(client.email or "")
    }

    created: list[dict[str, Any]] = []
    deleted: list[dict[str, Any]] = []
    recreated: list[dict[str, Any]] = []

    for email, client in list(existing_clients.items()):
        if not _is_subscription_profile_email(email):
            continue
        if email in desired_by_email:
            continue
        result = _xray_adapter().delete_client(client.client_id or client.client_uuid)
        if not result.ok:
            return {
                "ok": False,
                "status": "failed",
                "stage": "delete_stale_profile_client",
                "error_code": result.error_code or "XRAY_SUB_PROFILE_DELETE_FAILED",
                "error_message": result.message,
                "email": email,
                "details": _strip_raw_payload(result.details),
            }
        deleted.append(
            {
                "client_id": client.client_id,
                "client_uuid": client.client_uuid,
                "email": email,
            }
        )
        existing_clients.pop(email, None)

    for email, node in desired_by_email.items():
        existing = existing_clients.get(email)
        desired_uuid = str(node["client_uuid"])
        alias = str(node["xray_alias"])
        if existing is not None and str(existing.client_uuid) != desired_uuid:
            result = _xray_adapter().delete_client(existing.client_id or existing.client_uuid)
            if not result.ok:
                return {
                    "ok": False,
                    "status": "failed",
                    "stage": "replace_profile_client_delete",
                    "error_code": result.error_code or "XRAY_SUB_PROFILE_REPLACE_DELETE_FAILED",
                    "error_message": result.message,
                    "email": email,
                    "details": _strip_raw_payload(result.details),
                }
            recreated.append(
                {
                    "email": email,
                    "old_client_uuid": existing.client_uuid,
                    "new_client_uuid": desired_uuid,
                }
            )
            existing_clients.pop(email, None)
            existing = None

        if existing is None:
            result = _xray_adapter().create_client(
                alias=alias,
                email=email,
                client_uuid=desired_uuid,
            )
            if not result.ok:
                return {
                    "ok": False,
                    "status": "failed",
                    "stage": "create_profile_client",
                    "error_code": result.error_code or "XRAY_SUB_PROFILE_CREATE_FAILED",
                    "error_message": result.message,
                    "email": email,
                    "details": _strip_raw_payload(result.details),
                }
            client_payload = dict((result.details or {}).get("client") or {})
            existing = XrayClient(
                client_id=str(client_payload.get("client_id") or desired_uuid),
                client_uuid=str(client_payload.get("client_uuid") or desired_uuid),
                email=email,
                alias=alias,
                enabled=True,
                raw=dict(client_payload.get("raw") or {}),
            )
            existing_clients[email] = existing
            created.append(
                {
                    "client_id": existing.client_id,
                    "client_uuid": existing.client_uuid,
                    "email": email,
                    "server_id": node["server_id"],
                }
            )

    _sync_xray_inventory(requested_by)

    for node in desired_nodes:
        client_uuid = str(node["client_uuid"])
        subject = _xray_subject_for_client(client_uuid)
        if subject is None:
            return {
                "ok": False,
                "status": "failed",
                "stage": "profile_subject_lookup",
                "error_code": "XRAY_SUB_PROFILE_SUBJECT_MISSING",
                "error_message": f"Xray subject was not created for profile client {client_uuid}.",
                "client_uuid": client_uuid,
                "email": node["client_email"],
            }
        _set_local_alias(client_uuid, str(node["xray_alias"]))
        _upsert_xray_subject_server_override(
            subject_id=str(subject["subject_id"]),
            selected_server_id=str(node["server_id"]),
            requested_by=requested_by,
        )

    materialize_result: dict[str, Any] | None = None
    if materialize:
        materialize_result = _materialize_xray_runtime_bindings(requested_by=requested_by)
        if not materialize_result.get("ok"):
            return {
                "ok": False,
                "status": "failed",
                "stage": "materialize",
                "error_code": "XRAY_SUB_PROFILE_MATERIALIZE_FAILED",
                "error_message": "Failed to materialize Xray subscription profile bindings.",
                "materialize": materialize_result,
            }

    return {
        "ok": True,
        "status": "success",
        "nodes_count": len(desired_nodes),
        "created_count": len(created),
        "deleted_count": len(deleted),
        "recreated_count": len(recreated),
        "created": created,
        "deleted": deleted,
        "recreated": recreated,
        "nodes": [
            {
                "server_id": node["server_id"],
                "server_name": node["server_name"],
                "client_uuid": node["client_uuid"],
                "client_email": node["client_email"],
            }
            for node in desired_nodes
        ],
        "materialize": materialize_result,
    }


def export_subscription_profile_text(
    token_or_slug: str,
    *,
    user_agent: str | None,
    requested_format: str | None,
) -> dict[str, Any]:
    return render_subscription_profile(
        token_or_slug,
        user_agent=user_agent,
        requested_format=requested_format,
    )


def export_xray_vpn_auto_subscription_text(
    *,
    base64_encode: bool = True,
    requested_by: str = "api",
) -> dict[str, Any]:
    servers = _vpn_auto_servers_for_xray_subscription()
    if not servers:
        return {
            "ok": False,
            "content": "",
            "uris": [],
            "nodes_count": 0,
            "error_code": "XRAY_VPN_AUTO_EMPTY",
            "error_message": "No supported vpn-auto servers are available for Xray subscription.",
        }

    existing_clients = {
        str(client.email or ""): client
        for client in _xray_adapter().list_clients()
        if str(client.email or "")
    }

    nodes: list[dict[str, Any]] = []
    created_any = False

    for server in servers:
        server_id = str(server["server_id"])
        server_name = str(server["server_name"] or server_id)
        email = _vpn_auto_xray_client_email(server_id)

        client = existing_clients.get(email)
        if client is None:
            result = _xray_adapter().create_client(alias=server_name, email=email)
            if not result.ok:
                return {
                    "ok": False,
                    "content": "",
                    "uris": [],
                    "nodes_count": len(nodes),
                    "error_code": result.error_code or "XRAY_VPN_AUTO_CLIENT_CREATE_FAILED",
                    "error_message": result.message,
                    "details": _strip_raw_payload(result.details),
                }

            client_payload = dict((result.details or {}).get("client") or {})
            client = XrayClient(
                client_id=str(client_payload.get("client_id") or client_payload.get("client_uuid") or ""),
                client_uuid=str(client_payload.get("client_uuid") or client_payload.get("client_id") or ""),
                email=email,
                alias=server_name,
                enabled=True,
                raw=dict(client_payload.get("raw") or {}),
            )
            existing_clients[email] = client
            created_any = True

        nodes.append(
            {
                "server_id": server_id,
                "server_name": server_name,
                "client": client,
                "uri": _full_xray_client_uri(client, display_name=server_name),
            }
        )

    # Normal subscription refresh must be fast and must not restart Xray.
    # Heavy reconciliation is only needed when missing node-clients had to be created.
    if created_any:
        _sync_xray_inventory(requested_by)

        for node in nodes:
            client = node["client"]
            server_id = str(node["server_id"])
            server_name = str(node["server_name"])

            _set_local_alias(client.client_id, server_name)

            subject = _xray_subject_for_client(client.client_uuid) or _xray_subject_for_client(client.client_id)
            if subject is None:
                return {
                    "ok": False,
                    "content": "",
                    "uris": [],
                    "nodes_count": len(nodes),
                    "error_code": "XRAY_VPN_AUTO_SUBJECT_MISSING",
                    "error_message": f"Xray subject was not created for client {client.client_uuid}.",
                }

            _upsert_xray_subject_server_override(
                subject_id=str(subject["subject_id"]),
                selected_server_id=server_id,
                requested_by=requested_by,
            )

        materialize = _materialize_xray_runtime_bindings(requested_by=requested_by)
        if not materialize.get("ok"):
            return {
                "ok": False,
                "content": "",
                "uris": [node["uri"] for node in nodes],
                "nodes_count": len(nodes),
                "error_code": "XRAY_VPN_AUTO_MATERIALIZE_FAILED",
                "error_message": "Failed to materialize vpn-auto Xray subscription bindings.",
                "materialize": materialize,
            }
    else:
        materialize = {
            "ok": True,
            "status": "skipped",
            "reason": "subscription_read_only_refresh",
        }

    raw_content = chr(10).join(node["uri"] for node in nodes) + chr(10)
    content = (
        base64.b64encode(raw_content.encode("utf-8")).decode("ascii")
        if base64_encode
        else raw_content
    )

    return {
        "ok": True,
        "content": content,
        "uris": [node["uri"] for node in nodes],
        "base64": base64_encode,
        "nodes_count": len(nodes),
        "nodes": [
            {
                "server_id": node["server_id"],
                "server_name": node["server_name"],
                "client_id": node["client"].client_id,
                "client_uuid": node["client"].client_uuid,
                "email": node["client"].email,
            }
            for node in nodes
        ],
        "materialize": materialize,
    }


def export_xray_subscription_text(
    client_id: str,
    *,
    base64_encode: bool = True,
) -> dict[str, Any]:
    aliases = _client_alias_map()

    target: XrayClient | None = None
    for client in _xray_adapter().list_clients():
        if client.client_id == client_id or client.client_uuid == client_id:
            target = client
            break

    if target is None:
        return {
            "ok": False,
            "content": "",
            "uris": [],
            "error_code": "XRAY_CLIENT_NOT_FOUND",
            "error_message": f"Xray client not found: {client_id}",
        }

    uri = _full_xray_client_uri(
        target,
        display_name=aliases.get(target.client_id) or aliases.get(target.client_uuid),
    )
    raw_content = uri + "\n"
    content = (
        base64.b64encode(raw_content.encode("utf-8")).decode("ascii")
        if base64_encode
        else raw_content
    )

    return {
        "ok": True,
        "content": content,
        "uris": [uri],
        "base64": base64_encode,
        "nodes_count": 1,
        "client_id": target.client_id,
        "client_uuid": target.client_uuid,
    }


def export_xray_subscription(client_id: str) -> dict[str, Any]:
    result = _xray_adapter().export_vless_subscription(client_id)
    details = dict(result.details)
    subject = _xray_subject_for_client(client_id)
    effective_state = subject.get("effective_state") if isinstance(subject, dict) and isinstance(subject.get("effective_state"), dict) else {}
    scoped_runtime = effective_state.get("scoped_runtime") if isinstance(effective_state.get("scoped_runtime"), dict) else None
    return {
        "ok": result.ok,
        "client_id": client_id,
        "public_host": XRAY_PUBLIC_HOST,
        "public_port": XRAY_PUBLIC_PORT,
        "public_path": XRAY_PUBLIC_PATH,
        "transport": "ws",
        "security": "tls",
        "subscription_uri": details.get("subscription_uri"),
        "subject_id": subject.get("subject_id") if isinstance(subject, dict) else None,
        "server_binding": {
            "selected_server_id": effective_state.get("selected_server_id"),
            "selected_server_source": effective_state.get("selected_server_source"),
            "effective_mode": effective_state.get("effective_mode"),
            "dataplane_path": effective_state.get("dataplane_path"),
            "scoped_runtime": scoped_runtime,
            "binding_saved": bool(effective_state.get("selected_server_id")),
            "binding_applied": bool(scoped_runtime and scoped_runtime.get("status") == "applied"),
            "binding_verified": bool(scoped_runtime and scoped_runtime.get("status") == "applied"),
        },
        "result": {
            "message": result.message,
            "error_code": result.error_code,
            "details": details,
        },
    }
