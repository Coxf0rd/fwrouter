#!/usr/bin/env python3
"""Apply one root-approved fixed route to the private test Xray client."""
from __future__ import annotations
import hashlib, importlib.util, json, sqlite3, sys, time, threading, urllib.parse, urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
SECRET=Path('/run/fwrouter-v2/live-bench-xray/client-private.json')
REPORT=ROOT/'XRAY_FIXED_ROUTE_RAW.json'
HELPER=Path(__file__).with_name('xray_create_once.py')
TARGET='sub:2412af49235dd3d4b27585ee009991c372ced4e326ac746f8f9f26c3f1494512'
BASE='http://127.0.0.1:5000/api/v2'
sys.path.insert(0,'/opt/fwrouter-api')
spec=importlib.util.spec_from_file_location('xray_bench_helpers',HELPER)
helper=importlib.util.module_from_spec(spec); spec.loader.exec_module(helper)

def api(method,path,payload=None):
    body=None if payload is None else json.dumps(payload).encode()
    req=urllib.request.Request(BASE+path,data=body,method=method,headers={'Accept':'application/json','Content-Type':'application/json'})
    start=time.perf_counter_ns()
    with urllib.request.urlopen(req,timeout=20) as r: status=r.status; result=json.loads(r.read())
    return status,result,(time.perf_counter_ns()-start)/1e6

def summary_state():
    status,routing,route_ms=api('GET','/routing/global')
    status2,selector,selector_ms=api('GET','/selector/vpn-auto/state')
    if status!=200 or status2!=200: raise RuntimeError('routing_state_readback_failed')
    r=routing.get('data',{}).get('routing',{})
    s=selector.get('data',{}).get('vpn_auto',{})
    bind_path,cfg_path,bindings,config,active=helper.load_local_state()
    from fwrouter_api.core.config import get_settings
    db_path=get_settings().paths.db_path
    conn=sqlite3.connect(db_path.resolve().as_uri()+'?mode=ro',uri=True,timeout=5)
    try:
        db_revision=conn.execute("SELECT value_json FROM settings WHERE key='routing.auto_selection_revision'").fetchone()
        provider=conn.execute("SELECT current_member_id,applied_member_id,binding_revision,applied_revision,allow_automatic_member_switch FROM provider_bindings WHERE source_ref=?",('src:d5d22aee8008a59b3b53b0989e8bc48dccc97af7f6e54f9725b0ce41162c895b',)).fetchone()
    finally: conn.close()
    return {
      'routing':{k:r.get(k) for k in ('desired_mode','applied_mode','server_mode','apply_state')},
      'global_fixed_ref_sha256':hashlib.sha256(str(r.get('desired_fixed_server_id') or '').encode()).hexdigest()[:16],
      'global_auto_ref_sha256':hashlib.sha256(str(r.get('active_auto_server_id') or '').encode()).hexdigest()[:16],
      'vpn_auto':{k:s.get(k) for k in ('active_auto_target_valid','config_consistent','runtime_state')},
      'auto_revision_value_sha256':hashlib.sha256(str(db_revision[0] if db_revision else '').encode()).hexdigest()[:16],
      'provider':{'current_member_id':provider[0] if provider else None,'applied_member_id':provider[1] if provider else None,'binding_revision':provider[2] if provider else None,'applied_revision':provider[3] if provider else None,'allow_automatic_member_switch':bool(provider[4]) if provider else None},
      'bindings_count':len(bindings.get('bindings',[])),
      'bindings_semantics_sha256':helper.canon_hash(helper.binding_semantics(bindings)),
      'config_semantics_sha256':helper.canon_hash(helper.config_semantics(config)),
      'bindings_file_sha256':hashlib.sha256(bind_path.read_bytes()).hexdigest() if bind_path.exists() else None,
      'config_file_sha256':hashlib.sha256(cfg_path.read_bytes()).hexdigest(),
      'routing_read_ms':round(route_ms,3),'selector_read_ms':round(selector_ms,3),
      'active_jobs':active,
    }

