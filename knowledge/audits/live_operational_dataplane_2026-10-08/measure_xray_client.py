#!/usr/bin/env python3
"""Run a bounded local Xray client check for the dedicated audit account."""
from __future__ import annotations
import datetime as dt
import hashlib
import json
import os
import pathlib
import socket
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import uuid

API = "http://127.0.0.1:5000/api/v2"
PRIVATE = pathlib.Path("/run/fwrouter-v2/live-bench-xray/client-private.json")
XRAY_CONFIG = pathlib.Path("/var/lib/fwrouter-v2/xray/config.json")
IMAGE = "sha256:60c138250e2dca6e54259a1333188692fdf2560cb2dae7f6610b3ee92f6c3ff1"
ROOT = pathlib.Path(__file__).resolve().parent
PUBLIC_ONLY = "--public-only" in sys.argv[1:]
OUT = ROOT / ("xray_fixed_route_probe.json" if PUBLIC_ONLY else "xray_client_measurements.json")
TARGETS = ["https://www.gstatic.com/generate_204"]


def allocate_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def api_text(client_id):
    url = API + "/xray/clients/" + urllib.parse.quote(client_id, safe="") + "/subscription.txt?base64_encode=false&format=vless"
    req = urllib.request.Request(url, headers={"Accept": "text/plain", "User-Agent": "xray-live-bench"})
    with urllib.request.urlopen(req, timeout=8) as r:
        text = r.read(32768).decode("utf-8", "strict")
        status = r.status
    uris = [line.strip() for line in text.splitlines() if line.strip().startswith("vless://")]
    if status != 200 or len(uris) != 1:
        raise RuntimeError("subscription_profile_shape")
    return uris[0]


def uri_parts(uri):
    parsed = urllib.parse.urlsplit(uri)
    q = urllib.parse.parse_qs(parsed.query)
    def one(k, default=""):
        return (q.get(k) or [default])[0]
    return {"uuid": urllib.parse.unquote(parsed.username or ""), "host": parsed.hostname or "",
            "port": parsed.port or 443, "security": one("security", "tls"),
            "network": one("type", "ws"), "sni": one("sni", parsed.hostname or ""),
            "ws_host": one("host", parsed.hostname or ""), "path": one("path", "/vless"),
            "alpn": one("alpn", "http/1.1"), "fingerprint": one("fp", "chrome")}


def outbound(tag, p, *, raw=False, raw_ws=None):
    stream = {"network": "ws", "security": "none" if raw else "tls"}
    if raw:
        stream["wsSettings"] = raw_ws or {"path": p["path"]}
    else:
        stream["tlsSettings"] = {"serverName": p["sni"], "alpn": [p["alpn"]], "fingerprint": p["fingerprint"], "allowInsecure": False}
        stream["wsSettings"] = {"path": p["path"], "headers": {"Host": p["ws_host"]}}
    return {"tag": tag, "protocol": "vless", "settings": {"vnext": [{"address": p["host"], "port": p["port"],
            "users": [{"id": p["uuid"], "encryption": "none"}]}]}, "streamSettings": stream}


def curl_one(port, target):
    fmt = '{"dns_s":"%{time_namelookup}","connect_s":"%{time_connect}","tls_s":"%{time_appconnect}","ttfb_s":"%{time_starttransfer}","total_s":"%{time_total}","bytes":"%{size_download}","status":"%{http_code}"}'
    cmd = ["curl", "--silent", "--show-error", "--max-time", "9", "--connect-timeout", "4", "--proxy", f"socks5h://127.0.0.1:{port}", "--noproxy", "", "--output", "/dev/null", "--write-out", fmt, target]
    started = time.monotonic()
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=11, env={**os.environ, "NO_PROXY": "", "no_proxy": ""})
    try:
        values = json.loads(p.stdout)
    except Exception:
        values = {}
    for k in ("dns_s", "connect_s", "tls_s", "ttfb_s", "total_s"):
        try: values[k] = float(values[k])
        except (KeyError, ValueError, TypeError): values[k] = None
    try: values["bytes"] = int(values["bytes"])
    except (ValueError, TypeError): values["bytes"] = None
    values.update({"target": urllib.parse.urlsplit(target).hostname, "curl_exit": p.returncode,
                   "error_class": None if p.returncode == 0 else ("timeout" if p.returncode == 28 else f"curl_exit_{p.returncode}"),
                   "wall_s": round(time.monotonic() - started, 6)})
    return values


def docker_call(args, timeout=8):
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)


