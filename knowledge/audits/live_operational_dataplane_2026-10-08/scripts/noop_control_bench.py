#!/usr/bin/env python3
"""Bounded live identical Settings saves and same-false provider policy call."""
from __future__ import annotations
import hashlib, json, time, urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE = "http://127.0.0.1:5000/api/v2"
ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "NOOP_CONTROL_RAW.json"
SOURCE_REF = "src:d5d22aee8008a59b3b53b0989e8bc48dccc97af7f6e54f9725b0ce41162c895b"

def request(method, path, payload=None):
    body = None if payload is None else json.dumps(payload, separators=(",", ":")).encode()
    req = urllib.request.Request(BASE + path, data=body, method=method,
        headers={"Accept":"application/json", "Content-Type":"application/json"})
    start = time.perf_counter_ns()
    with urllib.request.urlopen(req, timeout=20) as response:
        raw = response.read()
        status = response.status
    return status, json.loads(raw), round((time.perf_counter_ns()-start)/1e6, 3)

def main():
    st, response, read_ms = request("GET", "/ui/settings/display")
    if st != 200 or not response.get("ok"):
        raise SystemExit("settings_read_failed")
    settings = response["data"]["display_settings"]
    # This endpoint excludes external connection credentials; custom systems are
    # read-only projections and are not accepted as a display-setting field.
    payload = {key: settings[key] for key in (
        "system_visibility", "show_inactive", "show_internal_vless",
        "hidden_subject_ids", "subject_traffic_preferences") if key in settings}
    stable_hash = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    saves = []
    for index in range(3):
        status, result, latency = request("PUT", "/ui/settings/display", payload)
        if status != 200 or not result.get("ok"):
            raise SystemExit(f"settings_save_failed_{index+1}")
        saves.append({"iteration": index+1, "http_status":status, "latency_ms":latency})
    status, result, latency = request("POST", f"/subscription/sources/{SOURCE_REF}/provider/configuration",
        {"allow_automatic_member_switch": False})
    binding = result.get("data", {}).get("binding", {}) if isinstance(result, dict) else {}
    if status != 200 or not result.get("ok") or binding.get("allow_automatic_member_switch") is not False:
        raise SystemExit("provider_same_false_noop_failed")
    out = {
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_baseline":"6ba7f6f",
        "classification":"measured_live_same_value_control_mutations",
        "settings_get_ms":read_ms,
        "settings_payload_sha256":stable_hash,
        "settings_payload_fields":sorted(payload),
        "settings_saves":saves,
        "provider_policy":{
            "method":"POST", "path":"/subscription/sources/{opaque-source-ref}/provider/configuration",
            "request_shape":"allow_automatic_member_switch=false only",
            "source_ref_sha256":hashlib.sha256(SOURCE_REF.encode()).hexdigest(),
            "http_status":status, "latency_ms":latency,
            "readback_same_false":binding.get("allow_automatic_member_switch") is False,
            "binding_revision":binding.get("binding_revision"),
        },
        "limitations":"API response latency only; no per-request DB/CPU instrumentation. Settings values and provider/source identity omitted. Same-value operations do not measure changed intent/apply paths.",
    }
    RAW.write_text(json.dumps(out, ensure_ascii=False, indent=2)+"\n")
    print(json.dumps(out, ensure_ascii=False, indent=2))

if __name__ == "__main__": main()
