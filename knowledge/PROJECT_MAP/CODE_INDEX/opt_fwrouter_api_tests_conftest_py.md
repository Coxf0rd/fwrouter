# `/opt/fwrouter-api/tests/conftest.py`

## Purpose

Defines global pytest isolation for backend unit tests.

## Behavior Notes

- Sets a per-test `FWROUTER_STATE_DIR` and `FWROUTER_ENVIRONMENT=test`.
- Initializes the per-test SQLite schema by default; `no_database_autoinit` is reserved for schema-drift tests that build their own legacy DB.
- Replaces the default dataplane adapter with an in-memory pytest adapter unless a test is marked `live_dataplane`.
- Stubs apply-pipeline live mode probing so unit tests do not depend on or mutate the host nftables state.
- Uses FWRouter-owned pytest temp/cache directories under `/tmp/fwrouter-pytest-tmp` and `/tmp/fwrouter-pytest-cache`.
- Removes those pytest directories and repo `__pycache__` at session finish unless `FWROUTER_PYTEST_KEEP_ARTIFACTS=1`.

## Review Notes

Read this file before adding tests that call apply, core bypass, runtime status, or dataplane verification paths.

## Runtime Impact

No production runtime impact. This file exists to keep tests from touching live router state.

## Guardrails

- Do not remove the default fake dataplane adapter for ordinary unit tests.
- Use the `live_dataplane` marker only for explicit live acceptance tests and never in the default fast test suite.
- Use `no_database_autoinit` only for tests that intentionally create a non-current SQLite schema.
- Set `FWROUTER_PYTEST_KEEP_ARTIFACTS=1` only when failed-test artifacts are needed for debugging.
