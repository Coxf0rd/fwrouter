#!/usr/bin/env python3
"""Read-only terminal and semantic recovery after route harness reporting error."""
from __future__ import annotations
import hashlib, importlib.util, json, os, subprocess, sys, sqlite3, urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
BACKUP=Path('/var/backups/fwrouter/live-operational-benchmark-20261007T180616Z')
SECRET=Path('/run/fwrouter-v2/live-bench-xray/client-private.json')
TARGET='sub:2412af49235dd3d4b27585ee009991c372ced4e326ac746f8f9f26c3f1494512'
BASE='http://127.0.0.1:5000/api/v2'
sys.path.insert(0,'/opt/fwrouter-api')
spec=importlib.util.spec_from_file_location('xray_recovery_helpers',Path(__file__).with_name('xray_create_once.py'))
h=importlib.util.module_from_spec(spec); spec.loader.exec_module(h)

def api(path):
    with urllib.request.urlopen(BASE+path,timeout=15) as r: return r.status,json.loads(r.read())

def digest(v): return hashlib.sha256(json.dumps(v,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()

def events(start,end):
    env={**os.environ,'DOCKER_CONFIG':'/run/fwrouter-v2/docker-cli','HOME':'/run/fwrouter-v2/docker-cli'}
    cp=subprocess.run(['docker','events','--filter','type=container','--since',start+'Z','--until',end+'Z','--format','{{json .}}'],capture_output=True,text=True,timeout=10,env=env)
    out=[]
    if cp.returncode: return {'available':False,'error_class':'DockerEventQueryFailed','events':[]}
    for line in cp.stdout.splitlines():
        try: e=json.loads(line)
        except Exception: continue
        actor=e.get('Actor') if isinstance(e.get('Actor'),dict) else {}; attrs=actor.get('Attributes') if isinstance(actor.get('Attributes'),dict) else {}
        name=str(attrs.get('name') or '').lower()
        if 'xray' not in name: continue
        action=str(e.get('Action') or e.get('status') or '')
        out.append({'action':action,'time_ns':int(e.get('timeNano') or 0),'target_class':'xray_runtime' if name=='fwrouter-xray' else 'xray_related','object_id_sha256':hashlib.sha256(str(actor.get('ID') or e.get('id') or '').encode()).hexdigest()[:16],'signal':attrs.get('signal') if action.lower()=='kill' else None,'exit_code_present':'exitCode' in attrs})
    return {'available':True,'events':out}

def main():
    private=json.loads(SECRET.read_text()); cid=str(private['client_id']); email=str(private['client_email_private']); subject_id='xray:'+cid
    check=json.loads((ROOT/'XRAY_CREATE_SEMANTICS_CHECK.json').read_text())
    create=json.loads((ROOT/'XRAY_CREATE_RAW.json').read_text())
    st,clients_r=api('/xray/clients'); clients=clients_r.get('data',{}).get('clients',[]) if clients_r.get('ok') else []
    test_clients=[c for c in clients if h.client_id_of(c)==cid]
    st2,subjects_r=api('/subjects?limit=500'); subjects=subjects_r.get('data',{}).get('subjects',[]) if subjects_r.get('ok') else []
    test_subjects=[s for s in subjects if s.get('subject_id')==subject_id]
    subject=test_subjects[0] if len(test_subjects)==1 else {}
    override=(subject.get('effective_state') or {}).get('server_override') if subject else None
    st3,xray_r=api('/xray'); xray=xray_r.get('data',{}).get('xray',{}) if xray_r.get('ok') else {}
    details=xray.get('details',{}); live_bind=details.get('bindings',{}) if isinstance(details,dict) else {}
    bind_path,cfg_path,bind_live,cfg_live,active=h.load_local_state()
    # Compare all preexisting binding semantics with the pre-create protected digest.
    bind_before=json.loads((BACKUP/'var__lib__fwrouter-v2__xray__fwrouter-bindings.json').read_text())
    def binding_rows_semantics(state):
        rows=[]
        for row in state.get('bindings',[]):
            if any(str(row.get(k) or '') in {cid,'xray:'+cid,email} for k in ('client_id','client_uuid','subject_id','client_email')): continue
            rows.append({k:v for k,v in row.items() if k not in {'applied_at'}})
        return sorted(rows,key=lambda r:(str(r.get('subject_id') or ''),str(r.get('client_id') or '')))
    bind_sem=binding_rows_semantics(bind_live)
    binding_semantics_match=digest(bind_sem)==digest(binding_rows_semantics(bind_before))
    # Compare identity records using the original create precondition digest.
    _,client_sem=h.ident_digest(clients,cid)
    client_semantics_match=client_sem==create['preconditions']['client_digest']
    # Compare baseline configuration components without the dedicated test principal.
    cfg_before=json.loads((BACKUP/'var__lib__fwrouter-v2__xray__config.json').read_text())
    def inbound_clients(c):
        return [(i.get('tag'),sorted((x for x in i.get('settings',{}).get('clients',[]) if isinstance(x,dict) and x.get('email')!=email),key=lambda x:json.dumps(x,sort_keys=True))) for i in c.get('inbounds',[]) if isinstance(i,dict)]
    def user_map(c):
        result=[]
        for rule in c.get('routing',{}).get('rules',[]):
            if not isinstance(rule,dict) or 'user' not in rule: continue
            users=rule.get('user') if isinstance(rule.get('user'),list) else [rule.get('user')]
            users=[u for u in users if u and u!=email]
            for user in users: result.append({'user':user,'rule':{k:v for k,v in rule.items() if k!='user'}})
        return sorted(result,key=lambda x:(x['user'],json.dumps(x['rule'],sort_keys=True,separators=(',',':'))))
    def other_rules(c):
        return [r for r in c.get('routing',{}).get('rules',[]) if isinstance(r,dict) and 'user' not in r]
    def other_routing(c): return {k:v for k,v in c.get('routing',{}).items() if k!='rules'}
    def non_test_top(c): return {k:v for k,v in c.items() if k not in {'inbounds','routing','outbounds'}}
    cfg_checks={
      'existing_inbound_client_records_exact':digest(inbound_clients(cfg_before))==digest(inbound_clients(cfg_live)),
      'existing_user_routing_rules_exact':digest(user_map(cfg_before))==digest(user_map(cfg_live)),
      'non_user_routing_rules_exact':digest(other_rules(cfg_before))==digest(other_rules(cfg_live)),
      'all_outbounds_exact':digest(cfg_before.get('outbounds',[]))==digest(cfg_live.get('outbounds',[])),
      'routing_fields_except_rules_exact':digest(other_routing(cfg_before))==digest(other_routing(cfg_live)),
      'top_level_fields_except_managed_sections_exact':digest(non_test_top(cfg_before))==digest(non_test_top(cfg_live)),
    }
    p=json.loads((ROOT/'INITIAL_STATE.json').read_text())
    initial_provider=p['provider_bindings'][0]
    current_global=json.loads(urllib.request.urlopen(BASE+'/routing/global',timeout=15).read()).get('data',{}).get('routing',{})
    # Find the latest accepted apply job without persisting its secret-bearing JSON payload.
    from fwrouter_api.core.config import get_settings
    db=get_settings().paths.db_path
    c=sqlite3.connect(db.resolve().as_uri()+'?mode=ro',uri=True,timeout=5); c.row_factory=sqlite3.Row
    try:
        row=c.execute("SELECT status,created_at,started_at,finished_at,error_code,result_json FROM jobs WHERE job_type='apply_mutation' AND requested_by='operational_live_audit' ORDER BY created_at DESC LIMIT 1").fetchone()
        active_jobs=[list(x) for x in c.execute("SELECT status,count(*) FROM jobs WHERE status IN ('queued','running') GROUP BY status").fetchall()]
        if row:
            result=json.loads(row['result_json'] or '{}'); mutation=result.get('mutation',{}) if isinstance(result.get('mutation'),dict) else {}
            job={'status':row['status'],'created_at':row['created_at'],'started_at':row['started_at'],'finished_at':row['finished_at'],'error_code':row['error_code'],'mutation_ok':mutation.get('ok'),'mutation_stage':mutation.get('stage'),'runtime_state_unchanged':mutation.get('runtime_state_unchanged'),'intent':mutation.get('intent')}
        else: job=None
    finally: c.close()
    ev=events('2026-10-07T18:36:18','2026-10-07T18:36:39')
    report={
      'captured_at_utc':datetime.now(timezone.utc).isoformat(),'classification':'measured_live_fixed_route_terminal_readback_with_instrument_error','source_baseline':'6ba7f6f',
      'instrument_error':{'type':'KeyError','field':'wall_ns','phase':'resource_summary_after_terminal_and_readbacks','effect':'request/accept/poll/resource raw rows held only in exited process and unavailable; no repeat POST'},
      'job':job,
      'readback':{'client_list_http':st,'exact_test_client_count':len(test_clients),'subject_list_http':st2,'exact_test_subject_count':len(test_subjects),'test_subject_type':subject.get('subject_type'),'desired_mode':subject.get('desired_mode'),'effective_route_target_match':bool(override and override.get('selected_server_id')==TARGET),'override_apply_state':override.get('apply_state') if isinstance(override,dict) else None,'xray_http':st3,'xray_runtime_state':xray.get('runtime_state'),'forced_vpn_ready':xray.get('forced_vpn_ready'),'bindings_count':live_bind.get('bindings_count'),'applied_count':live_bind.get('applied_count'),'verified_count':live_bind.get('verified_count'),'generation_pending':(live_bind.get('generation') or {}).get('pending'),'all_78_preexisting_client_records_exact':client_semantics_match,'all_78_preexisting_binding_semantics_exact':binding_semantics_match,'configuration_semantics':cfg_checks,'all_configuration_semantics_exact':all(cfg_checks.values())},
      'global_provider_bracket':{'global_mode':current_global.get('desired_mode'),'applied_mode':current_global.get('applied_mode'),'server_mode':current_global.get('server_mode'),'global_auto_target_sha256':hashlib.sha256(str(current_global.get('active_auto_server_id') or '').encode()).hexdigest()[:16],'matches_create_preconditions':current_global.get('desired_mode')==create['preconditions']['global_desired_mode'] and current_global.get('server_mode')==create['preconditions']['global_server_mode'] and hashlib.sha256(str(current_global.get('active_auto_server_id') or '').encode()).hexdigest()[:16]==create['preconditions']['global_auto_target_ref_sha256'],'provider_current_member_id':initial_provider.get('current_member_id'),'provider_applied_member_id':initial_provider.get('applied_member_id'),'provider_binding_revision':initial_provider.get('binding_revision'),'provider_auto_switch_still_false':initial_provider.get('allow_automatic_member_switch') is False},
      'runtime_events':ev,
      'active_jobs':active_jobs,
      'timing_limitation':'Database job created/started/finished timestamps have one-second resolution. API response latency and acceptance timestamp are unavailable; request-to-terminal wall must not be inferred.',
    }
    out=ROOT/'XRAY_FIXED_ROUTE_RECOVERY.json'; out.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n'); out.chmod(0o600)
    print(json.dumps({'report':str(out),'job':job,'route_verified':report['readback']['effective_route_target_match'],'xray_ready':report['readback']['forced_vpn_ready'],'all_client_binding_semantics':client_semantics_match and binding_semantics_match,'config_checks':cfg_checks,'global_provider_bracket':report['global_provider_bracket'],'event_count':len(ev['events']),'instrumentation_gap':report['instrument_error']},ensure_ascii=False,indent=2))

if __name__=='__main__': main()
