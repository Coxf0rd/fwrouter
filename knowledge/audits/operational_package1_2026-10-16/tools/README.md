# Reproducing Package 1 measurements

Use CPython3.11.2 and a **development** environment containing HTTPX/PyYAML with the project CI dependency versions. Do not install anything into production or use production configurations. The sanitizer tool uses only stdlib and loads the selected source file directly. The metadata tool imports the selected backend module, constructs private temporary synthetic configurations, disables socket observations and makes no controller calls. It clears inherited environment; neither tool loads dotenv/DB/user data. Run the pair sequentially without other benchmarks, using the same interpreter/dependencies and repeat count. Optional Linux network-namespace isolation is useful but is not a substitute for the explicit synthetic paths/transport setup.

The measured metadata pair used the same CPython3.11.2 interpreter, five baseline and seven candidate timings per shape; sanitizer five each. Timings exclude tracemalloc and call-count instrumentation, which have separate passes. The exact raw samples and source digests are under `../evidence/`. For a new repeated comparison use seven trials for each source and capture environment/versions separately. Don't overwrite accepted receipts.

Example (source checkout, private workspace already created; replace interpreter with your development Python):

```sh
mkdir -p "$audit_workspace/base"
git archive baafbf209331da763d95bae40c32e4176b158e4d backend | tar -x -C "$audit_workspace/base"
python knowledge/audits/operational_package1_2026-10-16/tools/benchmark_metadata.py \
  --backend "$audit_workspace/base/backend" --label baseline --repeats 7 \
  --json-out "$audit_workspace/metadata-baseline.json"
python knowledge/audits/operational_package1_2026-10-16/tools/benchmark_metadata.py \
  --backend "$PWD/backend" --label candidate --repeats 7 \
  --json-out "$audit_workspace/metadata-candidate.json"
env -i /usr/bin/python3 knowledge/audits/operational_package1_2026-10-16/tools/benchmark_sanitizer.py \
  "$audit_workspace/base" "$audit_workspace/sanitizer-baseline.json"
env -i /usr/bin/python3 knowledge/audits/operational_package1_2026-10-16/tools/benchmark_sanitizer.py \
  "$PWD" "$audit_workspace/sanitizer-candidate.json"
```

`audit_workspace` must be an explicitly selected private temporary directory outside source/live roots. Do not use live `/var/lib`, `/etc`, `/opt` or backups for fixture input/output. The examples write only source archives, synthetic temporary data and measurement receipts.

Compare metadata outputs after normalizing only the generated temporary config/contours path fields; all other fields and input sizes must match. Sanitizer digests must match exactly. Report median/min/max, sample count and CPU convention. Metadata CPU uses `thread_time`; sanitizer CPU uses `process_time` in a single-threaded measurement. Process RSS/HWM spans fixture/import/history; only separate tracemalloc passes measure incremental Python allocation. Native readback, full application startup, packet forwarding and full PR CI are different gates and are not replaced by these tools.

Comment-marker edge runner: `benchmark_comment_literals.py --help`. Final evidence uses paired A/B/A/B processes, five untraced samples each, same frozen base/candidate source and cleared environment. Compare output digests before interpreting timings.
