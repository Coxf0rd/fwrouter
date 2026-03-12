#!/usr/bin/env python3

import base64
import json
import os
import re
import sys
import uuid
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse


ROOT = Path("/app/vless-gateway")
ENV_PATH = ROOT / ".env"
XRAY_CONFIG_PATH = ROOT / "xray" / "config.json"
SUBSCRIPTION_DIR = ROOT / "subscription"
UPSTREAM_PATH = Path(os.getenv("UPSTREAM_SUB_PATH", "/var/lib/fwrouter/mihomo2/subscription.yaml"))
VPN_AUTO_CONFIG_PATH = Path(os.getenv("VPN_AUTO_CONFIG_PATH", "/etc/fwrouter/autolist.json"))
NAMESPACE = uuid.UUID("3c0e6e38-994f-4e67-8e55-6fef77ef17cd")


def read_env(path: Path) -> dict:
    out = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        out[key.strip()] = value.strip()
    return out


def yaml_quote(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def ensure_trailing_newline(text: str) -> str:
    if text.endswith("\n"):
        return text
    return text + "\n"


def write_if_changed(path: Path, text: str) -> bool:
    text = ensure_trailing_newline(text)
    old = path.read_text(encoding="utf-8") if path.exists() else None
    if old == text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return True


def decode_upstream_blob(blob: str) -> str:
    cleaned = "".join(blob.split())
    try:
        decoded = base64.b64decode(cleaned).decode("utf-8", errors="ignore")
        if "vless://" in decoded:
            return decoded
    except Exception:
        pass
    return blob


def parse_upstream_nodes(source_path: Path) -> list[dict]:
    if not source_path.exists():
        raise FileNotFoundError(f"upstream file not found: {source_path}")

    raw_text = source_path.read_text(encoding="utf-8", errors="ignore")
    decoded = decode_upstream_blob(raw_text)

    nodes = []
    for raw_line in decoded.splitlines():
        line = raw_line.strip()
        if not line.startswith("vless://"):
            continue

        parsed = urlparse(line)
        if parsed.scheme != "vless":
            continue

        upstream_id = unquote(parsed.username or "")
        address = parsed.hostname or ""
        port = parsed.port or 443
        query = {key: values[-1] if values else "" for key, values in parse_qs(parsed.query, keep_blank_values=True).items()}
        raw_name = unquote(parsed.fragment or "").strip() or f"{address}:{port}"

        if not upstream_id or not address:
            continue

        network = (query.get("type") or "tcp").lower()
        if network not in {"tcp", "grpc", "ws"}:
            continue

        security = (query.get("security") or "none").lower()
        if security not in {"reality", "tls", "none"}:
            continue

        if security == "reality" and not query.get("pbk"):
            continue

        nodes.append(
            {
                "raw_uri": line,
                "raw_name": re.sub(r"\s+", " ", raw_name),
                "upstream_id": upstream_id,
                "address": address,
                "port": int(port),
                "network": network,
                "security": security,
                "query": query,
            }
        )

    name_count = {}
    for node in nodes:
        base = node["raw_name"]
        seq = name_count.get(base, 0) + 1
        name_count[base] = seq
        node["name"] = base if seq == 1 else f"{base} ({seq})"

    return nodes


def load_vpn_auto_candidates(config_path: Path) -> list[str]:
    if not config_path.exists():
        return []
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return []

    raw_candidates = payload.get("candidates") or []
    out = []
    seen = set()
    for entry in raw_candidates:
        if isinstance(entry, dict):
            name = str(entry.get("name", "")).strip()
        else:
            name = str(entry).strip()
        if not name or name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out


def filter_nodes_by_candidate_names(nodes: list[dict], candidate_names: list[str]) -> tuple[list[dict], list[str]]:
    if not candidate_names:
        return nodes, []

    by_name = {node["name"]: node for node in nodes}
    filtered = []
    missing = []
    for name in candidate_names:
        node = by_name.get(name)
        if not node:
            missing.append(name)
            continue
        filtered.append(node)
    return filtered, missing


def build_outbound(node: dict, tag: str) -> dict:
    query = node["query"]
    security = node["security"]
    network = node["network"]

    user = {"id": node["upstream_id"], "encryption": "none"}
    flow = query.get("flow", "").strip()
    if flow:
        user["flow"] = flow

    outbound = {
        "tag": tag,
        "protocol": "vless",
        "settings": {
            "vnext": [
                {
                    "address": node["address"],
                    "port": node["port"],
                    "users": [user],
                }
            ]
        },
        "streamSettings": {
            "network": network,
            "security": security,
        },
    }

    sni = query.get("sni") or query.get("servername") or node["address"]

    if security == "reality":
        outbound["streamSettings"]["realitySettings"] = {
            "serverName": sni,
            "fingerprint": query.get("fp") or "chrome",
            "publicKey": query.get("pbk", ""),
            "shortId": query.get("sid", ""),
            "spiderX": unquote(query.get("spx", "/") or "/"),
        }
    elif security == "tls":
        outbound["streamSettings"]["tlsSettings"] = {
            "serverName": sni,
            "allowInsecure": False,
        }

    if network == "grpc":
        grpc_settings = {}
        if query.get("serviceName"):
            grpc_settings["serviceName"] = query.get("serviceName")
        if query.get("authority"):
            grpc_settings["authority"] = query.get("authority")
        if query.get("mode", "").lower() == "multi":
            grpc_settings["multiMode"] = True
        if grpc_settings:
            outbound["streamSettings"]["grpcSettings"] = grpc_settings
    elif network == "ws":
        ws_settings = {"path": unquote(query.get("path", "/") or "/")}
        host_header = query.get("host")
        if host_header:
            ws_settings["headers"] = {"Host": host_header}
        outbound["streamSettings"]["wsSettings"] = ws_settings
    elif network == "tcp":
        header_type = query.get("headerType", "")
        if header_type and header_type != "none":
            outbound["streamSettings"]["tcpSettings"] = {"header": {"type": header_type}}

    return outbound


def build_client_id(node: dict) -> str:
    query = node["query"]
    key = "|".join(
        [
            node["raw_name"],
            node["address"],
            str(node["port"]),
            node["network"],
            node["security"],
            query.get("sni", ""),
            query.get("serviceName", ""),
            query.get("path", ""),
        ]
    )
    return str(uuid.uuid5(NAMESPACE, key))


def build_happ_client_id(node: dict) -> str:
    return str(uuid.uuid5(NAMESPACE, build_client_id(node) + "|happ"))


def build_subscription_provider_yaml(domain: str, nodes: list[dict], reality: dict) -> str:
    lines = ["proxies:"]
    for node in nodes:
        lines.extend(
            [
                f"  - name: {yaml_quote(node['name'])}",
                "    type: vless",
                f"    server: {domain}",
                f"    port: {reality['port']}",
                f"    uuid: {node['client_uuid']}",
                "    network: tcp",
                "    tls: true",
                "    udp: true",
                "    flow: xtls-rprx-vision",
                f"    servername: {reality['server_name']}",
                "    client-fingerprint: chrome",
                "    reality-opts:",
                f"      public-key: {reality['public_key']}",
                f"      short-id: {reality['short_id']}",
            ]
        )
    return "\n".join(lines)


def build_subscription_clash_yaml(domain: str, nodes: list[dict], reality: dict) -> str:
    lines = [
        "port: 7890",
        "socks-port: 7891",
        "mixed-port: 7892",
        "allow-lan: true",
        "bind-address: '*'",
        "mode: rule",
        "log-level: info",
        "",
        "proxies:",
    ]
    for node in nodes:
        lines.extend(
            [
                f"  - name: {yaml_quote(node['name'])}",
                "    type: vless",
                f"    server: {domain}",
                f"    port: {reality['port']}",
                f"    uuid: {node['client_uuid']}",
                "    network: tcp",
                "    tls: true",
                "    udp: true",
                "    flow: xtls-rprx-vision",
                f"    servername: {reality['server_name']}",
                "    client-fingerprint: chrome",
                "    reality-opts:",
                f"      public-key: {reality['public_key']}",
                f"      short-id: {reality['short_id']}",
            ]
        )
    lines.extend(["", "proxy-groups:", "  - name: PROXY", "    type: select", "    proxies:"])
    for node in nodes:
        lines.append(f"      - {yaml_quote(node['name'])}")
    lines.extend(["", "rules:", "  - MATCH,PROXY"])
    return "\n".join(lines)


def build_uri_list(domain: str, nodes: list[dict], reality: dict) -> str:
    uris = []
    for node in nodes:
        fragment = quote(node["name"], safe="")
        uris.append(
            f"vless://{node['client_uuid']}@{domain}:{reality['port']}"
            f"?security=reality&type=tcp&sni={reality['server_name']}&fp=chrome"
            f"&pbk={reality['public_key']}&sid={reality['short_id']}&flow=xtls-rprx-vision&encryption=none"
            f"#{fragment}"
        )
    return "\n".join(uris)


def build_happ_uri_list(domain: str, nodes: list[dict], happ_public_port: int) -> str:
    uris = []
    for node in nodes:
        fragment = quote(node["name"], safe="")
        uris.append(
            f"vless://{node['happ_client_uuid']}@{domain}:{happ_public_port}"
            f"?type=ws&security=tls&sni={domain}&host={domain}&path=%2Fvless&encryption=none"
            f"#{fragment}"
        )
    return "\n".join(uris)


def build_index_html(node_count: int, happ_public_port: int) -> str:
    return f"""<!doctype html>
<html>
  <head><meta charset="utf-8"><title>vless subscriptions</title></head>
  <body>
    <h1>VLESS Subscriptions (auto, nodes: {node_count})</h1>
    <ul>
      <li><a href="/sub-vpn">Universal endpoint (auto format by app User-Agent)</a></li>
      <li><a href="/sub-vpn?format=clash">Universal endpoint forced to Clash full config</a></li>
      <li><a href="/sub-vpn?format=b64">Universal endpoint forced to base64 URI list</a></li>
      <li><a href="/sub-vpn-provider.yaml">Clash provider (recommended for Clash subscription)</a></li>
      <li><a href="/sub-vpn-clash.txt">Clash full config (text/plain)</a></li>
      <li><a href="/sub-vpn.yaml">Clash full config (.yaml)</a></li>
      <li><a href="/sub-vpn64.txt">Base64 URI list</a></li>
      <li><a href="/sub-vpn64-happ.txt">Base64 URI list (Happ WS+TLS :{happ_public_port})</a></li>
      <li><a href="/sub-vpn-uri-list.txt">Plain URI list</a></li>
      <li><a href="/sub-vpn-uri-list-happ.txt">Plain URI list (Happ WS+TLS :{happ_public_port})</a></li>
      <li><a href="/nodes-meta.json">Generated nodes metadata</a></li>
    </ul>
  </body>
</html>"""


def build_xray_config(
    legacy_uuid: str,
    nodes: list[dict],
    reality: dict,
    happ_listen_addr: str,
    happ_listen_port: int,
    mihomo_socks_host: str,
    mihomo_socks_port: int,
) -> dict:
    clients_reality = [{"id": legacy_uuid, "email": "vpn@mode", "flow": "xtls-rprx-vision"}]
    clients_reality.extend({"id": node["client_uuid"], "email": node["email"], "flow": "xtls-rprx-vision"} for node in nodes)
    clients_reality.extend(
        {"id": node["happ_client_uuid"], "email": node["happ_email"], "flow": "xtls-rprx-vision"} for node in nodes
    )
    clients_ws = [{"id": legacy_uuid, "email": "vpn@mode"}]
    clients_ws.extend({"id": node["client_uuid"], "email": node["email"]} for node in nodes)
    clients_ws.extend({"id": node["happ_client_uuid"], "email": node["happ_email"]} for node in nodes)

    inbounds = [
        {
            "tag": "vless-ws-nontls-via-npm",
            "port": happ_listen_port,
            "listen": happ_listen_addr,
            "protocol": "vless",
            "settings": {"clients": clients_ws, "decryption": "none"},
            "streamSettings": {
                "network": "ws",
                "security": "none",
                "wsSettings": {"path": "/vless"},
            },
        },
        {
            "tag": "vless-reality",
            "port": reality["port"],
            "listen": "0.0.0.0",
            "protocol": "vless",
            "settings": {"clients": clients_reality, "decryption": "none"},
            "streamSettings": {
                "network": "tcp",
                "security": "reality",
                "realitySettings": {
                    "show": False,
                    "dest": reality["dest"],
                    "xver": 0,
                    "serverNames": [reality["server_name"]],
                    "privateKey": reality["private_key"],
                    "shortIds": [reality["short_id"]],
                },
            },
        },
    ]

    outbounds = [
        {
            "tag": "out-vpn",
            "protocol": "socks",
            "settings": {
                "servers": [
                    {
                        "address": mihomo_socks_host,
                        "port": mihomo_socks_port,
                    }
                ]
            },
        }
    ]
    outbounds.extend(build_outbound(node, node["outbound_tag"]) for node in nodes)
    outbounds.extend(build_outbound(node, node["happ_outbound_tag"]) for node in nodes)

    rules = [{"type": "field", "user": [node["email"]], "outboundTag": node["outbound_tag"]} for node in nodes]
    rules.extend({"type": "field", "user": [node["happ_email"]], "outboundTag": node["happ_outbound_tag"]} for node in nodes)
    rules.append({"type": "field", "user": ["vpn@mode"], "outboundTag": "out-vpn"})

    return {
        "log": {"access": "/dev/stdout", "error": "/dev/stderr", "loglevel": "warning"},
        "inbounds": inbounds,
        "outbounds": outbounds,
        "routing": {"domainStrategy": "AsIs", "rules": rules},
    }


def main() -> int:
    env = read_env(ENV_PATH)
    domain = env.get("DOMAIN", "vpn.example.com")
    legacy_uuid = env.get("VPN_UUID", "9d39ae98-4ed8-4303-8cae-5b271dc59605")
    xray_port = int(env.get("XRAY_PORT", "8443"))
    happ_public_port = int(env.get("HAPP_PUBLIC_PORT", "443"))
    happ_listen_addr = env.get("HAPP_LISTEN_ADDR", "127.0.0.1")
    happ_listen_port = int(env.get("HAPP_LISTEN_PORT", "10000"))
    mihomo_socks_host = env.get("MIHOMO_SOCKS_HOST", "127.0.0.1")
    mihomo_socks_port = int(env.get("MIHOMO_SOCKS_PORT", "7895"))
    reality_private_key = env.get("REALITY_PRIVATE_KEY", "")
    reality_public_key = env.get("REALITY_PUBLIC_KEY", "")
    reality_short_id = env.get("REALITY_SHORT_ID", "")
    reality_server_name = env.get("REALITY_SERVER_NAME", "www.cloudflare.com")
    reality_dest = env.get("REALITY_DEST", f"{reality_server_name}:443")

    if not reality_private_key or not reality_public_key or not reality_short_id:
        print("[sync] missing REALITY_PRIVATE_KEY/REALITY_PUBLIC_KEY/REALITY_SHORT_ID in .env")
        return 1

    reality = {
        "port": xray_port,
        "private_key": reality_private_key,
        "public_key": reality_public_key,
        "short_id": reality_short_id,
        "server_name": reality_server_name,
        "dest": reality_dest,
    }

    nodes = parse_upstream_nodes(UPSTREAM_PATH)
    if not nodes:
        print("[sync] no valid nodes parsed from upstream; skip updates")
        return 1

    vpn_auto_candidates = load_vpn_auto_candidates(VPN_AUTO_CONFIG_PATH)
    nodes, missing_from_upstream = filter_nodes_by_candidate_names(nodes, vpn_auto_candidates)
    if vpn_auto_candidates and not nodes:
        print("[sync] vpn-auto candidates are configured but none found in upstream; skip updates")
        return 1
    if vpn_auto_candidates:
        print(f"[sync] vpn-auto candidates configured: {len(vpn_auto_candidates)}")
        print(f"[sync] nodes after vpn-auto filter: {len(nodes)}")
        if missing_from_upstream:
            print(f"[sync] vpn-auto missing in upstream: {len(missing_from_upstream)}")
            for name in missing_from_upstream:
                print(f"[sync] missing candidate: {name}")

    for index, node in enumerate(nodes, start=1):
        node["client_uuid"] = build_client_id(node)
        node["happ_client_uuid"] = build_happ_client_id(node)
        node["email"] = f"node-{index}@mode"
        node["happ_email"] = f"node-{index}-happ@mode"
        node["outbound_tag"] = f"out-node-{index}"
        node["happ_outbound_tag"] = f"out-node-{index}-happ"

    provider_yaml = build_subscription_provider_yaml(domain, nodes, reality)
    clash_yaml = build_subscription_clash_yaml(domain, nodes, reality)
    uri_list = build_uri_list(domain, nodes, reality)
    b64_list = base64.b64encode(ensure_trailing_newline(uri_list).encode("utf-8")).decode("ascii")
    happ_uri_list = build_happ_uri_list(domain, nodes, happ_public_port)
    happ_b64_list = base64.b64encode(ensure_trailing_newline(happ_uri_list).encode("utf-8")).decode("ascii")

    nodes_meta = [
        {
            "name": node["name"],
            "client_uuid": node["client_uuid"],
            "happ_client_uuid": node["happ_client_uuid"],
            "email": node["email"],
            "happ_email": node["happ_email"],
            "outbound_tag": node["outbound_tag"],
            "happ_outbound_tag": node["happ_outbound_tag"],
            "upstream_address": node["address"],
            "upstream_port": node["port"],
            "network": node["network"],
            "security": node["security"],
            "upstream_name": node["raw_name"],
        }
        for node in nodes
    ]

    xray_config = build_xray_config(
        legacy_uuid,
        nodes,
        reality,
        happ_listen_addr,
        happ_listen_port,
        mihomo_socks_host,
        mihomo_socks_port,
    )
    xray_text = json.dumps(xray_config, ensure_ascii=False, indent=2) + "\n"

    changed = False
    changed |= write_if_changed(XRAY_CONFIG_PATH, xray_text)
    changed |= write_if_changed(SUBSCRIPTION_DIR / "sub-vpn-provider.yaml", provider_yaml)
    changed |= write_if_changed(SUBSCRIPTION_DIR / "sub-vpn-clash.txt", clash_yaml)
    changed |= write_if_changed(SUBSCRIPTION_DIR / "sub-vpn.yaml", clash_yaml)
    changed |= write_if_changed(SUBSCRIPTION_DIR / "sub-vpn-uri-list.txt", uri_list)
    changed |= write_if_changed(SUBSCRIPTION_DIR / "sub-vpn-uri-list-happ.txt", happ_uri_list)
    changed |= write_if_changed(SUBSCRIPTION_DIR / "sub-vpn.txt", uri_list)
    changed |= write_if_changed(SUBSCRIPTION_DIR / "sub-vpn64.txt", b64_list)
    changed |= write_if_changed(SUBSCRIPTION_DIR / "sub-vpn64-happ.txt", happ_b64_list)
    changed |= write_if_changed(SUBSCRIPTION_DIR / "sub-vpn", b64_list)
    changed |= write_if_changed(SUBSCRIPTION_DIR / "sub-vpn.b64", b64_list)
    changed |= write_if_changed(SUBSCRIPTION_DIR / "nodes-meta.json", json.dumps(nodes_meta, ensure_ascii=False, indent=2) + "\n")
    changed |= write_if_changed(SUBSCRIPTION_DIR / "index.html", build_index_html(len(nodes), happ_public_port))

    print(f"[sync] parsed nodes: {len(nodes)}")
    if changed:
        print("[sync] files changed")
        return 20
    print("[sync] files unchanged")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"[sync] fatal error: {exc}")
        sys.exit(1)
