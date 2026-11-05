"""Explicitly invoked read-only deployed-host smoke; never part of routine execution."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "acceptance"))
import smoke


class HostSmokeTests(unittest.TestCase):
    @pytest.fixture(autouse=True)
    def _capture_config(self, request: pytest.FixtureRequest) -> None:
        self.config = request.config

    def test_manual_live_readonly_host_smoke(self) -> None:
        reason = self.config.getoption("--fwrouter-host-smoke-reason")
        api_base_url = self.config.getoption("--fwrouter-host-smoke-api-base-url")
        db_path = self.config.getoption("--fwrouter-host-smoke-db-path")
        validators = smoke._load_cli_json(
            self.config.getoption("--fwrouter-host-smoke-validators-json"), kind="validators")
        failed_units = smoke._load_cli_json(
            self.config.getoption("--fwrouter-host-smoke-failed-units-allowlist-json"),
            kind="failed-units-allowlist")
        if validators is None or failed_units is None:
            pytest.fail("manual smoke requires explicit validators and failed-unit allowlist JSON")
        result = smoke.run_smoke(
            "live-readonly", reason=reason, opt_in=True, api_base_url=api_base_url,
            db_path=db_path, validators=validators, failed_units_allowlist=failed_units,
            require_native=True)
        failed_checks = []
        for check in result.get("checks", []):
            if not isinstance(check, dict):
                failed_checks.append({"status": "invalid_check"})
            elif check.get("status") != "passed":
                failed_checks.append({key: check.get(key) for key in
                                      ("name", "status", "status_code", "reason") if key in check})
        self.assertEqual(result.get("status"), "passed",
                         "live-readonly smoke failed checks: " +
                         json.dumps(failed_checks, sort_keys=True, separators=(",", ":")))
        self.assertTrue(result.get("authorization", {}).get("opt_in"))
        mutation = next((row for row in result.get("checks", []) if row.get("name") == "mutation_guard"), {})
        self.assertEqual(mutation.get("api_mutations"), False)
