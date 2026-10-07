#!/usr/bin/env python3
"""Exactly-once live Xray test-client create; redacts identity and response payloads."""
from __future__ import annotations
import hashlib, json, os, random, re, resource, subprocess, threading, time, urllib.error, urllib.request, uuid
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

BASE = os.environ.get("FWROUTER_BENCH_BASE", "http://127.0.0.1:5000/api/v2")
STATE_DIR = Path("/run/fwrouter-v2/live-bench-xray")
SECRET_FILE = STATE_DIR / "client-private.json"
REPORT = Path(__file__).resolve().parents[1] / "XRAY_CREATE_RAW.json"
CG = Path("/sys/fs/cgroup/system.slice/fwrouter-api.service")
POLL_INTERVAL_S = 0.5
JOB_DEADLINE_S = 180
DOCKER_EVENTS_CMD = ["docker", "events", "--filter", "type=container", "--format", "{{json .}}"]

class SafeAbort(Exception): pass

def api(method: str, path: str, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method, headers={"Accept":"application/json", "Content-Type":"application/json"})
    start = time.perf_counter_ns()
    with urllib.request.urlopen(req, timeout=20) as resp:
        raw = resp.read()
        status = resp.status
    return status, json.loads(raw), (time.perf_counter_ns()-start)/1e6

def canon_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",",":"), ensure_ascii=False).encode()).hexdigest()

def client_id_of(client):
    return str(client.get("client_id") or client.get("client_uuid") or client.get("id") or "")

def ident_digest(clients, exclude_id=None):
    rows=[]
    for c in clients:
        cid=client_id_of(c)
        if exclude_id and cid == exclude_id: continue
        rows.append({"client_id":cid,"email":str(c.get("email") or ""),"alias":str(c.get("alias") or ""),"enabled":bool(c.get("enabled",True))})
    rows.sort(key=lambda x: (x["client_id"],x["email"],x["alias"],x["enabled"]))
    return len(rows), canon_hash(rows)

def _contains_test(value, test_id, test_email):
    if isinstance(value,str): return value == test_id or (bool(test_email) and value.lower() == test_email.lower()) or value == f"xray:{test_id}"
    return False

def prune(value, test_id, test_email):
    if _contains_test(value,test_id,test_email): return None
    if isinstance(value,list):
        kept=[prune(v,test_id,test_email) for v in value]
        return [v for v in kept if v is not None]
    if isinstance(value,dict):
        # Drop a row whose primary identity is the new test principal.
        for k in ("client_id","client_uuid","uuid","id","email","client_email","subject_id"):
            if k in value and _contains_test(value[k],test_id,test_email): return None
        out={}
        for k,v in value.items():
            pv=prune(v,test_id,test_email)
            if pv is not None: out[k]=pv
        return out
    return value

def binding_semantics(state, test_id=None, test_email=None):
    data=json.loads(json.dumps(state))
    if test_id:
        data=prune(data,test_id,test_email)
    if not isinstance(data,dict): return data
    # Volatile generation/receipt timestamps and aggregate counts are not binding semantics.
    def strip_receipts(value):
        if isinstance(value,dict):
            return {k:strip_receipts(v) for k,v in value.items() if k not in {"generated_at","created_at","updated_at","applied_at","verified_at","timestamp","receipt"} and not k.endswith("_count")}
        if isinstance(value,list): return [strip_receipts(v) for v in value]
        return value
    data=strip_receipts(data)
    for name in ("bindings","client_modes","handoff_listeners"):
        rows=data.get(name)
        if isinstance(rows,list):
            for row in rows:
                if isinstance(row,dict):
                    for k in ("subject_ids","client_emails","client_uuids","client_ids"):
                        if isinstance(row.get(k),list): row[k]=sorted(row[k],key=lambda x: str(x))
            data[name]=sorted(rows,key=lambda x: json.dumps(x,sort_keys=True,separators=(",",":")))
    return data

