import hashlib, json, os, resource, sys, tempfile, time
from pathlib import Path
SOURCE = Path(os.environ.get('FWROUTER_SOURCE_ROOT', '/srv/fwrouter')).resolve()
ROOT = Path(tempfile.mkdtemp(prefix='fwrouter-pass1-proof-')).resolve()
os.environ.update({'FWROUTER_STATE_DIR':str(ROOT/'state'),'FWROUTER_ENVIRONMENT':'test','PYTHONDONTWRITEBYTECODE':'1'})
sys.path.insert(0, str(SOURCE/'backend'))
sys.path.insert(0, str(SOURCE/'backend/tests'))
import pytest
from fwrouter_api.core.config import Settings, get_settings
from fwrouter_api.core.paths import FWRouterPaths
Settings.model_config['env_file'] = None
Settings.paths = property(lambda _self: FWRouterPaths(etc_dir=ROOT/'etc',state_dir=ROOT/'state',log_dir=ROOT/'logs',run_dir=ROOT/'run'))
get_settings.cache_clear()
settings=get_settings()
from fwrouter_api.db.connection import initialize_database
initialize_database()
from fwrouter_api.db import connection as dbc
from fwrouter_api.services.live_probe_cache import clear_live_probe_cache
from fwrouter_api.services import runtime, system_summary
mp=pytest.MonkeyPatch()
import conftest
conftest._install_no_live_runtime_guards(mp)
conftest._install_no_live_subprocess_guard(mp)
from fwrouter_api.adapters.mihomo import MihomoHealth, MihomoRuntimeState
from fwrouter_api.adapters.xray_common import XrayHealth, XrayRuntimeState
runtime._cached_mihomo_health=lambda: MihomoHealth(runtime_state=MihomoRuntimeState.NOT_CONFIGURED,details={'adapter':'isolated-fixture'})
runtime._cached_xray_health=lambda: XrayHealth(runtime_state=XrayRuntimeState.NOT_CONFIGURED,message='isolated fixture',details={'adapter':'isolated-fixture'})
from fwrouter_api.services import dataplane_status, dataplane_global
fixture_mihomo=MihomoHealth(runtime_state=MihomoRuntimeState.NOT_CONFIGURED,details={'adapter':'isolated-fixture'})
dataplane_global._mihomo_health=lambda: fixture_mihomo
dataplane_status.read_live_dataplane_payload=lambda: None
# Explicit deny socket access and count native/runtime enforcement boundaries.
import socket
socket.create_connection=lambda *a,**k: (_ for _ in ()).throw(RuntimeError('network denied'))
socket.socket.connect=lambda *a,**k: (_ for _ in ()).throw(RuntimeError('network denied'))
orig_enforcement=runtime.build_runtime_enforcement_state
calls={'enforcement':0,'live_payload':0,'summary_build':0}
def counted_enforcement(*a,**kw):
    calls['enforcement']+=1
    return orig_enforcement(*a,**kw)
runtime.build_runtime_enforcement_state=counted_enforcement
system_summary.build_runtime_enforcement_state=counted_enforcement
orig_live=runtime.read_live_dataplane_payload
def counted_live(*a,**kw):
    calls['live_payload']+=1
    return orig_live(*a,**kw)
runtime.read_live_dataplane_payload=counted_live
orig_connect=dbc.connect
sql={'connect':0,'kinds':{}}
def kind(q):
    w=q.lstrip().split(None,1)[0].split('(',1)[0].upper() if q.strip() else 'EMPTY'
    return w if w in {'SELECT','INSERT','UPDATE','DELETE','REPLACE','PRAGMA','BEGIN','COMMIT','ROLLBACK','CREATE','ALTER','DROP','WITH'} else 'OTHER'
def traced_connect():
    c=orig_connect(); sql['connect']+=1
    def trace(q):
        k=kind(q); sql['kinds'][k]=sql['kinds'].get(k,0)+1
    c.set_trace_callback(trace); return c
dbc.connect=traced_connect

