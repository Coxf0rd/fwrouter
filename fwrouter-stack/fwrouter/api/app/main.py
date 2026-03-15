import asyncio
import json
import os
import time
import subprocess
from pathlib import Path
from typing import AsyncIterator

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from .apply_engine import make_plan, apply_from_candidate, rollback as apply_rollback, status as apply_status, log_change


from .devices import get_devices_snapshot

from .mihomo import (
    get_version as mihomo_get_version,
    get_providers as mihomo_get_providers,
    update_provider as mihomo_update_provider,
    get_proxy_group as mihomo_get_proxy_group,
    set_proxy_group as mihomo_set_proxy_group,
    proxy_delay as mihomo_proxy_delay,
    get_proxies as mihomo_get_proxies,
    get_traffic as mihomo_get_traffic,
)
from .autolist import load_config as autolist_load_config, load_state as autolist_load_state, run_autolist, save_config as autolist_save_config
from .refilter import get_apply_status as refilter_get_apply_status, load_state as refilter_load_state, sync_latest_release_locked
from .subscription import get_subscription, update_subscription
from .routing import (
    get_global as routing_get_global,
    set_global as routing_set_global,
    write_device_override,
    read_device_overrides,
    remove_device_override,
)
from .device_names import set_name as set_device_name, set_name_ip as set_device_name_ip

APP_START_TS = int(time.time())

POSTGRES_DB = os.getenv("POSTGRES_DB", "fwrouter")
POSTGRES_USER = os.getenv("POSTGRES_USER", "fwrouter")
FWROUTER_ADMIN_PASSWORD = os.getenv("FWROUTER_ADMIN_PASSWORD", "zzz")
FWROUTER_TS_SUFFIX = os.getenv("FWROUTER_TS_SUFFIX", ".vpn.example.com")

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

app = FastAPI(title="fwrouter local mgmt", version="0.1.0")

# Static assets (/static/css/styles.css, /static/js/devices.js)
STATIC_DIR = BASE_DIR / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# --- Event bus (in-memory; достаточен для локального UI без polling) ---
class EventBus:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[str]] = set()
        self._lock = asyncio.Lock()

    async def subscribe(self) -> asyncio.Queue[str]:
        q: asyncio.Queue[str] = asyncio.Queue(maxsize=100)
        async with self._lock:
            self._subscribers.add(q)
        return q

    async def unsubscribe(self, q: asyncio.Queue[str]) -> None:
        async with self._lock:
            self._subscribers.discard(q)

    async def publish(self, event: dict) -> None:
        payload = json.dumps(event, separators=(",", ":"), ensure_ascii=False)
        async with self._lock:
            subs = list(self._subscribers)
        # best-effort fanout (drop if subscriber is slow)
        for q in subs:
            if q.full():
                continue
            q.put_nowait(payload)


bus = EventBus()


# --- Simple auth for admin endpoints ---
def require_admin(request: Request) -> None:
    # Admin auth disabled per UI requirement (single-user local UI).
    return


@app.get("/healthz")
async def healthz() -> dict:
    return {
        "ok": True,
        "service": "fwrouter-mgmt",
        "uptime_s": int(time.time()) - APP_START_TS,
        "db": {
            "name": POSTGRES_DB,
            "user": POSTGRES_USER,
            "note": "db is managed by compose; API connectivity tests are a later module",
        },
    }


@app.get("/api/whoami")
async def api_whoami(request: Request) -> dict:
    # Prefer proxy headers if present (NPM), fall back to direct socket.
    forwarded = request.headers.get("x-forwarded-for", "") or ""
    real_ip = request.headers.get("x-real-ip", "") or ""
    ip = ""
    used_proxy_header = False
    if forwarded:
        ip = forwarded.split(",")[0].strip()
        used_proxy_header = True
    if not ip and real_ip:
        ip = real_ip.strip()
        used_proxy_header = True
    if not ip and request.client:
        ip = request.client.host or ""
    # If we only see a Docker gateway IP (e.g. 172.16/12) and no proxy headers,
    # return empty so UI can show "unknown" instead of a misleading device.
    if ip and not used_proxy_header:
        try:
            parts = [int(p) for p in ip.split(".")]
            if len(parts) == 4 and parts[0] == 172 and 16 <= parts[1] <= 31:
                ip = ""
        except Exception:
            pass
    return {"ip": ip}


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> Response:
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "service": "fwrouter local mgmt",
            "started_at": APP_START_TS,
            "ts_suffix": FWROUTER_TS_SUFFIX,
        },
    )


