from __future__ import annotations

import base64
import hashlib
import json
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit
from uuid import NAMESPACE_DNS, uuid5

from fwrouter_api.core.config import get_settings
from fwrouter_api.db.connection import db_session
from fwrouter_api.services.auto_eligibility import auto_eligible_sql
from fwrouter_api.services.custom_servers import (
    VIRTUAL_CUSTOM_HTTPS_PROXY_SERVER_NAME,
    VIRTUAL_XRAY_VPN_AUTO_SERVER_ID,
    VIRTUAL_XRAY_VPN_AUTO_SERVER_NAME,
)
from fwrouter_api.services.xray_subscription import build_xray_vless_uri



CLASH_UA_MARKERS = (
    "clash",
    "mihomo",
    "stash",
    "shadowrocket",
    "clashx",
    "clash-verge",
    "clashmeta",
    "flclash",
)
HAPP_UA_MARKERS = ("happ/",)
CLASH_FORMATS = {"clash", "flclashx", "flclash", "mihomo"}
RAW_VLESS_FORMATS = {"raw-vless", "vless", "raw", "xray"}
BASE64_VLESS_FORMATS = {"base64-vless", "base64", "v2ray"}
HAPP_FORMATS = {"happ"}


def _yaml_quote(value: Any) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def _without_query_param(uri: str, param_name: str) -> str:
    parsed = urlsplit(str(uri))
    filtered = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key != param_name
    ]
    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urlencode(filtered),
            parsed.fragment,
        )
    )


def _without_query_params(uri: str, param_names: set[str]) -> str:
    parsed = urlsplit(str(uri))
    filtered = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key not in param_names
    ]
    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urlencode(filtered),
            parsed.fragment,
        )
    )


def _stable_digest(value: str, *, length: int = 12) -> str:
    return hashlib.sha1(str(value or "").encode("utf-8")).hexdigest()[:length]


def _subscription_email(token: str, server_id: str) -> str:
    return f"sub-{_stable_digest(token, length=10)}-{_stable_digest(server_id, length=12)}@fwrouter.local"


def _subscription_uuid(token: str, server_id: str) -> str:
    return str(uuid5(NAMESPACE_DNS, f"fwrouter-subscription:{token}:{server_id}"))


def _normalize_format(value: str | None) -> str:
    normalized = str(value or "auto").strip().lower()
    if normalized in CLASH_FORMATS:
        return "clash"
    if normalized in RAW_VLESS_FORMATS:
        return "raw-vless"
    if normalized in BASE64_VLESS_FORMATS:
        return "base64-vless"
    if normalized in HAPP_FORMATS:
        return "happ"
    return "auto"


def _detect_format(*, requested_format: str | None, app_type: str | None, user_agent: str | None) -> str:
    explicit = _normalize_format(requested_format)
    if explicit != "auto":
        return explicit

    saved = _normalize_format(app_type)
    if saved != "auto":
        return saved

    normalized_ua = str(user_agent or "").strip().lower()
    if any(marker in normalized_ua for marker in HAPP_UA_MARKERS):
        return "happ"
    if any(marker in normalized_ua for marker in CLASH_UA_MARKERS):
        return "clash"
    return "raw-vless"


def _title_from_slug(slug: str) -> str:
    text = str(slug or "").strip().replace("-", " ").replace("_", " ")
    return text.title() if text else "FWRouter"


