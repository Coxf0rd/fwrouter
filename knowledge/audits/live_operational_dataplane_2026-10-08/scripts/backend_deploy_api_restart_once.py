#!/usr/bin/env python3
"""One same-baseline backend deploy and API restart with bounded readiness sampling."""
from __future__ import annotations
import hashlib, json, os, sqlite3, subprocess, threading, time, urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT=Path('/srv/fwrouter')
AUDIT=ROOT/'knowledge/audits/live_operational_dataplane_2026-10-08'
REPORT=AUDIT/'API_DEPLOY_RESTART_RAW.json'
MARKER=Path('/run/fwrouter-v2/live-bench-xray/api-restart-begin')
CG=Path('/sys/fs/cgroup/system.slice/fwrouter-api.service')
BASE='http://127.0.0.1:5000/api/v2'
BACKUP=Path('/var/backups/fwrouter/live-operational-benchmark-20261007T180616Z')
MAX_ROWS=3600

def utc(): return datetime.now(timezone.utc).isoformat()

def api(path,timeout=5):
    req=urllib.request.Request(BASE+path,headers={'Accept':'application/json','Cache-Control':'no-cache'})
    start=time.perf_counter_ns()
    with urllib.request.urlopen(req,timeout=timeout) as r: status=r.status; body=json.loads(r.read())
    return status,body,(time.perf_counter_ns()-start)/1e6

def cpu_usec():
    try: return int(next(x.split()[1] for x in (CG/'cpu.stat').read_text().splitlines() if x.startswith('usage_usec ')))
    except (OSError,StopIteration,ValueError): return None

def read_counter():
    result={'wall_ns':time.monotonic_ns(),'cpu_usec':cpu_usec(),'memory_current':None,'io':{},'main_pid':None,'main_rss_bytes':None,'main_cpu_ticks':None}
    try: result['memory_current']=int((CG/'memory.current').read_text())
    except (OSError,ValueError): pass
    try:
        for line in (CG/'io.stat').read_text().splitlines():
            for field in line.split()[1:]:
                key,value=field.split('='); result['io'][key]=result['io'].get(key,0)+int(value)
    except (OSError,ValueError): pass
    return result

def service_main_pid():
    cp=subprocess.run(['systemctl','show','fwrouter-api.service','-p','MainPID','--value'],capture_output=True,text=True,timeout=2)
    try: return int(cp.stdout.strip()) if cp.returncode==0 and cp.stdout.strip() else None
    except ValueError: return None

def process_read(pid):
    if not pid or pid<=0: return None,None
    try:
        stat=Path(f'/proc/{pid}/stat').read_text().rsplit(') ',1)[1].split()
        ticks=int(stat[11])+int(stat[12])
        rss=int(Path(f'/proc/{pid}/statm').read_text().split()[1])*os.sysconf('SC_PAGE_SIZE')
        return ticks,rss
    except (OSError,ValueError,IndexError): return None,None

