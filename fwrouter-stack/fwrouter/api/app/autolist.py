import json
import time
from pathlib import Path
import requests
from urllib.parse import quote
import os

CONFIG_PATH = Path("/etc/fwrouter/autolist.json")
STATE_PATH = Path("/var/lib/fwrouter/autolist_state.json")

MIHOMO_API_BASE = os.getenv("MIHOMO_API_BASE", "http://mihomo:9090")
MIHOMO_API_SECRET = os.getenv("MIHOMO_API_SECRET", "")


def _headers():
    if MIHOMO_API_SECRET:
        return {"Authorization": f"Bearer {MIHOMO_API_SECRET}"}
    return {}


def _read_json(path: Path, fallback: dict) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return fallback


def load_config() -> dict:
    default = {
        "enabled": False,
        "group": "PROXY",
        "url": "http://www.gstatic.com/generate_204",
        "ip_check_direct_url": "https://api.ipify.org?format=json",
        "ip_check_vpn_url": "https://api.ipify.org?format=json",
        "timeout_ms": 2500,
        "cooldown_sec": 900,
        "min_interval_sec": 300,
        "candidates": [],
        "hidden_user": [],
    }
    cfg = _read_json(CONFIG_PATH, default)
    for k, v in default.items():
        cfg.setdefault(k, v)
    # Backward compatibility: if dedicated IP-check URLs are absent, inherit from url/default.
    if not str(cfg.get("ip_check_direct_url", "")).strip():
        cfg["ip_check_direct_url"] = str(cfg.get("url") or default["ip_check_direct_url"])
    if not str(cfg.get("ip_check_vpn_url", "")).strip():
        cfg["ip_check_vpn_url"] = str(cfg.get("url") or default["ip_check_vpn_url"])
    return cfg


def save_config(cfg: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_state() -> dict:
    default = {
        "last_run": 0,
        "last_switch": 0,
        "last_best": "",
        "last_error": "",
    }
    st = _read_json(STATE_PATH, default)
    for k, v in default.items():
        st.setdefault(k, v)
    return st


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def mihomo_group(name: str, timeout=3) -> dict:
    r = requests.get(f"{MIHOMO_API_BASE}/proxies/{quote(name, safe='')}", headers=_headers(), timeout=timeout)
    r.raise_for_status()
    return r.json()


def mihomo_delay(name: str, url: str, timeout_ms: int) -> dict:
    r = requests.get(
        f"{MIHOMO_API_BASE}/proxies/{quote(name, safe='')}/delay",
        params={"timeout": timeout_ms, "url": url},
        headers=_headers(),
        timeout=max(1, int(timeout_ms / 1000) + 1),
    )
    r.raise_for_status()
    return r.json()


def mihomo_set_group(name: str, target: str, timeout=3) -> dict:
    r = requests.put(
        f"{MIHOMO_API_BASE}/proxies/{quote(name, safe='')}",
        json={"name": target},
        headers=_headers(),
        timeout=timeout,
    )
    r.raise_for_status()
    if r.status_code == 204 or not r.text:
        return {"ok": True}
    return r.json()


def run_autolist(force: bool = False) -> dict:
    cfg = load_config()
    state = load_state()

    if not cfg.get("enabled"):
        return {"ok": False, "disabled": True, "state": state, "config": cfg}

    now = int(time.time())
    min_interval = int(cfg.get("min_interval_sec", 300))
    if not force and now - int(state.get("last_run", 0)) < min_interval:
        return {"ok": False, "rate_limited": True, "state": state, "config": cfg}

    group = cfg.get("group", "PROXY")
    url = cfg.get("url")
    timeout_ms = int(cfg.get("timeout_ms", 2500))
    cooldown = int(cfg.get("cooldown_sec", 900))

    state["last_run"] = now

    try:
        grp = mihomo_group(group)
        now_name = grp.get("now", "")
        all_names = grp.get("all", []) or []

        candidates = cfg.get("candidates") or []
        # flatten (allow list of objects {name, priority})
        cand_names = []
        for c in candidates:
            if isinstance(c, dict):
                name = c.get("name")
                if name:
                    cand_names.append(name)
            else:
                cand_names.append(str(c))

        cand_names = [c for c in cand_names if c and c != "DIRECT"]

        if not cand_names:
            state["last_error"] = "no candidates configured"
            try:
                mihomo_set_group(group, "DIRECT")
                state["last_best"] = "DIRECT"
                state["last_switch"] = now
            except Exception:
                pass
            save_state(state)
            return {"ok": True, "fallback": "DIRECT", "state": state, "config": cfg}

        results = []
        for name in cand_names:
            try:
                d = mihomo_delay(name, url, timeout_ms)
                delay = d.get("delay", -1)
                if isinstance(delay, int) and delay >= 0:
                    results.append({"name": name, "delay": delay})
            except Exception:
                continue

        if not results:
            state["last_error"] = "no candidates with delay"
            try:
                mihomo_set_group(group, "DIRECT")
                state["last_best"] = "DIRECT"
                state["last_switch"] = now
            except Exception:
                pass
            save_state(state)
            return {"ok": True, "fallback": "DIRECT", "state": state, "config": cfg}

        results.sort(key=lambda x: x["delay"])
        best = results[0]["name"]
        state["last_best"] = best
        state["last_error"] = ""

        can_switch = (now - int(state.get("last_switch", 0))) >= cooldown
        if force:
            can_switch = True
        switched = False
        if best != now_name and can_switch:
            mihomo_set_group(group, best)
            state["last_switch"] = now
            switched = True

        save_state(state)
        return {
            "ok": True,
            "switched": switched,
            "best": best,
            "now": now_name,
            "results": results,
            "state": state,
            "config": cfg,
        }
    except Exception as e:
        state["last_error"] = str(e)
        save_state(state)
        return {"ok": False, "error": str(e), "state": state, "config": cfg}
