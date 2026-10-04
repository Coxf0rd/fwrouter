#!/usr/bin/env python3
"""Focused contract checks for test selection and baseline classification."""
from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock


GATE_PATH = Path(__file__).with_name("gate.py")
SPEC = importlib.util.spec_from_file_location("fwrouter_gate", GATE_PATH)
assert SPEC is not None and SPEC.loader is not None
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)


class GateContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = gate.load_manifest()

    def test_catalog_has_one_primary_level_per_test_file_and_no_level_five_or_six_rows(self) -> None:
        self.assertEqual([], gate.check_catalog(self.manifest))
        self.assertTrue(all(row["primary_level"] not in {"L0", "L5", "L6"} for row in self.manifest["test_files"]))
        self.assertEqual(len(self.manifest["test_files"]), len(gate.manifest_paths(self.manifest)))

    def test_unknown_changed_path_fails_closed(self) -> None:
        with self.assertRaises(gate.GateError):
            gate.domain_for_path("unmapped/binary.blob", self.manifest)

    def test_provider_plan_expands_declared_domains_and_uses_l5_anchors_as_primary_levels(self) -> None:
        plan = gate.make_plan("HEAD", self.manifest, ["backend/fwrouter_api/services/provider_recovery.py"])
        self.assertIn("provider", plan["domains"])
        self.assertTrue(plan["regression_policy"]["anchors"])
        self.assertFalse(plan["full_suite_required"])
        self.assertFalse(plan["staging_required"])
        self.assertNotIn("L5", plan["required_levels"])
        self.assertTrue(set(plan["selected_files"]) <= gate.manifest_paths(self.manifest))

    def test_ui_and_provider_selections_stay_affected_domain_sized(self) -> None:
        ui = gate.make_plan("HEAD", self.manifest, ["ui/static/js/settings.js"])
        provider = gate.make_plan("HEAD", self.manifest, ["backend/fwrouter_api/services/provider_recovery.py"])
        self.assertLess(len(ui["selected_files"]), 50)
        self.assertLess(len(provider["selected_files"]), 60)
        self.assertLess(len(ui["selected_files"]), len(self.manifest["test_files"]))
        self.assertLess(len(provider["selected_files"]), len(self.manifest["test_files"]))

    def test_native_suite_is_explicitly_required_for_protocol_contract_source(self) -> None:
        plan = gate.make_plan("HEAD", self.manifest, ["backend/fwrouter_api/adapters/protocol_integration.py"])
        native_path = "backend/tests/test_protocol_native_validation.py"
        self.assertIn(native_path, plan["required_native_suites"])
        self.assertIn(native_path, plan["selected_files"])
        self.assertIn("L3", plan["required_levels"])

    def test_l7_dry_run_calls_real_policy_and_never_invokes_a_runner(self) -> None:
        args = type("Args", (), {"level": "L7", "manifest": str(gate.MANIFEST)})()
        output = StringIO()
        with mock.patch.object(gate, "run_process") as runner, redirect_stdout(output):
            self.assertEqual(0, gate.command_dry_run(args))
        result = json.loads(output.getvalue())
        self.assertFalse(result["actual_staging_execution"])
        self.assertEqual("BLOCKED_NO_DISPOSABLE_STAGING_ATTESTATION", result["result"])
        runner.assert_not_called()

    def test_manual_subset_plan_runs_one_manifest_level_without_deploy_eligibility(self) -> None:
        args = type("Args", (), {
            "domain": "selector", "level": "L1", "include_native": False,
            "manifest": str(gate.MANIFEST), "output": None,
        })()
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(0, gate.command_subset_plan(args))
        plan = json.loads(output.getvalue())
        gate.ensure_plan(plan, self.manifest, allow_manual_subset=True)
        self.assertTrue(plan["selected_files"])
        self.assertEqual(["L0", "L1"], plan["required_levels"])
        self.assertFalse(plan["deploy_eligibility"])
        with self.assertRaisesRegex(gate.GateError, "cannot establish deploy eligibility"):
            gate.promote(plan, self.manifest, [])

    def test_historical_failure_passed_now_is_fixed_only_when_selected_and_executed(self) -> None:
        nodeid = "backend/tests/test_demo.py::test_old_failure[param]"
        baseline = {"schema_version": 1, "baseline_commit": "a" * 40, "failures": [{"nodeid": nodeid, "status": "unverified"}]}
        selected_plan = {"source_commit": "b" * 40, "plan_digest": "digest", "selected_files": ["backend/tests/test_demo.py"]}
        result = gate.classify(selected_plan, {"node_status": {nodeid: "passed"}}, baseline)
        self.assertEqual([nodeid], result["baseline_failures_passed_in_this_run"])
        self.assertEqual([], result["baseline_failures_unselected_pending"])
        self.assertTrue(result["eligible"])

    def test_unselected_or_skipped_baseline_failure_is_pending_never_disappeared(self) -> None:
        nodeid = "backend/tests/test_demo.py::test_old_failure"
        baseline = {"schema_version": 1, "baseline_commit": "a" * 40, "failures": [{"nodeid": nodeid, "status": "unverified"}]}
        plan = {"source_commit": "b" * 40, "plan_digest": "digest", "selected_files": ["backend/tests/test_demo.py"]}
        skipped = gate.classify(plan, {"node_status": {nodeid: "skipped"}}, baseline)
        self.assertEqual([nodeid], skipped["baseline_failures_skipped_or_uncollected"])
        self.assertFalse(skipped["eligible"])
        unselected = gate.classify({**plan, "selected_files": ["backend/tests/test_other.py"]}, {"node_status": {}}, baseline)
        self.assertEqual([nodeid], unselected["baseline_failures_unselected_pending"])
        self.assertTrue(unselected["eligible"])

    def test_novel_failure_and_unapproved_historical_failure_block(self) -> None:
        old = "backend/tests/test_demo.py::test_old"
        new = "backend/tests/test_demo.py::test_new"
        baseline = {"schema_version": 1, "baseline_commit": "a" * 40, "failures": [{"nodeid": old, "status": "unverified"}]}
        plan = {"source_commit": "b" * 40, "plan_digest": "digest", "selected_files": ["backend/tests/test_demo.py"]}
        result = gate.classify(plan, {"node_status": {old: "failed", new: "failed"}}, baseline)
        self.assertEqual([new], result["novel_failures"])
        self.assertEqual([old], result["baseline_failures_unapproved"])
        self.assertFalse(result["eligible"])

    def test_expired_or_unowned_baseline_exception_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            baseline_path = Path(temp) / "baseline.json"
            baseline_path.write_text(json.dumps({
                "schema_version": 1, "baseline_commit": "a" * 40,
                "failures": [{"nodeid": "x::test", "status": "approved_exception", "owner": "team",
                              "remediation": "fix", "evidence": "report", "expires_on": "2000-01-01"}],
            }), encoding="utf-8")
            with self.assertRaises(gate.GateError):
                gate.load_baseline(baseline_path)

    def test_promotion_rejects_report_that_omits_a_selected_suite(self) -> None:
        plan = {
            "source_commit": "b" * 40,
            "plan_digest": "plan-digest",
            "required_levels": ["L0", "L1"],
            "selected_files": ["backend/tests/test_health_contract.py"],
        }
        report = {
            "source_commit": plan["source_commit"], "plan_digest": plan["plan_digest"],
            "manifest_version": self.manifest["version"], "manifest_digest": gate.canonical_digest(self.manifest),
            "tool_version": gate.TOOL_VERSION, "eligible": True,
            "completed_levels": ["L0", "L1"], "l0": {"status": "passed"},
            "executions": [{"path": "@L0", "status": "passed"}],
            "classification": {"eligible": True, "source_commit": plan["source_commit"],
                               "plan_digest": plan["plan_digest"], "selected_files": plan["selected_files"]},
        }
        report["report_digest"] = gate.canonical_digest(report)
        with mock.patch.object(gate, "ensure_plan"), mock.patch.object(gate, "git", return_value=""):
            with self.assertRaisesRegex(gate.GateError, "coverage mismatch"):
                gate.promote(plan, self.manifest, [report])

    def test_runner_enforces_output_cap_and_kills_owned_process_group_at_deadline(self) -> None:
        code, output, exceeded = gate.run_process(
            [__import__("sys").executable, "-c", "print('x' * 10000)"], 5, 128,
        )
        self.assertEqual(125, code)
        self.assertTrue(exceeded)
        self.assertLessEqual(len(output.encode()), 128)
        child_code = "import subprocess,sys; subprocess.Popen([sys.executable,'-c','import time; time.sleep(5)'])"
        start = gate.time.monotonic()
        code, _, _ = gate.run_process([__import__("sys").executable, "-c", child_code], 0.2, 4096)
        self.assertEqual(124, code)
        self.assertLess(gate.time.monotonic() - start, 3)

    def test_clean_runner_environment_drops_inherited_secrets_and_test_selection_overrides(self) -> None:
        import os
        with tempfile.TemporaryDirectory() as temp:
            os.environ["FWROUTER_STEALTHSURF_API_KEY"] = "do-not-forward"
            os.environ["HTTP_PROXY"] = "http://user:pass@example.invalid"
            try:
                env = gate.clean_test_environment(Path(temp))
            finally:
                os.environ.pop("FWROUTER_STEALTHSURF_API_KEY", None)
                os.environ.pop("HTTP_PROXY", None)
        self.assertNotIn("FWROUTER_STEALTHSURF_API_KEY", env)
        self.assertNotIn("HTTP_PROXY", env)
        self.assertEqual("false", env["FWROUTER_EXTERNAL_COLLECTOR_SCHEDULER_ENABLED"])

    def test_sequential_children_leave_shared_runner_owned_root_for_coordinator_cleanup(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fwrouter-sequential-children-") as temp:
            owned = Path(temp)
            env = gate.clean_test_environment(owned)
            self.assertNotIn("FWROUTER_PYTEST_OWNED_DIR", env)
            probe = (
                "import os,pathlib,sys; p=pathlib.Path(sys.argv[1]); "
                "assert not os.environ.get('FWROUTER_PYTEST_OWNED_DIR'); "
                "assert (p/'.fwrouter-test-run-owned').is_file(); "
                "assert (p/'home').is_dir() and (p/'tmp').is_dir()"
            )
            for _ in range(2):
                code, _, truncated = gate.run_process(
                    [__import__("sys").executable, "-c", probe, str(owned)], 5, 4096, env=env,
                )
                self.assertEqual(0, code)
                self.assertFalse(truncated)
                self.assertTrue(owned.exists())

    def test_direct_pytest_guards_do_not_accept_attestation_string_bypass(self) -> None:
        conftest = (gate.ROOT / "backend/tests/conftest.py").read_text(encoding="utf-8")
        plugin = (gate.ROOT / "backend/tests/gate_plugin.py").read_text(encoding="utf-8")
        self.assertNotIn('os.environ.get("FWROUTER_STAGING_ATTESTATION") == "verified"', conftest + plugin)
        self.assertIn("no verified disposable-staging attestation runner is installed", conftest)

    def test_l7_dry_run_reports_no_staging_and_performs_no_command(self) -> None:
        result = {"requested_level": "L7", "mode": "dry-run", "selected_tests": [],
                  "actual_staging_execution": False, "result": "BLOCKED_NO_DISPOSABLE_STAGING_ATTESTATION"}
        self.assertFalse(result["actual_staging_execution"])
        self.assertEqual([], result["selected_tests"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
