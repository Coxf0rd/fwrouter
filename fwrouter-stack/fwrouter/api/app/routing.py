from __future__ import annotations

from pathlib import Path
from typing import Dict, List

FWROUTER_CONF = Path("/etc/fwrouter/fwrouter.conf")
DEVICES_CONF = Path("/etc/fwrouter/devices.conf")


def _read_kv(path: Path) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip().lower()] = v.strip()
    return out


def _write_kv(path: Path, data: Dict[str, str]) -> None:
    lines: List[str] = []
    seen: set[str] = set()
    if path.exists():
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if raw.strip().startswith("#") or "=" not in raw:
                lines.append(raw)
                continue
            k, _v = raw.split("=", 1)
            k = k.strip().lower()
            if k in data:
                if k not in seen:
                    lines.append(f"{k}={data[k]}")
                    seen.add(k)
            else:
                lines.append(raw)
    for k, v in data.items():
        if k not in seen:
            lines.append(f"{k}={v}")
    text = "\n".join(lines) + "\n"
    path.write_text(text, encoding="utf-8")


def get_global() -> Dict[str, str]:
    kv = _read_kv(FWROUTER_CONF)
    return {
        "enabled": kv.get("enabled", "false"),
        "mode": kv.get("mode", "DIRECT"),
        "selective_default": kv.get("selective_default", "DIRECT"),
        "self_mode": kv.get("self_mode", "GLOBAL"),
    }


def set_global(
    enabled: str,
    mode: str,
    selective_default: str | None = None,
    self_mode: str | None = None,
) -> None:
    kv = _read_kv(FWROUTER_CONF)
    kv["enabled"] = enabled
    kv["mode"] = mode
    if selective_default:
        kv["selective_default"] = selective_default
    if self_mode:
        kv["self_mode"] = self_mode
    _write_kv(FWROUTER_CONF, kv)


def read_device_overrides() -> Dict[str, str]:
    out: Dict[str, str] = {}
    if not DEVICES_CONF.exists():
        return out
    for raw in DEVICES_CONF.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        ip, mode = parts[0], parts[1].upper()
        out[ip] = mode
    return out


def write_device_override(ip: str, mode: str) -> None:
    lines: List[str] = []
    found = False
    if DEVICES_CONF.exists():
        for raw in DEVICES_CONF.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                lines.append(raw)
                continue
            parts = line.split()
            if len(parts) >= 2 and parts[0] == ip:
                lines.append(f"{ip} {mode}")
                found = True
            else:
                lines.append(raw)
    if not found:
        lines.append(f"{ip} {mode}")
    DEVICES_CONF.write_text("\n".join(lines) + "\n", encoding="utf-8")


def remove_device_override(ip: str) -> None:
    if not DEVICES_CONF.exists():
        return
    out: List[str] = []
    for raw in DEVICES_CONF.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            out.append(raw)
            continue
        parts = line.split()
        if len(parts) >= 1 and parts[0] == ip:
            continue
        out.append(raw)
    DEVICES_CONF.write_text("\n".join(out) + "\n", encoding="utf-8")
