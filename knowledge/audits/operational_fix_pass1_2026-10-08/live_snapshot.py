"""Bounded read-only delivery observer; never records credentials/config bodies."""
import datetime
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:5500/api/v2"

def get(path):
    start = time.perf_counter()
    with urllib.request.urlopen(BASE + path, timeout=45) as response:
        raw = response.read()
        return json.loads(raw), {"status": response.status, "bytes": len(raw),
                                 "wall_ms": round((time.perf_counter()-start)*1000, 3)}

def digest(raw):
    return hashlib.sha256(raw).hexdigest()

def snapshot():
    result = {"timestamp_utc": datetime.datetime.now(datetime.timezone.utc).isoformat()}
    subscription, _ = get("/subscription")
    managed = subscription["data"]["subscription"]["provider_managed"]
    result["provider_metrics"] = managed["metrics"]
    result["provider_bindings"] = [{k: b.get(k) for k in (
        "source_ref", "enabled", "configured", "current_member_id", "applied_member_id",
        "observed_protocol", "applied_protocol", "binding_revision", "applied_revision",
        "allow_automatic_member_switch", "last_outcome")} for b in managed["bindings"]]
    result["effective_override"] = managed.get("effective_override")
    connection = sqlite3.connect("file:/var/lib/fwrouter-v2/fwrouter.db?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    result["routing"] = dict(connection.execute("SELECT desired_mode,applied_mode,server_mode,active_auto_server_id,apply_state FROM routing_global_state WHERE id=1").fetchone())
    result["intent"] = {r["key"]: json.loads(r["value_json"]) for r in connection.execute("SELECT key,value_json FROM settings WHERE key IN ('routing.auto_selection_revision','routing.auto_selection_provenance','vpn_auto_exclusive_source_ref')")}
    result["active_jobs"] = [dict(r) for r in connection.execute("SELECT job_id,job_type,status FROM jobs WHERE status IN ('queued','running','pending')")]
    connection.close()
    result["http"] = {}
    for path in ("/health", "/ui/router-summary", "/selector/vpn-auto/state", "/xray"):
        body, timing = get(path)
        result["http"][path] = {**timing, "ok": body.get("ok")}
        if path == "/health": result["health"] = body["data"]
        if path == "/selector/vpn-auto/state":
            state = body["data"]["vpn_auto"]
            result["selector"] = {k: state.get(k) for k in ("auto_selectable_candidate_ids", "active_auto_server_id", "active_auto_target_valid", "config_consistent", "runtime_state")}
            result["selector"]["runtime_now"] = state.get("selector_runtime", {}).get("vpn_auto_now")
        if path == "/xray":
            state = body["data"]["xray"]
            result["xray"] = {"runtime_state": state.get("runtime_state"), "egress_state": state.get("egress", {}).get("state"), "bindings": {k: state.get("details", {}).get("bindings", {}).get(k) for k in ("bindings_count", "applied_count", "verified_count", "handoff_count")}}
    result["summary_serial"] = [get("/system/summary")[1] for _ in range(3)]
    result["artifacts"] = {}
    for label, host, container, mounted in (
        ("mihomo", "/var/lib/fwrouter-v2/generated/mihomo/config.yaml", "fwrouter-mihomo", "/config/config.yaml"),
        ("xray", "/var/lib/fwrouter-v2/xray/config.json", "fwrouter-xray", "/etc/xray/config.json"),
    ):
        raw = Path(host).read_bytes()
        active = subprocess.run(["docker", "exec", container, "cat", mounted], capture_output=True, timeout=20, check=True).stdout
        result["artifacts"][label] = {"sha256": digest(raw), "bytes": len(raw), "mounted_sha256": digest(active), "parity": raw == active}
    for label, filename in (("applied_nft", "/var/lib/fwrouter-v2/generated/dataplane/applied.nft"), ("applied_manifest", "/var/lib/fwrouter-v2/generated/dataplane/applied-manifest.json")):
        result["artifacts"][label] = {"sha256": digest(Path(filename).read_bytes())}
    bindings = json.loads(Path("/var/lib/fwrouter-v2/xray/fwrouter-bindings.json").read_text())
    def strip_receipts(value):
        if isinstance(value, dict): return {k: strip_receipts(v) for k,v in value.items() if k not in ("generated_at", "applied_at")}
        if isinstance(value, list): return [strip_receipts(v) for v in value]
        return value
    result["binding_semantic_sha256"] = digest(json.dumps(strip_receipts(bindings), sort_keys=True).encode())
    result["failed_units"] = subprocess.run(["systemctl", "--failed", "--no-legend", "--plain"], capture_output=True, text=True, check=True).stdout.splitlines()
    result["api_pid"] = subprocess.run(["systemctl", "show", "fwrouter-api", "-p", "MainPID", "--value"], capture_output=True, text=True, check=True).stdout.strip()
    return result

if __name__ == "__main__":
    destination = Path(sys.argv[1])
    result = snapshot()
    destination.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({"evidence": str(destination), "health": result["health"]["status"], "revision": result["intent"].get("routing.auto_selection_revision"), "active_jobs": len(result["active_jobs"]), "parity": {k:v["parity"] for k,v in result["artifacts"].items() if "parity" in v}}, ensure_ascii=False))
