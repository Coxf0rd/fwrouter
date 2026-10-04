# Backend tests

Follow the [Test Architecture and CI/CD Foundation](../../knowledge/PROJECT_MAP/TEST_ARCHITECTURE_AND_CICD_FOUNDATION.md) for test levels, affected-test selection, isolation, baseline classification and durable evidence. Routine fixes run L0 plus the affected L1/L2 tests; the full suite is reserved for milestone, nightly, release or major shared-contract gates. Tests must not touch production state or runtime unless explicitly designated as live acceptance.

The runner needs the versioned, hash-locked Python environment from `tests/gates/requirements-ci.txt`; UI cases also need Node 22.22.2. Use a disposable virtual environment for local work. Common local commands:

```sh
python3 tests/gates/gate.py validate
python3 tests/gates/gate.py dry-run --level L7
test_tmp_dir="$(mktemp -d)"
python3 tests/gates/gate.py subset-plan --domain selector --level L1 --output "$test_tmp_dir/plan.json"
python3 tests/gates/gate.py run --plan "$test_tmp_dir/plan.json" --manual-subset --output "$test_tmp_dir/report.json"
python3 tests/gates/gate.py smoke --profile isolated
```

Manual subset reports document local cohort results and cannot grant deploy eligibility. The isolated smoke installs only into a temporary target and uses temporary database state; it is not live or staging evidence.