class Sampler:
    def __init__(self): self.rows=[]; self.cost=[]; self.stop=threading.Event(); self.pid=None; self.last_pid=0
    def run(self):
        while not self.stop.is_set():
            start=time.perf_counter_ns(); now=time.monotonic()
            if now-self.last_pid>=1:
                self.pid=service_main_pid(); self.last_pid=now
            row=read_counter(); row['main_pid']=self.pid
            row['main_cpu_ticks'],row['main_rss_bytes']=process_read(self.pid)
            self.rows.append(row); self.cost.append(time.perf_counter_ns()-start)
            if len(self.rows)>MAX_ROWS: del self.rows[:len(self.rows)-MAX_ROWS]
            self.stop.wait(0.1)
    def summary(self):
        rows=self.rows
        if not rows:return {'samples':0,'raw_numeric_rows':[]}
        peaks=[]
        for a,b in zip(rows,rows[1:]):
            if a['cpu_usec'] is not None and b['cpu_usec'] is not None and b['cpu_usec']>=a['cpu_usec']:
                peaks.append({'cpu_delta_usec':b['cpu_usec']-a['cpu_usec'],'interval_ms':round((b['wall_ns']-a['wall_ns'])/1e6,3)})
        pid_epochs=[]; epoch=None
        for x in rows:
            if x['main_pid']!=epoch:
                pid_epochs.append({'pid_changed':True,'rss_bytes':x['main_rss_bytes'],'cpu_ticks':x['main_cpu_ticks']}); epoch=x['main_pid']
        first,last=rows[0],rows[-1]
        stable=first['cpu_usec'] is not None and last['cpu_usec'] is not None and last['cpu_usec']>=first['cpu_usec']
        mem=[r['memory_current'] for r in rows if r['memory_current'] is not None]
        io_keys=set(first['io'])|set(last['io']); io_stable=stable and all(last['io'].get(k,0)>=first['io'].get(k,0) for k in io_keys)
        rss=[r['main_rss_bytes'] for r in rows if r['main_rss_bytes'] is not None]
        peak=max(peaks,key=lambda x:x['cpu_delta_usec']) if peaks else None
        return {'samples':len(rows),'target_interval_ms':100,'api_cgroup_cpu_delta_usec':last['cpu_usec']-first['cpu_usec'] if stable else None,'api_cgroup_cpu_delta_available':stable,'api_cgroup_cpu_peak_adjacent_interval':peak,'api_cgroup_memory_current_start':first['memory_current'],'api_cgroup_memory_current_peak_sampled':max(mem) if mem else None,'api_cgroup_memory_current_end':last['memory_current'],'api_cgroup_io_delta':{k:last['io'].get(k,0)-first['io'].get(k,0) for k in io_keys} if io_stable else None,'main_pid_epochs':pid_epochs,'api_main_process_rss_peak_sampled':max(rss) if rss else None,'sampler_wall_cost_ns_p50':sorted(self.cost)[len(self.cost)//2],'sampler_wall_cost_ns_max':max(self.cost),'raw_numeric_rows':rows}

def read_model():
    hs,health,hms=api('/health')
    xs,xray,xms=api('/xray')
    ss,selector,sms=api('/selector/vpn-auto/state')
    rs,routing,rms=api('/routing/global')
    h=health.get('data',{}) if isinstance(health,dict) else {}
    if not h: h=health if isinstance(health,dict) else {}
    x=xray.get('data',{}).get('xray',{}) if isinstance(xray,dict) else {}
    b=x.get('details',{}).get('bindings',{}) if isinstance(x.get('details'),dict) else {}
    g=routing.get('data',{}).get('routing',{}) if isinstance(routing,dict) else {}
    v=selector.get('data',{}).get('vpn_auto',{}) if isinstance(selector,dict) else {}
    ready=(hs==200 and bool(health.get('ok')) and xs==200 and bool(xray.get('ok')) and x.get('runtime_state')=='running' and bool(x.get('forced_vpn_ready')) and b.get('bindings_count')==78 and b.get('applied_count')==78 and b.get('verified_count')==78 and not (b.get('generation') or {}).get('pending') and ss==200 and v.get('active_auto_target_valid') is True and v.get('config_consistent') is True and rs==200 and g.get('desired_mode')=='selective' and g.get('applied_mode')=='selective' and g.get('server_mode')=='auto' and g.get('apply_state')=='clean')
    out={'http':{'health':hs,'xray':xs,'selector':ss,'routing':rs},'latency_ms':{'health':round(hms,3),'xray':round(xms,3),'selector':round(sms,3),'routing':round(rms,3)},'ready':ready,'health_api_ok':bool(health.get('ok')),'xray_runtime_state':x.get('runtime_state'),'forced_vpn_ready':x.get('forced_vpn_ready'),'bindings_count':b.get('bindings_count'),'applied_count':b.get('applied_count'),'verified_count':b.get('verified_count'),'generation_pending':(b.get('generation') or {}).get('pending'),'selector_auto_valid':v.get('active_auto_target_valid'),'selector_config_consistent':v.get('config_consistent'),'global_mode':g.get('desired_mode'),'applied_mode':g.get('applied_mode'),'server_mode':g.get('server_mode'),'global_auto_target_sha256':hashlib.sha256(str(g.get('active_auto_server_id') or '').encode()).hexdigest()[:16],'routing_apply_state':g.get('apply_state')}
    return out

def active_jobs():
    sys_path='/opt/fwrouter-api'
    import sys; sys.path.insert(0,sys_path)
    from fwrouter_api.core.config import get_settings
    c=sqlite3.connect(get_settings().paths.db_path.resolve().as_uri()+'?mode=ro',uri=True,timeout=5)
    try: return [list(r) for r in c.execute("SELECT status,count(*) FROM jobs WHERE status IN ('queued','running') GROUP BY status").fetchall()]
    finally:c.close()

def provider_snapshot():
    import sys; sys.path.insert(0,'/opt/fwrouter-api')
    from fwrouter_api.core.config import get_settings
    c=sqlite3.connect(get_settings().paths.db_path.resolve().as_uri()+'?mode=ro',uri=True,timeout=5); c.row_factory=sqlite3.Row
    try:
        row=c.execute('SELECT current_member_id,applied_member_id,binding_revision,applied_revision,allow_automatic_member_switch FROM provider_bindings WHERE source_ref=?',('src:d5d22aee8008a59b3b53b0989e8bc48dccc97af7f6e54f9725b0ce41162c895b',)).fetchone()
        rev=c.execute("SELECT value_json FROM settings WHERE key='routing.auto_selection_revision'").fetchone()
    finally:c.close()
    return {'current_member_id':row['current_member_id'] if row else None,'applied_member_id':row['applied_member_id'] if row else None,'binding_revision':row['binding_revision'] if row else None,'applied_revision':row['applied_revision'] if row else None,'allow_automatic_member_switch':bool(row['allow_automatic_member_switch']) if row else None,'auto_selection_revision_sha256':hashlib.sha256(str(rev['value_json'] if rev else '').encode()).hexdigest()[:16]}

def main():
    if REPORT.exists() or MARKER.exists(): raise SystemExit('once_guard_exists')
    if not BACKUP.is_dir(): raise SystemExit('protected_backup_missing')
    rev=subprocess.run(['git','-C',str(ROOT),'rev-parse','HEAD'],capture_output=True,text=True,check=True).stdout.strip()
    if rev!='6ba7f6f003ef67cbe350bea1e2d092e34c3eb660': raise SystemExit('source_baseline_mismatch')
    jobs=active_jobs()
    if jobs: raise SystemExit('active_jobs_present')
    pre=read_model(); provider_pre=provider_snapshot()
    if not pre['ready']: raise SystemExit('critical_state_not_ready_before_deploy')
    sampler=Sampler(); thread=threading.Thread(target=sampler.run,daemon=True); thread.start()
    deploy_start_utc=utc(); deploy_start_ns=time.perf_counter_ns()
    cp=subprocess.run([str(ROOT/'installer/install.sh'),'--deploy','--component','backend'],cwd=ROOT,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=300)
    deploy_end_utc=utc(); deploy_end_ns=time.perf_counter_ns()
    if cp.returncode!=0:
        sampler.stop.set(); thread.join(timeout=2)
        REPORT.write_text(json.dumps({'classification':'deploy_failed_stop_without_restart','deploy_returncode':cp.returncode,'deploy_start_utc':deploy_start_utc,'deploy_elapsed_ms':(deploy_end_ns-deploy_start_ns)/1e6,'pre':pre,'resources':sampler.summary()},indent=2)+'\n'); REPORT.chmod(0o600)
        raise SystemExit('deploy_failed; do_not_restart')
    deploy_readiness=read_model(); deploy_ready_utc=utc(); deploy_ready_ns=time.perf_counter_ns()
    if not deploy_readiness['ready']:
        sampler.stop.set(); thread.join(timeout=2)
        REPORT.write_text(json.dumps({'classification':'deploy_completed_readiness_failed_stop_without_restart','deploy_returncode':cp.returncode,'deploy_start_utc':deploy_start_utc,'deploy_end_utc':deploy_end_utc,'deploy_readiness':deploy_readiness,'pre':pre,'resources':sampler.summary()},indent=2)+'\n'); REPORT.chmod(0o600)
        raise SystemExit('post_deploy_readiness_failed; do_not_restart')
    marker={'created_at_utc':utc(),'phase':'immediately_before_fwrouter_api_service_restart','source_baseline':rev}
    fd=os.open(MARKER,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w') as f: json.dump(marker,f); f.flush(); os.fsync(f.fileno())
    restart_start_utc=utc(); restart_start_ns=time.perf_counter_ns()
    command=subprocess.run(['systemctl','restart','fwrouter-api.service'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=180)
    restart_command_end_utc=utc(); restart_command_end_ns=time.perf_counter_ns()
    ready=None; ready_utc=None; ready_ms=None; polls=[]; deadline=time.monotonic()+180
    while time.monotonic()<deadline:
        try:
            ready=read_model(); polls.append({'at_utc':utc(),'ready':ready['ready'],'http':ready['http'],'latency_ms':ready['latency_ms']})
            if ready['ready']:
                ready_utc=utc(); ready_ms=(time.perf_counter_ns()-restart_start_ns)/1e6; break
        except Exception as exc: polls.append({'at_utc':utc(),'ready':False,'error_class':type(exc).__name__})
        time.sleep(0.5)
    sampler.stop.set(); thread.join(timeout=2); resources=sampler.summary()
    post=read_model() if ready and ready.get('ready') else ready
    provider_post=provider_snapshot(); jobs_after=active_jobs()
    report={'captured_at_utc':utc(),'source_baseline':rev,'classification':'measured_live_same_baseline_backend_deploy_and_api_restart',
      'preflight':{'protected_backup_present':True,'active_jobs_before':jobs,'source_head':rev,'critical_state_ready_before':pre['ready'],'pre':pre},
      'backend_deploy':{'argv':['installer/install.sh','--deploy','--component','backend'],'returncode':cp.returncode,'start_utc':deploy_start_utc,'end_utc':deploy_end_utc,'command_elapsed_ms':round((deploy_end_ns-deploy_start_ns)/1e6,3),'post_deploy_readiness_utc':deploy_ready_utc,'post_deploy_readiness':deploy_readiness,'command_start_to_readiness_ms':round((deploy_ready_ns-deploy_start_ns)/1e6,3)},
      'api_restart':{'marker_path':str(MARKER),'marker':marker,'restart_argv':['systemctl','restart','fwrouter-api.service'],'returncode':command.returncode,'start_utc':restart_start_utc,'command_end_utc':restart_command_end_utc,'command_elapsed_ms':round((restart_command_end_ns-restart_start_ns)/1e6,3),'http_critical_readiness_utc':ready_utc,'restart_start_to_ready_ms':round(ready_ms,3) if ready_ms is not None else None,'ready':bool(ready and ready.get('ready')),'polls':polls,'post_restart_readback':post},
      'resources':resources,'provider_bracket':{'before':provider_pre,'after':provider_post,'unchanged':provider_pre==provider_post},'active_jobs_after':jobs_after,'limitations':'Cgroup/process counters are co-interval and include background work plus the sampler. API cgroup counter resets are never bridged; if any counter reset occurs the whole-window CPU/I/O delta is unavailable. Poll requests and approved t+10/t+20 traffic probes contribute to the observed interval. DB statement counts/WAL/fsync are not measured. --deploy does not restart the API; restart readiness is a separate operation.',
    }
    REPORT.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n'); REPORT.chmod(0o600)
    print(json.dumps({'report':str(REPORT),'deploy_ms':report['backend_deploy']['command_elapsed_ms'],'post_deploy_ready':deploy_readiness['ready'],'restart_command_ms':report['api_restart']['command_elapsed_ms'],'restart_ready_ms':report['api_restart']['restart_start_to_ready_ms'],'restart_ready':report['api_restart']['ready'],'resource_samples':resources['samples']},indent=2))
    if command.returncode!=0 or not report['api_restart']['ready']: raise SystemExit('api_restart_readiness_failed; keep marker/evidence and notify root')

if __name__=='__main__': main()
