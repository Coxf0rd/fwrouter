from __future__ import annotations

import base64
import hashlib
import json
from typing import Any
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit
from uuid import NAMESPACE_DNS, uuid5

from fwrouter_api.db.connection import db_session
from fwrouter_api.services.custom_servers import (
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


def _ensure_legacy_subscription_identity(token_or_slug: str) -> dict[str, Any]:
    slug = str(token_or_slug or "").strip().lower()
    display_name = _title_from_slug(slug)
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
                updated_at = CURRENT_TIMESTAMP
            """,
            (slug, display_name),
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
            VALUES (?, ?, 'auto', 1, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(token) DO UPDATE SET
                enabled = 1,
                updated_at = CURRENT_TIMESTAMP
            """,
            (account["account_id"], slug, display_name),
        )

    return resolve_subscription_client(slug, None, "auto", auto_create_legacy=False)


def resolve_subscription_client(
    token_or_slug: str,
    user_agent: str | None,
    requested_format: str | None,
    *,
    auto_create_legacy: bool = True,
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

        connection.execute(
            """
            UPDATE subscription_clients
            SET
                last_seen_at = CURRENT_TIMESTAMP,
                last_user_agent = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE client_id = ?
            """,
            (str(user_agent or ""), row["client_id"]),
        )

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
            """
            SELECT s.server_id, s.server_name
            FROM servers AS s
            JOIN server_preferences AS p ON p.server_id = s.server_id
            WHERE COALESCE(p.vpn_auto, 0) = 1
              AND s.inventory_state = 'active'
              AND COALESCE(p.manually_deleted_at, '') = ''
              AND s.server_id NOT IN (
                  SELECT server_id FROM server_custom_https_proxy
              )
            ORDER BY s.server_name, s.server_id
            """
        ).fetchall()
        proxy_rows = connection.execute(
            """
            SELECT s.server_id, s.server_name
            FROM servers AS s
            JOIN server_preferences AS p ON p.server_id = s.server_id
            JOIN server_custom_https_proxy AS c ON c.server_id = s.server_id
            WHERE s.inventory_state = 'active'
              AND COALESCE(p.global_list, 1) = 1
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
        item["server_name"] = "Proxy (не заходить)"
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


def build_subscription_nodes(resolved: dict[str, Any]) -> list[dict[str, Any]]:
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
                "server_id": server_id,
                "server_name": server_name,
                "client_uuid": _subscription_uuid(token, server_id),
                "client_email": _subscription_email(token, server_id),
                "uri": build_xray_vless_uri(
                    client_uuid=_subscription_uuid(token, server_id),
                    label=server_name,
                ),
                "xray_alias": f"{account_name} / {client_name} / {server_name}",
            }
        )
    return nodes


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
        lines.extend(
            [
                f"  - name: {_yaml_quote(node['server_name'])}",
                "    type: vless",
                '    server: "xray.minisk.ru"',
                "    port: 443",
                f"    uuid: {_yaml_quote(node['client_uuid'])}",
                "    tls: true",
                '    servername: "xray.minisk.ru"',
                "    udp: true",
                "    network: ws",
                "    client-fingerprint: chrome",
                "    alpn:",
                "      - http/1.1",
                "    ws-opts:",
                '      path: "/vless"',
                "      headers:",
                '        Host: "xray.minisk.ru"',
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
) -> dict[str, Any]:
    resolved = resolve_subscription_client(token_or_slug, user_agent, requested_format)
    if not resolved.get("ok"):
        return resolved

    nodes = build_subscription_nodes(resolved)
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