def ensure_subscription_identity(
    token_or_slug: str,
    *,
    display_name: str | None = None,
    app_type: str = "auto",
) -> dict[str, Any]:
    slug = str(token_or_slug or "").strip().lower()
    profile_name = str(display_name or "").strip() or _title_from_slug(slug)
    normalized_app_type = _normalize_format(app_type)
    with db_session() as connection:
        connection.execute(
            """
            INSERT INTO subscription_accounts (
                slug,
                display_name,
                enabled,
                updated_at
            )
            VALUES (?, ?, 1, CURRENT_TIMESTAMP)
            ON CONFLICT(slug) DO UPDATE SET
                enabled = 1,
                display_name = excluded.display_name,
                updated_at = CURRENT_TIMESTAMP
            """,
            (slug, profile_name),
        )
        account = connection.execute(
            """
            SELECT account_id, slug, display_name, enabled
            FROM subscription_accounts
            WHERE slug = ?
            LIMIT 1
            """,
            (slug,),
        ).fetchone()
        if account is None:
            raise RuntimeError(f"Failed to create subscription account: {slug}")

        connection.execute(
            """
            INSERT INTO subscription_clients (
                account_id,
                token,
                app_type,
                enabled,
                display_name,
                updated_at
            )
            VALUES (?, ?, ?, 1, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(token) DO UPDATE SET
                enabled = 1,
                app_type = excluded.app_type,
                display_name = excluded.display_name,
                updated_at = CURRENT_TIMESTAMP
            """,
            (account["account_id"], slug, normalized_app_type, profile_name),
        )

    return resolve_subscription_client(slug, None, "auto", auto_create_legacy=False)


def _ensure_legacy_subscription_identity(token_or_slug: str) -> dict[str, Any]:
    return ensure_subscription_identity(token_or_slug)


def disable_subscription_identity(token_or_slug: str) -> dict[str, Any]:
    normalized = str(token_or_slug or "").strip()
    if not normalized:
        return {
            "ok": False,
            "error_code": "SUBSCRIPTION_TOKEN_REQUIRED",
            "error_message": "Subscription token is required.",
        }

    with db_session() as connection:
        row = connection.execute(
            """
            SELECT account_id, slug, display_name, enabled
            FROM subscription_accounts
            WHERE slug = ?
            LIMIT 1
            """,
            (normalized.lower(),),
        ).fetchone()
        if row is None:
            return {
                "ok": False,
                "error_code": "SUBSCRIPTION_CLIENT_NOT_FOUND",
                "error_message": f"Subscription token is not registered: {normalized}",
            }
        was_enabled = bool(row["enabled"])
        client_rows = connection.execute(
            """
            SELECT client_id, enabled
            FROM subscription_clients
            WHERE account_id = ?
            """,
            (row["account_id"],),
        ).fetchall()
        enabled_clients_count = sum(1 for client_row in client_rows if bool(client_row["enabled"]))

        connection.execute(
            """
            UPDATE subscription_accounts
            SET enabled = 0, updated_at = CURRENT_TIMESTAMP
            WHERE account_id = ?
            """,
            (row["account_id"],),
        )
        connection.execute(
            """
            UPDATE subscription_clients
            SET enabled = 0, updated_at = CURRENT_TIMESTAMP
            WHERE account_id = ?
            """,
            (row["account_id"],),
        )

    return {
        "ok": True,
        "account": {
            "account_id": row["account_id"],
            "slug": row["slug"],
            "display_name": row["display_name"] or row["slug"],
            "enabled": False,
            "was_enabled": was_enabled,
            "enabled_clients_count": enabled_clients_count,
        },
    }