@app.get("/events")
async def events(request: Request) -> StreamingResponse:
    """
    Server-Sent Events endpoint.
    UI uses EventSource (no polling).
    """

    q = await bus.subscribe()

    async def gen() -> AsyncIterator[bytes]:
        # initial hello
        yield b"event: hello\ndata: {\"ok\":true}\n\n"
        try:
            while True:
                # client disconnected?
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=25.0)
                    yield f"event: update\ndata: {msg}\n\n".encode("utf-8")
                except asyncio.TimeoutError:
                    # keep-alive comment (not a request from UI)
                    yield b": keep-alive\n\n"
        finally:
            await bus.unsubscribe(q)

    headers = {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",  # harmless if not behind nginx
    }
    return StreamingResponse(gen(), media_type="text/event-stream", headers=headers)


@app.post("/admin/notify")
async def admin_notify(request: Request, _: None = Depends(require_admin)) -> dict:
    body = {}
    try:
        body = await request.json()
    except Exception:
        body = {}

    event = {
        "ts": int(time.time()),
        "type": body.get("type", "manual"),
        "message": body.get("message", "admin notify"),
    }
    await bus.publish(event)
    return {"ok": True, "published": event}


@app.get("/api/devices")
def api_devices(refresh: str | None = None):
    # Optional refresh of tailscale cache (on explicit UI refresh).
    if refresh and str(refresh).lower() in ("1", "true", "yes", "ts"):
        try:
            subprocess.run(["/usr/local/sbin/fwrouter-sync-tailscale"], check=True, timeout=8)
        except Exception:
            # Non-fatal: still return current snapshot
            pass
    return get_devices_snapshot()

# --- Mihomo integration (NO traffic intercept in this stage) ---

@app.get("/api/mihomo/status")
async def api_mihomo_status() -> dict:
    """
    Read-only status:
      - config loaded (container running)
      - version reachable
      - providers listing reachable
    """
    try:
        ver = mihomo_get_version()
        providers = mihomo_get_providers()
        return {"ok": True, "version": ver, "providers": providers}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"mihomo status error: {e}")

@app.post("/api/mihomo/update")
async def api_mihomo_update() -> dict:
    """
    Manual provider update (event-driven; no timers).
    Calls Mihomo API: PUT /providers/proxies/{provider_name}
    """
    try:
        res = mihomo_update_provider()
        event = {
            "ts": int(time.time()),
            "type": "mihomo",
            "message": "provider updated",
            "provider": res.get("provider"),
        }
        await bus.publish(event)
        return {"ok": True, **res}
    except Exception as e:
        event = {
            "ts": int(time.time()),
            "type": "mihomo_error",
            "message": str(e),
        }
        await bus.publish(event)
        raise HTTPException(status_code=502, detail=f"mihomo update error: {e}")

@app.get("/api/subscription")
async def api_subscription() -> dict:
    return {"ok": True, **get_subscription()}

@app.put("/api/subscription")
async def api_subscription_update(payload: dict, _: None = Depends(require_admin)) -> dict:
    url = (payload or {}).get("url", "")
    header = (payload or {}).get("header") or {}
    try:
        # Make installs "one-step": user can paste URL only.
        # Some providers require HWID headers; default to /etc/machine-id.
        ensured = dict(header or {})
        if "User-Agent" not in ensured:
            ensured["User-Agent"] = ["fwrouter/1.0"]
        if "X-HWID" not in ensured:
            try:
                mid = Path("/etc/machine-id").read_text(encoding="utf-8", errors="ignore").strip()
            except Exception:
                mid = ""
            if mid:
                ensured["X-HWID"] = [mid]
        update_subscription(url, ensured)
        # best-effort provider update
        try:
            mihomo_update_provider()
        except Exception:
            pass
        event = {"ts": int(time.time()), "type": "subscription", "message": "updated"}
        await bus.publish(event)
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"subscription update failed: {e}")

@app.get("/api/mihomo/proxy_group")
async def api_mihomo_proxy_group(name: str) -> dict:
    try:
        data = mihomo_get_proxy_group(name)
        return {"ok": True, "group": name, "now": data.get("now"), "all": data.get("all", [])}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"mihomo proxy group error: {e}")

@app.put("/api/mihomo/proxy_group")
async def api_mihomo_proxy_group_set(payload: dict, _: None = Depends(require_admin)) -> dict:
    name = (payload or {}).get("name")
    target = (payload or {}).get("target")
    if not name or not target:
        raise HTTPException(status_code=400, detail="name and target required")
    try:
        res = mihomo_set_proxy_group(name, target)
        event = {"ts": int(time.time()), "type": "mihomo", "message": f"group {name} -> {target}"}
        await bus.publish(event)
        return {"ok": True, "result": res}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"mihomo proxy group set error: {e}")

