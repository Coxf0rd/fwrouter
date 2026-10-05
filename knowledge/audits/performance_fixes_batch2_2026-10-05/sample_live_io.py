#!/usr/bin/env python3
"""Bounded passive /proc and serial normal-GET sample; emits sanitized aggregates only."""
from __future__ import annotations
import json, os, pathlib, subprocess, time, urllib.request
from datetime import datetime, timezone

BASE_URL = 'http://127.0.0.1:5000/api/v2'
PERIOD_S = 120
INTERVAL_S = 10


def utc(): return datetime.now(timezone.utc).isoformat(timespec='seconds')

def configured_db_path():
    """Resolve only the service's allowlisted state path; never print its environment."""
    state_dir = None
    try:
        pid = subprocess.check_output(
            ['systemctl', 'show', 'fwrouter-api.service', '-p', 'MainPID', '--value'],
            timeout=3, text=True,
        ).strip()
        env = pathlib.Path(f'/proc/{int(pid)}/environ').read_bytes().split(b'\0')
        entries = {}
        for item in env:
            if b'=' in item:
                key, value = item.split(b'=', 1)
                if key in (b'FWROUTER_STATE_DIR', b'STATE_DIR'):
                    entries[key.decode()] = value.decode(errors='replace')
        state_dir = entries.get('FWROUTER_STATE_DIR') or entries.get('STATE_DIR')
    except OSError:
        pass
    return pathlib.Path(state_dir or '/var/lib/fwrouter-v2') / 'fwrouter.db'

BASE = configured_db_path()

def pid_io(pid):
    try:
        raw = pathlib.Path(f'/proc/{pid}/io').read_text().splitlines()
        vals = {k: int(v) for line in raw if len((p := line.split(':', 1))) == 2 for k, v in [p]}
        status = pathlib.Path(f'/proc/{pid}/status').read_text().splitlines()
        rss = next((int(x.split()[1]) * 1024 for x in status if x.startswith('VmRSS:')), None)
        stat = pathlib.Path(f'/proc/{pid}/stat').read_text().split()
        cpu_ticks = int(stat[13]) + int(stat[14])
        comm = pathlib.Path(f'/proc/{pid}/comm').read_text().strip()
        return {'pid': pid, 'comm': comm, 'cpu_ticks': cpu_ticks, 'rss_bytes': rss, **vals}
    except (OSError, ValueError, IndexError): return None

def db_holders():
    out = []
    for proc in pathlib.Path('/proc').iterdir():
        if not proc.name.isdigit(): continue
        pid = int(proc.name)
        try:
            for fd in (proc / 'fd').iterdir():
                target = os.readlink(fd)
                if target in (str(BASE), str(BASE) + '-wal', str(BASE) + '-shm'):
                    row = pid_io(pid)
                    if row:
                        row['db_fd_kind'] = pathlib.Path(target).name.removeprefix(BASE.name).lstrip('-') or 'db'
                        out.append(row)
                    break
        except (OSError, PermissionError): pass
    return out

def file_state(path):
    try:
        st = pathlib.Path(path).stat()
        return {'bytes': st.st_size, 'mtime_ns': st.st_mtime_ns}
    except OSError: return None

def provider_metrics():
    with urllib.request.urlopen(BASE_URL + '/subscription', timeout=5) as r:
        obj = json.load(r)
        snap = obj.get('data', {}).get('subscription', {}).get('provider_managed', {}).get('metrics')
        return {'status': r.status, 'metrics': snap if isinstance(snap, dict) else None}

def get(url):
    start = time.monotonic()
    try:
        with urllib.request.urlopen(BASE_URL + url, timeout=5) as r:
            payload = r.read()
            return {'path': url.split('?', 1)[0], 'status': r.status,
                    'latency_ms': round((time.monotonic()-start)*1000, 2), 'payload_bytes': len(payload)}
    except Exception as e:
        return {'path': url.split('?', 1)[0], 'error_type': type(e).__name__,
                'latency_ms': round((time.monotonic()-start)*1000, 2)}

def timers():
    try:
        raw = subprocess.check_output(['systemctl','list-timers','--all','--no-legend','--no-pager'], timeout=4, text=True)
        return {'rows': sum(bool(x.strip()) for x in raw.splitlines()),
                'fwrouter_rows': sum('fwrouter' in x.lower() for x in raw.splitlines())}
    except Exception as e: return {'error_type': type(e).__name__}

def main():
    before = provider_metrics()
    request_rows = [get('/servers?limit=1000&inventory_state=active&include_provider_legacy=false'),
                    get('/selector/vpn-auto?check_on_demand=false&update_ping_state=false'),
                    get('/ui/router-summary')]
    after_requests = provider_metrics()
    samples = []
    start = time.monotonic()
    for index in range(PERIOD_S // INTERVAL_S + 1):
        pid_text = subprocess.run(['systemctl','show','fwrouter-api.service','-p','MainPID','--value'],
                                  capture_output=True, text=True, timeout=3).stdout.strip()
        api = pid_io(int(pid_text)) if pid_text.isdigit() and int(pid_text) else None
        samples.append({'at_utc': utc(), 'api': api, 'db_holders': db_holders(),
                        'db': file_state(BASE), 'wal': file_state(str(BASE)+'-wal'),
                        'shm': file_state(str(BASE)+'-shm')})
        if index < PERIOD_S // INTERVAL_S:
            time.sleep(max(0, start + (index+1)*INTERVAL_S - time.monotonic()))
    after = provider_metrics()
    print(json.dumps({'captured_at_utc': utc(), 'sample_period_s': PERIOD_S,
        'sample_interval_s': INTERVAL_S, 'samples': samples, 'normal_gets_serial': request_rows,
        'resolved_db_path': str(BASE),
        'provider_metrics': {'before': before, 'immediately_after_gets': after_requests, 'after_sample_window': after},
        'timers': timers()}, separators=(',',':'), sort_keys=True))
if __name__ == '__main__': main()