def resolve_subscription_client(
    token_or_slug: str,
    user_agent: str | None,
    requested_format: str | None,
    *,
    auto_create_legacy: bool = False,
) -> dict[str, Any]:
    normalized = str(token_or_slug or "").strip()
    if not normalized:
        return {
            "ok": False,
            "error_code": "SUBSCRIPTION_TOKEN_REQUIRED",
            "error_message": "Subscription token is required.",
        }

    with db_session() as connection:
        row = connection.execute(
            """
            SELECT
                sc.client_id,
                sc.account_id,
                sc.token,
                sc.app_type,
                sc.enabled AS client_enabled,
                sc.display_name AS client_display_name,
                sc.last_seen_at,
                sc.last_user_agent,
                sa.slug,
                sa.display_name AS account_display_name,
                sa.enabled AS account_enabled
            FROM subscription_clients AS sc
            JOIN subscription_accounts AS sa ON sa.account_id = sc.account_id
            WHERE sc.token = ?
            LIMIT 1
            """,
            (normalized,),
        ).fetchone()

        if row is None:
            row = connection.execute(
                """
                SELECT
                    sc.client_id,
                    sc.account_id,
                    sc.token,
                    sc.app_type,
                    sc.enabled AS client_enabled,
                    sc.display_name AS client_display_name,
                    sc.last_seen_at,
                    sc.last_user_agent,
                    sa.slug,
                    sa.display_name AS account_display_name,
                    sa.enabled AS account_enabled
                FROM subscription_accounts AS sa
                JOIN subscription_clients AS sc ON sc.account_id = sa.account_id
                WHERE sa.slug = ?
                  AND sc.enabled = 1
                ORDER BY sc.client_id
                LIMIT 1
                """,
                (normalized.lower(),),
            ).fetchone()

        if row is None:
            if auto_create_legacy:
                return _ensure_legacy_subscription_identity(normalized)
            return {
                "ok": False,
                "error_code": "SUBSCRIPTION_CLIENT_NOT_FOUND",
                "error_message": f"Subscription token is not registered: {normalized}",
            }

        if not bool(row["account_enabled"]) or not bool(row["client_enabled"]):
            return {
                "ok": False,
                "error_code": "SUBSCRIPTION_CLIENT_DISABLED",
                "error_message": f"Subscription token is disabled: {normalized}",
            }

    detected_format = _detect_format(
        requested_format=requested_format,
        app_type=row["app_type"],
        user_agent=user_agent,
    )
    return {
        "ok": True,
        "account": {
            "account_id": row["account_id"],
            "slug": row["slug"],
            "display_name": row["account_display_name"] or row["slug"],
            "enabled": bool(row["account_enabled"]),
        },
        "client": {
            "client_id": row["client_id"],
            "token": row["token"],
            "app_type": row["app_type"],
            "display_name": row["client_display_name"] or row["token"],
            "enabled": bool(row["client_enabled"]),
        },
        "detected_format": detected_format,
        "requested_format": _normalize_format(requested_format),
        "user_agent": str(user_agent or ""),
    }


def _subscription_servers() -> list[dict[str, Any]]:
    with db_session() as connection:
        vpn_auto_rows = connection.execute(
            f"""
            SELECT s.server_id, s.server_name
            FROM servers AS s
            JOIN server_preferences AS p ON p.server_id = s.server_id
            WHERE {auto_eligible_sql(server_alias="s", preferences_alias="p")}
              AND s.server_id NOT IN (
                  SELECT server_id FROM server_custom_https_proxy
              )
            ORDER BY s.server_name, s.server_id
            """
        ).fetchall()
        proxy_rows = connection.execute(
            f"""
            SELECT s.server_id, s.server_name
            FROM servers AS s
            JOIN server_preferences AS p ON p.server_id = s.server_id
            JOIN server_custom_https_proxy AS c ON c.server_id = s.server_id
            WHERE s.inventory_state = 'active'
              AND COALESCE(p.manually_deleted_at, '') = ''
            ORDER BY s.server_name, s.server_id
            """
        ).fetchall()

    seen_ids: set[str] = set()
    normal_servers: list[dict[str, Any]] = []
    proxy_server: dict[str, Any] | None = None
    for row in vpn_auto_rows:
        item = dict(row)
        server_id = str(item["server_id"])
        if server_id in seen_ids:
            continue
        seen_ids.add(server_id)
        normal_servers.append(item)
    for row in proxy_rows:
        item = dict(row)
        server_id = str(item["server_id"])
        if server_id in seen_ids:
            continue
        seen_ids.add(server_id)
        item["server_name"] = VIRTUAL_CUSTOM_HTTPS_PROXY_SERVER_NAME
        proxy_server = item

    result: list[dict[str, Any]] = [
        {
            "server_id": VIRTUAL_XRAY_VPN_AUTO_SERVER_ID,
            "server_name": VIRTUAL_XRAY_VPN_AUTO_SERVER_NAME,
        }
    ]
    if proxy_server is not None:
        result.append(proxy_server)
    result.extend(normal_servers)
    return result


