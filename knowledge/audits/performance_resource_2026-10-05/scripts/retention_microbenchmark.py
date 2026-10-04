"""Isolated synthetic JSONL retention cost; never accesses production state."""
import json
import statistics
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from fwrouter_api.services.logs_retention import _cleanup_jsonl_file

original_open = Path.open
results = []
with tempfile.TemporaryDirectory(prefix='fwrouter-stage4-retention-') as directory:
    path = Path(directory) / 'synthetic.jsonl'
    record = json.dumps({'created_at': datetime.now(timezone.utc).isoformat(), 'message': 'synthetic audit record', 'detail': 'x' * 100}) + '\n'
    path.write_text(record * 10000)
    input_bytes = path.stat().st_size
    for _ in range(5):
        counter = {'bytes': 0}
        class Writer:
            def __init__(self, handle): self.handle = handle
            def write(self, value):
                counter['bytes'] += len(value.encode('utf-8'))
                return self.handle.write(value)
            def __getattr__(self, name): return getattr(self.handle, name)
        def tracked_open(target, *args, **kwargs):
            handle = original_open(target, *args, **kwargs)
            mode = args[0] if args else kwargs.get('mode', 'r')
            return Writer(handle) if target.suffix == '.tmp' and 'w' in mode else handle
        wall, cpu = time.perf_counter(), time.process_time()
        with patch.object(Path, 'open', tracked_open):
            result = _cleanup_jsonl_file(path, timestamp_field='created_at', retention_days=14, dry_run=False)
        results.append({'wall_ms': (time.perf_counter()-wall)*1000, 'cpu_ms': (time.process_time()-cpu)*1000,
                        'temp_write_bytes': counter['bytes'], 'deleted_lines': result['deleted_lines'], 'rewritten': result['rewritten']})
    assert all(r['deleted_lines'] == 0 and not r['rewritten'] and r['temp_write_bytes'] == input_bytes for r in results)
print(json.dumps({'method': 'synthetic 10000 current records, isolated temporary directory; counted write calls, not SSD physical writes',
                  'samples': results, 'input_bytes': input_bytes, 'p50_wall_ms': statistics.median(r['wall_ms'] for r in results),
                  'max_wall_ms': max(r['wall_ms'] for r in results)}, indent=2))