def load_local_state():
    # Imported in this root-owned one-shot process only; settings never printed.
    import sys
    sys.path.insert(0,"/opt/fwrouter-api")
    from fwrouter_api.core.config import get_settings
    from fwrouter_api.adapters.xray import DEFAULT_XRAY_ADAPTER
    paths=get_settings().paths
    db_uri=paths.db_path.resolve().as_uri()+"?mode=ro"
    conn=sqlite3.connect(db_uri,uri=True,timeout=5)
    try: active_jobs=dict(conn.execute("SELECT status, COUNT(*) FROM jobs WHERE status IN ('queued','running') GROUP BY status").fetchall())
    finally: conn.close()
    bind_path=paths.state_dir/"xray"/"fwrouter-bindings.json"
    cfg_path=Path(DEFAULT_XRAY_ADAPTER.config_path)
    bindings=json.loads(bind_path.read_text()) if bind_path.exists() else {"bindings":[],"client_modes":[],"handoff_listeners":[]}
    config=json.loads(cfg_path.read_text())
    return bind_path,cfg_path,bindings,config,active_jobs

def config_semantics(config,test_id=None,test_email=None):
    result=json.loads(json.dumps(config))
    if test_id:
        # Filter only test identity and references. Keep all other ordered config as-is.
        result=prune(result,test_id,test_email)
    return result

def _cgroup_metrics(groups):
    cpu=memory=0; io={}; found=0
    for cg in groups:
        try:
            base=Path("/sys/fs/cgroup")/str(cg).lstrip("/")
            cpu+=int(next(x.split()[1] for x in (base/"cpu.stat").read_text().splitlines() if x.startswith("usage_usec ")))
            memory+=int((base/"memory.current").read_text()); found+=1
            for line in (base/"io.stat").read_text().splitlines():
                for p in line.split()[1:]:
                    k,v=p.split("="); io[k]=io.get(k,0)+int(v)
        except (OSError,StopIteration,ValueError): continue
    return {"cpu_usec":cpu if found else None,"memory_current":memory if found else None,"io":io,"cgroups":found,"signature":hashlib.sha256("\n".join(sorted(groups)).encode()).hexdigest()[:16] if found else None}

def proc_sample(runtime_pids,runtime_groups):
    out={"api_cpu_usec":None,"api_memory_current":None,"api_io":{},"xray_processes":0,"xray_pid_signature":None,"xray_cpu_ticks":0,"xray_rss_bytes":0,"xray_io_write_bytes":0,"xray_container":_cgroup_metrics(runtime_groups.get("xray",[])),"mihomo_processes":0,"mihomo_cpu_ticks":0,"mihomo_rss_bytes":0,"mihomo_io_write_bytes":0,"mihomo_container":_cgroup_metrics(runtime_groups.get("mihomo",[])),"wall_ns":time.monotonic_ns()}
    try:
        out["api_cpu_usec"]=int(next(x.split()[1] for x in (CG/"cpu.stat").read_text().splitlines() if x.startswith("usage_usec ")))
        out["api_memory_current"]=int((CG/"memory.current").read_text())
        for line in (CG/"io.stat").read_text().splitlines():
            parts=line.split();
            for p in parts[1:]:
                k,v=p.split("="); out["api_io"][k]=out["api_io"].get(k,0)+int(v)
    except (OSError,StopIteration,ValueError): pass
    # Runtime PID discovery is throttled to 1 s; target process counters are sampled every 100 ms.
    pids=list(runtime_pids)
    seen=set()
    for pid in pids:
        if pid in seen: continue
        seen.add(pid)
        try:
            comm=Path(f"/proc/{pid}/comm").read_text().strip().lower()
            if comm not in {"xray","mihomo","clash-meta"}: continue
            stat=Path(f"/proc/{pid}/stat").read_text().rsplit(") ",1)[1].split()
            ticks=int(stat[11])+int(stat[12]); rss=int(Path(f"/proc/{pid}/statm").read_text().split()[1])*os.sysconf("SC_PAGE_SIZE")
            io={}
            for line in Path(f"/proc/{pid}/io").read_text().splitlines():
                if line.startswith("write_bytes:"): io["write_bytes"]=int(line.split()[1])
            prefix="xray" if comm=="xray" else "mihomo"
            out[prefix+"_processes"]+=1; out[prefix+"_cpu_ticks"]+=ticks; out[prefix+"_rss_bytes"]+=rss; out[prefix+"_io_write_bytes"]+=io.get("write_bytes",0)
        except (OSError,ValueError,IndexError): continue
    xray_pid_values=sorted(int(pid) for pid in runtime_pids if Path(f"/proc/{pid}/comm").exists())
    out["xray_pid_signature"]=hashlib.sha256(",".join(map(str,xray_pid_values)).encode()).hexdigest()[:16] if xray_pid_values else None
    return out

