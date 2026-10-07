# Stage 4B source-fix test receipt

Source under test: local changes on top of deployed `49591c2`. This is source/test evidence; it is not a claim that these edits have been committed or deployed.

## Gates run

- **L0:** gate manifest validation (`python3 tests/gates/gate.py validate`), `compileall` on changed backend/tests modules, and `git diff --check` — PASS. Manifest version `2026.10.07.1` adds the exact maintenance CLI path to the reviewed `maintenance` domain because the prior fail-closed planner correctly rejected this changed path as unclassified.
- **L1/L2:** targeted maintenance admission and candidate-validation contract tests — 14 passed before the final two missing-database admission cases were added; `backend/tests/test_maintenance.py` then passed 8/8.
- **L3/L5 affected regression:** `test_xray.py` + `test_maintenance.py` — 120 passed / 2 failed; lifecycle, targeted refresh and runtime-summary integration subset — 145 passed / 1 failed / 1 skipped. All three failed IDs were reproduced on exact baseline `49591c2`; see [baseline reproduction](BASELINE_FAILURE_REPRO_49591c2.md).
- **L4:** `python tests/gates/smoke.py --profile isolated --reason 'Stage 4B backend affected-domain L4 smoke'` — PASS. It installed into an owned temporary target, used an isolated DB, and checked Health plus critical state. Native validation was not requested by this component L4 profile.
- **Test-infrastructure contract:** `tests/gates/test_gate_contract.py` and `tests/gates/test_smoke_contract.py` — 29 passed after the manifest mapping/version change.
- **L5:** reviewed plan anchors and pinned Xray native readback — 64 passed / 6 failed / 1 skipped. All six failed exact IDs were reproduced at baseline `49591c2`; details and outputs are in [baseline classification](BASELINE_FAILURE_REPRO_49591c2.md). The changed Xray, subscription, maintenance, and counter contracts passed their directly affected suites. This manual execution does not claim promotion eligibility for the full planner selection.
- **L6/L7:** not run; not required by the affected-domain contract, and no staging failure acceptance was requested.

## Call-count contract for generated Mihomo candidates

The production-shaped deterministic fixture writes identical transition/final candidate bytes (same exact SHA): previous source performed two native validations; changed source performs one successful native validation when and only when the local immutable image ID was resolved. Local structural validation still runs twice. For distinct candidate SHA values, the changed source performs two native validations. Missing/invalid image identity, failed validation, file mutation, and a later generation operation do not reuse prior results. Each distinct image identity is pinned to the validator command; no persistent cache was added. Tests exercise these branches.

An actual read-only native run used the currently generated 8,591,982-byte Mihomo config mounted read-only, `--network none`, and the locally resolved immutable image ID; result PASS/exit0, 849.4 ms including Docker process/startup. See [pinned native validation](PINNED_MIHOMO_VALIDATION.json). This was config validation only, not an endpoint handshake or runtime reload.

## Scope exclusions

No provider API requests, provider PATCH, subscription refresh, member switch, forced outage, manual production runtime apply, or destructive tests were run. No Stage 5 work or L6 full suite was run. Normal-path provider request counters after the earlier approved deployment of `49591c2` remained zero during its bounded GET verification; the final source edits still require their own post-deploy verification.

Subsequent delivery: source commit `7de9f88` deployed and boundedly accepted; see [live receipt](LIVE.md). Historical test/microprofile numbers above retain their original scope.
