import importlib.util
import json
import os
import platform
import sys
import time
import tracemalloc
from pathlib import Path

sys.path.insert(0, '/srv/fwrouter/backend')
if os.environ.get('CACHE_MODULE_PATH'):
    module_spec = importlib.util.spec_from_file_location(
        'package23_cache_benchmark', os.environ['CACHE_MODULE_PATH'],
    )
    cache = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(cache)
else:
    from fwrouter_api.services import live_probe_cache as cache


def synthetic_subjects(count):
    return [
        {
            'subject_id': f'subject-{i}', 'kind': 'external_network_client',
            'name': f'Client {i}', 'is_active': True, 'is_deleted': False,
            'effective_state': {'mode': 'vpn', 'server_id': f'server-{i % 8}', 'reason': None},
            'health': {'state': 'healthy', 'checked_at': '2026-10-16T00:00:00+00:00', 'latency_ms': i % 100},
            'metadata': {'provider': 'synthetic', 'labels': [f'label-{i % 4}', 'managed']},
        }
        for i in range(count)
    ]


def tracked_case(name, operation):
    cache.clear_live_probe_cache()
    tracemalloc.start()
    before = tracemalloc.take_snapshot()
    cpu0, wall0 = time.process_time(), time.perf_counter()
    detail = operation()
    wall = time.perf_counter() - wall0
    cpu = time.process_time() - cpu0
    after = tracemalloc.take_snapshot()
    allocated = sum(stat.size_diff for stat in after.compare_to(before, 'filename'))
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    with cache._CACHE_LOCK:
        entries = len(cache._CACHE)
        inflight = len(cache._INFLIGHT)
    return {
        'case': name, 'traced_wall_seconds': wall, 'traced_cpu_seconds': cpu,
        'tracemalloc_net_bytes': allocated, 'tracemalloc_peak_bytes': peak,
        'entries_after': entries, 'inflight_after': inflight,
        'accounted_retained_bytes': getattr(cache, '_CACHE_RETAINED_BYTES', None),
        **detail,
    }


def timed_case(name, operation):
    cache.clear_live_probe_cache()
    cpu0, wall0 = time.process_time(), time.perf_counter()
    detail = operation()
    return {
        'case': name,
        'wall_seconds': time.perf_counter() - wall0,
        'cpu_seconds': time.process_time() - cpu0,
        'entries_after': len(cache._CACHE),
        'accounted_retained_bytes': getattr(cache, '_CACHE_RETAINED_BYTES', None),
        **detail,
    }


subjects100 = synthetic_subjects(100)
subjects1000 = synthetic_subjects(1000)
summary = {
    'ok': True, 'status': 'healthy',
    'runtime': {
        'mihomo': {
            'connected': True,
            'proxies': [
                {'name': f'proxy-{i}', 'type': 'vless', 'alive': True}
                for i in range(32)
            ],
        },
        'xray': {'running': True},
    },
    'subjects': subjects100,
    'modules': [
        {
            'module_id': f'module-{i}', 'enabled': i % 2 == 0,
            'lifecycle_mode': 'external', 'status': 'healthy',
        }
        for i in range(12)
    ],
    'counts': {'subjects': 100, 'modules': 12},
}


def cold_and_hits(payload):
    first = cache.get_live_probe_cache(
        'bench.payload', ttl_seconds=60, loader=lambda: payload,
    )
    cold_result = {'same_object': first is payload}
    wall0, cpu0 = time.perf_counter(), time.process_time()
    for _ in range(5000):
        cache.get_live_probe_cache('bench.payload', ttl_seconds=60, loader=lambda: None)
    cold_result.update({
        '5000_hit_wall_seconds': time.perf_counter() - wall0,
        '5000_hit_cpu_seconds': time.process_time() - cpu0,
    })
    return cold_result


def unique_live():
    for i in range(1000):
        payload = bytes(bytearray([i % 256]) * 1024)
        cache.get_live_probe_cache(
            f'bench.unique.{i}', ttl_seconds=60,
            loader=lambda payload=payload: payload,
        )
    return {'expected_unique_payloads': 1000, 'payload_bytes_each': 1024}


def expired_release():
    for i in range(1000):
        payload = bytes(bytearray([i % 256]) * 1024)
        cache.get_live_probe_cache(
            f'bench.expired.{i}', ttl_seconds=0.001,
            loader=lambda payload=payload: payload,
        )
    time.sleep(1.01)
    before = len(cache._CACHE)
    cache.get_live_probe_cache('bench.expired.trigger', ttl_seconds=60, loader=lambda: None)
    return {'expired_before_trigger': before, 'expired_after_trigger': len(cache._CACHE)}


def untraced_repeats():
    cases = [
        ('summary_100_subjects_and_32_proxies', summary),
        ('subject_projection_1000', subjects1000),
    ]
    repeated = {}
    for name, payload in cases:
        samples = []
        for _ in range(5):
            samples.append(timed_case(name, lambda payload=payload: cold_and_hits(payload)))
        repeated[name] = samples

    churn_samples = []
    for _ in range(5):
        churn_samples.append(timed_case('unique_live_1000_one_kib', unique_live))
    return {'five_repeat_cold_and_warm': repeated, 'five_repeat_unique_churn': churn_samples}


results = {
    'source': os.environ.get(
        'CACHE_SOURCE', os.popen('git -C /srv/fwrouter rev-parse HEAD').read().strip(),
    ),
    'platform': platform.platform(),
    'python': sys.version,
    'untraced_repeats': untraced_repeats(),
    'traced_diagnostics': [
        tracked_case('summary_100_subjects_and_32_proxies', lambda: cold_and_hits(summary)),
        tracked_case('subject_projection_1000', lambda: cold_and_hits(subjects1000)),
        tracked_case('unique_live_1000_one_kib', unique_live),
        tracked_case('expired_unique_1000_one_kib', expired_release),
    ],
}
out = Path(os.environ.get(
    'CACHE_BENCH_OUTPUT', '/root/fwrouter-recovery/package23-20261016/cache/baseline.json',
))
out.write_text(json.dumps(results, indent=2) + '\n')
print(json.dumps(results, indent=2))
