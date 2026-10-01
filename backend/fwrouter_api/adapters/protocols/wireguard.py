"""WireGuard and AmneziaWG v2 Mihomo mapping and INI parser."""

from __future__ import annotations

import configparser
import ipaddress
from io import StringIO
from typing import Any
from urllib.parse import urlparse

from .base import ProtocolHookResult, ProtocolIntegration, ProtocolValidationIssue, capabilities


_AMNEZIA_INT = {"jc", "jmin", "jmax", "s1", "s2", "s3", "s4"}
_AMNEZIA_STRING = {"h1", "h2", "h3", "h4", "i1", "i2", "i3", "i4", "i5"}
_AMNEZIA_KEYS = _AMNEZIA_INT | _AMNEZIA_STRING | {"version"}
def _matches(proxy: dict[str, Any]) -> bool:
    return str(proxy.get("type") or proxy.get("protocol") or "").strip().lower() in {"wireguard", "amneziawg", "amneziawg2"}


def _normalize(proxy: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(proxy)
    normalized["type"] = "wireguard"
    if "amneziawg-option" in normalized and "amnezia-wg-option" not in normalized:
        normalized["amnezia-wg-option"] = normalized.pop("amneziawg-option")
    return normalized


def _validate(proxy: dict[str, Any]) -> tuple[ProtocolValidationIssue, ...]:
    issues: list[ProtocolValidationIssue] = []
    if not isinstance(proxy.get("private-key"), str) or not proxy["private-key"].strip():
        issues.append(ProtocolValidationIssue("missing_field", "private-key"))
    peers = proxy.get("peers")
    if isinstance(peers, list) and peers:
        for index, peer in enumerate(peers):
            field_prefix = f"peers.{index}"
            if not isinstance(peer, dict):
                issues.append(ProtocolValidationIssue("invalid_field", "peers"))
                continue
            if not isinstance(peer.get("server"), str) or not peer["server"].strip():
                issues.append(ProtocolValidationIssue("missing_field", f"{field_prefix}.server"))
            peer_port = peer.get("port")
            if isinstance(peer_port, bool) or not isinstance(peer_port, int) or not 1 <= peer_port <= 65535:
                issues.append(ProtocolValidationIssue("invalid_endpoint", f"{field_prefix}.port"))
            if not isinstance(peer.get("public-key"), str) or not peer["public-key"].strip():
                issues.append(ProtocolValidationIssue("missing_field", f"{field_prefix}.public-key"))
    else:
        if not isinstance(proxy.get("server"), str) or not proxy["server"].strip():
            issues.append(ProtocolValidationIssue("missing_field", "server"))
        port = proxy.get("port")
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            issues.append(ProtocolValidationIssue("invalid_endpoint", "port"))
        if not isinstance(proxy.get("public-key"), str) or not proxy["public-key"].strip():
            issues.append(ProtocolValidationIssue("missing_field", "public-key"))
        if not any(isinstance(proxy.get(field), str) and proxy[field].strip() for field in ("ip", "ipv6")):
            issues.append(ProtocolValidationIssue("missing_field", "ip"))
    for field in ("ip", "ipv6"):
        value = proxy.get(field)
        if value:
            try:
                ipaddress.ip_interface(value)
            except (ValueError, TypeError):
                issues.append(ProtocolValidationIssue("invalid_field", field))
    if "udp" in proxy and not isinstance(proxy["udp"], bool):
        issues.append(ProtocolValidationIssue("invalid_field", "udp"))
    opts = proxy.get("amnezia-wg-option")
    if opts is not None:
        if not isinstance(opts, dict):
            issues.append(ProtocolValidationIssue("invalid_security_options", "amnezia-wg-option"))
        else:
            for key in opts:
                if key not in _AMNEZIA_KEYS:
                    issues.append(ProtocolValidationIssue("unsupported_option", f"amnezia-wg-option.{key}"))
            version = opts.get("version", 2)
            if isinstance(version, bool) or version != 2:
                issues.append(ProtocolValidationIssue("unsupported_security", "amnezia-wg-option.version"))
            for key in _AMNEZIA_INT & opts.keys():
                value = opts[key]
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    issues.append(ProtocolValidationIssue("invalid_field", f"amnezia-wg-option.{key}"))
            for key in _AMNEZIA_STRING & opts.keys():
                value = opts[key]
                if not isinstance(value, (str, int)):
                    issues.append(ProtocolValidationIssue("invalid_field", f"amnezia-wg-option.{key}"))
                elif key.startswith("h"):
                    raw = str(value)
                    if not raw.isdigit() and not ("-" in raw and all(part.isdigit() for part in raw.split("-", 1))):
                        issues.append(ProtocolValidationIssue("invalid_field", f"amnezia-wg-option.{key}"))
    for field in ("security", "tls", "reality-opts"):
        if field in proxy:
            issues.append(ProtocolValidationIssue("unsupported_security", field))
    return tuple(issues)


def _parse_ini(value: str) -> ProtocolHookResult:
    if not value.lstrip().lower().startswith("[interface]"):
        return ProtocolHookResult(False)
    parser = configparser.ConfigParser(interpolation=None, strict=True, delimiters=("="))
    parser.optionxform = str
    try:
        parser.read_file(StringIO(value))
    except (configparser.Error, UnicodeError):
        return ProtocolHookResult(True, issues=(ProtocolValidationIssue("invalid_ini", "ini"),))
    sections = parser.sections()
    if len(sections) != 2 or sections[0].lower() != "interface" or sections[1].lower() != "peer":
        return ProtocolHookResult(True, issues=(ProtocolValidationIssue("unsupported_topology", "sections"),))
    interface = {key.lower(): val.strip() for key, val in parser.items(sections[0])}
    peer = {key.lower(): val.strip() for key, val in parser.items(sections[1])}
    allowed_interface = {"privatekey", "address", "dns", "mtu", "jc", "jmin", "jmax", "s1", "s2", "s3", "s4", "h1", "h2", "h3", "h4", "i1", "i2", "i3", "i4", "i5", "headerprotectionkey", "contentpaddingaddition", "rekeyaftertime", "rekeytimeout", "rejectaftertime", "keepalivetimeout", "maxhandshakeattempts"}
    allowed_peer = {"publickey", "presharedkey", "endpoint", "allowedips", "persistentkeepalive"}
    if set(interface) - allowed_interface or set(peer) - allowed_peer:
        return ProtocolHookResult(True, issues=(ProtocolValidationIssue("unsupported_option", "ini"),))
    if any(key in interface for key in {"headerprotectionkey", "contentpaddingaddition", "rekeyaftertime", "rekeytimeout", "rejectaftertime", "keepalivetimeout", "maxhandshakeattempts"}):
        return ProtocolHookResult(True, issues=(ProtocolValidationIssue("unsupported_security", "amnezia.version"),))
    endpoint = peer.get("endpoint", "")
    try:
        parsed = urlparse("//" + endpoint)
        port = parsed.port
    except ValueError:
        port = None
    if not parsed.hostname or not port:
        return ProtocolHookResult(True, issues=(ProtocolValidationIssue("invalid_endpoint", "Endpoint"),))
    addresses = [item.strip() for item in interface.get("address", "").split(",") if item.strip()]
    proxy: dict[str, Any] = {
        "type": "wireguard", "name": parsed.hostname, "server": parsed.hostname, "port": port,
        "private-key": interface.get("privatekey"), "public-key": peer.get("publickey"), "udp": True,
    }
    if peer.get("presharedkey"):
        proxy["pre-shared-key"] = peer["presharedkey"]
    for address in addresses:
        try:
            version = ipaddress.ip_interface(address).version
        except ValueError:
            return ProtocolHookResult(True, issues=(ProtocolValidationIssue("invalid_field", "Address"),))
        proxy["ip" if version == 4 else "ipv6"] = address
    if interface.get("dns"):
        proxy["dns"] = [item.strip() for item in interface["dns"].split(",") if item.strip()]
    if interface.get("mtu"):
        try:
            proxy["mtu"] = int(interface["mtu"])
        except ValueError:
            return ProtocolHookResult(True, issues=(ProtocolValidationIssue("invalid_field", "MTU"),))
    if peer.get("allowedips"):
        proxy["allowed-ips"] = [part.strip() for part in peer["allowedips"].split(",") if part.strip()]
    if peer.get("persistentkeepalive"):
        try:
            proxy["persistent-keepalive"] = int(peer["persistentkeepalive"])
        except ValueError:
            return ProtocolHookResult(True, issues=(ProtocolValidationIssue("invalid_field", "PersistentKeepalive"),))
    awg_values = {key: interface[key] for key in (_AMNEZIA_INT | _AMNEZIA_STRING) if key in interface}
    if awg_values:
        opts: dict[str, Any] = {"version": 2}
        for key, raw in awg_values.items():
            if key in _AMNEZIA_INT:
                try:
                    opts[key] = int(raw)
                except ValueError:
                    return ProtocolHookResult(True, issues=(ProtocolValidationIssue("invalid_field", f"amnezia-wg-option.{key}"),))
            else:
                opts[key] = raw
        proxy["amnezia-wg-option"] = opts
    return ProtocolHookResult(True, proxy=proxy)


INTEGRATION = ProtocolIntegration(
    capabilities=capabilities("wireguard", "wireguard_or_amnezia_v2", ("clash_yaml", "wireguard_ini"), transport="WireGuard and AmneziaWG v2 fields; Mihomo v1.19.31 native validation required"),
    matcher=_matches, normalizer=_normalize, validator=_validate, parse_ini=_parse_ini,
    project_mihomo=lambda proxy: ProtocolHookResult(True, _normalize(proxy)),
)