def proc_io():
    d={}
    for ln in Path('/proc/self/io').read_text().splitlines():
        k,_,v=ln.partition(':')
        if k in {'rchar','wchar','read_bytes','write_bytes','syscr','syscw'}: d[k]=int(v.strip())
    return d

def run_summary():
    from fwrouter_api.db.schema_state import summarize_schema_state
    schema={'ok':True,'status':'ok','expected_schema_version':1,'actual_schema_version':1,'rebuild_required':False,'problem_count':0,'problems':[],'tables':{}}
    schema_summary=summarize_schema_state(schema)
    samples=[]; out_hash=None
    for _ in range(3):
        clear_live_probe_cache(); sql['connect']=0; sql['kinds'].clear(); calls.update(enforcement=0,live_payload=0)
        cpu0=time.process_time(); io0=proc_io(); rss0=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss; t0=time.perf_counter()
        result=system_summary._build_system_summary_uncached(schema_summary=schema_summary)
        wall=(time.perf_counter()-t0)*1000; cpu=(time.process_time()-cpu0)*1000; io1=proc_io(); rss1=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        digest=hashlib.sha256(json.dumps(result,sort_keys=True,ensure_ascii=False,default=str).encode()).hexdigest()
        samples.append({'wall_ms':round(wall,3),'process_cpu_ms':round(cpu,3),'peak_rss_delta_kib':max(0,rss1-rss0),'sql_connections':sql['connect'],'sql':dict(sql['kinds']),'calls':dict(calls),'io_delta':{k:io1[k]-io0.get(k,0) for k in io1},'summary_sha256':digest})
        out_hash=digest
    print(json.dumps({'mode':'summary','samples':samples,'runtime_enforcement':result['backend']['runtime_enforcement'],'scoped_egress':result['backend']['readiness']['scoped_egress'],'summary_sha256':out_hash,'wal_bytes':Path(str(settings.paths.db_path)+'-wal').stat().st_size if Path(str(settings.paths.db_path)+'-wal').exists() else 0,'sqlite_bytes':settings.paths.db_path.stat().st_size},sort_keys=True))

def run_logs():
    from fwrouter_api.services.logs import write_operational_log, list_operational_logs
    target=settings.paths.operational_events_path
    sql['connect']=0; sql['kinds'].clear(); calls.update(enforcement=0,live_payload=0)
    cpu0=time.process_time(); io0=proc_io(); rss0=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss; t0=time.perf_counter()
    events=[]
    for n in range(100):
        events.append(write_operational_log(event_type='pass1_fixture',message='isolated event',details={'event_code':'pass1.fixture','sequence':n}))
    wall=(time.perf_counter()-t0)*1000; cpu=(time.process_time()-cpu0)*1000; io1=proc_io(); rss1=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    writer_sql={'connections':sql['connect'],'kinds':dict(sql['kinds'])}
    readback=list_operational_logs(limit=100,event_type='pass1_fixture')
    lines=target.read_bytes().splitlines()
    print(json.dumps({'mode':'logging','wall_ms':round(wall,3),'process_cpu_ms':round(cpu,3),'peak_rss_delta_kib':max(0,rss1-rss0),'sql_connections':writer_sql['connections'],'sql':writer_sql['kinds'],'readback_selects':sql['kinds'].get('SELECT',0)-writer_sql['kinds'].get('SELECT',0),'io_delta':{k:io1[k]-io0.get(k,0) for k in io1},'rows':len(readback),'jsonl_lines':len(lines),'jsonl_bytes':target.stat().st_size,'wal_bytes':Path(str(settings.paths.db_path)+'-wal').stat().st_size if Path(str(settings.paths.db_path)+'-wal').exists() else 0,'sqlite_bytes':settings.paths.db_path.stat().st_size,'events_sha256':hashlib.sha256(json.dumps(events,sort_keys=True,ensure_ascii=False).encode()).hexdigest(),'jsonl_sha256':hashlib.sha256(b'\n'.join(lines)).hexdigest()},sort_keys=True))

try:
    run_summary() if sys.argv[1]=='summary' else run_logs()
finally:
    mp.undo()
