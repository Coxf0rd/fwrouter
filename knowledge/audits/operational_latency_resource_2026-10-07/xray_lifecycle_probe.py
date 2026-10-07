#!/usr/bin/env python3
"""Bounded audit-only Xray lifecycle probe; temporary DB/config, fake runner."""
from __future__ import annotations
import hashlib, importlib.util, json, os, socket, subprocess, tempfile, time, shutil, traceback, atexit
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = Path(__file__).resolve().parents[3]
TEMP = Path(tempfile.mkdtemp(prefix="fwrouter-xray-audit-owned-")).resolve()
atexit.register(lambda: shutil.rmtree(TEMP, ignore_errors=True))
os.environ.update({"FWROUTER_STATE_DIR":str(TEMP/"state"), "FWROUTER_ENVIRONMENT":"test", "FWROUTER_XRAY_PUBLIC_HOST":"xray.example.test", "FWROUTER_JOB_RUN_NOW_WAIT_TIMEOUT_SECONDS":"2", "PYTHONDONTWRITEBYTECODE":"1"})
from fwrouter_api.core.config import Settings, get_settings
from fwrouter_api.core.paths import FWRouterPaths
Settings.model_config["env_file"] = None
Settings.paths = property(lambda _self: FWRouterPaths(etc_dir=TEMP/"etc", state_dir=TEMP/"state", log_dir=TEMP/"logs", run_dir=TEMP/"run"))
get_settings.cache_clear()
paths=get_settings().paths
for name in ("etc_dir","state_dir","log_dir","run_dir","db_path","generated_dir","jobs_dir","runtime_state_dir"):
    value=Path(getattr(paths,name)).resolve()
    if not value.is_relative_to(TEMP): raise RuntimeError(f"unsafe path {name}: outside isolated root")
from fwrouter_api.db.connection import db_session, initialize_database
initialize_database()
sampler_spec=importlib.util.spec_from_file_location("audit_resource_sampler", HERE/"resource_sampler.py")
sampler_module=importlib.util.module_from_spec(sampler_spec); sampler_spec.loader.exec_module(sampler_module)
spec=importlib.util.spec_from_file_location("audit_xray_helpers", REPO/"backend/tests/test_xray.py")
helpers=importlib.util.module_from_spec(spec); spec.loader.exec_module(helpers)
import pytest
patcher=pytest.MonkeyPatch(); helpers._configure_env(patcher,TEMP); helpers._patch_runtime(patcher); helpers._enable_xray_module()
config,_=helpers._xray_paths(); helpers._write_xray_config(config,[])
runner=helpers._FakeRunner(); adapter=helpers._build_adapter(TEMP,runner=runner); helpers._patch_xray_adapters(patcher,adapter)
# The canonical test helper does not patch the Xray status module's imported
# adapter. Bind it explicitly so preflight health remains behind the fake runner.
from fwrouter_api.services import xray_status as xray_status_service
xray_status_service.DEFAULT_XRAY_ADAPTER=adapter
# Runtime convergence in this lifecycle checks the selective-DNS contract.
# It is an external host probe, so supply a deterministic healthy fixture at
# that boundary rather than allowing the isolated job to inspect the host.
from fwrouter_api.services import dnsmasq as dnsmasq_service
dnsmasq_service._inspect_dnsmasq_selective_status_uncached=lambda: {"ok":True,"missing":[],"dns_capture_status":{"ok":True,"missing":[]},"nftset_probe_status":{"ok":True,"missing":[]}}

# Hard-deny external process/network paths. Record API name and stack function
# names only (never arguments, paths, payloads, or secrets) to diagnose fixture gaps.
blocked_attempts=[]
def denied(*_a,**_kw):
    frames=traceback.extract_stack(limit=8)
    blocked_attempts.append({"boundary":"blocked_external_call","callsite_functions":[f.name for f in frames[-5:-1]]})
    raise RuntimeError("external operation blocked by isolated Xray audit")
subprocess.run=subprocess.Popen=os.system=denied
socket.create_connection=denied
socket.socket.connect=denied

from fwrouter_api.main import create_app
from fastapi.testclient import TestClient
app=create_app(enable_startup_tasks=False)
def wait_job(client,job_id):
    deadline=time.monotonic()+4
    while time.monotonic()<deadline:
        response=client.get(f"/api/v2/jobs/{job_id}")
        job=response.json().get("data",{}).get("job",{})
        if job.get("status") in {"success","failed","cancelled","stale"}: return job
        time.sleep(.02)
    return job