class Sampler:
    def __init__(self): self.rows=[]; self.stop=threading.Event(); self.cost=[]; self.runtime_pids=[]; self.runtime_groups={"xray":[],"mihomo":[]}; self.last_scan=0.0
    def run(self):
        while not self.stop.is_set():
            t=time.perf_counter_ns()
            now=time.monotonic()
            if now-self.last_scan >= 1.0:
                found=[]; groups={"xray":set(),"mihomo":set()}
                try:
                    for item in Path("/proc").iterdir():
                        if item.name.isdigit():
                            try:
                                comm=Path(f"/proc/{item.name}/comm").read_text().strip().lower()
                                if comm in {"xray","mihomo","clash-meta"}:
                                    found.append(int(item.name)); prefix="xray" if comm=="xray" else "mihomo"
                                    for line in Path(f"/proc/{item.name}/cgroup").read_text().splitlines():
                                        if line.startswith("0::"):
                                            cg=line[3:].strip().lstrip("/")
                                            if cg and ".." not in Path(cg).parts: groups[prefix].add(cg)
                            except OSError: pass
                except OSError: pass
                self.runtime_pids=found; self.runtime_groups={k:sorted(v) for k,v in groups.items()}; self.last_scan=now
            self.rows.append(proc_sample(self.runtime_pids,self.runtime_groups)); self.cost.append(time.perf_counter_ns()-t); self.stop.wait(0.1)
    def summary(self):
        rows=self.rows
        if not rows: return {"samples":0}
        def peak_interval(samples, counter):
            values=[]
            for left,right in zip(samples,samples[1:]):
                if left.get("signature") and left.get("signature")==right.get("signature") and left.get(counter) is not None and right.get(counter) is not None and right[counter]>=left[counter]:
                    values.append({"delta":right[counter]-left[counter],"duration_ms":round((right["wall_ns"]-left["wall_ns"])/1e6,3)})
            return max(values,key=lambda x:x["delta"]) if values else None
        def cgroup_summary(name):
            samples=[{**x[name+"_container"],"wall_ns":x["wall_ns"]} for x in rows]
            groups_stable=all(x["signature"]==samples[0]["signature"] and x["cgroups"]>0 for x in samples)
            start,end=samples[0],samples[-1]
            mem=[x["memory_current"] for x in samples if x["memory_current"] is not None]
            io_keys=set(start["io"])|set(end["io"])
            io_ok=groups_stable and all(end["io"].get(k,0)>=start["io"].get(k,0) for k in io_keys)
            return {"cgroup_count_min_max":[min(x["cgroups"] for x in samples),max(x["cgroups"] for x in samples)],"cpu_usec_delta":end["cpu_usec"]-start["cpu_usec"] if groups_stable and end["cpu_usec"] is not None and start["cpu_usec"] is not None and end["cpu_usec"]>=start["cpu_usec"] else None,"cpu_peak_adjacent_interval":peak_interval(samples,"cpu_usec"),"memory_current_peak_sampled":max(mem) if mem else None,"io_delta":{k:end["io"].get(k,0)-start["io"].get(k,0) for k in io_keys} if io_ok else None,"cpu_attribution":"stable cgroup counters" if groups_stable else "unavailable across cgroup/incarnation change"}
        same_process=all(x["xray_pid_signature"]==rows[0]["xray_pid_signature"] and x["xray_pid_signature"] is not None for x in rows)
        api_samples=[{"signature":"api-service-cgroup","cpu_usec":x["api_cpu_usec"],"wall_ns":x["wall_ns"]} for x in rows]
        api_io_keys=set(rows[0]["api_io"])|set(rows[-1]["api_io"])
        result={"samples":len(rows),"interval_target_ms":100,"api_cpu_usec_delta":rows[-1]["api_cpu_usec"]-rows[0]["api_cpu_usec"] if rows[-1]["api_cpu_usec"] is not None and rows[0]["api_cpu_usec"] is not None and rows[-1]["api_cpu_usec"]>=rows[0]["api_cpu_usec"] else None,"api_cpu_peak_adjacent_interval":peak_interval(api_samples,"cpu_usec"),"api_memory_current_peak_sampled":max((x["api_memory_current"] or 0) for x in rows),"api_memory_current_start":rows[0]["api_memory_current"],"api_memory_current_end":rows[-1]["api_memory_current"],"api_io_delta":{k:rows[-1]["api_io"].get(k,0)-rows[0]["api_io"].get(k,0) for k in api_io_keys} if all(rows[-1]["api_io"].get(k,0)>=rows[0]["api_io"].get(k,0) for k in api_io_keys) else None,"xray_process_count_min_max":[min(x["xray_processes"] for x in rows),max(x["xray_processes"] for x in rows)],"xray_rss_sampled_peak":max(x["xray_rss_bytes"] for x in rows),"xray_cpu_ticks_delta_if_same_pid":rows[-1]["xray_cpu_ticks"]-rows[0]["xray_cpu_ticks"] if same_process and rows[-1]["xray_cpu_ticks"]>=rows[0]["xray_cpu_ticks"] else None,"xray_container":cgroup_summary("xray"),"xray_io_write_bytes_process_delta_if_same_pid":rows[-1]["xray_io_write_bytes"]-rows[0]["xray_io_write_bytes"] if same_process and rows[-1]["xray_io_write_bytes"]>=rows[0]["xray_io_write_bytes"] else None,"mihomo_process_count_min_max":[min(x["mihomo_processes"] for x in rows),max(x["mihomo_processes"] for x in rows)],"mihomo_rss_sampled_peak":max(x["mihomo_rss_bytes"] for x in rows),"mihomo_container":cgroup_summary("mihomo"),"sampler_cost_ns_p50":sorted(self.cost)[len(self.cost)//2],"sampler_cost_ns_max":max(self.cost),"raw_numeric_rows":rows}
        return result

class DockerEvents:
    def __init__(self): self.events=[]; self.error=None; self.stop=threading.Event(); self.proc=None; self.thread=None
    def start(self):
        env={**os.environ,"DOCKER_CONFIG":"/run/fwrouter-v2/docker-cli","HOME":"/run/fwrouter-v2/docker-cli"}
        self.proc=subprocess.Popen(DOCKER_EVENTS_CMD,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True,env=env,bufsize=1)
        def reader():
            try:
                assert self.proc and self.proc.stdout
                for line in self.proc.stdout:
                    if self.stop.is_set(): break
                    try: event=json.loads(line)
                    except Exception: continue
                    typ=str(event.get("Type") or event.get("type") or "")
                    action=str(event.get("Action") or event.get("status") or "")
                    actor=event.get("Actor") if isinstance(event.get("Actor"),dict) else {}
                    attrs=actor.get("Attributes") if isinstance(actor.get("Attributes"),dict) else {}
                    name=str(attrs.get("name") or "").lower()
                    if typ=="container" and "xray" not in name: continue
                    if typ=="container" or (typ=="image" and action.lower() in {"pull","tag","untag","delete"}):
                        self.events.append({"type":typ,"action":action,"time_ns":int(event.get("timeNano") or 0),"target_class":"xray_runtime" if name=="fwrouter-xray" else "xray_related" if typ=="container" else "image_event","object_id_sha256":hashlib.sha256(str(actor.get("ID") or event.get("id") or "").encode()).hexdigest()[:16],"signal":attrs.get("signal") if action.lower()=="kill" else None,"exit_code_present":"exitCode" in attrs})
                        if len(self.events)>100: self.events=self.events[-100:]
            except Exception as exc: self.error=type(exc).__name__
        self.thread=threading.Thread(target=reader,daemon=True); self.thread.start()
        deadline=time.monotonic()+2
        while time.monotonic()<deadline:
            if self.proc.poll() is not None: raise SafeAbort("docker_events_unavailable")
            if self.thread.is_alive():
                time.sleep(0.25)
                if self.proc.poll() is None: return
                raise SafeAbort("docker_events_unavailable")
            time.sleep(0.02)
        raise SafeAbort("docker_events_start_timeout")
    def close(self):
        self.stop.set()
        if self.proc and self.proc.poll() is None: self.proc.terminate()
        if self.proc:
            try: self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired: self.proc.kill(); self.proc.wait(timeout=2)
        if self.thread: self.thread.join(timeout=2)
        return self.events

def xray_container_inspect():
    env={**os.environ,"DOCKER_CONFIG":"/run/fwrouter-v2/docker-cli","HOME":"/run/fwrouter-v2/docker-cli"}
    cp=subprocess.run(["docker","inspect","--format","{{.Id}}|{{.State.Running}}|{{.State.StartedAt}}|{{.RestartCount}}|{{.Image}}|{{.Config.Image}}","fwrouter-xray"],capture_output=True,text=True,timeout=5,env=env)
    if cp.returncode: raise SafeAbort("xray_container_inspect_unavailable")
    parts=cp.stdout.strip().split("|",5)
    if len(parts)!=6: raise SafeAbort("xray_container_inspect_shape")
    return {"container_id_sha256":hashlib.sha256(parts[0].encode()).hexdigest()[:16],"running":parts[1]=="true","started_at":parts[2],"restart_count":int(parts[3]),"image_id_sha256":hashlib.sha256(parts[4].encode()).hexdigest()[:16],"configured_image":parts[5]}

def atomic_private(payload):
    tmp=SECRET_FILE.with_suffix(".tmp")
    fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,"w") as f: json.dump(payload,f); f.flush(); os.fsync(f.fileno())
    os.replace(tmp,SECRET_FILE); os.chmod(SECRET_FILE,0o600)

def main():
    if SECRET_FILE.exists() or REPORT.exists(): raise SafeAbort("once_guard_exists")
    if not Path("/srv/fwrouter/knowledge/audits/live_operational_dataplane_2026-10-08/BACKUP.json").is_file(): raise SafeAbort("protected_backup_missing")
    STATE_DIR.mkdir(mode=0o700,parents=True,exist_ok=True); os.chmod(STATE_DIR,0o700)
    # Read active jobs through an immutable SQLite URI: public list_jobs GET also cleans stale jobs.
    q3,clients_response,_=api("GET","/xray/clients")
    if q3!=200 or not clients_response.get("ok"): raise SafeAbort("client_inventory_unavailable")
    clients=clients_response.get("data",{}).get("clients",[])
    xs,xray_response,_=api("GET","/xray")
    rs,routing_response,_=api("GET","/routing/global")
    if xs!=200 or rs!=200 or not xray_response.get("ok") or not routing_response.get("ok"): raise SafeAbort("xray_or_routing_readback_unavailable")
    xray_state=xray_response.get("data",{}).get("xray",{})
    xray_egress=xray_state.get("egress",{}) if isinstance(xray_state,dict) else {}
    xray_module=xray_state.get("module",{}) if isinstance(xray_state,dict) else {}
    if str(xray_module.get("desired_state") or "")!="enabled" or not bool(xray_egress.get("traffic_available")) or not bool(xray_state.get("forced_vpn_ready")):
        raise SafeAbort("xray_egress_not_already_ready")
    routing=routing_response.get("data",{}).get("routing",{})
    if not isinstance(routing,dict): raise SafeAbort("global_route_state_unavailable")
    bind_path,cfg_path,bindings,config,active_jobs=load_local_state()
    if active_jobs.get("queued",0) or active_jobs.get("running",0): raise SafeAbort("active_jobs_present")
    before_clients=ident_digest(clients)
    before_bind=binding_semantics(bindings)
    before_config=config_semantics(config)
    if "error_code" in bindings: raise SafeAbort("bindings_state_invalid")
    xray_before=xray_container_inspect()
    if not xray_before["running"]: raise SafeAbort("xray_not_running_before_create")
    docker_events=DockerEvents(); docker_events.start()
    # Ensure no alias collision even though it is generated randomly.
    alias="audit-live-20261008-"+uuid.uuid4().hex[:10]
    before_contract={"active_queued_jobs":active_jobs.get("queued",0),"active_running_jobs":active_jobs.get("running",0),"client_count":before_clients[0],"client_digest":before_clients[1],"bindings_count":len(bindings.get("bindings",[])),"bindings_digest":canon_hash(before_bind),"config_client_count":sum(len(i.get("settings",{}).get("clients",[])) for i in config.get("inbounds",[]) if isinstance(i,dict)),"config_semantic_digest":canon_hash(before_config),"config_sha256":hashlib.sha256(cfg_path.read_bytes()).hexdigest(),"bindings_file_sha256":hashlib.sha256(bind_path.read_bytes()).hexdigest() if bind_path.exists() else None,"xray_forced_vpn_ready":bool(xray_state.get("forced_vpn_ready")),"xray_egress_traffic_available":bool(xray_egress.get("traffic_available")),"global_desired_mode":routing.get("desired_mode"),"global_server_mode":routing.get("server_mode"),"global_fixed_server_ref_sha256":hashlib.sha256(str(routing.get("desired_fixed_server_id") or "").encode()).hexdigest()[:16] if routing.get("desired_fixed_server_id") else None,"global_auto_target_ref_sha256":hashlib.sha256(str(routing.get("active_auto_server_id") or "").encode()).hexdigest()[:16] if routing.get("active_auto_server_id") else None}
    run_id=str(uuid.uuid4())
    private={"run_id":run_id,"alias":alias,"job_id":None,"client_id":None,"created_at_utc":datetime.now(timezone.utc).isoformat()}
    atomic_private(private)
    sampler=Sampler(); thread=threading.Thread(target=sampler.run,daemon=True); thread.start()
    req_start_wall=datetime.now(timezone.utc).isoformat(); req_start=time.perf_counter_ns()
    try:
        status,body,create_ms=api("POST","/xray/clients",{"alias":alias,"requested_by":"operational_live_audit","allow_blocked_egress":False})
    except Exception as exc:
        # Never retry. Resolve by exact unique alias via read-only inventory and retain private ID if found.
        try:
            _,latest,_=api("GET","/xray/clients")
            found=[c for c in latest.get("data",{}).get("clients",[]) if c.get("alias")==alias]
            if len(found)==1:
                private["client_id"]=client_id_of(found[0]); private["recovery_after_ambiguous_create"]=True; atomic_private(private)
        except Exception: pass
        sampler.stop.set(); thread.join(timeout=2); observed_docker_events=docker_events.close()
        REPORT.write_text(json.dumps({"source_baseline":"6ba7f6f","classification":"measured_partially_or_ambiguous","alias":alias,"request_error_type":type(exc).__name__,"create_retried":False,"preconditions":before_contract,"resource_samples":sampler.summary()},indent=2)+"\n")
        os.chmod(REPORT,0o600)
        raise SafeAbort("create_response_ambiguous_do_not_retry")
    accepted_wall=datetime.now(timezone.utc).isoformat(); accepted_ns=time.perf_counter_ns()
    data=body.get("data",{}) if isinstance(body,dict) else {}
    job=data.get("job") if isinstance(data.get("job"),dict) else None
    job_id=str((job or {}).get("job_id") or "")
    if not job_id:
        # Existing-client response means alias collision or source contract drift; do not delete blindly.
        sampler.stop.set(); thread.join(timeout=2); docker_events.close()
        raise SafeAbort("create_response_missing_job_id")
    private["job_id"]=job_id; atomic_private(private)
    poll_rows=[]; terminal=None; deadline=time.monotonic()+JOB_DEADLINE_S
    while time.monotonic()<deadline:
        pstatus,pbody,pms=api("GET",f"/jobs/{job_id}")
        poll_rows.append({"latency_ms":round(pms,3),"status":pstatus})
        terminal=(pbody.get("data",{}).get("job") if isinstance(pbody,dict) else None)
        if isinstance(terminal,dict) and terminal.get("status") in {"success","failed","cancelled"}: break
        time.sleep(POLL_INTERVAL_S)
    if not isinstance(terminal,dict) or terminal.get("status") not in {"success","failed","cancelled"}:
        private["job_timeout"]=True; atomic_private(private)
        sampler.stop.set(); thread.join(timeout=2); docker_events.close()
        raise SafeAbort("job_not_terminal_private_recovery_state_saved")
    job_result=terminal.get("result") if isinstance(terminal.get("result"),dict) else {}
    xray_result=job_result.get("xray_client") if isinstance(job_result.get("xray_client"),dict) else {}
    nested_client=xray_result.get("client") if isinstance(xray_result.get("client"),dict) else {}
    test_id=str(nested_client.get("client_id") or job_result.get("client_id") or "")
    test_email=str(nested_client.get("email") or job_result.get("email") or "")
    verified=bool(job_result.get("runtime_verified")) and terminal.get("status")=="success"
    if not test_id and verified:
        # Avoid generic recursive search to prevent accidentally capturing an unrelated identity.
        private["terminal_success_missing_client_id"]=True; atomic_private(private)
        sampler.stop.set(); thread.join(timeout=2); docker_events.close()
        raise SafeAbort("verified_job_missing_exact_identity")
    if test_id:
        private["client_id"]=test_id; private["job_status"]=terminal.get("status"); atomic_private(private)
    terminal_wall=datetime.now(timezone.utc).isoformat(); terminal_ns=time.perf_counter_ns()
    # Independent readback via public list and status projection; bodies stay memory-only.
    rb1,post_clients_response,rb1_ms=api("GET","/xray/clients")
    post_clients=post_clients_response.get("data",{}).get("clients",[]) if rb1==200 else []
    exact_matches=[c for c in post_clients if client_id_of(c)==test_id]
    post_client_count,post_client_digest=ident_digest(post_clients,test_id)
    post_bind_path,post_cfg_path,post_bindings,post_config,_=load_local_state()
    post_bind_sem=binding_semantics(post_bindings,test_id,test_email)
    post_cfg_sem=config_semantics(post_config,test_id,test_email)
    binding_match=canon_hash(before_bind)==canon_hash(post_bind_sem)
    config_match=canon_hash(before_config)==canon_hash(post_cfg_sem)
    existing_clients_match=before_clients==(post_client_count,post_client_digest)
    rb2,xray_status_body,rb2_ms=api("GET","/xray")
    xray_data=xray_status_body.get("data",{}).get("xray",{}) if rb2==200 else {}
    details=xray_data.get("details",{}) if isinstance(xray_data,dict) else {}
    live_readback={"http_status":rb2,"forced_vpn_ready":xray_data.get("forced_vpn_ready"),"runtime_state":xray_data.get("runtime_state"),"binding_count":(details.get("bindings") or {}).get("bindings_count"),"applied_count":(details.get("bindings") or {}).get("applied_count"),"verified_count":(details.get("bindings") or {}).get("verified_count"),"binding_generation_pending":((details.get("bindings") or {}).get("generation") or {}).get("pending")}
    sampler.stop.set(); thread.join(timeout=2); observed_docker_events=docker_events.close(); xray_after=xray_container_inspect()
    report={"captured_at_utc":datetime.now(timezone.utc).isoformat(),"source_baseline":"6ba7f6f","classification":"measured_live_mutation_single_xray_test_client","operation":"create_only_client_retained_for_authorized_data_probe","preconditions":before_contract,"request":{"method":"POST","path":"/xray/clients","requested_by":"operational_live_audit","email_omitted":True,"allow_blocked_egress":False,"http_status":status,"api_response_ms":round(create_ms,3),"request_started_utc":req_start_wall,"accepted_observed_utc":accepted_wall,"accepted_to_terminal_verified_ms":round((terminal_ns-accepted_ns)/1e6,3),"request_to_terminal_verified_ms":round((terminal_ns-req_start)/1e6,3)},"job":{"status":terminal.get("status"),"runtime_verified":verified,"created_at":terminal.get("created_at"),"started_at":terminal.get("started_at"),"finished_at":terminal.get("finished_at"),"poll_count":len(poll_rows),"poll_elapsed_ms":round(sum(x["latency_ms"] for x in poll_rows),3),"poll_latency_ms":[x["latency_ms"] for x in poll_rows],"terminal_observed_utc":terminal_wall},"readback":{"xray_clients_http":rb1,"exact_test_client_count":len(exact_matches),"existing_client_identity_semantics_match":existing_clients_match,"pre_client_count":before_clients[0],"post_existing_client_count":post_client_count,"pre_client_digest":before_clients[1],"post_existing_client_digest":post_client_digest,"non_test_bindings_semantics_match":binding_match,"pre_bindings_digest":canon_hash(before_bind),"post_non_test_bindings_digest":canon_hash(post_bind_sem),"non_test_config_semantics_match":config_match,"pre_config_semantic_digest":canon_hash(before_config),"post_non_test_config_semantic_digest":canon_hash(post_cfg_sem),"xray_status_api_ms":round(rb2_ms,3),"xray_status":live_readback},"docker_runtime":{"before":xray_before,"after":xray_after,"events":observed_docker_events,"event_count":len(observed_docker_events),"scope":"filtered Docker container events with xray in container name, bounded by harness start/stop; IDs hashed; action/time only"},"resource_samples":sampler.summary(),"measurement_limits":"100ms API cgroup/process counter samples overlap natural background. Process discovery is 1 s and can miss transient PIDs; cgroup API IO excludes Xray container. Job polling overhead reported separately. Docker events provide actual container start/die sequence; source path expects candidate test container plus Xray runtime restart, but unrelated xray-named events in interval remain possible. No raw job/API response, client ID, email, UUID, or subscription URI written to report.","private_identity_file":"/run/fwrouter-v2/live-bench-xray/client-private.json","cleanup":"held for root/data-plane probe; no edit, route change, disable, or delete in this step"}
    REPORT.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n")
    os.chmod(REPORT,0o600)
    print(json.dumps({"report_path":str(REPORT),"private_identity_file":str(SECRET_FILE),"alias":alias,"job_status":terminal.get("status"),"runtime_verified":verified,"exact_client_readback_count":len(exact_matches),"existing_client_identity_semantics_match":existing_clients_match,"non_test_bindings_semantics_match":binding_match,"non_test_config_semantics_match":config_match,"accepted_to_terminal_verified_ms":report["request"]["accepted_to_terminal_verified_ms"],"poll_count":len(poll_rows),"resource_sample_count":report["resource_samples"]["samples"]},indent=2))
    if terminal.get("status")!="success" or not verified or len(exact_matches)!=1 or not existing_clients_match or not binding_match or not config_match: raise SafeAbort("post_create_semantic_readback_gate_failed_client_held")

if __name__=="__main__":
    try: main()
    except SafeAbort as exc: print(json.dumps({"aborted":str(exc)},indent=2)); raise SystemExit(3)
    except Exception as exc: print(json.dumps({"aborted":"unexpected_exception","exception_type":type(exc).__name__},indent=2)); raise SystemExit(4)