@app.get("/api/mihomo/servers")
async def api_mihomo_servers(
    group: str = "PROXY",
    url: str = "http://www.gstatic.com/generate_204",
    timeout_ms: int = 6000,
    measure: int = 0,
    max_tests: int = 5,
    budget_ms: int = 5000,
) -> dict:
    try:
        grp = mihomo_get_proxy_group(group)
        all_names = grp.get("all", []) or []
        now = grp.get("now", "")
        proxies = mihomo_get_proxies().get("proxies", {})
        servers = []
        start_ts = time.time()
        tested = 0
        for name in all_names:
            if name == "DIRECT":
                continue
            delay = -1
            if measure and tested < max_tests and (time.time() - start_ts) * 1000 < budget_ms:
                try:
                    delay = mihomo_proxy_delay(name, url, timeout_ms).get("delay", -1)
                except Exception:
                    delay = -1
                tested += 1
            else:
                proxy = proxies.get(name)
                if proxy:
                    history = proxy.get("history") or []
                    if history:
                        delay = history[-1].get("delay", -1)
            servers.append({"name": name, "delay": delay})
        return {"ok": True, "group": group, "now": now, "servers": servers}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"mihomo servers error: {e}")

@app.put("/api/mihomo/select")
async def api_mihomo_select(payload: dict, _: None = Depends(require_admin)) -> dict:
    group = (payload or {}).get("group", "PROXY")
    target = (payload or {}).get("target")
    if not target:
        raise HTTPException(status_code=400, detail="target required")
    try:
        res = mihomo_set_proxy_group(group, target)
        event = {"ts": int(time.time()), "type": "mihomo", "message": f"{group} -> {target}"}
        await bus.publish(event)
        return {"ok": True, "result": res}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"mihomo select error: {e}")

@app.get("/api/autolist/status")
async def api_autolist_status() -> dict:
    return {"ok": True, "config": autolist_load_config(), "state": autolist_load_state()}

@app.post("/api/autolist/run")
async def api_autolist_run(_: None = Depends(require_admin)) -> dict:
    res = run_autolist()
    event = {"ts": int(time.time()), "type": "autolist", "message": "run", "ok": res.get("ok")}
    await bus.publish(event)
    return res

@app.put("/api/autolist/config")
async def api_autolist_config(payload: dict, _: None = Depends(require_admin)) -> dict:
    cfg = autolist_load_config()
    for key in ["enabled", "group", "url", "timeout_ms", "cooldown_sec", "min_interval_sec", "candidates"]:
        if key in payload:
            cfg[key] = payload[key]
    autolist_save_config(cfg)
    event = {"ts": int(time.time()), "type": "autolist", "message": "config updated"}
    await bus.publish(event)

    # If vpn-auto is enabled, run immediately so it doesn't stick to manual selection.
    if cfg.get("enabled"):
        res = run_autolist(force=True)
        event2 = {"ts": int(time.time()), "type": "autolist", "message": "run", "ok": res.get("ok")}
        await bus.publish(event2)
        return {"ok": True, "config": cfg, "run": res}

    return {"ok": True, "config": cfg}

CONFIG_FILES = {
    "fwrouter": "/etc/fwrouter/fwrouter.conf",
    "devices": "/etc/fwrouter/devices.conf",
    "routes": "/etc/fwrouter/routes.conf",
    "domains": "/etc/fwrouter/domains.conf",
    "policy": "/etc/fwrouter/policy.conf",
    "autolist": "/etc/fwrouter/autolist.json",
}

@app.get("/api/stats")
async def api_stats() -> dict:
    stats_path = Path("/var/lib/fwrouter/stats.json")
    series = []
    try:
        if stats_path.exists():
            series = json.loads(stats_path.read_text(encoding="utf-8")).get("series", [])
    except Exception:
        series = []

    # Pull current traffic from mihomo (bytes/s)
    try:
        t = mihomo_get_traffic()
        up = int(t.get("up", 0))
        down = int(t.get("down", 0))
        value = up + down
        series.append({"ts": int(time.time()), "value": value})
        series = series[-60:]
        stats_path.parent.mkdir(parents=True, exist_ok=True)
        stats_path.write_text(json.dumps({"series": series}, ensure_ascii=False) + "\n", encoding="utf-8")
    except Exception:
        up = 0
        down = 0

    return {
        "ok": True,
        "vpn": {"total_bytes": 0, "series": series, "up": up, "down": down},
        "devices": [],
    }

