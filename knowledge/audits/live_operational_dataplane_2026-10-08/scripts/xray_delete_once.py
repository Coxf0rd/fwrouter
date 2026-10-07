#!/usr/bin/env python3
"""Delete only the exact private audit Xray client and verify restored state."""
from __future__ import annotations
import hashlib, importlib.util, json, os, sqlite3, sys, threading, time, urllib.parse, urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
SECRET=Path('/run/fwrouter-v2/live-bench-xray/client-private.json')
REPORT=ROOT/'XRAY_DELETE_RAW.json'
BACKUP=Path('/var/backups/fwrouter/live-operational-benchmark-20261007T180616Z')
BASE='http://127.0.0.1:5000/api/v2'
sys.path.insert(0,'/opt/fwrouter-api')
spec=importlib.util.spec_from_file_location('xray_delete_helpers',Path(__file__).with_name('xray_create_once.py'))
h=importlib.util.module_from_spec(spec); spec.loader.exec_module(h)

def api(method,path,payload=None):
    body=None if payload is None else json.dumps(payload).encode()
    req=urllib.request.Request(BASE+path,data=body,method=method,headers={'Accept':'application/json','Content-Type':'application/json'})
    t=time.perf_counter_ns()
    with urllib.request.urlopen(req,timeout=20) as r: status=r.status; result=json.loads(r.read())
    return status,result,(time.perf_counter_ns()-t)/1e6

def state():
    bind_path,cfg_path,bindings,config,active=h.load_local_state()
    return bind_path,cfg_path,bindings,config,active

def route_state():
    s,r,_=api('GET','/routing/global'); routing=r.get('data',{}).get('routing',{}) if r.get('ok') else {}
    return {'http_status':s,'desired_mode':routing.get('desired_mode'),'applied_mode':routing.get('applied_mode'),'server_mode':routing.get('server_mode'),'active_auto_target_sha256':hashlib.sha256(str(routing.get('active_auto_server_id') or '').encode()).hexdigest()[:16],'apply_state':routing.get('apply_state')}

def binding_rows_sem(state,client_id,email):
    rows=[]
    for row in state.get('bindings',[]):
        if any(str(row.get(k) or '') in {client_id,'xray:'+client_id,email} for k in ('client_id','client_uuid','subject_id','client_email')): continue
        rows.append({k:v for k,v in row.items() if k!='applied_at'})
    return sorted(rows,key=lambda r:(str(r.get('subject_id') or ''),str(r.get('client_id') or '')))

def config_sem(c,email):
    def inbound_clients():
        return [(i.get('tag'),sorted((x for x in i.get('settings',{}).get('clients',[]) if isinstance(x,dict) and x.get('email')!=email),key=lambda x:json.dumps(x,sort_keys=True))) for i in c.get('inbounds',[]) if isinstance(i,dict)]
    def user_rules():
        out=[]
        for rule in c.get('routing',{}).get('rules',[]):
            if not isinstance(rule,dict) or 'user' not in rule: continue
            users=rule.get('user') if isinstance(rule.get('user'),list) else [rule.get('user')]
            for user in users:
                if user and user!=email: out.append({'user':user,'rule':{k:v for k,v in rule.items() if k!='user'}})
        return sorted(out,key=lambda x:(x['user'],json.dumps(x['rule'],sort_keys=True,separators=(',',':'))))
    return {'inbound_clients':inbound_clients(),'user_rules':user_rules(),'non_user_rules':[r for r in c.get('routing',{}).get('rules',[]) if isinstance(r,dict) and 'user' not in r],'outbounds':c.get('outbounds',[]),'routing_fields':{k:v for k,v in c.get('routing',{}).items() if k!='rules'},'other_top':{k:v for k,v in c.items() if k not in {'inbounds','routing','outbounds'}}}

