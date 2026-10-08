#!/usr/bin/env python3
"""Focused contract checks for test selection and baseline classification."""
from __future__ import annotations

import copy
import importlib.util
import json
import os
import shutil
import subprocess
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

    def test_dependency_closure_is_transitive_and_cycle_safe(self) -> None:
        graph = {"a": ["b"], "b": ["c"], "c": ["a", "d"], "d": []}
        expected = {"a", "b", "c", "d"}
        self.assertEqual(expected, gate.dependency_closure({"a"}, graph))
        self.assertEqual(expected, gate.dependency_closure({"c", "a"}, graph))
        cyclic_manifest = copy.deepcopy(self.manifest)
        cyclic_manifest["domain_dependencies"]["database"] = ["core"]
        gate.validate_manifest(cyclic_manifest)
        cyclic_manifest["domain_dependencies"]["database"] = ["unknown-domain"]
        with self.assertRaisesRegex(gate.GateError, "unique declared domains"):
            gate.validate_manifest(cyclic_manifest)

    def test_l0_whitespace_check_uses_the_committed_range(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fwrouter-git-range-") as temp:
            repo = Path(temp)
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.email", "gate@example.invalid"], check=True)
            subprocess.run(["git", "-C", str(repo), "config", "user.name", "Gate Contract"], check=True)
            sample = repo / "sample.py"
            sample.write_text("value = 1\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "sample.py"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-qm", "baseline"], check=True)
            base = subprocess.run(
                ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True,
                capture_output=True, text=True,
            ).stdout.strip()
            sample.write_text("value = 1 \n", encoding="utf-8")
            subprocess.run(["git", "-C", str(repo), "add", "sample.py"], check=True)
            subprocess.run(["git", "-C", str(repo), "commit", "-qm", "whitespace"], check=True)
            self.assertNotEqual(0, gate.diff_check(base, cwd=repo).returncode)

    def test_installer_script_cleans_owned_targets_after_early_failure(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fwrouter-installer-cleanup-") as temp:
            root = Path(temp)
            tmp = root / "tmp"
            installer = root / "repo" / "installer"
            tmp.mkdir()
            installer.mkdir(parents=True)
            shutil.copyfile(gate.ROOT / "installer/test-install.sh", installer / "test-install.sh")
            install = installer / "install.sh"
            install.write_text("#!/bin/sh\nexit 42\n", encoding="utf-8")
            install.chmod(0o755)
            (installer / "install-host-dependencies.sh").write_text("#!/bin/sh\nexit 42\n", encoding="utf-8")
            env = {"PATH": os.defpath, "TMPDIR": str(tmp)}
            result = subprocess.run(["sh", str(installer / "test-install.sh")], env=env,
                                    capture_output=True, check=False, timeout=10)
            self.assertEqual(42, result.returncode)
            self.assertEqual([], list(tmp.iterdir()))

    def test_fresh_child_isolation_bootstrap_smoke(self) -> None:
        smoke_path = Path(__file__).with_name("isolation_smoke.py")
        spec = importlib.util.spec_from_file_location("fwrouter_isolation_smoke", smoke_path)
        self.assertIsNotNone(spec)
        assert spec and spec.loader
        smoke = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(smoke)
        report = smoke.run_probe()
        self.assertEqual("passed", report["status"], report)
        self.assertEqual(
            {"isolated_environment", "owned_state_boundary", "deployed_dotenv_denied",
             "arbitrary_dotenv_denied", "root_credentials_denied",
             "production_sqlite_denied", "encoded_sqlite_uri_denied", "protected_symlink_denied",
             "subprocess_denied", "exec_denied", "internet_socket_denied",
             "internet_v6_socket_denied", "unix_connect_denied", "dirfd_rename_denied",
             "unix_socketpair_allowed", "owned_sqlite_allowed", "owned_artifact_cleanup",
             "probe_artifact_cleanup"},
            {row["name"] for row in report["checks"]},
        )

    def test_affected_plan_selects_all_non_opt_in_suites_in_transitive_domains(self) -> None:
        plan = gate.make_plan("HEAD", self.manifest, ["ui/static/js/settings.js"])
        expected_domains = gate.dependency_closure(set(plan["domains"]), self.manifest["domain_dependencies"])
        expected_files = {
            row["path"] for row in self.manifest["test_files"]
            if row["domain"] in expected_domains and not row.get("opt_in")
        }
        expected_files.update(plan["regression_policy"]["anchors"])
        self.assertEqual(sorted(expected_domains - set(plan["domains"])), plan["dependency_domains"])
        self.assertEqual(expected_files, set(plan["selected_files"]))

    def test_core_change_selects_shared_l5_anchors_for_writer_and_projection_contracts(self) -> None:
        plan = gate.make_plan("HEAD", self.manifest, ["backend/fwrouter_api/core/writer.py"])
        self.assertIn("core", plan["domains"])
        self.assertIn("core", plan["regression_policy"]["reasons"])
        for path in ("backend/tests/test_configuration_contract.py", "backend/tests/test_health_contract.py",
                     "backend/tests/test_state_routes.py", "backend/tests/test_db_migrations.py"):
            self.assertIn(path, plan["selected_files"])

    def test_required_child_process_suite_blocks_without_qualified_profile(self) -> None:
        path = "backend/tests/test_vpn_auto_writer_guard.py"
        row = next(item for item in self.manifest["test_files"] if item["path"] == path)
        plan = {
            "source_commit": "c" * 40, "base_commit": "b" * 40,
            "manifest_version": self.manifest["version"],
            "manifest_digest": gate.canonical_digest(self.manifest),
            "plan_digest": "synthetic", "changed_paths": ["backend/fwrouter_api/services/xray_common.py"],
            "selected_files": [path], "required_levels": ["L0", row["primary_level"]],
            "required_execution_profiles": ["qualified-child-process"],
            "required_native_suites": [], "include_native": False,
            "full_suite_required": False, "manual_subset": False,
        }
        with tempfile.TemporaryDirectory(prefix="fwrouter-block-profile-") as temp:
            plan_path = Path(temp) / "plan.json"
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            args = type("Args", (), {
                "manifest": str(gate.MANIFEST), "plan": str(plan_path),
                "manual_full_suite": False, "manual_subset": False,
                "include_native": False, "output": None,
                "baseline": str(gate.ROOT / self.manifest["baseline_policy_file"]),
                "mihomo_binary": None, "xray_image": None,
            })()
            output = StringIO()
            with mock.patch.object(gate, "ensure_plan"), \
                 mock.patch.object(gate, "run_l0", return_value=(True, [])), \
                 mock.patch.object(gate, "environment_evidence", return_value={"python": "test"}), \
                 mock.patch.object(gate, "run_process") as suite_runner, \
                 redirect_stdout(output):
                self.assertEqual(1, gate.command_run(args))
            suite_runner.assert_not_called()
        report = json.loads(output.getvalue())
        self.assertFalse(report["eligible"])
        self.assertIn("qualified child-process profile", " ".join(report["blocked_reasons"]))
        self.assertEqual("blocked_missing_execution_profile", report["executions"][1]["status"])

    def test_ui_and_provider_selections_remain_subsets_of_manifest(self) -> None:
        ui = gate.make_plan("HEAD", self.manifest, ["ui/static/js/settings.js"])
        provider = gate.make_plan("HEAD", self.manifest, ["backend/fwrouter_api/services/provider_recovery.py"])
        self.assertLess(len(ui["selected_files"]), len(self.manifest["test_files"]))
        self.assertLess(len(provider["selected_files"]), len(self.manifest["test_files"]))

    def test_native_suite_is_explicitly_required_for_protocol_contract_source(self) -> None:
        plan = gate.make_plan("HEAD", self.manifest, ["backend/fwrouter_api/adapters/protocol_integration.py"])
        native_path = "backend/tests/test_protocol_native_validation.py"
        self.assertIn(native_path, plan["required_native_suites"])
        self.assertIn(native_path, plan["selected_files"])
        self.assertIn("qualified-child-process", plan["required_execution_profiles"])
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
            with mock.patch.dict(os.environ, {
                "FWROUTER_STEALTHSURF_API_KEY": "do-not-forward",
                "HTTP_PROXY": "http://user:pass@example.invalid",
                "AWS_SECRET_ACCESS_KEY": "do-not-forward",
                "PYTHONPATH": "/opt/fwrouter-api",
            }):
                env = gate.clean_test_environment(Path(temp))
        self.assertNotIn("FWROUTER_STEALTHSURF_API_KEY", env)
        self.assertNotIn("HTTP_PROXY", env)
        self.assertNotIn("AWS_SECRET_ACCESS_KEY", env)
        self.assertNotIn("PYTHONPATH", env)
        self.assertEqual("false", env["FWROUTER_EXTERNAL_COLLECTOR_SCHEDULER_ENABLED"])
        self.assertEqual("1", env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"])
        self.assertEqual("1", env["PYTHONDONTWRITEBYTECODE"])

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

    def test_pytest_suite_coordinator_root_is_private_and_per_suite(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fwrouter-suite-roots-") as temp:
            owned = Path(temp)
            first, first_reports = gate.create_suite_root(owned, "suite/a")
            second, second_reports = gate.create_suite_root(owned, "suite/b")
            self.assertNotEqual(first, second)
            for root, reports in ((first, first_reports), (second, second_reports)):
                self.assertEqual(0o700, root.stat().st_mode & 0o777)
                self.assertEqual("FWROUTER_GATE_TEST_ROOT_V1\n",
                                 (root / ".fwrouter-gate-test-root-owned").read_text())
                self.assertEqual(root, reports.parent)
                self.assertTrue(reports.is_dir())

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
