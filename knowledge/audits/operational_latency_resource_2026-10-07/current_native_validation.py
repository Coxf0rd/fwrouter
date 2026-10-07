import fcntl, hashlib, json, re, shutil, subprocess, tempfile, time
from pathlib import Path
OUT=Path('/srv/fwrouter/knowledge/audits/operational_latency_resource_2026-10-07')
CASES=[
 {'label':'mihomo_current_generated','source':Path('/var/lib/fwrouter-v2/generated/mihomo/config.yaml'),'image':'sha256:ab5d4cf7e192b941f7a238bdb2450d7437d3499b51da1c13669945ba1a138529','binary':'/mihomo','args':['-t','-f','/config/config.yaml'],'config_dest':'/config/config.yaml','mounts':[('/var/lib/fwrouter-v2/rules','/root/.config/mihomo/rules')]},
 {'label':'xray_current_generated','source':Path('/var/lib/fwrouter-v2/xray/config.json'),'image':'sha256:60c138250e2dca6e54259a1333188692fdf2560cb2dae7f6610b3ee92f6c3ff1','binary':'/usr/bin/xray','args':['-test','-config','/etc/xray/config.json'],'config_dest':'/etc/xray/config.json','mounts':[]},
]
def cg_read(path):
 def r(name):
  try:return (path/name).read_text().strip()
  except OSError:return None
 cpu=r('cpu.stat') or ''
 cpud={k:int(v) for line in cpu.splitlines() if len(line.split())==2 for k,v in [line.split()] if v.isdigit()}
 return {'cpu':cpud,'mem_current':r('memory.current'),'mem_peak':r('memory.peak'),'pids_current':r('pids.current'),'io':r('io.stat')}
def remove_owned_container(name):
 if not re.fullmatch(r'fwrouter-audit-current-[a-z0-9_-]{1,80}-[0-9]{13}',name):
  raise ValueError('Refusing cleanup for a container name outside this audit namespace')
 for _ in range(3):
  subprocess.run(['docker','rm','-f',name],capture_output=True,text=True,timeout=10)
  check=subprocess.run(['docker','container','inspect',name],capture_output=True,text=True,timeout=10)
  if check.returncode:
   if 'no such object' in check.stderr.lower() or 'no such container' in check.stderr.lower():return True
   return False
 return False
def run(case):
 src=case['source']
 if not src.is_file():return {'label':case['label'],'status':'source_absent'}
 with tempfile.TemporaryDirectory(prefix='fwrouter-current-native-') as td:
  base=Path(td); suffix='.yaml' if 'mihomo' in case['label'] else '.json'; cfg=base/('private-config'+suffix)
  shutil.copyfile(src,cfg);cfg.chmod(0o600); digest=hashlib.sha256(cfg.read_bytes()).hexdigest();size=cfg.stat().st_size
  output=base/'out';output.mkdir(mode=0o700);name='fwrouter-audit-current-'+case['label'].replace('_','-')+'-'+str(int(time.time()*1000))
  inner=' '.join([case['binary'],*case['args']])+' >/dev/null 2>&1; rc=$?; printf "%s\\n" "$rc" >/out/rc; sleep 0.4; exit "$rc"'
  cmd=['docker','run','--pull=never','--name',name,'--network','none','--read-only','--memory=512m','--memory-swap=512m','--cpus=1','--pids-limit=128','--cap-drop=ALL','--security-opt=no-new-privileges','--tmpfs','/tmp:rw,noexec,nosuid,size=16m','-v',f'{cfg}:{case["config_dest"]}:ro']
  for host,dest in case['mounts']:
   if Path(host).is_dir():cmd.extend(['-v',f'{host}:{dest}:ro'])
  cmd.extend(['-v',f'{output}:/out:rw','--entrypoint','/bin/sh',case['image'],'-c',inner])
  start=time.perf_counter();p=subprocess.Popen(['docker','run','-d',*cmd[2:]],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
  cid=None;cleanup_done=False
  try:
   try:cidout,err=p.communicate(timeout=45)
   except subprocess.TimeoutExpired:
    p.kill();p.communicate(timeout=5);raise
   started=time.perf_counter()
   if p.returncode:return {'label':case['label'],'status':'launch_failed','config_bytes':size,'config_sha256':digest,'returncode':p.returncode,'stderr_omitted':True,'operation_start_ms':round((started-start)*1000,3)}
   cid=cidout.strip();cgroup=Path('/sys/fs/cgroup/system.slice')/f'docker-{cid}.scope';samples=[];marker_at=None;deadline=time.monotonic()+60
   while time.monotonic()<deadline:
    if cgroup.exists():samples.append(cg_read(cgroup))
    if (output/'rc').exists():marker_at=time.perf_counter();break
    time.sleep(.002)
   wait=subprocess.run(['docker','wait',cid],capture_output=True,text=True,timeout=30)
   rc=(output/'rc').read_text().strip() if (output/'rc').exists() else None
   last=samples[-1] if samples else {};memory=[];pids=[]
   for s in samples:
    try:memory.append(int(s['mem_peak']))
    except (ValueError,TypeError):pass
    try:pids.append(int(s['pids_current']))
    except (ValueError,TypeError):pass
   container_removed=remove_owned_container(name);cleanup_done=True
   return {'label':case['label'],'status':'measured','input_origin':'read-only copy of current generated config; original not changed','config_bytes':size,'config_sha256':digest,'native_validation_ok':rc=='0','validation_returncode':int(rc) if rc and rc.isdigit() else None,'operation_start_to_verified_marker_ms':round((marker_at-start)*1000,3) if marker_at else None,'docker_start_return_ms':round((started-start)*1000,3),'post_start_to_marker_ms':round((marker_at-started)*1000,3) if marker_at else None,'cgroup_v2_sample_count':len(samples),'cgroup_memory_peak_bytes':max(memory) if memory else None,'cgroup_pids_peak':max(pids) if pids else None,'cgroup_cpu_usage_usec_at_marker':(last.get('cpu') or {}).get('usage_usec'),'cgroup_cpu_stat_at_marker':last.get('cpu'),'cgroup_memory_current_at_marker':last.get('mem_current'),'cgroup_io_stat_at_marker':last.get('io'),'cgroup_limits':{'network':'none','rootfs':'read-only','cpu':'1 core','memory':'512 MiB','pids':'128','capabilities':'all dropped','tmpfs':'16 MiB'},'validation_output_saved':False,'container_removed':container_removed,'docker_wait_exit':wait.stdout.strip(),'limitations':'One current profile per runtime; elapsed includes Docker start; cgroup CPU is cumulative container use through validation marker; no apply/reload/API calls.'}
  finally:
   if not cleanup_done:remove_owned_container(name)
lock=open('/tmp/fwrouter-operational-audit.lock','w');fcntl.flock(lock,fcntl.LOCK_EX)
try:
 results=[run(c) for c in CASES]
 (OUT/'current_generated_native_validation.json').write_text(json.dumps(results,indent=2)+'\n')
 print(json.dumps(results,indent=2))
finally:fcntl.flock(lock,fcntl.LOCK_UN)