def build_subscription_nodes(
    resolved: dict[str, Any],
    *,
    public_host: str | None = None,
    public_port: int | None = None,
    public_path: str | None = None,
) -> list[dict[str, Any]]:
    if not resolved.get("ok"):
        return []
    token = str(resolved["client"]["token"])
    account_name = str(resolved["account"]["display_name"] or resolved["account"]["slug"])
    client_name = str(resolved["client"]["display_name"] or token)
    nodes: list[dict[str, Any]] = []
    for server in _subscription_servers():
        server_id = str(server["server_id"])
        server_name = str(server["server_name"] or server_id)
        nodes.append(
            {
                "subscription_token": token,
                "server_id": server_id,
                "server_name": server_name,
                "client_uuid": _subscription_uuid(token, server_id),
                "client_email": _subscription_email(token, server_id),
                "uri": build_xray_vless_uri(
                    client_uuid=_subscription_uuid(token, server_id),
                    label=server_name,
                    public_host=public_host,
                    public_port=public_port,
                    public_path=public_path,
                ),
                "xray_alias": f"{account_name} / {client_name} / {server_name}",
            }
        )
    return nodes


def _xray_module_enabled() -> bool:
    with db_session() as connection:
        module = connection.execute(
            """
            SELECT desired_state
            FROM modules
            WHERE module_name = 'xray'
            LIMIT 1
            """
        ).fetchone()
    return module is not None and str(module["desired_state"] or "") == "enabled"


def _load_xray_runtime_exportable_clients() -> dict[str, dict[str, Any]]:
    if not _xray_module_enabled():
        return {}

    config_path = get_settings().paths.state_dir / "xray" / "config.json"
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(payload, dict):
        return {}

    inbounds = payload.get("inbounds") if isinstance(payload.get("inbounds"), list) else []
    runtime_clients: dict[str, dict[str, Any]] = {}
    for inbound in inbounds:
        if not isinstance(inbound, dict):
            continue
        if str(inbound.get("tag") or "") != "vless-ws":
            continue
        settings = inbound.get("settings") if isinstance(inbound.get("settings"), dict) else {}
        clients = settings.get("clients") if isinstance(settings.get("clients"), list) else []
        for client in clients:
            if not isinstance(client, dict):
                continue
            email = str(client.get("email") or "").strip()
            if email:
                runtime_clients[email] = client

    outbounds = payload.get("outbounds") if isinstance(payload.get("outbounds"), list) else []
    outbound_tags = {
        str(outbound.get("tag") or "")
        for outbound in outbounds
        if isinstance(outbound, dict)
    }
    routing = payload.get("routing") if isinstance(payload.get("routing"), dict) else {}
    rules = routing.get("rules") if isinstance(routing.get("rules"), list) else []
    routed_emails: set[str] = set()
    stale_api_emails: set[str] = set()
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        users = rule.get("user") or []
        if isinstance(users, str):
            users = [users]
        inbound_tags = rule.get("inboundTag") or []
        if isinstance(inbound_tags, str):
            inbound_tags = [inbound_tags]
        if "vless-ws" not in {str(item) for item in inbound_tags}:
            continue
        outbound_tag = str(rule.get("outboundTag") or "")
        for user in users:
            email = str(user or "").strip()
            if not email:
                continue
            if outbound_tag == "fwrouter-api":
                stale_api_emails.add(email)
            if outbound_tag.startswith("fwrouter-egress-") and outbound_tag in outbound_tags:
                routed_emails.add(email)

    exportable: dict[str, dict[str, Any]] = {}
    for email, client in runtime_clients.items():
        binding = client.get("fwrouterBinding") if isinstance(client.get("fwrouterBinding"), dict) else {}
        if not binding:
            continue
        if email in stale_api_emails:
            continue
        if email in routed_emails:
            exportable[email] = client
    return exportable


def _load_xray_runtime_exportable_emails() -> set[str]:
    return set(_load_xray_runtime_exportable_clients())


def _server_names_by_id(server_ids: set[str]) -> dict[str, str]:
    if not server_ids:
        return {}
    placeholders = ", ".join("?" for _ in server_ids)
    with db_session() as connection:
        rows = connection.execute(
            f"SELECT server_id, server_name FROM servers WHERE server_id IN ({placeholders})",
            tuple(sorted(server_ids)),
        ).fetchall()
    return {str(row["server_id"]): str(row["server_name"] or row["server_id"]) for row in rows}


