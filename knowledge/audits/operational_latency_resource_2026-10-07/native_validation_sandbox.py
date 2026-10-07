"""Run one bounded synthetic native config validation in a no-network container."""
import fcntl, json, re, subprocess, tempfile, time
from pathlib import Path

ROOT=Path('/srv/fwrouter')
OUT=ROOT/'knowledge/audits/operational_latency_resource_2026-10-07'
IMAGE_MIHOMO='sha256:ab5d4cf7e192b941f7a238bdb2450d7437d3499b51da1c13669945ba1a138529'
IMAGE_XRAY='sha256:60c138250e2dca6e54259a1333188692fdf2560cb2dae7f6610b3ee92f6c3ff1'
MIHOMO='''mixed-port: 7890\nallow-lan: false\nmode: rule\nlog-level: warning\nexternal-controller: 127.0.0.1:9090\nproxies:\n  - {name: DIRECT, type: direct}\nproxy-groups:\n  - {name: PROXY, type: select, proxies: [DIRECT]}\nrules:\n  - MATCH,PROXY\n'''
XRAY='''{"log":{"loglevel":"warning"},"inbounds":[{"tag":"test-in","listen":"127.0.0.1","port":10086,"protocol":"dokodemo-door","settings":{"address":"127.0.0.1"}}],"outbounds":[{"tag":"direct","protocol":"freedom","settings":{}}]}\n'''
def remove_owned_container(name):
    if not re.fullmatch(r'fwrouter-audit-(?:mihomo|xray)-[0-9]{13}',name):
        raise ValueError('Refusing cleanup for a container name outside this audit namespace')
    for _ in range(3):
        subprocess.run(['docker','rm','-f',name],capture_output=True,text=True,timeout=10)
        check=subprocess.run(['docker','container','inspect',name],capture_output=True,text=True,timeout=10)
        if check.returncode:
            if 'no such object' in check.stderr.lower() or 'no such container' in check.stderr.lower(): return True
            return False
    return False
def read_cgroup(path):
    def val(name):
        try:return (path/name).read_text().strip()
        except OSError:return None
    cpu=val('cpu.stat') or ''
    cpud={k:int(v) for line in cpu.splitlines() if len(line.split())==2 for k,v in [line.split()] if v.isdigit()}
    io=val('io.stat') or ''
    return {'cpu_stat':cpud,'memory_current':val('memory.current'),'memory_peak':val('memory.peak'),'memory_max':val('memory.max'),'cpu_max':val('cpu.max'),'pids_current':val('pids.current'),'pids_max':val('pids.max'),'io_stat':io}
def one(label,image,config,config_name,argv):
    with tempfile.TemporaryDirectory(prefix='fwrouter-native-'+label+'-') as td:
        root=Path(td); cfg=root/config_name; cfg.write_text(config); out=root/'out'; out.mkdir(); cidfile=root/'cid'
        name='fwrouter-audit-'+label+'-'+str(int(time.time()*1000))
        script=f'{argv} >/out/stdout 2>/out/stderr; rc=$?; printf "%s\\n" "$rc" >/out/rc; sleep 0.4; exit "$rc"'
        cmd=['docker','run','--pull=never','--name',name,'--network','none','--read-only','--memory=512m','--memory-swap=512m','--cpus=1','--pids-limit=128','--cap-drop=ALL','--security-opt=no-new-privileges','--tmpfs','/tmp:rw,noexec,nosuid,size=16m','-v',f'{cfg}:/config/{config_name}:ro','-v',f'{out}:/out:rw','--entrypoint','/bin/sh',image,'-c',script]
        t0=time.perf_counter(); p=subprocess.Popen(['docker','run','-d',*cmd[2:]],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        cleanup_done=False
        try:
            try: stdout,stderr=p.communicate(timeout=30)
            except subprocess.TimeoutExpired:
                p.kill();p.communicate(timeout=5);raise
            launch_ms=(time.perf_counter()-t0)*1000
            if p.returncode: return {'label':label,'phase':'launch_failed','returncode':p.returncode,'stderr':stderr[-2000:],'launch_ms':launch_ms,'command':' '.join(cmd)}
            cid=stdout.strip(); cg=Path('/sys/fs/cgroup/system.slice')/f'docker-{cid}.scope'
            deadline=time.monotonic()+30; start=time.perf_counter(); marker=False; observations=[]
            while time.monotonic()<deadline:
                if cg.exists(): observations.append(read_cgroup(cg))
                if (out/'rc').exists(): marker=True; validation_ms=(time.perf_counter()-start)*1000; break
                time.sleep(.005)
            wait=subprocess.run(['docker','wait',cid],capture_output=True,text=True,timeout=30)
            logs=subprocess.run(['docker','logs',cid],capture_output=True,text=True,timeout=10)
            final_rc=(out/'rc').read_text().strip() if (out/'rc').exists() else None
            final_cgroup=observations[-1] if observations else {}
            peaks={}
            for key in ('memory_peak','pids_current'):
                vals=[]
                for sample in observations:
                    try: vals.append(int(sample[key]))
                    except (ValueError,TypeError): pass
                peaks[key]=max(vals) if vals else None
            result={'label':label,'image_id':image,'container_name':name,'cgroup_v2_path':str(cg),'container_limits':{'network':'none','rootfs':'read-only','cpu':'1 core','memory':'512 MiB','pids':'128','capabilities':'all dropped','tmpfs':'16 MiB'},'launch_to_docker_return_ms':round(launch_ms,3),'launch_to_validation_marker_ms':round(validation_ms,3) if marker else None,'validation_returncode':int(final_rc) if final_rc and final_rc.isdigit() else None,'docker_wait_returncode':wait.returncode,'docker_wait_output':wait.stdout.strip(),'container_resource_samples':len(observations),'container_peak_memory_bytes_sampled':peaks['memory_peak'],'container_peak_pids_sampled':peaks['pids_current'],'container_cpu_stat_at_end':final_cgroup.get('cpu_stat'),'container_memory_current_at_end':final_cgroup.get('memory_current'),'container_io_stat_at_end':final_cgroup.get('io_stat'),'native_stdout':(out/'stdout').read_text()[-1500:] if (out/'stdout').exists() else logs.stdout[-1500:],'native_stderr':(out/'stderr').read_text()[-1500:] if (out/'stderr').exists() else logs.stderr[-1500:],'returncode':int(final_rc) if final_rc and final_rc.isdigit() else None,'command':'docker run --pull=never --network none --read-only --memory=512m --cpus=1 --pids-limit=128; synthetic config bind-mounted read-only','sample_interval_ms':5,'limitations':'One synthetic candidate per pinned local image; container launch plus validation is timed; cgroup CPU includes command runtime/startup and small post-validation hold; 5ms samples may miss brief memory peaks.'}
            result['container_removed']=remove_owned_container(name);cleanup_done=True
            return result
        finally:
            if not cleanup_done:remove_owned_container(name)
if __name__=='__main__':
    lock=open('/tmp/fwrouter-operational-audit.lock','w'); fcntl.flock(lock,fcntl.LOCK_EX)
    results=[one('mihomo',IMAGE_MIHOMO,MIHOMO,'config.yaml','/mihomo -t -f /config/config.yaml'),one('xray',IMAGE_XRAY,XRAY,'config.json','/usr/bin/xray -test -config /config/config.json')]
    (OUT/'native_validation_metrics.json').write_text(json.dumps(results,indent=2)+'\n')
    print(json.dumps(results,indent=2))
    fcntl.flock(lock,fcntl.LOCK_UN)