@app.get("/api/config")
async def api_config() -> dict:
    out = {}
    for name, path in CONFIG_FILES.items():
        try:
            out[name] = Path(path).read_text(encoding="utf-8", errors="replace")
        except Exception:
            out[name] = ""
    return {"ok": True, "files": out}

@app.post("/api/config")
async def api_config_save(payload: dict, _: None = Depends(require_admin)) -> dict:
    name = (payload or {}).get("name")
    content = (payload or {}).get("content", "")
    if name not in CONFIG_FILES:
        raise HTTPException(status_code=400, detail="unknown config name")
    path = Path(CONFIG_FILES[name])
    try:
        path.write_text(content if content.endswith("\n") else content + "\n", encoding="utf-8")
        event = {"ts": int(time.time()), "type": "config", "message": f"{name} updated"}
        await bus.publish(event)
        return {"ok": True, "name": name}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"write failed: {e}")


def _is_ip_or_cidr(value: str) -> bool:
    import re
    ipv4 = r"(?:\\d{1,3}\\.){3}\\d{1,3}"
    cidr = rf"^{ipv4}(?:/\\d{{1,2}})?$"
    return re.match(cidr, value) is not None


@app.get("/api/rules")
async def api_rules() -> dict:
    rules_path = Path("/etc/fwrouter/rules.conf")
    if rules_path.exists():
        content = rules_path.read_text(encoding="utf-8", errors="replace")
    else:
        routes = Path("/etc/fwrouter/routes.conf").read_text(encoding="utf-8", errors="replace")
        domains = Path("/etc/fwrouter/domains.conf").read_text(encoding="utf-8", errors="replace")
        content = routes.strip() + "\n\n" + domains.strip() + "\n"
    return {"ok": True, "content": content}

@app.post("/api/rules/refresh")
async def api_rules_refresh(payload: dict, _: None = Depends(require_admin)) -> dict:
    mode = (payload or {}).get("mode", "small")
    cmd = ["/usr/local/sbin/fwrouter-resolve-domains"]
    if str(mode).lower() == "all":
        cmd.append("all")
    else:
        cmd.append("small")
    try:
        async def _run_refresh() -> None:
            try:
                await asyncio.to_thread(subprocess.run, cmd, check=True, timeout=600)
            except Exception as exc:
                # Non-fatal: keep UI responsive, log for debugging.
                log_change("ERROR", "/usr/local/sbin/fwrouter-resolve-domains", f"rules refresh failed: {exc}")

        asyncio.create_task(_run_refresh())
        return {"ok": True, "queued": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"rules refresh failed: {e}")


@app.get("/api/rules/upstream/status")
async def api_rules_upstream_status() -> dict:
    return {"ok": True, "state": refilter_load_state(), "apply": refilter_get_apply_status()}


@app.post("/api/rules/update-all")
async def api_rules_update_all(_: None = Depends(require_admin)) -> dict:
    try:
        res = await asyncio.to_thread(sync_latest_release_locked)
        event = {
            "ts": int(time.time()),
            "type": "rules_upstream",
            "message": "updated" if res.get("changed") else "skipped",
            "tag": (res.get("state") or {}).get("tag", ""),
        }
        await bus.publish(event)
        return res
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        log_change("ERROR", "/etc/fwrouter/rules.d", f"refilter sync failed: {e}")
        event = {"ts": int(time.time()), "type": "rules_upstream_error", "message": str(e)}
        await bus.publish(event)
        raise HTTPException(status_code=500, detail=f"rules update-all failed: {e}")


@app.post("/api/rules")
async def api_rules_save(payload: dict, _: None = Depends(require_admin)) -> dict:
    content = (payload or {}).get("content", "")
    lines = content.splitlines()

    routes_lines = []
    domains_lines = []
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        action = parts[0].upper()
        target = parts[1]
        if action not in ("VPN", "DIRECT"):
            continue
        if _is_ip_or_cidr(target):
            routes_lines.append(f"{action} {target}")
        else:
            domains_lines.append(f"{action} {target}")

    Path("/etc/fwrouter/rules.conf").write_text(content if content.endswith("\n") else content + "\n", encoding="utf-8")

    event = {"ts": int(time.time()), "type": "rules", "message": "rules updated"}
    await bus.publish(event)
    return {"ok": True}

@app.get("/api/routing/status")
async def api_routing_status() -> dict:
    return {"ok": True, "global": routing_get_global(), "overrides": read_device_overrides()}

@app.put("/api/routing/global")
async def api_routing_global(payload: dict, _: None = Depends(require_admin)) -> dict:
    enabled = (payload or {}).get("enabled", "false")
    mode = (payload or {}).get("mode", "DIRECT")
    sel_def = (payload or {}).get("selective_default", None)
    try:
        routing_set_global(str(enabled).lower(), str(mode).upper(), str(sel_def).upper() if sel_def else None)
        event = {"ts": int(time.time()), "type": "routing", "message": f"global={mode}"}
        await bus.publish(event)
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"routing update failed: {e}")