def _runtime_subscription_nodes(
    resolved: dict[str, Any],
    *,
    public_host: str | None,
    public_port: int | None,
    public_path: str | None,
) -> list[dict[str, Any]]:
    """Render only identities that the effective Xray config can serve now."""

    token = str(resolved["client"]["token"])
    prefix = f"sub-{_stable_digest(token, length=10)}-"
    runtime_clients = _load_xray_runtime_exportable_clients()
    selected_ids = {
        str((client.get("fwrouterBinding") or {}).get("selected_server_id") or "").strip()
        for email, client in runtime_clients.items()
        if email.lower().startswith(prefix)
    }
    names_by_id = _server_names_by_id({server_id for server_id in selected_ids if server_id})
    nodes: list[dict[str, Any]] = []
    for email, client in sorted(runtime_clients.items()):
        if not email.lower().startswith(prefix):
            continue
        binding = client.get("fwrouterBinding") if isinstance(client.get("fwrouterBinding"), dict) else {}
        server_id = str(binding.get("selected_server_id") or "").strip()
        client_uuid = str(client.get("id") or "").strip()
        if not server_id or not client_uuid:
            continue
        server_name = names_by_id.get(server_id)
        if not server_name and server_id == VIRTUAL_XRAY_VPN_AUTO_SERVER_ID:
            server_name = VIRTUAL_XRAY_VPN_AUTO_SERVER_NAME
        if not server_name:
            server_name = str(client.get("fwrouterAlias") or server_id).rsplit(" / ", 1)[-1]
        nodes.append(
            {
                "subscription_token": token,
                "server_id": server_id,
                "server_name": server_name,
                "client_uuid": client_uuid,
                "client_email": email,
                "uri": build_xray_vless_uri(
                    client_uuid=client_uuid,
                    label=server_name,
                    public_host=public_host,
                    public_port=public_port,
                    public_path=public_path,
                ),
                "xray_alias": str(client.get("fwrouterAlias") or ""),
            }
        )
    return nodes


def _snapshot_subscription_nodes(
    resolved: dict[str, Any],
    *,
    public_host: str | None,
    public_port: int | None,
    public_path: str | None,
) -> list[dict[str, Any]] | None:
    token = str(resolved["client"]["token"])
    with db_session() as connection:
        row = connection.execute(
            "SELECT nodes_json FROM subscription_profile_snapshots WHERE token = ?",
            (token,),
        ).fetchone()
    if row is None:
        return None
    try:
        stored = json.loads(row["nodes_json"] or "[]")
    except json.JSONDecodeError:
        return None
    if not isinstance(stored, list):
        return None
    nodes: list[dict[str, Any]] = []
    for item in stored:
        if not isinstance(item, dict):
            continue
        client_uuid = str(item.get("client_uuid") or "").strip()
        server_id = str(item.get("server_id") or "").strip()
        server_name = str(item.get("server_name") or server_id).strip()
        email = str(item.get("client_email") or "").strip()
        if not client_uuid or not server_id or not email:
            continue
        nodes.append(
            {
                "subscription_token": token,
                "server_id": server_id,
                "server_name": server_name,
                "client_uuid": client_uuid,
                "client_email": email,
                "uri": build_xray_vless_uri(
                    client_uuid=client_uuid,
                    label=server_name,
                    public_host=public_host,
                    public_port=public_port,
                    public_path=public_path,
                ),
                "xray_alias": str(item.get("xray_alias") or ""),
            }
        )
    return nodes