def create(client,alias):
    sampler=sampler_module.sample_operation("xray_create",os.getpid(),sqlite_paths=[paths.db_path,Path(str(paths.db_path)+"-wal"),Path(str(paths.db_path)+"-shm")],temp_paths=[config],interval_ms=25)
    t=time.perf_counter(); response=client.post("/api/v2/xray/clients",json={"alias":alias,"requested_by":"audit-fixture","allow_blocked_egress":True})
    body=response.json(); job=body.get("data",{}).get("job",{})
    terminal=wait_job(client,job.get("job_id")) if job.get("job_id") else job
    nested=terminal.get("result",{}); xray=nested.get("xray_client",{}) if isinstance(nested,dict) else {}
    error_message=str(terminal.get("error_message") or "")[:500].replace(str(TEMP),"<TEMP>")
    value={"http":response.status_code,"wall_ms":round((time.perf_counter()-t)*1000,3),"status":terminal.get("status"),"job_error_code":terminal.get("error_code"),"terminal_error_message":error_message,"result_error_code":nested.get("error_code") if isinstance(nested,dict) else None,"message":(nested.get("result") or {}).get("message") if isinstance(nested,dict) and isinstance(nested.get("result"),dict) else None,"client_id":(xray.get("client") or {}).get("client_id"),"response_bytes":len(response.content)}
    value["resource_sample"]=sampler.finish(); return value

result={"source_commit":"65c302454038932021f25d2900b53fd0474e9db4","script_sha256":hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),"fixture_sha256":hashlib.sha256((REPO/"backend/tests/test_xray.py").read_bytes()).hexdigest(),"mode":"isolated temporary SQLite/config; fake Xray runner; network/subprocess denied","fixture_boundaries":["Xray adapter runner","Xray runtime health through same fake adapter","selective DNS host probe deterministic healthy result"],"temp_root_hash":hashlib.sha256(str(TEMP).encode()).hexdigest()[:12],"runner_actions":[]}
with TestClient(app) as client:
    first=create(client,"audit-single-0"); result["initial_create_reason"]=first
    ids=[]
    if first.get("status")=="success" and first.get("client_id"): ids.append(first["client_id"])
    # Continue only after successful admission; otherwise preserve exact stop reason.
    if ids:
        one=create(client,"audit-batch-1"); two=create(client,"audit-batch-2")
        result["batch_creates"]=[one,two]
        ids += [v["client_id"] for v in (one,two) if v.get("status")=="success" and v.get("client_id")]
        if ids:
            cid=ids[0]
            sampler=sampler_module.sample_operation("xray_alias_edit",os.getpid(),sqlite_paths=[paths.db_path],temp_paths=[config],interval_ms=25)
            t=time.perf_counter(); edit=client.patch(f"/api/v2/xray/clients/{cid}",json={"alias":"audit-renamed","requested_by":"audit-fixture"})
            edit_body=edit.json(); edit_error=edit_body.get("error") or {}
            result["alias_edit"]={"http":edit.status_code,"wall_ms":round((time.perf_counter()-t)*1000,3),"ok":edit_body.get("ok"),"error_code":edit_error.get("code") if isinstance(edit_error,dict) else None,"resource_sample":sampler.finish()}
            result["deletes"]=[]
            for cid in ids:
                sampler=sampler_module.sample_operation("xray_delete",os.getpid(),sqlite_paths=[paths.db_path],temp_paths=[config],interval_ms=25)
                t=time.perf_counter(); response=client.request("DELETE",f"/api/v2/xray/clients/{cid}",json={"requested_by":"audit-fixture"}); body=response.json(); job=body.get("data",{}).get("job",{}); terminal=wait_job(client,job.get("job_id")) if job.get("job_id") else job
                result["deletes"].append({"http":response.status_code,"wall_ms":round((time.perf_counter()-t)*1000,3),"status":terminal.get("status"),"error_code":terminal.get("error_code"),"resource_sample":sampler.finish()})
            result["end_list_count"]=len(client.get("/api/v2/xray/clients").json().get("data",{}).get("clients",[]))
result["runner_actions"]=[name for name,_ in runner.calls]
result["runner_reload_count"]=runner.reload_count
result["external_attempts"]="denied by wrappers; no allowed sockets/subprocess"
result["blocked_callsite_functions"]=blocked_attempts
# Observer-only baseline: three bounded sampler lifecycles around a no-op.
cal=[]
for _ in range(3):
    t=time.perf_counter(); sampler=sampler_module.sample_operation("observer_noop_calibration",os.getpid(),sqlite_paths=[paths.db_path],interval_ms=25); _=None; sample=sampler.finish()
    cal.append({"observer_wall_ms":round((time.perf_counter()-t)*1000,3),"sample_count":sample["sample_count"],"reported_duration_ms":sample["duration_ms"],"process_cpu_ms":sample["process_cpu_ms"],"rss_delta_kib":sample["sampled_peak_tree_rss_delta_kib"]})
result["observer_noop_calibration"]=cal
result["temp_root_removed"]=True
def scrub(value):
    if isinstance(value,dict):
        out={key:scrub(item) for key,item in value.items() if key not in {"client_id","client_uuid","email","subject_id"}}
        if value.get("client_id"):
            out["client_id_hash"]=hashlib.sha256(str(value["client_id"]).encode()).hexdigest()[:12]
        return out
    if isinstance(value,list): return [scrub(item) for item in value]
    return value
result=scrub(result)
(HERE/"XRAY_LIFECYCLE_PROBE.json").write_text(json.dumps(result,sort_keys=True,indent=2)+"\n",encoding="utf-8")
shutil.rmtree(TEMP)
print(json.dumps(result,sort_keys=True))
