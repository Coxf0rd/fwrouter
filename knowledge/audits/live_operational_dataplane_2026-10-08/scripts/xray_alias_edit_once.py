#!/usr/bin/env python3
"""One metadata-only alias edit for the exact private test client."""
import hashlib, json, time, urllib.parse, urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE="http://127.0.0.1:5000/api/v2"
SECRET=Path('/run/fwrouter-v2/live-bench-xray/client-private.json')
OUT=Path(__file__).resolve().parents[1]/'XRAY_ALIAS_EDIT_RAW.json'
ALIAS='audit-live-20261008-e4e1e7eacb-edited'

def api(method,path,payload=None):
    data=None if payload is None else json.dumps(payload).encode()
    req=urllib.request.Request(BASE+path,data=data,method=method,headers={'Accept':'application/json','Content-Type':'application/json'})
    t=time.perf_counter_ns()
    with urllib.request.urlopen(req,timeout=20) as r: code=r.status; body=json.loads(r.read())
    return code,body,(time.perf_counter_ns()-t)/1e6

def main():
    if OUT.exists(): raise SystemExit('once_guard_exists')
    ident=json.loads(SECRET.read_text())
    cid=str(ident.get('client_id') or ident.get('uuid') or ident.get('id') or '')
    if not cid: raise SystemExit('private_identity_shape_invalid')
    st,body,read_ms=api('GET','/xray/clients')
    clients=body.get('data',{}).get('clients',[]) if body.get('ok') else []
    matches=[c for c in clients if str(c.get('client_id') or c.get('client_uuid') or c.get('id') or '')==cid]
    if st!=200 or len(matches)!=1: raise SystemExit('exact_test_identity_not_unique')
    current=str(matches[0].get('alias') or '')
    if not current.startswith('audit-live-20261008-'): raise SystemExit('alias_scope_mismatch')
    path='/xray/clients/'+urllib.parse.quote(cid,safe='')
    st,result,ms=api('PATCH',path,{'alias':ALIAS,'requested_by':'operational_live_audit'})
    if st!=200 or not result.get('ok'): raise SystemExit('alias_patch_failed')
    st,body,verify_ms=api('GET','/xray/clients')
    clients=body.get('data',{}).get('clients',[]) if body.get('ok') else []
    matches=[c for c in clients if str(c.get('client_id') or c.get('client_uuid') or c.get('id') or '')==cid]
    if st!=200 or len(matches)!=1 or matches[0].get('alias')!=ALIAS: raise SystemExit('alias_readback_failed')
    out={'captured_at_utc':datetime.now(timezone.utc).isoformat(),'source_baseline':'6ba7f6f','classification':'measured_live_xray_alias_metadata_edit',
      'method':'PATCH','path':'/xray/clients/{private-test-client-id}','requested_by':'operational_live_audit',
      'previous_alias_sha256':hashlib.sha256(current.encode()).hexdigest(),'new_alias_sha256':hashlib.sha256(ALIAS.encode()).hexdigest(),
      'identity_read_before_ms':round(read_ms,3),'patch_http_status':st,'patch_response_ms':round(ms,3),'readback_ms':round(verify_ms,3),
      'exact_identity_count':len(matches),'alias_readback_match':True,
      'runtime_effect':'source path is metadata-only; no Xray native config apply/reload expected; container events not yet inspected',
      'limitation':'API latency and readback only; no per-call DB/resource attribution'}
    OUT.write_text(json.dumps(out,indent=2)+'\n')
    print(json.dumps(out,indent=2))
if __name__=='__main__':main()
