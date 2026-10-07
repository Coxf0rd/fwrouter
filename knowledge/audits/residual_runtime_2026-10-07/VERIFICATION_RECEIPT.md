# Verification receipt

This receipt preserves evidence from the completed audit run. The follow-up that added this file and the preserved harness did not rerun tests or measurements.

## Runtime

- Python: `3.11.2` (`/opt/fwrouter-api/.venv/bin/python`)
- pytest: `9.0.3`
- Repository working directory for commands: `/srv/fwrouter`

## Commands and recorded results

```text
cd /srv/fwrouter/backend
/opt/fwrouter-api/.venv/bin/python -m pytest -q tests/test_scoped_egress.py -k 'explicit_client_binding'
7 passed, 13 deselected

/opt/fwrouter-api/.venv/bin/python -m pytest -q tests/test_scoped_egress.py
20 passed

/opt/fwrouter-api/.venv/bin/python -m pytest -q tests/test_scoped_egress.py::test_explicit_client_binding_load_waits_for_terminal_gates[disabled_path]
1 passed

/opt/fwrouter-api/.venv/bin/python -m pytest -q tests/test_xray.py::test_explicit_xray_modes_are_scoped_direct_or_fail_closed tests/test_xray.py::test_xray_collects_vpn_auto_bindings_without_active_auto_server_id
2 passed
```

The isolated L4 smoke command was run from `/srv/fwrouter`:

```text
/opt/fwrouter-api/.venv/bin/python tests/gates/gate.py smoke --profile isolated
```

Sanitized receipt:

```json
{
  "profile": "isolated",
  "status": "passed",
  "checks": {
    "temporary_deploy_target": "passed",
    "temporary_schema_readonly": "passed",
    "health_api_status": 200,
    "critical_state_api_status": 200,
    "native_validation": "not_requested"
  },
  "isolation": {
    "database_owned_temp": true,
    "target_owned_temp": true,
    "production_env_imported": false,
    "api_mutations": false,
    "provider_calls": false,
    "public_network": false
  }
}
```

The command output also reported schema version valid with 41 temporary user tables. No raw fixture state, credentials, production config, or response bodies are retained here.

The 20-test scoped-egress cohort passed before the explicit disabled-path case was added. That complementary new node was then run alone and passed; the full file was not rerun. Report these as a 20-test cohort plus one complementary PASS, not as a single 21-test suite execution.

## A/B interpretation

The prior isolated summary profile recorded 524 calls to the Xray binding loader over four builds, but it did not record whether the bindings path existed. If absent, the loader returns before reading or parsing JSON. The separate [`scoped_egress_binding_file_ab.py`](scoped_egress_binding_file_ab.py) workload uses a real temporary JSON file and directly counted `Path.read_text()` and `json.loads()` calls: both fell from 524 to 8 across four rounds, with identical output SHA-256. The recorded wall times were 14.216ms and 2.102ms for the last round of this synthetic fixture and are not a production or full-summary latency claim. See [`SANITIZED_METRICS.json`](SANITIZED_METRICS.json) for the full sanitized values.

The saved harness's exact reproduction command is `cd /srv/fwrouter && /opt/fwrouter-api/.venv/bin/python knowledge/audits/residual_runtime_2026-10-07/scoped_egress_binding_file_ab.py`. This command is recorded for future use and was not run during the documentation correction.
