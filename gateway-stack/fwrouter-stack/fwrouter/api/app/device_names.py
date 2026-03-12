from __future__ import annotations

import json
import os
import time
import tempfile
from pathlib import Path
from typing import Dict, Any

STATE_PATH = Path("/etc/fwrouter/device_names.json")
LEGACY_STATE_PATH = Path("/var/lib/fwrouter/device_names.json")


def _migrate_legacy() -> None:
    if STATE_PATH.exists() or not LEGACY_STATE_PATH.exists():
        return
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        LEGACY_STATE_PATH.replace(STATE_PATH)
    except Exception:
        # best-effort migration; fall back to legacy read
        return


def _load() -> Dict[str, Any]:
    _migrate_legacy()
    if not STATE_PATH.exists():
        if LEGACY_STATE_PATH.exists():
            try:
                return json.loads(LEGACY_STATE_PATH.read_text(encoding="utf-8"))
            except Exception:
                return {}
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        # fallback to .bak if file was partially written
        bak_path = STATE_PATH.with_suffix(STATE_PATH.suffix + ".bak")
        try:
            return json.loads(bak_path.read_text(encoding="utf-8"))
        except Exception:
            return {}


def _save(data: Dict[str, Any]) -> None:
    _migrate_legacy()
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    # Write atomically + keep a .bak to survive partial writes
    tmp_fd, tmp_path = tempfile.mkstemp(prefix="device_names.", dir=str(STATE_PATH.parent))
    try:
        with open(tmp_fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        bak_path = STATE_PATH.with_suffix(STATE_PATH.suffix + ".bak")
        if STATE_PATH.exists():
            try:
                bak_path.write_text(STATE_PATH.read_text(encoding="utf-8"), encoding="utf-8")
            except Exception:
                pass
        Path(tmp_path).replace(STATE_PATH)
    finally:
        try:
            Path(tmp_path).unlink(missing_ok=True)
        except Exception:
            pass


def _key_for_mac(mac: str) -> str:
    return mac.lower()


def _key_for_ip(ip: str) -> str:
    return f"ip:{ip}"


def get_name(mac: str) -> str:
    data = _load()
    entry = data.get(_key_for_mac(mac), {})
    return entry.get("name", "")


def get_name_ip(ip: str) -> str:
    data = _load()
    entry = data.get(_key_for_ip(ip), {})
    return entry.get("name", "")


def touch_seen(mac: str, name: str | None = None) -> None:
    data = _load()
    key = _key_for_mac(mac)
    entry = data.get(key, {})
    if name:
        entry["name"] = name
    entry["last_seen"] = int(time.time())
    data[key] = entry
    _save(data)


def touch_seen_ip(ip: str, name: str | None = None) -> None:
    data = _load()
    key = _key_for_ip(ip)
    entry = data.get(key, {})
    if name:
        entry["name"] = name
    entry["last_seen"] = int(time.time())
    data[key] = entry
    _save(data)


def set_name(mac: str, name: str) -> None:
    data = _load()
    key = _key_for_mac(mac)
    if not name:
        data.pop(key, None)
    else:
        data[key] = {
            "name": name,
            "last_seen": int(time.time()),
        }
    _save(data)


def set_name_ip(ip: str, name: str) -> None:
    data = _load()
    key = _key_for_ip(ip)
    if not name:
        data.pop(key, None)
    else:
        data[key] = {
            "name": name,
            "last_seen": int(time.time()),
        }
    _save(data)


def cleanup_old(days: int = 180) -> None:
    data = _load()
    if not data:
        return
    cutoff = int(time.time()) - days * 24 * 3600
    changed = False
    for mac, entry in list(data.items()):
        last_seen = int(entry.get("last_seen", 0))
        if last_seen and last_seen < cutoff:
            data.pop(mac, None)
            changed = True
    if changed:
        _save(data)
