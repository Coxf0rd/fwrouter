"""Measure safe backend bootstrap foundation against a private temporary SQLite root."""
import json, os, sys, time, collections, resource
from pathlib import Path
state=Path(sys.argv[1]).resolve()
os.environ['FWROUTER_STATE_DIR']=str(state)
os.environ['FWROUTER_STARTUP_RECOVERY_ENABLED']='false'
sys.path.insert(0,'/srv/fwrouter/backend')
from fwrouter_api.services import bootstrap
from fwrouter_api.db import connection as db
bootstrap._run_startup_dnsmasq_reconcile_step=lambda:{'ok':True,'skipped':True,'reason':'isolated_no_runtime'}
real_connect=db.connect
trace=[]
def traced_connect():
    c=real_connect(); c.set_trace_callback(trace.append); return c
db.connect=traced_connect
rows=[]
for run in range(5):
    trace.clear(); dbpath=state/'fwrouter.db'; before={x.name:(x.stat().st_size if x.exists() else 0) for x in [dbpath,Path(str(dbpath)+'-wal'),Path(str(dbpath)+'-shm')]}
    usage_before=resource.getrusage(resource.RUSAGE_SELF)
    t=time.perf_counter(); result=bootstrap.bootstrap_backend(); elapsed=(time.perf_counter()-t)*1000
    usage_after=resource.getrusage(resource.RUSAGE_SELF)
    after={x.name:(x.stat().st_size if x.exists() else 0) for x in [dbpath,Path(str(dbpath)+'-wal'),Path(str(dbpath)+'-shm')]}
    verbs=collections.Counter((s.lstrip().split(None,1) or [''])[0].upper() for s in trace)
    rows.append({'run':run+1,'elapsed_ms':round(elapsed,3),'process_user_cpu_ms':round((usage_after.ru_utime-usage_before.ru_utime)*1000,3),'process_system_cpu_ms':round((usage_after.ru_stime-usage_before.ru_stime)*1000,3),'process_cpu_ms':round(((usage_after.ru_utime-usage_before.ru_utime)+(usage_after.ru_stime-usage_before.ru_stime))*1000,3),'process_context_switch_delta':{'voluntary':usage_after.ru_nvcsw-usage_before.ru_nvcsw,'involuntary':usage_after.ru_nivcsw-usage_before.ru_nivcsw},'process_fault_delta':{'minor':usage_after.ru_minflt-usage_before.ru_minflt,'major':usage_after.ru_majflt-usage_before.ru_majflt},'process_block_io_delta':{'input':usage_after.ru_inblock-usage_before.ru_inblock,'output':usage_after.ru_oublock-usage_before.ru_oublock},'process_lifetime_peak_rss_kib_not_per_operation':usage_after.ru_maxrss,'schema_ok':bool((result.get('database_schema') or {}).get('ok')),'schema_version':(result.get('database_schema') or {}).get('actual_schema_version'),'startup_recovery_enabled':result.get('startup_recovery_enabled'),'recovery':'disabled','dnsmasq':'isolated_stub','sql_statement_count':len(trace),'sql_verb_counts':dict(verbs),'db_sizes_before':before,'db_sizes_after':after,'stale_jobs_changed':(result.get('stale_jobs_cleaned') or {}).get('updated_count',0),'normalized_subject_count':(result.get('subject_taxonomy') or {}).get('normalized_external_network_client_count',0),'builtin_subject_count':(result.get('builtin_system_subjects') or {}).get('normalized_count',0)})
print(json.dumps(rows,indent=2))