def main():
    started_at_utc = dt.datetime.now(dt.timezone.utc).isoformat()
    private = json.loads(PRIVATE.read_text())
    client_id = str(private.get("client_id") or "")
    if not client_id:
        raise SystemExit("private_client_ref_unavailable")
    private_ref = hashlib.sha256(client_id.encode()).hexdigest()[:12]
    sub_uri = api_text(client_id)
    public = uri_parts(sub_uri)
    if public["security"] != "tls" or public["network"] != "ws" or not public["uuid"] or not public["host"]:
        raise SystemExit("public_profile_transport_not_supported")
    xcfg = json.loads(XRAY_CONFIG.read_text())
    inbound = next(x for x in xcfg.get("inbounds", []) if int(x.get("port") or 0) == 5300 and x.get("protocol") == "vless")
    private_email = str(private.get("client_email_private") or "")
    matched_user = any(str(c.get("email") or "") == private_email and str(c.get("id") or "") == public["uuid"]
                       for c in (inbound.get("settings") or {}).get("clients", []))
    if not matched_user:
        raise SystemExit("dedicated_client_profile_identity_mismatch")
    inbound_stream = inbound.get("streamSettings") or {}
    raw_ws = inbound_stream.get("wsSettings") or {"path": public["path"]}
    raw_host = (raw_ws.get("headers") or {}).get("Host")
    raw = dict(public)
    raw.update({"host": "172.23.0.3", "port": 5300})
    if raw_host:
        raw_ws = {**raw_ws, "headers": {"Host": raw_host}}
    ports = {"raw_ws": allocate_port(), "public_tls_wss": allocate_port()}
    cfg = {
        "log": {"loglevel": "none"},
        "inbounds": [
            {"tag": "socks-raw", "listen": "127.0.0.1", "port": ports["raw_ws"], "protocol": "socks", "settings": {"auth": "noauth", "udp": False}},
            {"tag": "socks-public", "listen": "127.0.0.1", "port": ports["public_tls_wss"], "protocol": "socks", "settings": {"auth": "noauth", "udp": False}},
        ],
        "outbounds": [outbound("test-raw", raw, raw=True, raw_ws=raw_ws), outbound("test-public", public)],
        "routing": {"rules": [
            {"type": "field", "inboundTag": ["socks-raw"], "outboundTag": "test-raw"},
            {"type": "field", "inboundTag": ["socks-public"], "outboundTag": "test-public"},
        ]},
    }
    if PUBLIC_ONLY:
        cfg["inbounds"] = [cfg["inbounds"][1]]
        cfg["outbounds"] = [cfg["outbounds"][1]]
        cfg["routing"]["rules"] = [cfg["routing"]["rules"][1]]
    tmpdir = pathlib.Path(tempfile.mkdtemp(prefix="fwrouter-live-xray-"))
    os.chmod(tmpdir, 0o700)
    config_path = tmpdir / "client.json"
    config_path.write_text(json.dumps(cfg))
    os.chmod(config_path, 0o600)
    name = "fwrouter-livebench-xray-" + uuid.uuid4().hex[:10]
    selected_ports = {"public_tls_wss": ports["public_tls_wss"]} if PUBLIC_ONLY else ports
    container_id = None
    result = {"client_ref": private_ref, "xray_image_pinned": IMAGE, "xray_client_version": "26.2.6",
              "public_profile_security": "TLS verified; allowInsecure=false", "raw_profile_endpoint_class": "existing_Xray_container_private_bridge",
              "public_profile_endpoint_class": "configured_public_TLS_WSS", "socks_listener_bind": "127.0.0.1",
              "resource_caps": {"cpu_cores": 0.5, "memory_bytes": 134217728, "pids": 64}, "profiles": {}}
    try:
        created = docker_call(["create", "--name", name, "--network", "host", "--cpus", "0.5", "--memory", "134217728",
            "--pids-limit", "64", "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--tmpfs", "/tmp:rw,nosuid,nodev,noexec,size=8m,mode=700", "--mount", f"type=bind,src={config_path},dst=/bench/client.json,readonly",
            IMAGE, "/usr/bin/xray", "run", "-c", "/bench/client.json"])
        if created.returncode != 0:
            raise RuntimeError("owned_container_create_failed")
        container_id = created.stdout.strip()
        if len(container_id) < 12:
            raise RuntimeError("owned_container_id_missing")
        started = docker_call(["start", container_id])
        if started.returncode != 0:
            raise RuntimeError("owned_container_start_failed")
        for _ in range(60):
            state = docker_call(["inspect", "--format", "{{.State.Running}}", container_id]).stdout.strip()
            if state != "true":
                raise RuntimeError("owned_container_exited_before_ready")
            ready = True
            for port in selected_ports.values():
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=.15): pass
                except OSError:
                    ready = False
            if ready: break
            time.sleep(.1)
        else:
            raise RuntimeError("loopback_socks_readiness_timeout")
        for profile, port in selected_ports.items():
            samples = []
            for target in TARGETS:
                samples.append(curl_one(port, target))
            result["profiles"][profile] = {"samples": samples, "successes": sum(x["curl_exit"] == 0 for x in samples),
                "errors": sum(x["curl_exit"] != 0 for x in samples), "download_bytes_total": sum(x.get("bytes") or 0 for x in samples)}
        result["container_ready"] = True
        result["container_cleanup"] = "pending_finally"
    except Exception as exc:
        result["harness_error_class"] = str(exc) if str(exc).startswith("owned_") or str(exc).startswith("loopback_") else type(exc).__name__
    finally:
        if container_id:
            docker_call(["stop", "--time", "1", container_id], timeout=5)
            removed = docker_call(["rm", container_id], timeout=5)
            result["container_cleanup"] = "removed" if removed.returncode == 0 else "remove_failed"
        for path in tmpdir.iterdir():
            if path.is_file(): path.unlink()
        tmpdir.rmdir()
    result["started_at_utc"] = started_at_utc
    result["finished_at_utc"] = dt.datetime.now(dt.timezone.utc).isoformat()
    OUT.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"output": str(OUT), "client_ref": private_ref,
        "route_context": "fixed_override" if PUBLIC_ONLY else "default_created_route",
        "cleanup": result.get("container_cleanup"), "harness_error_class": result.get("harness_error_class"),
        "profiles": {k: {"successes": v["successes"], "errors": v["errors"], "statuses": [x.get("status") for x in v["samples"]], "latencies_s": [x.get("total_s") for x in v["samples"]]} for k,v in result.get("profiles",{}).items()}}, indent=2))


if __name__ == "__main__":
    main()
