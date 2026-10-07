#!/usr/bin/env python3
"""Isolated control-plane microbenchmarks for FWRouter baseline 65c3024.

Run only under the audit flock and FWRouter's installed venv. Every path is
redirected to one temporary root before product modules import. Xray/Docker,
Mihomo, native, socket, systemctl, and provider boundaries are faked/denied.
The output contains counts and hashes only; no SQL text or client identities.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import socket
import shutil
import statistics
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent
REPO = Path(__file__).resolve().parents[3]
TEMP = Path(tempfile.mkdtemp(prefix="fwrouter-operational-audit-owned-")).resolve()
os.environ["FWROUTER_STATE_DIR"] = str(TEMP / "state")
os.environ["FWROUTER_ENVIRONMENT"] = "test"
os.environ["FWROUTER_XRAY_PUBLIC_HOST"] = "xray.example.test"
os.environ["FWROUTER_JOB_RUN_NOW_WAIT_TIMEOUT_SECONDS"] = "1"
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"

from fwrouter_api.core.config import Settings, get_settings  # noqa: E402
from fwrouter_api.core.paths import FWRouterPaths  # noqa: E402
Settings.model_config["env_file"] = None
Settings.paths = property(lambda _self: FWRouterPaths(
    etc_dir=TEMP / "etc", state_dir=TEMP / "state", log_dir=TEMP / "logs", run_dir=TEMP / "run"
))
get_settings.cache_clear()
settings = get_settings()
for field in ("etc_dir", "state_dir", "log_dir", "run_dir", "db_path", "rules_dir", "generated_dir", "jobs_dir", "cache_dir", "runtime_state_dir", "operational_log_dir", "technical_log_dir", "operational_events_path"):
    path = Path(getattr(settings.paths, field)).resolve()
    if not path.is_relative_to(TEMP):
        raise RuntimeError(f"unsafe test path escaped owned root: {field}")

from fwrouter_api.db import connection as db_connection  # noqa: E402
from fwrouter_api.db.connection import db_session, initialize_database  # noqa: E402
initialize_database()

_sql_lock = threading.Lock()
_sql_current: dict[str, int] = {}
_connection_count = 0
_sql_total: dict[str, int] = {}
_network_attempts = 0
_original_connect = db_connection.connect


def _kind(statement: str) -> str:
    word = statement.lstrip().split(None, 1)[0].split("(", 1)[0].upper() if statement.strip() else "EMPTY"
    return word if word in {"SELECT", "INSERT", "UPDATE", "DELETE", "REPLACE", "PRAGMA", "BEGIN", "COMMIT", "ROLLBACK", "CREATE", "ALTER", "DROP", "WITH"} else "OTHER"


def _traced_connect():
    global _connection_count
    connection = _original_connect()
    with _sql_lock:
        _connection_count += 1
    def trace(statement: str) -> None:
        kind = _kind(statement)
        with _sql_lock:
            _sql_current[kind] = _sql_current.get(kind, 0) + 1
            _sql_total[kind] = _sql_total.get(kind, 0) + 1
    connection.set_trace_callback(trace)
    return connection


db_connection.connect = _traced_connect
DB_PATH = settings.paths.db_path
DB_WAL = Path(f"{DB_PATH}-wal")
DB_SHM = Path(f"{DB_PATH}-shm")

def reset_counts() -> None:
    global _connection_count
    with _sql_lock:
        _sql_current.clear()
        _connection_count = 0

def sql_snapshot() -> dict:
    with _sql_lock:
        return {"connect_calls": _connection_count, "statement_kinds": dict(_sql_current)}

def ensure_subprocess_denied() -> None:
    def denied(*_args, **_kwargs):
        raise RuntimeError("isolated audit blocked subprocess execution")
    subprocess.run = denied
    subprocess.Popen = denied
    os.system = denied
    def deny_network(*_args, **_kwargs):
        global _network_attempts
        _network_attempts += 1
        raise RuntimeError("isolated audit blocked network access")
    socket.create_connection = deny_network
    socket.socket.connect = deny_network


def import_xray_test_helpers():
    path = REPO / "backend/tests/test_xray.py"
    spec = importlib.util.spec_from_file_location("operational_audit_xray_fixture_helpers", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load existing isolated Xray fixture helpers")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def percentile95(values: list[float]) -> float:
    return max(values) if len(values) < 20 else statistics.quantiles(values, n=20)[18]


def measure(label: str, fn: Callable[[int], dict], *, repeats: int = 5,
            xray_config: Path | None = None, sample_ms: int = 25) -> dict:
    reset_counts()
    before_connections = _connection_count
    sample_paths = [DB_PATH, DB_WAL, DB_SHM]
    temp_paths = [settings.paths.operational_events_path]
    if xray_config:
        temp_paths.append(xray_config)
    from resource_sampler import sample_operation
    sampler = sample_operation(label, os.getpid(), sqlite_paths=sample_paths, temp_paths=temp_paths, interval_ms=sample_ms)
    wall_ms: list[float] = []
    statuses: list[str] = []
    response_bytes: list[int] = []
    results: list[dict] = []
    for idx in range(repeats):
        began = time.perf_counter()
        outcome = fn(idx)
        wall_ms.append((time.perf_counter() - began) * 1000)
        statuses.append(str(outcome.get("status", outcome.get("outcome", "completed"))))
        response_bytes.append(int(outcome.get("response_bytes") or 0))
        results.append({k: v for k, v in outcome.items() if k not in {"raw", "email", "client_id", "subject_id"}})
    resources = sampler.finish()
    counts = sql_snapshot()
    counts["statement_kinds"] = dict(counts["statement_kinds"])
    return {
        "operation": label, "sample_count": repeats,
        "wall_ms": {"min": round(min(wall_ms), 3), "p50": round(statistics.median(wall_ms), 3), "p95_small_sample_max": round(percentile95(wall_ms), 3), "max": round(max(wall_ms), 3)},
        "response_bytes_min": min(response_bytes, default=0), "response_bytes_max": max(response_bytes, default=0),
        "outcomes": statuses, "operation_summaries": results,
        "db": counts, "resources_for_repeated_batch": resources,
    }


ensure_subprocess_denied()
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from fwrouter_api.main import create_app  # noqa: E402
from fwrouter_api.jobs.manager import get_default_job_manager  # noqa: E402
from fwrouter_api.services.logs import list_operational_logs, write_operational_log, write_technical_log  # noqa: E402
from fwrouter_api.services.maintenance import OPERATIONAL_LOG_RETENTION_DAYS, TECHNICAL_LOG_RETENTION_DAYS
from fwrouter_api.services.logs_retention import cleanup_log_retention  # noqa: E402
from fwrouter_api.services.server_preferences import update_server_preferences  # noqa: E402
from fwrouter_api.services.provider_managed import save_provider_configuration  # noqa: E402
from fwrouter_api.services.traffic import cleanup_traffic_history  # noqa: E402
from fwrouter_api.services.subscription import _source_id  # noqa: E402
from fwrouter_api.db import provider_managed as provider_store  # noqa: E402
from fwrouter_api.services import server_preferences as prefs_module  # noqa: E402

helpers = import_xray_test_helpers()
patcher = pytest.MonkeyPatch()
helpers._configure_env(patcher, TEMP)
helpers._patch_runtime(patcher)
helpers._enable_xray_module()
xray_config, _compose_path = helpers._xray_paths()
helpers._write_xray_config(xray_config, [])
adapter = helpers._build_adapter(TEMP, runner=helpers._FakeRunner())
helpers._patch_xray_adapters(patcher, adapter)

# Deny background prewarm work while counting its dispatch. Isolated service
# timings below measure synchronous save/apply path, not asynchronous prewarm.
from fwrouter_api.services import ui_state_settings as ui_settings_service  # noqa: E402
prewarm_dispatches = {"count": 0}
ui_settings_service.prime_runtime_read_models_async = lambda **_kw: prewarm_dispatches.__setitem__("count", prewarm_dispatches["count"] + 1)

# Fixture inventory: two ordinary servers, one Xray client subscription source,
# and one provider binding. No account credential is ever inserted.
with db_session() as connection:
    for server_id in ("bench-a", "bench-b"):
        connection.execute("INSERT INTO servers(server_id,server_name,provider_name,inventory_state) VALUES(?,?, 'fixture','active')", (server_id, server_id))
        connection.execute("INSERT INTO server_preferences(server_id,vpn_auto,vpn_auto_priority,global_list) VALUES(?,1,1,1)", (server_id,))
        connection.execute("INSERT OR IGNORE INTO server_ping_state(server_id,status) VALUES(?,'success')", (server_id,))
    connection.execute("INSERT INTO routing_global_state(id,desired_mode,applied_mode,selective_default,server_mode,active_auto_server_id,apply_state) VALUES(1,'vpn','vpn','direct','auto','bench-a','clean')")
    sub_url = "https://fixture.invalid/source"
    source_ref = _source_id(sub_url)
    connection.execute("INSERT INTO subscription_state(id,url,status,metadata_json) VALUES(1,?,'success',?)", (sub_url, json.dumps({"subscriptions":{"items":[{"url":sub_url,"enabled":True}]}})))
    provider_store.save_binding(connection, source_ref, "stealthsurf", "1", "provider-fixture", "hysteria2", True)

app = create_app(enable_startup_tasks=False)
client = TestClient(app)
provider_call_counter = {"requests": 0, "discoveries": 0, "mutations": 0}
runtime_callback_counter = {"preference_reconcile": 0, "xray_runner_actions": adapter._runner.reload_count}
original_reselect = prefs_module._maybe_reselect_vpn_auto_after_membership_change
prefs_module._maybe_reselect_vpn_auto_after_membership_change = lambda **_kw: {"ok": True, "triggered": False, "status": "isolated_selector_stub"}

def fake_reconcile(*, enabled: bool):
    runtime_callback_counter["preference_reconcile"] += int(enabled)
    return {"ok": True, "synthetic_runtime_boundary": True}

def _job_result(response) -> tuple[dict, float, int]:
    body = response.json()
    job = body.get("data", {}).get("job") or {}
    job_id = job.get("job_id")
    started = time.perf_counter()
    last = job
    deadline = time.monotonic() + 5.0
    while job_id and last.get("status") not in {"success", "failed", "cancelled", "stale"}:
        if time.monotonic() >= deadline:
            raise RuntimeError("isolated audit job exceeded five-second deadline")
        current = client.get(f"/api/v2/jobs/{job_id}")
        last = current.json().get("data", {}).get("job") or {}
        if last.get("status") not in {"success", "failed", "cancelled", "stale"}:
            time.sleep(0.02)
    return last, (time.perf_counter() - started) * 1000, len(response.content)

def settings_save(i: int) -> dict:
    response = client.put("/api/v2/ui/settings/display", json={"show_inactive": bool(i % 2), "hidden_subject_ids": [f"fixture-{i}"]})
    return {"status": str(response.status_code), "response_bytes": len(response.content), "prewarm_dispatch": "async_noop_stub"}

def auto_membership(i: int) -> dict:
    value = bool(i % 2)
    result = update_server_preferences("bench-b", vpn_auto=value, reconcile_mihomo=True,
                                       reconcile_after_preferences=fake_reconcile, requested_by="audit-fixture")
    return {"status": "ok" if result.get("ok") else str(result.get("error_code")), "changed": result.get("changed"), "synthetic_runtime_callback": bool(result.get("mihomo_reconcile"))}

def provider_policy(i: int) -> dict:
    result = save_provider_configuration(source_ref, allow_automatic_member_switch=bool(i % 2))
    return {"status": "ok", "binding_revision": result.get("binding_revision"), "configured": result.get("configured"), "provider_call_count": 0}

def job_cycle(i: int) -> dict:
    response = client.post("/api/v2/jobs", json={"job_type":"noop","requested_by":"audit-fixture","run_now":True,"input_data":{"fixture":i}})
    job = response.json().get("data", {}).get("job") or {}
    return {"status": job.get("status", "unknown"), "response_bytes": len(response.content)}

def event_one(i: int) -> dict:
    event = write_operational_log(event_type="audit_fixture", message="isolated benchmark event", details={"sequence": i, "event_code":"audit.fixture"})
    return {"status": "written", "event_id_hash": hashlib.sha256(str(event.get("event_id") or "").encode()).hexdigest()[:12]}

def event_burst(_i: int) -> dict:
    for n in range(100):
        write_operational_log(event_type="audit_fixture_burst", message="isolated benchmark event", details={"sequence": n, "event_code":"audit.fixture.burst"})
    return {"status": "written_100"}

def event_read(_i: int) -> dict:
    rows = list_operational_logs(limit=100, event_type="audit_fixture_burst")
    return {"status": "read", "rows": len(rows)}

def technical_burst(_i: int) -> dict:
    for n in range(100):
        write_technical_log(component="audit_fixture", event_type="burst", message="isolated benchmark event", details={"sequence": n})
    return {"status": "appended_100"}

def retention(_i: int) -> dict:
    return cleanup_log_retention(operational_retention_days=OPERATIONAL_LOG_RETENTION_DAYS, technical_retention_days=TECHNICAL_LOG_RETENTION_DAYS, dry_run=False)

def db_transaction(i: int) -> dict:
    with db_session() as connection:
        connection.execute("INSERT INTO settings(key,value_json) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json", (f"audit.transaction.{i}", json.dumps({"n":i})))
    return {"status":"committed"}

from fwrouter_api.services.traffic import _retention_cutoff_month
with db_session() as connection:
    connection.execute("INSERT INTO subjects(subject_id,subject_type,stable_key,display_name,desired_mode,runtime_state,is_active,is_deleted,metadata_json) VALUES('audit-traffic','external_network_client','audit-traffic','fixture','direct','active',1,0,'{}')")
    connection.execute("INSERT INTO traffic_monthly(subject_id,period_month) VALUES('audit-traffic','2000-01')")

def traffic_cleanup(_i: int) -> dict:
    result = cleanup_traffic_history(dry_run=False)
    return {"status":"cleaned_fixture", "deleted_count": result.get("deleted_count")}

# Representative service lifecycle APIs. Generated configuration and all
# adapter calls occur in temp state; RealXrayAdapter's process runner is fake.
def xray_create(i: int) -> dict:
    started = time.perf_counter()
    response = client.post("/api/v2/xray/clients", json={"alias":f"fixture-{i}", "requested_by":"audit-fixture", "allow_blocked_egress":True})
    terminal, wait_ms, size = _job_result(response)
    job_result = terminal.get("result") if isinstance(terminal.get("result"), dict) else {}
    nested = job_result.get("xray_client") if isinstance(job_result.get("xray_client"), dict) else {}
    nested_result = nested.get("result") if isinstance(nested.get("result"), dict) else {}
    return {"status": terminal.get("status", "unknown"), "error_code": terminal.get("error_code") or job_result.get("error_code") or nested_result.get("error_code"), "accept_ms": round((time.perf_counter()-started)*1000-wait_ms,3), "terminal_ms": round((time.perf_counter()-started)*1000,3), "response_bytes": size, "job_id_hash": hashlib.sha256(str(terminal.get("job_id") or "").encode()).hexdigest()[:12]}

def xray_alias(i: int) -> dict:
    clients = client.get("/api/v2/xray/clients").json().get("data",{}).get("clients",[])
    if not clients:
        return {"status":"no_fixture_client"}
    response = client.patch(f"/api/v2/xray/clients/{clients[0]['client_id']}", json={"alias":f"renamed-{i}", "requested_by":"audit-fixture"})
    return {"status":"ok" if response.status_code == 200 and response.json().get("ok") else f"http_{response.status_code}", "response_bytes":len(response.content)}

def xray_delete(i: int) -> dict:
    clients = client.get("/api/v2/xray/clients").json().get("data",{}).get("clients",[])
    if not clients:
        return {"status":"no_fixture_client"}
    response = client.delete(f"/api/v2/xray/clients/{clients[0]['client_id']}", json={"requested_by":"audit-fixture"})
    terminal, wait_ms, size = _job_result(response)
    return {"status":terminal.get("status","unknown"), "terminal_ms":round(wait_ms,3), "response_bytes":size}

def xray_list(i: int) -> dict:
    response = client.get("/api/v2/xray/clients")
    payload = response.json().get("data", {}).get("clients", [])
    return {"status":str(response.status_code), "clients":len(payload), "response_bytes":len(response.content)}

def subject_route_intent(i: int) -> dict:
    from fwrouter_api.services.server_subject_overrides import set_subject_server_override
    with db_session() as connection:
        connection.execute("INSERT OR IGNORE INTO subjects(subject_id,subject_type,stable_key,display_name,desired_mode,runtime_state,is_active,is_deleted,metadata_json) VALUES('xray:audit-fixture','explicit_external_client','xray:audit-fixture','fixture','enabled','active',1,0,'{}')")
    result = set_subject_server_override("xray:audit-fixture", "bench-a", requested_by="audit-fixture")
    return {"status":"ok" if result.get("ok") else str(result.get("error_code")), "runtime_materialization":"not_supported_for_Xray_subject_by_current_contract"}

results = []
with client:
    results.append(measure("display_settings_save_api", settings_save, repeats=5))
    results.append(measure("ordinary_auto_membership_toggle_service_mocked_reconcile", auto_membership, repeats=5))
    results.append(measure("provider_auto_switch_policy_toggle_service", provider_policy, repeats=5))
    results.append(measure("job_create_run_complete_noop_api", job_cycle, repeats=5))
    results.append(measure("operational_log_single_db_jsonl_write", event_one, repeats=5))
    results.append(measure("operational_log_burst_100_db_jsonl", event_burst, repeats=1))
    results.append(measure("operational_log_filtered_read", event_read, repeats=5))
    results.append(measure("technical_log_burst_100_jsonl", technical_burst, repeats=1))
    old_timestamp = "2000-01-01T00:00:00+00:00"
    write_operational_log(event_type="audit_expired", message="expired fixture", timestamp=old_timestamp, details={"event_code":"audit.expired"})
    write_technical_log(component="audit_fixture", event_type="expired", message="expired fixture", timestamp=old_timestamp, details={"event_code":"audit.expired"})
    results.append(measure("log_retention_cleanup_temp_state", retention, repeats=3))
    results.append(measure("sqlite_small_transaction_commit", db_transaction, repeats=5))
    results.append(measure("traffic_history_temp_retention_cleanup", traffic_cleanup, repeats=3))
    reloads_before_create = adapter._runner.reload_count
    created = measure("xray_external_client_create_fake_native_runtime", xray_create, repeats=3, xray_config=xray_config)
    created["fake_xray_runner_action_counts"] = {action: sum(1 for name, _ in adapter._runner.calls if name == action) for action in sorted({name for name, _ in adapter._runner.calls})}
    created["fake_xray_reload_count_delta"] = adapter._runner.reload_count - reloads_before_create
    results.append(created)
    results.append(measure("xray_external_client_alias_edit_fake_runtime", xray_alias, repeats=3, xray_config=xray_config))
    results.append(measure("xray_external_client_list", xray_list, repeats=5, xray_config=xray_config))
    results.append(measure("xray_private_route_intent_only_temp_db", subject_route_intent, repeats=3, xray_config=xray_config))
    results.append(measure("xray_external_client_delete_fake_native_runtime", xray_delete, repeats=3, xray_config=xray_config))

provider = provider_call_counter
output = {
    "baseline_commit": "65c3024",
    "environment": {"python": os.sys.version.split()[0], "mode":"isolated_temp_sqlite", "startup_tasks":False, "native_docker_system_network_provider":"denied_or_fake", "sample_interval_ms":25},
    "temp_root_hash": hashlib.sha256(str(TEMP).encode()).hexdigest()[:12],
    "temp_root_removed_at_exit": True,
    "provider_calls": {"provider_operation_calls_scheduled": 0, "network_attempts_blocked_or_made": _network_attempts, "note":"policy-only path; no provider adapter invocation is in this call chain"},
    "runtime_callbacks": runtime_callback_counter,
    "async_prewarm_dispatches": prewarm_dispatches["count"],
    "operations": results,
    "limits": {"max_batch":100, "xray_lifecycle_repeats":3, "fast_operation_repeats":5, "no_production_state":True},
}
out = ROOT / "CONTROL_PLANE_METRICS.json"
out.write_text(json.dumps(output, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print(json.dumps({"output":str(out),"operations":len(results),"provider_calls":output["provider_calls"],"xray_reload_count":adapter._runner.reload_count,"temp_root_hash":output["temp_root_hash"]}, sort_keys=True))
client.close()
patcher.undo()
shutil.rmtree(TEMP, ignore_errors=True)