def main():
    if REPORT.exists(): raise SystemExit('once_guard_exists')
    if not SECRET.exists(): raise SystemExit('private_test_identity_missing')
    private=json.loads(SECRET.read_text()); client_id=str(private.get('client_id') or '')
    email=str(private.get('client_email_private') or '')
    if not client_id or not email: raise SystemExit('private_identity_shape_invalid')
    st,clients_r,clients_ms=api('GET','/xray/clients')
    clients=clients_r.get('data',{}).get('clients',[]) if clients_r.get('ok') else []
    matches=[c for c in clients if helper.client_id_of(c)==client_id]
    if st!=200 or len(matches)!=1: raise SystemExit('exact_test_identity_not_unique')
    st,subjects_r,subjects_ms=api('GET','/subjects?limit=500')
    subjects=subjects_r.get('data',{}).get('subjects',[]) if subjects_r.get('ok') else []
    test_subjects=[x for x in subjects if email in json.dumps(x,ensure_ascii=False)]
    if st!=200 or len(test_subjects)!=1: raise SystemExit('exact_test_subject_not_unique')
    subject=test_subjects[0]; subject_id=str(subject.get('subject_id') or '')
    if not subject_id.startswith('xray:'): raise SystemExit('test_subject_type_mismatch')
    current=subject.get('effective_state',{})
    if current.get('server_override') or str(current.get('selected_server_id') or '')==TARGET: raise SystemExit('unexpected_test_route_precondition')
    st,servers_r,servers_ms=api('GET','/servers?limit=500')
    servers=servers_r.get('data',{}).get('servers',[]) if servers_r.get('ok') else []
    target=[x for x in servers if str(x.get('server_id') or x.get('id') or '')==TARGET]
    if st!=200 or len(target)!=1 or target[0].get('inventory_state')!='active': raise SystemExit('approved_fixed_target_not_active')
    applied=[x for x in subjects if x.get('subject_type') in ('xray','explicit_external_client') and (x.get('effective_state') or {}).get('selected_server_id')==TARGET and (x.get('effective_state') or {}).get('dataplane_path')=='vpn']
    if not applied: raise SystemExit('target_not_in_existing_applied_subject_set')
    pre=summary_state()
    if pre['active_jobs'].get('queued',0) or pre['active_jobs'].get('running',0): raise SystemExit('active_jobs_present')
    pre_bindings=helper.load_local_state()[2]
    pre_config=helper.load_local_state()[3]
    pre_bind=helper.binding_semantics(pre_bindings,client_id,email)
    pre_config_sem=helper.config_semantics(pre_config,client_id,email)
    xray0_status,xray0,xray0_ms=api('GET','/xray')
    x0=xray0.get('data',{}).get('xray',{})
    if xray0_status!=200 or not x0.get('forced_vpn_ready') or x0.get('runtime_state')!='running': raise SystemExit('xray_not_ready_pre_route')
    container_before=helper.xray_container_inspect()
    if not container_before.get('running'): raise SystemExit('xray_container_not_running')
    events=helper.DockerEvents(); events.start()
    sampler=helper.Sampler(); thread=threading.Thread(target=sampler.run,daemon=True); thread.start()
    started=datetime.now(timezone.utc).isoformat(); begin=time.perf_counter_ns()
    path='/subjects/'+urllib.parse.quote(subject_id,safe='')+'/server-override'
    status,response,api_ms=api('POST',path,{'server_id':TARGET,'actor_scope':'user','requested_by':'operational_live_audit','run_now':True})
    data=response.get('data',{}) if isinstance(response,dict) else {}
    job=data.get('job') if isinstance(data.get('job'),dict) else None
    job_id=str((job or {}).get('job_id') or '')
    accepted=datetime.now(timezone.utc).isoformat(); accepted_ns=time.perf_counter_ns()
    if status!=200 or not response.get('ok') or not job_id:
        sampler.stop.set(); thread.join(timeout=2); events.close()
        raise SystemExit('route_apply_not_accepted; preserve current state and notify root')
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
        raise SystemExit('route_job_nonterminal; preserve evidence and notify root')
    terminal_observed=datetime.now(timezone.utc).isoformat(); terminal_ns=time.perf_counter_ns()
    # Readback deliberately excludes all secret-bearing job/client payloads.
    st,subject_r,subject_ms=api('GET','/subjects/'+urllib.parse.quote(subject_id,safe=''))
    post_subject=subject_r.get('data',{}).get('subject',{}) if subject_r.get('ok') else {}
    st2,xray_r,xray_ms=api('GET','/xray'); xray=xray_r.get('data',{}).get('xray',{}) if xray_r.get('ok') else {}
    post=summary_state()
    st3,clients2_r,clients2_ms=api('GET','/xray/clients')
    clients2=clients2_r.get('data',{}).get('clients',[]) if clients2_r.get('ok') else []
    test_count=sum(1 for c in clients2 if helper.client_id_of(c)==client_id)
    bind_path,cfg_path,bindings,config,active=helper.load_local_state()
    non_test_bind=helper.binding_semantics(bindings,client_id,email)
    non_test_cfg=helper.config_semantics(config,client_id,email)
    before_bind=helper.binding_semantics(pre_bindings,client_id,email)
    # Compare pre-operation host config/bindings captured immediately above the POST.
    before_bind_digest=helper.canon_hash(pre_bind)
    after_bind_digest=helper.canon_hash(non_test_bind)
    before_cfg_digest=helper.canon_hash(pre_config_sem)
    after_cfg_digest=helper.canon_hash(non_test_cfg)
    sampler.stop.set(); thread.join(timeout=2); docker_events=events.close(); container_after=helper.xray_container_inspect()
    override=(post_subject.get('effective_state') or {}).get('server_override') if isinstance(post_subject,dict) else None
    xdetails=xray.get('details',{}) if isinstance(xray,dict) else {}
    bindings_live=xdetails.get('bindings',{}) if isinstance(xdetails,dict) else {}
    report={
      'captured_at_utc':datetime.now(timezone.utc).isoformat(),'source_baseline':'6ba7f6f','classification':'measured_live_single_test_client_fixed_route_apply',
      'request':{'method':'POST','path':'/subjects/{private-test-subject}/server-override','request_shape':'server_id=approved existing fixed target, actor_scope=user, run_now=true','requested_by':'operational_live_audit','http_status':status,'api_response_ms':round(api_ms,3),'request_started_utc':started,'accepted_observed_utc':accepted,'accepted_to_terminal_ms':round((terminal_ns-accepted_ns)/1e6,3),'request_to_terminal_ms':round((terminal_ns-begin)/1e6,3)},
      'preconditions':{'test_client_exact_count':len(matches),'test_subject_exact_count':len(test_subjects),'test_subject_id_sha256':hashlib.sha256(subject_id.encode()).hexdigest()[:16],'old_route_source':current.get('selected_server_source'),'old_target_ref_sha256':hashlib.sha256(str(current.get('selected_server_id') or '').encode()).hexdigest()[:16],'no_existing_override':not bool(current.get('server_override')),'target_ref_sha256':hashlib.sha256(TARGET.encode()).hexdigest()[:16],'target_active_server_count':len(target),'existing_applied_subject_count_for_target':len(applied),'xray_ready':True,'container_before':container_before,'snapshot':pre,'xray_status_ms':round(xray0_ms,3)},
      'job':{'status':terminal.get('status'),'created_at':terminal.get('created_at'),'started_at':terminal.get('started_at'),'finished_at':terminal.get('finished_at'),'poll_count':len(polls),'poll_elapsed_ms':round(sum(x['latency_ms'] for x in polls),3),'poll_latency_ms':[x['latency_ms'] for x in polls],'terminal_observed_utc':terminal_observed},
      'readback':{'subject_http':st,'subject_read_ms':round(subject_ms,3),'override_target_matches':bool(override and override.get('selected_server_id')==TARGET),'override_apply_state':override.get('apply_state') if isinstance(override,dict) else None,'xray_http':st2,'xray_read_ms':round(xray_ms,3),'xray_runtime_state':xray.get('runtime_state'),'forced_vpn_ready':xray.get('forced_vpn_ready'),'bindings_count':bindings_live.get('bindings_count'),'applied_count':bindings_live.get('applied_count'),'verified_count':bindings_live.get('verified_count'),'generation_pending':(bindings_live.get('generation') or {}).get('pending'),'exact_test_client_count':test_count,'existing_binding_semantics_match':before_bind_digest==after_bind_digest,'existing_config_semantics_match':before_cfg_digest==after_cfg_digest,'global_and_provider_bracket_match':pre['routing']==post['routing'] and pre['global_auto_ref_sha256']==post['global_auto_ref_sha256'] and pre['provider']==post['provider'] and pre['auto_revision_value_sha256']==post['auto_revision_value_sha256'],'post_snapshot':post,'pre_non_test_binding_digest':before_bind_digest,'post_non_test_binding_digest':after_bind_digest,'pre_non_test_config_digest':before_cfg_digest,'post_non_test_config_digest':after_cfg_digest},
      'runtime_events':{'before':container_before,'after':container_after,'events':docker_events,'xray_runtime_restart_count':sum(1 for e in docker_events if e.get('target_class')=='xray_runtime' and e.get('action')=='restart'),'candidate_container_count':sum(1 for e in docker_events if e.get('target_class')=='xray_related' and e.get('action')=='create')},
      'resources':sampler.summary(),
      'limitations':'Resource rows are co-interval with polls/readbacks and background work; sampler overhead is included. Job internals are not persisted. Existing client traffic is not changed by the scoped selector update; config apply may restart Xray and interrupt existing streams. No DB/API handler per-request timings; no provider requests are expected by the scoped route path.'
    }
    REPORT.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n'); REPORT.chmod(0o600)
    print(json.dumps({'report':str(REPORT),'http_status':status,'job_status':terminal.get('status'),'runtime_verified_readback':report['readback']['override_target_matches'] and report['readback']['verified_count']==report['readback']['bindings_count'],'request_to_terminal_ms':report['request']['request_to_terminal_ms'],'poll_count':len(polls),'restarts':report['runtime_events']['xray_runtime_restart_count'],'existing_binding_semantics_match':report['readback']['existing_binding_semantics_match'],'existing_config_semantics_match':report['readback']['existing_config_semantics_match'],'global_provider_bracket_match':report['readback']['global_and_provider_bracket_match'],'resource_samples':report['resources']['samples']},indent=2))
    if terminal.get('status')!='success' or not report['readback']['override_target_matches'] or report['readback']['verified_count']!=report['readback']['bindings_count'] or not report['readback']['existing_binding_semantics_match'] or not report['readback']['existing_config_semantics_match'] or not report['readback']['global_and_provider_bracket_match']:
        raise SystemExit('post_route_gate_failed; STOP all further transitions and notify root')

if __name__=='__main__': main()