def main():
    if REPORT.exists(): raise SystemExit('once_guard_exists')
    if not SECRET.exists(): raise SystemExit('private_test_client_file_missing')
    private=json.loads(SECRET.read_text()); client_id=str(private.get('client_id') or ''); email=str(private.get('client_email_private') or '')
    if not client_id or not email: raise SystemExit('private_identity_incomplete')
    st,clients_r,_=api('GET','/xray/clients'); clients=clients_r.get('data',{}).get('clients',[]) if clients_r.get('ok') else []
    matches=[c for c in clients if h.client_id_of(c)==client_id]
    if st!=200 or len(matches)!=1: raise SystemExit('exact_owned_client_not_unique; do_not_delete')
    st,subjects_r,_=api('GET','/subjects?limit=500'); subjects=subjects_r.get('data',{}).get('subjects',[]) if subjects_r.get('ok') else []
    subject_id='xray:'+client_id
    subject=[x for x in subjects if x.get('subject_id')==subject_id]
    override=(subject[0].get('effective_state') or {}).get('server_override') if len(subject)==1 else None
    if len(subject)!=1 or not override or override.get('apply_state')!='clean': raise SystemExit('test_subject_fixed_route_not_verified; do_not_delete')
    bind_path,cfg_path,bindings,config,active=state()
    if active.get('queued',0) or active.get('running',0): raise SystemExit('active_jobs_present')
    before_route=route_state(); before_container=h.xray_container_inspect()
    st,xray_r,_=api('GET','/xray'); xray=xray_r.get('data',{}).get('xray',{}) if xray_r.get('ok') else {}
    if st!=200 or xray.get('runtime_state')!='running' or not xray.get('forced_vpn_ready'): raise SystemExit('xray_not_ready_before_delete')
    events=h.DockerEvents(); events.start()
    sampler=h.Sampler(); thread=threading.Thread(target=sampler.run,daemon=True); thread.start()
    started=datetime.now(timezone.utc).isoformat(); begin=time.perf_counter_ns()
    status,body,api_ms=api('DELETE','/xray/clients/'+urllib.parse.quote(client_id,safe=''),{'requested_by':'operational_live_audit'})
    accepted=datetime.now(timezone.utc).isoformat(); accepted_ns=time.perf_counter_ns()
    job=(body.get('data',{}).get('job') if isinstance(body,dict) else None)
    job_id=str((job or {}).get('job_id') or '')
    if status!=200 or not body.get('ok') or not job_id:
        sampler.stop.set(); thread.join(timeout=2); events.close()
        raise SystemExit('delete_not_accepted_or_ambiguous; no_retry_preserve_private_file_notify_root')
    polls=[]; terminal=None; deadline=time.monotonic()+180
    while time.monotonic()<deadline:
        ps,pbody,pms=api('GET','/jobs/'+urllib.parse.quote(job_id,safe=''))
        j=pbody.get('data',{}).get('job') if ps==200 and isinstance(pbody,dict) else None
        polls.append({'http_status':ps,'latency_ms':round(pms,3),'status':j.get('status') if isinstance(j,dict) else None})
        terminal=j
        if isinstance(j,dict) and j.get('status') in {'success','failed','cancelled'}: break
        time.sleep(0.5)
    if not isinstance(terminal,dict) or terminal.get('status') not in {'success','failed','cancelled'}:
        sampler.stop.set(); thread.join(timeout=2); events.close()
        raise SystemExit('delete_nonterminal; preserve private file and notify root')
    done=datetime.now(timezone.utc).isoformat(); done_ns=time.perf_counter_ns()
    st2,clients_after_r,clients_ms=api('GET','/xray/clients'); clients_after=clients_after_r.get('data',{}).get('clients',[]) if clients_after_r.get('ok') else []
    st3,subjects_after_r,subjects_ms=api('GET','/subjects?limit=500'); subjects_after=subjects_after_r.get('data',{}).get('subjects',[]) if subjects_after_r.get('ok') else []
    st4,xray_after_r,xray_ms=api('GET','/xray'); xray_after=xray_after_r.get('data',{}).get('xray',{}) if xray_after_r.get('ok') else {}
    bind_path,cfg_path,bind_after,cfg_after,active_after=state()
    old_client_count,old_client_digest=h.ident_digest(clients_after)
    expected_client=create_digest=json.loads((ROOT/'XRAY_CREATE_RAW.json').read_text())['preconditions']['client_digest']
    cfg_before=json.loads((BACKUP/'var__lib__fwrouter-v2__xray__config.json').read_text())
    bindings_before=json.loads((BACKUP/'var__lib__fwrouter-v2__xray__fwrouter-bindings.json').read_text())
    post_client_match=(old_client_count==78 and old_client_digest==expected_client)
    binding_match=hashlib.sha256(json.dumps(binding_rows_sem(bind_after,client_id,email),sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()==hashlib.sha256(json.dumps(binding_rows_sem(bindings_before,client_id,email),sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()
    config_match=hashlib.sha256(json.dumps(config_sem(cfg_after,email),sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()==hashlib.sha256(json.dumps(config_sem(cfg_before,email),sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()
    after_route=route_state(); no_subject=not any(s.get('subject_id')==subject_id for s in subjects_after)
    details=xray_after.get('details',{}); live=details.get('bindings',{}) if isinstance(details,dict) else {}
    job_result=terminal.get('result',{}) if isinstance(terminal.get('result'),dict) else {}
    runtime_verified=bool(job_result.get('runtime_verified')) and terminal.get('status')=='success'
    sample_summary=sampler.summary(); sampler.stop.set(); thread.join(timeout=2); docker=events.close(); container_after=h.xray_container_inspect()
    report={'captured_at_utc':datetime.now(timezone.utc).isoformat(),'source_baseline':'6ba7f6f','classification':'measured_live_exact_audit_client_delete',
      'request':{'method':'DELETE','path':'/xray/clients/{private-owned-test-client-id}','requested_by':'operational_live_audit','http_status':status,'api_response_ms':round(api_ms,3),'request_started_utc':started,'accepted_observed_utc':accepted,'accepted_to_terminal_verified_ms':round((done_ns-accepted_ns)/1e6,3),'request_to_terminal_verified_ms':round((done_ns-begin)/1e6,3)},
      'job':{'status':terminal.get('status'),'runtime_verified':runtime_verified,'created_at':terminal.get('created_at'),'started_at':terminal.get('started_at'),'finished_at':terminal.get('finished_at'),'poll_count':len(polls),'poll_elapsed_ms':round(sum(x['latency_ms'] for x in polls),3),'poll_latency_ms':[x['latency_ms'] for x in polls],'terminal_observed_utc':done},
      'readback':{'client_list_http':st2,'client_list_ms':round(clients_ms,3),'exact_test_client_count':sum(1 for c in clients_after if h.client_id_of(c)==client_id),'existing_client_count':old_client_count,'all_78_existing_clients_exact':post_client_match,'subject_list_http':st3,'subject_list_ms':round(subjects_ms,3),'test_subject_absent':no_subject,'xray_http':st4,'xray_status_ms':round(xray_ms,3),'runtime_state':xray_after.get('runtime_state'),'forced_vpn_ready':xray_after.get('forced_vpn_ready'),'bindings_count':live.get('bindings_count'),'applied_count':live.get('applied_count'),'verified_count':live.get('verified_count'),'generation_pending':(live.get('generation') or {}).get('pending'),'all_78_binding_rows_exact_except_applied_at':binding_match,'all_non_test_config_semantics_exact':config_match,'routing_before':before_route,'routing_after':after_route,'global_route_unchanged':before_route==after_route,'active_jobs_after':active_after},
      'runtime_events':{'before':before_container,'after':container_after,'events':docker,'xray_runtime_restarts':sum(1 for e in docker if e.get('target_class')=='xray_runtime' and e.get('action')=='restart'),'candidate_container_creates':sum(1 for e in docker if e.get('target_class')=='xray_related' and e.get('action')=='create')},
      'resources':sample_summary,'limitations':'Resource samples overlap polling/readbacks and background work. Process discovery 1s may miss short-lived native processes. DB/API per-request counts and WAL/fsync attribution are unavailable. API response success is acceptance; terminal success/runtime_verified and readback establish convergence.',
    }
    REPORT.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n'); REPORT.chmod(0o600)
    gates=terminal.get('status')=='success' and runtime_verified and report['readback']['exact_test_client_count']==0 and no_subject and post_client_match and binding_match and config_match and report['readback']['forced_vpn_ready'] and live.get('bindings_count')==78 and live.get('applied_count')==78 and live.get('verified_count')==78 and not report['readback']['generation_pending'] and report['readback']['global_route_unchanged']
    print(json.dumps({'report':str(REPORT),'status':terminal.get('status'),'runtime_verified':runtime_verified,'accepted_to_terminal_ms':report['job']['accepted_to_terminal_verified_ms'],'request_to_terminal_ms':report['job']['request_to_terminal_verified_ms'],'restart_count':report['runtime_events']['xray_runtime_restarts'],'existing_clients_exact':post_client_match,'bindings_exact':binding_match,'config_exact':config_match,'private_file_removed':False,'all_cleanup_gates_passed':gates},indent=2))
    if not gates: raise SystemExit('cleanup_parity_gate_failed; stop and notify root; do not restore DB/config manually')
    SECRET.unlink()
    report['private_identity_file_removed']=True
    REPORT.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n'); REPORT.chmod(0o600)

if __name__=='__main__': main()