def promote_runtime_verified_subscription_nodes(nodes: list[dict[str, Any]]) -> dict[str, int]:
    """Persist public profiles only after their corresponding runtime converged."""

    by_token: dict[str, list[dict[str, Any]]] = {}
    for node in nodes:
        token = str(node.get("subscription_token") or "").strip()
        if token:
            by_token.setdefault(token, []).append(
                {
                    key: node.get(key)
                    for key in ("server_id", "server_name", "client_uuid", "client_email", "xray_alias")
                }
            )
    with db_session() as connection:
        for token, token_nodes in by_token.items():
            connection.execute(
                """
                INSERT INTO subscription_profile_snapshots (token, nodes_json, runtime_verified_at, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                ON CONFLICT(token) DO UPDATE SET
                    nodes_json = excluded.nodes_json,
                    runtime_verified_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (token, json.dumps(token_nodes, ensure_ascii=False, sort_keys=True)),
            )
    return {"profiles_count": len(by_token), "nodes_count": sum(len(items) for items in by_token.values())}


def filter_runtime_exportable_subscription_nodes(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not _xray_module_enabled():
        return nodes

    exportable_emails = _load_xray_runtime_exportable_emails()
    return [
        node
        for node in nodes
        if str(node.get("client_email") or "").strip() in exportable_emails
    ]


def list_desired_subscription_xray_clients(token_or_slug: str | None = None) -> list[dict[str, Any]]:
    where_clause = ""
    params: list[Any] = []
    if token_or_slug:
        where_clause = "AND (sa.slug = ? OR sc.token = ?)"
        params.extend([token_or_slug.lower(), token_or_slug])

    with db_session() as connection:
        rows = connection.execute(
            f"""
            SELECT
                sa.slug,
                sa.display_name AS account_display_name,
                sc.token,
                sc.display_name AS client_display_name
            FROM subscription_accounts AS sa
            JOIN subscription_clients AS sc ON sc.account_id = sa.account_id
            WHERE sa.enabled = 1
              AND sc.enabled = 1
              {where_clause}
            ORDER BY sa.slug, sc.client_id
            """,
            tuple(params)
        ).fetchall()

    desired: list[dict[str, Any]] = []
    for row in rows:
        resolved = {
            "ok": True,
            "account": {
                "slug": row["slug"],
                "display_name": row["account_display_name"] or row["slug"],
            },
            "client": {
                "token": row["token"],
                "display_name": row["client_display_name"] or row["token"],
            },
        }
        desired.extend(build_subscription_nodes(resolved))
    return desired


def render_raw_vless_subscription(resolved: dict[str, Any], nodes: list[dict[str, Any]]) -> dict[str, Any]:
    raw_content = "\n".join(str(node["uri"]) for node in nodes) + ("\n" if nodes else "")
    return {
        "renderer": "raw-vless",
        "content": raw_content,
        "media_type": "text/plain; charset=utf-8",
    }


def render_base64_vless_subscription(resolved: dict[str, Any], nodes: list[dict[str, Any]]) -> dict[str, Any]:
    raw = render_raw_vless_subscription(resolved, nodes)
    encoded = base64.b64encode(str(raw["content"]).encode("utf-8")).decode("ascii")
    return {
        "renderer": "base64-vless",
        "content": encoded,
        "media_type": "text/plain; charset=utf-8",
    }


def render_happ_subscription(resolved: dict[str, Any], nodes: list[dict[str, Any]]) -> dict[str, Any]:
    happ_uris = [
        _without_query_params(
            str(node["uri"]),
            {"alpn", "fp", "packetEncoding"},
        )
        for node in nodes
    ]
    raw_content = "\n".join(happ_uris) + ("\n" if happ_uris else "")
    return {
        "renderer": "happ",
        "content": base64.b64encode(raw_content.encode("utf-8")).decode("ascii"),
        "media_type": "text/plain; charset=utf-8",
    }


def _happ_management_headers(resolved: dict[str, Any]) -> dict[str, str]:
    profile_title = str(resolved["account"]["display_name"] or resolved["account"]["slug"])
    return {
        # Keep exact Happ doc parameter names as lowercase response headers.
        "profile-title": profile_title,
        "profile-update-interval": "1",
        "subscription-userinfo": "upload=0; download=0; total=0; expire=0",
    }


def render_clash_subscription(resolved: dict[str, Any], nodes: list[dict[str, Any]]) -> dict[str, Any]:
    profile_name = str(resolved["account"]["display_name"] or resolved["account"]["slug"])
    lines: list[str] = []
    lines.append("proxies:")
    for node in nodes:
        parsed = urlsplit(str(node["uri"]))
        uri_params = dict(parse_qsl(parsed.query, keep_blank_values=True))
        server = parsed.hostname or "localhost"
        port = parsed.port or 443
        path = uri_params.get("path") or "/vless"
        host = uri_params.get("host") or server
        sni = uri_params.get("sni") or server
        lines.extend(
            [
                f"  - name: {_yaml_quote(node['server_name'])}",
                "    type: vless",
                f"    server: {_yaml_quote(server)}",
                f"    port: {int(port)}",
                f"    uuid: {_yaml_quote(node['client_uuid'])}",
                "    tls: true",
                f"    servername: {_yaml_quote(sni)}",
                "    udp: true",
                "    network: ws",
                "    client-fingerprint: chrome",
                "    alpn:",
                "      - http/1.1",
                "    ws-opts:",
                f"      path: {_yaml_quote(path)}",
                "      headers:",
                f"        Host: {_yaml_quote(host)}",
            ]
        )

    lines.append("proxy-groups:")
    lines.append(f"  - name: {_yaml_quote(profile_name)}")
    lines.append("    type: select")
    lines.append("    proxies:")
    for node in nodes:
        lines.append(f"      - {_yaml_quote(node['server_name'])}")
    lines.append('      - "DIRECT"')

    lines.append("rules:")
    lines.append(f"  - MATCH,{profile_name}")
    lines.append("")
    return {
        "renderer": "clash",
        "content": "\n".join(lines),
        "media_type": "application/yaml; charset=utf-8",
    }


def render_subscription_profile(
    token_or_slug: str,
    *,
    user_agent: str | None,
    requested_format: str | None,
    public_host: str | None = None,
    public_port: int | None = None,
    public_path: str | None = None,
) -> dict[str, Any]:
    resolved = resolve_subscription_client(token_or_slug, user_agent, requested_format)
    if not resolved.get("ok"):
        return resolved

    nodes = _snapshot_subscription_nodes(
        resolved,
        public_host=public_host,
        public_port=public_port,
        public_path=public_path,
    )
    if nodes is None and _xray_module_enabled():
        nodes = _runtime_subscription_nodes(
            resolved,
            public_host=public_host,
            public_port=public_port,
            public_path=public_path,
        )
    if nodes is None:
        nodes = filter_runtime_exportable_subscription_nodes(
            build_subscription_nodes(
                resolved,
                public_host=public_host,
                public_port=public_port,
                public_path=public_path,
            )
        )
    detected_format = str(resolved["detected_format"])
    if detected_format == "happ":
        rendered = render_happ_subscription(resolved, nodes)
    elif detected_format == "clash":
        rendered = render_clash_subscription(resolved, nodes)
    elif detected_format == "base64-vless":
        rendered = render_base64_vless_subscription(resolved, nodes)
    else:
        rendered = render_raw_vless_subscription(resolved, nodes)

    headers = {
        "subscription-userinfo": "upload=0; download=0; total=0; expire=0",
        "profile-title": str(resolved["account"]["display_name"] or resolved["account"]["slug"]),
        "profile-update-interval": "1",
        "Cache-Control": "no-store",
        "X-FWRouter-Subscription-Client": str(resolved["client"]["token"]),
        "X-FWRouter-Detected-Format": detected_format,
        "X-FWRouter-Nodes-Count": str(len(nodes)),
        "X-FWRouter-Xray-Clients-Count": str(len(nodes)),
        "X-FWRouter-Handoff-Count": str(len({str(node["server_id"]) for node in nodes})),
        "X-FWRouter-Renderer": rendered["renderer"],
    }
    if detected_format == "happ":
        headers.update(_happ_management_headers(resolved))

    return {
        "ok": True,
        "subscription_client": resolved["client"],
        "subscription_account": resolved["account"],
        "detected_format": detected_format,
        "nodes_count": len(nodes),
        "xray_clients_count": len(nodes),
        "handoff_count": len({str(node["server_id"]) for node in nodes}),
        "renderer": rendered["renderer"],
        "media_type": rendered["media_type"],
        "content": rendered["content"],
        "uris": [str(node["uri"]) for node in nodes],
        "headers": headers,
        "nodes": nodes,
    }
