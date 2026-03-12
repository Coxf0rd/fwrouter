import os
import requests
from urllib.parse import quote

MIHOMO_API_BASE = os.getenv("MIHOMO_API_BASE", "http://mihomo:9090")
MIHOMO_API_SECRET = os.getenv("MIHOMO_API_SECRET", "")
MIHOMO_PROVIDER_NAME = os.getenv("MIHOMO_PROVIDER_NAME", "subscription")

def _headers():
    if MIHOMO_API_SECRET:
        return {"Authorization": f"Bearer {MIHOMO_API_SECRET}"}
    return {}

def get_version(timeout=3):
    r = requests.get(f"{MIHOMO_API_BASE}/version", headers=_headers(), timeout=timeout)
    r.raise_for_status()
    return r.json()

def get_providers(timeout=3):
    r = requests.get(f"{MIHOMO_API_BASE}/providers/proxies", headers=_headers(), timeout=timeout)
    r.raise_for_status()
    return r.json()

def update_provider(name: str | None = None, timeout=10):
    provider = name or MIHOMO_PROVIDER_NAME
    r = requests.put(f"{MIHOMO_API_BASE}/providers/proxies/{provider}", headers=_headers(), timeout=timeout)
    r.raise_for_status()
    return {"ok": True, "provider": provider}

def get_proxy_group(name: str, timeout=3):
    safe = quote(name, safe="")
    r = requests.get(f"{MIHOMO_API_BASE}/proxies/{safe}", headers=_headers(), timeout=timeout)
    r.raise_for_status()
    return r.json()

def set_proxy_group(name: str, target: str, timeout=3):
    r = requests.put(
        f"{MIHOMO_API_BASE}/proxies/{quote(name, safe='')}",
        headers=_headers(),
        json={"name": target},
        timeout=timeout,
    )
    r.raise_for_status()
    if r.status_code == 204 or not r.text:
        return {"ok": True}
    return r.json()

def proxy_delay(name: str, url: str, timeout_ms: int = 2500):
    r = requests.get(
        f"{MIHOMO_API_BASE}/proxies/{quote(name, safe='')}/delay",
        params={"timeout": timeout_ms, "url": url},
        headers=_headers(),
        timeout=max(1, int(timeout_ms / 1000) + 1),
    )
    r.raise_for_status()
    return r.json()

def get_proxies(timeout=3):
    r = requests.get(f"{MIHOMO_API_BASE}/proxies", headers=_headers(), timeout=timeout)
    r.raise_for_status()
    return r.json()

def get_traffic(timeout=3):
    r = requests.get(f"{MIHOMO_API_BASE}/traffic", headers=_headers(), timeout=timeout, stream=True)
    r.raise_for_status()
    line = None
    for chunk in r.iter_lines(decode_unicode=True):
        if chunk:
            line = chunk
            break
    r.close()
    if not line:
        return {"up": 0, "down": 0, "upTotal": 0, "downTotal": 0}
    try:
        import json
        return json.loads(line)
    except Exception:
        return {"up": 0, "down": 0, "upTotal": 0, "downTotal": 0}
