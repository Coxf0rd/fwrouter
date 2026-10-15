import importlib.util
import json
import os
import sqlite3
import sys
import time
import tracemalloc
from pathlib import Path

sys.path.insert(0, "/srv/fwrouter/backend")
mode = os.environ["BENCH_MODE"]
if mode == "baseline":
    spec = importlib.util.spec_from_file_location("fwrouter_api.db.baseline_connection", "/root/fwrouter-recovery/package23-20261016/io/baseline_connection.py")
    db = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(db)
else:
    from fwrouter_api.db import connection as db

root = Path(f"/root/fwrouter-recovery/package23-20261016/io/bench/{mode}/state")
root.mkdir(parents=True, exist_ok=True, mode=0o700)
db_path = root / "fwrouter.db"
db.get_db_path = lambda: db_path
with db.connect() as conn:
    conn.execute("CREATE TABLE IF NOT EXISTS synthetic (id INTEGER PRIMARY KEY, value TEXT)")
    conn.execute("INSERT OR IGNORE INTO synthetic VALUES (1, 'fixture')")
real_chmod, real_stat, real_mkdir = Path.chmod, Path.stat, Path.mkdir
counts = {"chmod": 0, "stat": 0, "mkdir": 0}
def count_chmod(self, *args, **kwargs):
    counts["chmod"] += 1
    return real_chmod(self, *args, **kwargs)
def count_stat(self, *args, **kwargs):
    counts["stat"] += 1
    return real_stat(self, *args, **kwargs)
def count_mkdir(self, *args, **kwargs):
    counts["mkdir"] += 1
    return real_mkdir(self, *args, **kwargs)
Path.chmod, Path.stat, Path.mkdir = count_chmod, count_stat, count_mkdir
results = []
for repeat in range(5):
    counts.update({key: 0 for key in counts})
    cpu_start, wall_start = time.process_time(), time.perf_counter()
    for _ in range(500):
        conn = db.connect()
        conn.execute("SELECT value FROM synthetic WHERE id=1").fetchone()
        conn.close()
    results.append({"repeat": repeat, "wall_seconds": time.perf_counter() - wall_start, "cpu_seconds": time.process_time() - cpu_start, "counts": dict(counts)})
counts.update({key: 0 for key in counts})
tracemalloc.start()
for _ in range(20):
    conn = db.connect()
    conn.execute("SELECT value FROM synthetic WHERE id=1").fetchone()
    conn.close()
current, peak = tracemalloc.get_traced_memory()
tracemalloc.stop()
print(json.dumps({"mode": mode, "source_head": "3f96e1e73be0e0fc25208a406ecc9ff259f6480f", "workload": "five repeats x 500 sequential connect + SELECT + close; separate 20-session tracemalloc run", "results": results, "tracemalloc_current_bytes": current, "tracemalloc_peak_bytes": peak, "modes": {"parent": oct(real_stat(root).st_mode & 0o777), "db": oct(real_stat(db_path).st_mode & 0o777)}, "synthetic_only": True, "prod_inputs": False}, sort_keys=True))