@app.put("/api/routing/device")
async def api_routing_device(payload: dict, _: None = Depends(require_admin)) -> dict:
    ip = (payload or {}).get("ip", "")
    mode = (payload or {}).get("mode", "")
    if not ip or not mode:
        raise HTTPException(status_code=400, detail="ip and mode required")
    try:
        mode_up = str(mode).upper()
        if mode_up == "GLOBAL":
            remove_device_override(ip)
        else:
            write_device_override(ip, mode_up)
        event = {"ts": int(time.time()), "type": "routing", "message": f"{ip}={mode_up}"}
        await bus.publish(event)
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"device override failed: {e}")

@app.put("/api/device/name")
async def api_device_name(payload: dict) -> dict:
    mac = (payload or {}).get("mac", "").lower()
    ip = (payload or {}).get("ip", "")
    name = (payload or {}).get("name", "").strip()
    if not mac and not ip:
        raise HTTPException(status_code=400, detail="mac or ip required")
    try:
        if mac:
            set_device_name(mac, name)
            msg = f"name:{mac}"
        else:
            set_device_name_ip(ip, name)
            msg = f"name:{ip}"
        event = {"ts": int(time.time()), "type": "device", "message": msg}
        await bus.publish(event)
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"device name failed: {e}")

# --- Apply engine v1 (FILES ONLY; no net changes) ---

@app.get("/api/apply/status")
async def api_apply_status() -> dict:
    try:
        return {"ok": True, "status": apply_status()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"apply status error: {e}")


@app.post("/api/apply/dry-run")
async def api_apply_dry_run() -> dict:
    # dry-run is safe; allow without admin if you want. If you want strict control, add Depends(require_admin).
    try:
        plan = make_plan()
        event = {"ts": int(time.time()), "type": "apply_plan", "message": "plan created", "plan": plan.ts}
        await bus.publish(event)
        return {"ok": True, "plan": plan.ts, "plan_dir": str(plan.plan_dir), "diff": plan.diff}
    except Exception as e:
        event = {"ts": int(time.time()), "type": "apply_error", "message": str(e)}
        await bus.publish(event)
        raise HTTPException(status_code=400, detail=f"apply dry-run error: {e}")


@app.post("/api/apply/apply")
async def api_apply_apply(request: Request, _: None = Depends(require_admin)) -> dict:
    """
    Apply candidate built by last dry-run/plan.
    Files-only: swaps /etc/fwrouter/generated + refreshes /var/lib/fwrouter/last-good
    """
    try:
        body = {}
        try:
            body = await request.json()
        except Exception:
            body = {}
        ts = body.get("plan")
        if not ts:
            raise ValueError("missing 'plan' (timestamp)")

        prev, lastgood = apply_from_candidate(ts)

        # Log coarse-grained changes
        log_change("MODIFY", "/etc/fwrouter/generated", f"apply-engine v1: applied plan {ts} (files-only)")
        log_change("MODIFY", "/var/lib/fwrouter/last-good", f"apply-engine v1: refreshed last-good for plan {ts}")
        log_change("CREATE", f"/var/lib/fwrouter/plan/{ts}", "apply-engine v1: saved plan (diff+summary)")
        if str(prev) and str(prev) != ".":
            log_change("CREATE", str(prev), f"apply-engine v1: preserved previous generated snapshot for plan {ts}")

        event = {"ts": int(time.time()), "type": "apply", "message": "applied", "plan": ts}
        await bus.publish(event)
        return {"ok": True, "applied": ts, "prev": str(prev), "last_good": str(lastgood)}
    except Exception as e:
        event = {"ts": int(time.time()), "type": "apply_error", "message": str(e)}
        await bus.publish(event)
        raise HTTPException(status_code=400, detail=f"apply error: {e}")


@app.post("/api/apply/rollback")
async def api_apply_rb(_: None = Depends(require_admin)) -> dict:
    try:
        apply_rollback()
        log_change("MODIFY", "/etc/fwrouter/generated", "apply-engine v1: rollback restored generated from last-good")
        event = {"ts": int(time.time()), "type": "apply_rollback", "message": "rollback done"}
        await bus.publish(event)
        return {"ok": True}
    except Exception as e:
        event = {"ts": int(time.time()), "type": "apply_error", "message": str(e)}
        await bus.publish(event)
        raise HTTPException(status_code=400, detail=f"rollback error: {e}")
