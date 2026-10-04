# Test Architecture and CI/CD Foundation Report

Date: 2026-10-04
Repository source at start: `489d0e42f378ad0b42779ce51442d136083326f8`
Current implementation status: source and bounded local acceptance complete; final architecture/diff review passed. The coherent foundation commit contains this report; resolve its hash from Git history. Remote CI execution remains unverified.

## Implemented source

- `tests/gates/manifest.json` classifies all 149 discovered test files. Each file has one primary level and explicit domain, runtime dependencies, fixture owner and identity, isolation, risk/opt-in flags, timeout, output and artifact bounds. Node-level metadata overrides are supported without copying tests across levels.
- `tests/gates/gate.py` validates catalog coverage, deterministically selects suites from immutable changed paths and direct dependency edges, plans explicit L5 anchors, gates required native suites, runs serially in a sanitized test-owned environment, bounds captured output and deadlines, and classifies exact baseline IDs. Reports include commit/plan/manifest/tool binding, per-suite elapsed time and fixture identity hashes. Promotion checks exact selected-file execution coverage and evidence digests; it emits eligibility only and performs no deployment.
- `backend/tests/gate_plugin.py` supplies manifest metadata to pytest collection and denies live/destructive suites in routine runs. `backend/tests/conftest.py` suppresses environment-file loading and cleans only the test-owned temporary directory. No application runtime behavior changed.
- `tests/gates/smoke.py` defines isolated temporary-target and explicitly requested read-only profiles. Live profile requires opt-in, reason, explicit loopback URL and DB path, and only makes GET requests. It never infers a live target. Smoke acceptance tests use fake adapters and temporary SQLite only; they do not establish production or staging acceptance.
- The isolated component smoke additionally stages the backend to a fresh temp target and checks real FastAPI health and system-state routes through TestClient with a temporary canonical-schema database and lifecycle disabled. Native Mihomo/Xray validation is separately opt-in and required by complete-native/live profiles.
- `tests/gates/subset-plan` creates an explicit single-domain and/or level cohort for local reproduction. It uses the same safe runner but binds the report as `manual_subset`, which promotion rejects.
- `.github/workflows/test-gates.yml` defines default affected gates; `.github/workflows/test-full-suite.yml` defines scheduled/manual L6. Both use hosted runners, immutable action SHAs, `contents: read` only, and no deployment credentials or production steps. Reports/plans are written under `$RUNNER_TEMP`.

## Local verification

On Python 3.11.2 and Node v22.22.2:

- `python3 -m py_compile tests/gates/gate.py tests/gates/smoke.py tests/gates/test_smoke_contract.py tests/gates/test_gate_contract.py` — passed.
- `python3 tests/gates/test_gate_contract.py` — 16 passed in the last complete gate-contract run. After the shared-temp cleanup fix, the additional sequential-child regression test passed individually; the combined 17-test file was not rerun. These checks cover fail-closed selection, L5/L6/L7 policy, exact baseline semantics, promotion coverage/digests, sanitized environment, output limits, process-group timeout, and shared-root ownership.
- The smoke contract helper run passed 10 tests using fake subprocess/HTTP adapters and temporary databases. After the final CLI/native-scope additions, two focused tests passed with the other 10 deselected; the combined 12-test file was not rerun.
- `python3 tests/gates/gate.py validate` — passed; manifest covers 149 test files.
- `python3 tests/gates/gate.py dry-run --level L7` — returned `BLOCKED_NO_DISPOSABLE_STAGING_ATTESTATION`, with no command execution.
- `/opt/fwrouter-api/.venv/bin/python tests/gates/gate.py smoke --profile isolated` — passed temp-target installer deployment, temp read-only canonical schema, FastAPI TestClient health, and fixture-backed system-state checks. Native checks were explicitly `not_requested`. This used the existing virtualenv without modifying it; no live profile was invoked.
- One representative selector test ran through the real pytest metadata plugin with sanitized temp state: `backend/tests/test_selector.py::test_runtime_target_identity_requires_unique_mapping_and_keeps_logical_id` passed and emitted that exact node ID in the plugin JSON report. The existing virtualenv emitted a `cache_dir` unknown-option warning from pytest 9; no pytest 9 output was treated as CI evidence.
- `python3 tests/gates/gate.py subset-plan --domain selector --level L1` — emitted six selected files, exactly L0/L1, and `deploy_eligibility: false`.
- The sequential-child regression test later passed alone after the runner cleanup change; all 17 gate-contract cases are therefore covered across runs, but not by one combined invocation.
- A deterministic protocol adapter plan selected 19 files and included `backend/tests/test_protocol_native_validation.py` as required native evidence. Without the pinned dependency the runner blocks rather than reports a skip as green.

The foundation acceptance commands above used system Python 3.11.2; it has no pytest module. In parallel baseline work and the isolated component smoke, the existing `/opt/fwrouter-api/.venv` was used but not modified; it has pytest 9.0.3 although project development requirements exclude pytest 9. The audit first selected 35 non-native and 17 native exact historical IDs (36 failed and 16 passed across the two disjoint runs). Later targeted test-only correction runs passed 31 selected backend cases and the UI fixture suite. The durable exact-ID rollup records 41 passed/resolved observations across selected runs (16 native and 25 fixture/signature IDs), with 11 last-observed failures not rerun after fixes. No single post-fix 52-ID run or L6 full suite was performed. The hosted workflows pin Python 3.11.2, Node 22.22.2, and a hash-locked Python 3.11 test environment with pytest 8.3.5; project runtime dependency ranges remain in `backend/pyproject.toml`.

## Baseline and boundaries

The exact 52 historical failure IDs, individual triage and latest observations are preserved in `BASELINE_STATUS.json`; `approved_baseline_exception` is false. Its historical evidence is not a standing exemption. IDs not actually selected and passed remain pending. The audit reports 11 failed-last-observation/unclassified blockers and does not claim a complete post-fix rerun. The foundation engineer ran no product suite or L6 suite. The baseline audit exercised selected tests and a hash-verified local Mihomo test binary with synthetic fixtures; this did not touch a live native runtime, provider API, production DB/service, or staging VM.

The local source does not prove that GitHub Actions is activated or that a hosted run passed. The L7 gate remains open because no disposable staging attestation verifier/environment exists. The isolated smoke proves only the temp-target install and isolated component API checks that its run reports; it does not establish native deploy parity or live verification. This source/test/documentation checkpoint is committed after final review. No production deploy, service restart, or production mutation was performed.

## Final review

Final static validation passed for Python syntax, manifest/baseline JSON, workflow structure, SHA-pinned action references, hosted runners and read-only workflow permissions. Production application source was unchanged. This checkpoint completes the implemented foundation scope; remote activation/green hosted CI, exact locked-environment product acceptance, native runtime provisioning and disposable L7 acceptance remain explicit gates. Next: Step 4 documentation reconciliation, then Stage 4 measured Performance & Resource Efficiency Audit.
