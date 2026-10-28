#!/usr/bin/env python3
"""Focused contract checks for test selection and baseline classification."""
from __future__ import annotations

import copy
import importlib.util
import json
import os
import shutil
import shlex
import subprocess
import tempfile
import textwrap
import unittest
import zipfile
from types import SimpleNamespace
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock


GATE_PATH = Path(__file__).with_name("gate.py")
SPEC = importlib.util.spec_from_file_location("fwrouter_gate", GATE_PATH)
assert SPEC is not None and SPEC.loader is not None
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)
PARALLEL_SPEC = importlib.util.spec_from_file_location("fwrouter_parallel_ci", Path(__file__).with_name("parallel_ci.py"))
assert PARALLEL_SPEC and PARALLEL_SPEC.loader
parallel_ci = importlib.util.module_from_spec(PARALLEL_SPEC)
PARALLEL_SPEC.loader.exec_module(parallel_ci)
PLUGIN_SPEC = importlib.util.spec_from_file_location(
    "fwrouter_gate_plugin", gate.ROOT / "backend/tests/gate_plugin.py")
assert PLUGIN_SPEC and PLUGIN_SPEC.loader
gate_plugin = importlib.util.module_from_spec(PLUGIN_SPEC)
PLUGIN_SPEC.loader.exec_module(gate_plugin)
PLUGIN_PATH = gate.ROOT / "backend/tests/gate_plugin.py"
PLUGIN_SPEC = importlib.util.spec_from_file_location("fwrouter_gate_plugin_contract", PLUGIN_PATH)
assert PLUGIN_SPEC is not None and PLUGIN_SPEC.loader is not None
gate_plugin = importlib.util.module_from_spec(PLUGIN_SPEC)
PLUGIN_SPEC.loader.exec_module(gate_plugin)
WHEEL_VERIFIER_PATH = Path(__file__).with_name("verify_backend_wheel.py")
WHEEL_VERIFIER_SPEC = importlib.util.spec_from_file_location(
    "fwrouter_wheel_verifier_contract", WHEEL_VERIFIER_PATH)
assert WHEEL_VERIFIER_SPEC is not None and WHEEL_VERIFIER_SPEC.loader is not None
wheel_verifier = importlib.util.module_from_spec(WHEEL_VERIFIER_SPEC)
WHEEL_VERIFIER_SPEC.loader.exec_module(wheel_verifier)


class GateContractTests(unittest.TestCase):
    def test_proof_only_admission_branch_can_select_existing_qualified_cohort(self) -> None:
        branch = "fix/proof-only-legacy-admission"
        workflow_path = gate.ROOT / ".github/workflows/phase-d-validation.yml"
        workflow = workflow_path.read_text(encoding="utf-8")
        trigger = workflow.split("on:", 1)[1].split("permissions:", 1)[0]
        self.assertIn(branch, trigger)
        base = "9a48562ab347f6e5324f24504cec1ecbc9f48f7c"
        self.assertGreaterEqual(workflow.count(f"github.ref == 'refs/heads/{branch}'") , 3)
        self.assertGreaterEqual(workflow.count(f"'{base}'"), 3)
        self.assertIn("'6bdf37e9c73b5728b8d50e971f34d848610eb345'", workflow)
        self.assertIn("'1b302e91b3cd652cd4aab4f45bbdd88c088e5786'", workflow)
        self.assertIn("PHASE_D_BASE_SHA: ${{ github.ref == 'refs/heads/" + branch + "' && '" + base + "' || '6bdf37e9c73b5728b8d50e971f34d848610eb345' }}", workflow)
        self.assertIn('"ci:validate-cohort": "provider-cohort"', workflow)
        self.assertIn('if [[ "$VALIDATION_STAGE" == provider-cohort ]]; then suite=provider-cohort; fi', workflow)
        launcher_source = (gate.ROOT / "tests/acceptance/launcher.py").read_text(encoding="utf-8")
        target_node = (
            "tests/application_acceptance/test_xray_generation.py::"
            "test_unverified_subscription_inventory_stays_pending_across_startup_until_native_retry"
        )
        self.assertIn(target_node, launcher_source)

    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest = gate.load_manifest()

    def test_catalog_has_one_primary_level_per_test_file_and_no_level_five_or_six_rows(self) -> None:
        self.assertEqual([], gate.check_catalog(self.manifest))
        self.assertTrue(all(row["primary_level"] not in {"L0", "L5", "L6"} for row in self.manifest["test_files"]))
        self.assertEqual(len(self.manifest["test_files"]), len(gate.manifest_paths(self.manifest)))

    def test_existing_standard_library_contracts_have_explicit_routine_runner_adapters(self) -> None:
        paths = {row["path"] for row in self.manifest["test_files"]}
        self.assertEqual({
            "tests/gates/test_core_snapshot_recovery_contract.py",
            "tests/gates/test_network_fixture_contract.py",
            "tests/gates/test_subscription_identity_contract.py",
            "tests/gates/test_evidence_bundle.py",
            "tests/gates/test_visibility_report.py",
            "tests/gates/test_l6_ci.py",
            "tests/gates/test_routine_shards.py",
            "tests/acceptance/test_dependency_image_contract.py",
            "tests/acceptance/test_consume_dependency_image_contract.py",
        }, gate.STDLIB_CONTRACT_PATHS - {
            "tests/gates/test_acceptance_contract.py", "tests/gates/test_gate_contract.py",
            "tests/gates/test_smoke_contract.py",
        })
        self.assertTrue(gate.STDLIB_CONTRACT_PATHS <= paths)
        self.assertTrue(all(not next(row for row in self.manifest["test_files"] if row["path"] == path).get("native")
                            for path in gate.STDLIB_CONTRACT_PATHS))

    def test_unknown_changed_path_fails_closed(self) -> None:
        with self.assertRaises(gate.GateError):
            gate.domain_for_path("unmapped/binary.blob", self.manifest)

    def test_ci_v3_architecture_doc_has_explicit_specific_path_classification(self) -> None:
        domains, rules = gate.domain_for_path("docs/CI_V3.md", self.manifest)
        self.assertEqual({"test-infrastructure"}, domains)
        self.assertEqual(["docs/CI_V3.md"], [row["glob"] for row in rules])

    def test_stdlib_pytest_method_ids_are_not_downgraded_to_file_markers(self) -> None:
        path = "tests/gates/test_evidence_bundle.py"
        row = next(item for item in self.manifest["test_files"] if item["path"] == path)
        node = path + "::EvidenceBundleTests::test_native_handoff_preserves_qcp_and_docker_xray_profile_closure"
        plan = {"source_commit": "a" * 40, "plan_digest": "b" * 64,
                "manifest_digest": gate.canonical_digest(self.manifest),
                "manifest_version": self.manifest["version"]}
        execution = {"suite": row["id"], "path": path, "primary_level": row["primary_level"],
                     "domain": row["domain"], "execution_group": row["execution_group"],
                     "execution_profile": None, "status": "passed", "elapsed_seconds": 0.1,
                     "collected_node_ids": [node], "executed_node_ids": [node]}
        report = gate._routine_shard_report(
            plan, self.manifest, row["execution_group"], [row], [execution], {node: "passed"}, [],
            l0_owned=False, l0_ok=True, l0_elapsed=0, l0_problems=[])
        self.assertEqual([node], report["collected_node_ids"])
        self.assertEqual([], report["file_marker_paths"])

    def test_gate_plugin_resolves_root_and_backend_nodeids_from_collected_item_paths(self) -> None:
        root_relative = "tests/acceptance/test_dependency_image_contract.py"
        backend_relative = "backend/tests/test_isolation_bootstrap.py"
        root_raw = "tests/acceptance/test_dependency_image_contract.py::test_contract[param]"
        backend_raw = "tests/test_isolation_bootstrap.py::test_contract[param]"
        items = [
            SimpleNamespace(nodeid=root_raw, path=gate.ROOT / root_relative),
            SimpleNamespace(nodeid=backend_raw, path=gate.ROOT / backend_relative),
        ]
        gate_plugin._EXACT_NODEIDS.clear()
        gate_plugin._COLLECTED_NODEIDS.clear()
        gate_plugin.pytest_collection_finish(SimpleNamespace(items=items))
        self.assertEqual(root_relative + "::test_contract[param]",
                         gate_plugin._exact_repo_nodeid(root_raw))
        self.assertEqual(backend_relative + "::test_contract[param]",
                         gate_plugin._exact_repo_nodeid(backend_raw))
        self.assertEqual(sorted([root_relative + "::test_contract[param]",
                                 backend_relative + "::test_contract[param]"]),
                         sorted(gate_plugin._COLLECTED_NODEIDS))
        gate_plugin.pytest_runtest_logstart(root_raw, ("", None, ""))
        gate_plugin.pytest_runtest_logreport(SimpleNamespace(
            nodeid=root_raw, failed=False, skipped=False, when="call", passed=True))
        self.assertEqual("passed", gate_plugin._NODE_STATUSES[
            root_relative + "::test_contract[param]"])
        self.assertEqual("tests/uncollected.py::test_case",
                         gate_plugin._exact_repo_nodeid("tests/uncollected.py::test_case"))

    def test_installer_fixture_cleanup_rejects_symlink_without_touching_target(self) -> None:
        source = (gate.ROOT / "installer/test-install.sh").read_text(encoding="utf-8")
        start = source.index("cleanup_test_root() {")
        end = source.index("\ntrap cleanup_test_root EXIT", start)
        cleanup_function = source[start:end]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "protected-target"
            target.mkdir()
            sentinel = target / "keep.txt"
            sentinel.write_text("owned fixture data", encoding="utf-8")
            link = root / "fwrouter-install-suite.symlink"
            link.symlink_to(target, target_is_directory=True)
            result = subprocess.run(
                ["/bin/sh", "-c", cleanup_function + '\nTEST_ROOT="$1"\ncleanup_test_root\n',
                 "cleanup-probe", str(link)], check=False, capture_output=True, text=True, timeout=5)
            self.assertNotEqual(0, result.returncode)
            self.assertTrue(link.is_symlink())
            self.assertEqual("owned fixture data", sentinel.read_text(encoding="utf-8"))

    def test_gate_owned_temp_cleanup_removes_readonly_fixture_without_following_symlinks(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fwrouter-gate-cleanup-") as temp:
            parent = Path(temp)
            root = parent / "fwrouter-gate-owned"
            readonly = root / "pytest-tmp" / "test-safe-recovery" / "artifact"
            readonly.mkdir(parents=True)
            (readonly / "manifest.json").write_text("synthetic receipt", encoding="utf-8")
            external = parent / "external-target"
            external.mkdir()
            sentinel = external / "keep.txt"
            sentinel.write_text("outside the owned root", encoding="utf-8")
            (root / "external-link").symlink_to(external, target_is_directory=True)
            readonly.chmod(0o555)

            result = gate.cleanup_owned_temp(root)

            self.assertEqual("removed", result["status"])
            self.assertGreaterEqual(result["readonly_directories_restored"], 1)
            self.assertFalse(root.exists())
            self.assertEqual("outside the owned root", sentinel.read_text(encoding="utf-8"))

    def test_gate_owned_temp_cleanup_reports_bounded_metadata_without_file_contents(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fwrouter-gate-cleanup-report-") as temp:
            root = Path(temp) / "fwrouter-gate-owned"
            nested = root / "readonly"
            nested.mkdir(parents=True)
            (nested / "private-name.json").write_text("synthetic-secret-value", encoding="utf-8")
            nested.chmod(0o555)
            with mock.patch.object(gate.shutil, "rmtree", side_effect=PermissionError):
                result = gate.cleanup_owned_temp(root)

            self.assertEqual("residual", result["status"])
            self.assertIn("readonly", [entry["path"] for entry in result["entries"]])
            self.assertNotIn("synthetic-secret-value", json.dumps(result))
            self.assertTrue(any(entry.get("mode") == 0o555 for entry in result["entries"]))
            self.assertEqual(1, result["readonly_directories_restored"])
            self.assertTrue(root.exists())

    def test_gate_owned_temp_cleanup_rejects_symlink_root(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fwrouter-gate-cleanup-link-") as temp:
            parent = Path(temp)
            target = parent / "protected"
            target.mkdir()
            sentinel = target / "keep.txt"
            sentinel.write_text("preserve", encoding="utf-8")
            root = parent / "owned-root-link"
            root.symlink_to(target, target_is_directory=True)

            result = gate.cleanup_owned_temp(root)

            self.assertEqual("residual", result["status"])
            self.assertIn("unsafe_root_type_or_owner", result["diagnostic_errors"])
            self.assertTrue(root.is_symlink())
            self.assertEqual("preserve", sentinel.read_text(encoding="utf-8"))

    def test_gate_owned_temp_cleanup_detects_root_swap_before_removal(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fwrouter-gate-cleanup-swap-") as temp:
            parent = Path(temp)
            root = parent / "fwrouter-gate-owned"
            readonly = root / "readonly"
            readonly.mkdir(parents=True)
            original_sentinel = readonly / "original.txt"
            original_sentinel.write_text("original tree", encoding="utf-8")
            readonly.chmod(0o555)
            target = parent / "protected-target"
            target.mkdir()
            target_sentinel = target / "keep.txt"
            target_sentinel.write_text("protected target", encoding="utf-8")
            saved_original = parent / "renamed-original"
            real_stat = gate.os.stat
            parent_fd: int | None = None
            root_path_stat_calls = 0

            def swap_before_final_identity_check(path, *args, **kwargs):
                nonlocal parent_fd, root_path_stat_calls
                if isinstance(path, str) and path == root.name and "dir_fd" in kwargs:
                    if parent_fd is None:
                        parent_fd = kwargs["dir_fd"]
                    if kwargs["dir_fd"] == parent_fd:
                        root_path_stat_calls += 1
                        if root_path_stat_calls == 2:
                            root.rename(saved_original)
                            root.symlink_to(target, target_is_directory=True)
                return real_stat(path, *args, **kwargs)

            with mock.patch.object(gate.os, "stat", side_effect=swap_before_final_identity_check):
                result = gate.cleanup_owned_temp(root)

            self.assertEqual("residual", result["status"])
            self.assertIn("root_path_identity_changed_before_cleanup", result["diagnostic_errors"])
            self.assertTrue(saved_original.is_dir())
            self.assertEqual("original tree",
                             (saved_original / "readonly" / "original.txt").read_text(encoding="utf-8"))
            self.assertTrue(root.is_symlink())
            self.assertEqual(target, root.resolve())
            self.assertEqual("protected target", target_sentinel.read_text(encoding="utf-8"))

    def test_backend_uv_lock_selects_test_infrastructure_in_quick_and_required_plans(self) -> None:
        path = "backend/uv.lock"
        expected = {
            "backend/tests/test_isolation_bootstrap.py",
            "tests/gates/test_gate_contract.py",
        }
        for planner in (gate.make_quick_push_plan, gate.make_plan):
            with self.subTest(planner=planner.__name__):
                plan = planner("HEAD", self.manifest, [path]) if planner is gate.make_quick_push_plan \
                    else planner("HEAD", self.manifest, [path], include_native=True)
                self.assertEqual([path], plan["changed_paths"])
                self.assertEqual(["test-infrastructure"], plan["domains"])
                self.assertTrue(expected <= set(plan["selected_files"]))

    def test_targeted_marker_fails_required_ci_immediately_after_checkout(self) -> None:
        workflow = (gate.ROOT / ".github/workflows/test-gates.yml").read_text(encoding="utf-8")
        checkout = workflow.index("uses: actions/checkout@")
        guard = workflow.index("- name: Stop targeted qualification before full required CI")
        python_setup = workflow.index("uses: actions/setup-python@")
        block = workflow[guard:python_setup]
        self.assertLess(checkout, guard)
        self.assertLess(guard, python_setup)
        self.assertIn("github.event.pull_request.head.repo.full_name == github.repository", block)
        self.assertIn("github.event.pull_request.head.ref == 'fix/api-runtime-health-scale-stability'", block)
        self.assertIn(
            "git log -1 --format=%B | grep -Eq -- 'ci:validate-(identities|final-fixes|packet)'",
            block,
        )
        self.assertIn(
            "Targeted qualification requested; full required CI NOT RUN. Normal final push required.",
            block,
        )
        self.assertIn("exit 1", block)
        self.assertNotIn("continue-on-error", block)

        script = textwrap.dedent(block.split("        run: |", 1)[1].split("\n      - ", 1)[0])
        with tempfile.TemporaryDirectory(prefix="fwrouter-targeted-marker-") as temp:
            repo = Path(temp)
            subprocess.run(["git", "init", "--quiet"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "Gate Contract"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.email", "gate-contract@example.invalid"],
                           cwd=repo, check=True)
            (repo / "receipt.txt").write_text("targeted\n", encoding="utf-8")
            subprocess.run(["git", "add", "receipt.txt"], cwd=repo, check=True)
            for marker in ("ci:validate-identities", "ci:validate-final-fixes", "ci:validate-packet"):
                with self.subTest(marker=marker):
                    subprocess.run(["git", "commit", "--quiet", "--allow-empty", "-m", marker],
                                   cwd=repo, check=True)
                    targeted = subprocess.run(["bash", "-euo", "pipefail", "-c", script], cwd=repo,
                                              text=True, capture_output=True, check=False)
                    self.assertEqual(1, targeted.returncode)
                    self.assertIn(
                        "Targeted qualification requested; full required CI NOT RUN. Normal final push required.",
                        targeted.stderr,
                    )
                    subprocess.run(["git", "commit", "--quiet", "--allow-empty", "-m", "final source"],
                                   cwd=repo, check=True)
                    ordinary = subprocess.run(["bash", "-euo", "pipefail", "-c", script], cwd=repo,
                                              text=True, capture_output=True, check=False)
                    self.assertEqual(0, ordinary.returncode, ordinary.stderr)

    def test_main_and_trusted_final_pr_use_mandatory_all_plan_policy(self) -> None:
        workflow = (gate.ROOT / ".github/workflows/test-gates.yml").read_text(encoding="utf-8")
        self.assertIn("name: Affected L0-L5 with hosted profiles", workflow)
        self.assertIn("github.event_name == 'pull_request'", workflow)
        self.assertIn("github.ref == 'refs/heads/main'", workflow)
        self.assertIn("PR_HEAD_REPO: ${{ github.event.pull_request.head.repo.full_name }}", workflow)
        self.assertIn("&& [ \"$PR_HEAD_REPO\" = \"$GITHUB_REPOSITORY\" ]", workflow)
        self.assertIn("grep -Fq -- 'ci:full-acceptance'", workflow)
        self.assertIn("PLAN_ARGS+=(--mandatory-all)", workflow)
        self.assertIn("python tests/gates/gate.py plan \"${PLAN_ARGS[@]}\"", workflow)
        self.assertIn("PLAN_ARGS=(--base \"$BASE\" --include-native --output \"$PLAN\")", workflow)

        start = workflow.index("          PLAN_ARGS=(--base")
        end = workflow.index("          python tests/gates/gate.py plan ", start)
        policy = workflow[start:end]
        with tempfile.TemporaryDirectory(prefix="fwrouter-mandatory-event-policy-") as temp:
            repo = Path(temp)
            subprocess.run(["git", "init", "--quiet"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "Gate Contract"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.email", "gate-contract@example.invalid"],
                           cwd=repo, check=True)
            (repo / "event.txt").write_text("event policy\n", encoding="utf-8")
            subprocess.run(["git", "add", "event.txt"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "--quiet", "-m", "ci:full-acceptance"], cwd=repo, check=True)

            cases = (
                ("push", "refs/heads/main", "Coxf0rd/fwrouter", "Coxf0rd/fwrouter", True, "true"),
                ("pull_request", "refs/pull/8/merge", "Coxf0rd/fwrouter", "Coxf0rd/fwrouter", True, "true"),
                ("pull_request", "refs/pull/8/merge", "Coxf0rd/fwrouter", "Coxf0rd/fwrouter", False, "false"),
                ("pull_request", "refs/pull/8/merge", "fork/fwrouter", "Coxf0rd/fwrouter", True, "false"),
                ("push", "refs/heads/feature", "Coxf0rd/fwrouter", "Coxf0rd/fwrouter", True, "false"),
            )
            for event, ref, head_repo, repository, marker, expected in cases:
                with self.subTest(event=event, ref=ref, head_repo=head_repo):
                    subprocess.run(
                        ["git", "commit", "--quiet", "--allow-empty", "-m",
                         "ci:full-acceptance" if marker else "ordinary commit"],
                        cwd=repo, check=True,
                    )
                    output_path = repo / "github-output.txt"
                    env = {
                        **os.environ,
                        "EVENT_NAME": event,
                        "BASE": "base-commit",
                        "GITHUB_REF": ref,
                        "PR_HEAD_REPO": head_repo,
                        "GITHUB_REPOSITORY": repository,
                        "PLAN": str(repo / "plan.json"),
                        "GITHUB_OUTPUT": str(output_path),
                    }
                    output_path.write_text("", encoding="utf-8")
                    completed = subprocess.run(
                        ["bash", "-euo", "pipefail", "-c", policy], cwd=repo,
                        env=env, text=True, capture_output=True, check=False,
                    )
                    self.assertEqual(0, completed.returncode, completed.stderr)
                    args = subprocess.run(
                        ["bash", "-euo", "pipefail", "-c", policy + '\nprintf \'%s\\n\' "${PLAN_ARGS[@]}"'],
                        cwd=repo, env=env, text=True, capture_output=True, check=False,
                    )
                    self.assertEqual(0, args.returncode, args.stderr)
                    self.assertEqual(expected == "true", "--mandatory-all" in args.stdout.splitlines())

    def test_mandatory_all_selects_every_safe_suite_and_rejects_omission(self) -> None:
        changed = "backend/fwrouter_api/services/bootstrap.py"
        safe = {
            row["path"] for row in self.manifest["test_files"]
            if row["primary_level"] in {"L1", "L2", "L3", "L4"} and not row.get("destructive")
        }
        l5_anchors = set(self.manifest["policies"]["L5"]["anchors"])
        staging = {
            row["path"] for row in self.manifest["test_files"]
            if row["primary_level"] == "L7" or row.get("destructive")
        }
        with mock.patch.object(gate, "git", return_value="c" * 40), \
             mock.patch.object(gate, "changed_paths", return_value=[changed]):
            plan = gate.make_mandatory_suite_plan("b" * 40, self.manifest, [changed])
            gate.ensure_plan(plan, self.manifest)

            self.assertTrue(plan["mandatory_suite_required"])
            self.assertFalse(plan["full_suite_required"])
            self.assertTrue(plan["include_native"])
            self.assertEqual(safe, set(plan["selected_files"]))
            self.assertTrue(l5_anchors <= set(plan["selected_files"]))
            self.assertEqual(staging, set(plan["optional_suites"]))
            self.assertEqual(staging, set(plan["deferred_staging_suites"]))
            self.assertNotIn("L7", plan["required_levels"])
            self.assertEqual({"L0", "L1", "L2", "L3", "L4"}, set(plan["required_levels"]))

            omitted = copy.deepcopy(plan)
            omitted["selected_files"].pop()
            omitted["plan_digest"] = gate.canonical_digest(
                {key: value for key, value in omitted.items() if key != "plan_digest"})
            with self.assertRaisesRegex(gate.GateError, "deterministic complete safe-suite selection"):
                gate.ensure_plan(omitted, self.manifest)

            wrong_paths = copy.deepcopy(plan)
            wrong_paths["changed_paths"] = ["backend/fwrouter_api/services/other.py"]
            wrong_paths["plan_digest"] = gate.canonical_digest(
                {key: value for key, value in wrong_paths.items() if key != "plan_digest"})
            with self.assertRaisesRegex(gate.GateError, "actual immutable base..HEAD diff"):
                gate.ensure_plan(wrong_paths, self.manifest)

            no_native = copy.deepcopy(plan)
            no_native["include_native"] = False
            no_native["plan_digest"] = gate.canonical_digest(
                {key: value for key, value in no_native.items() if key != "plan_digest"})
            with self.assertRaisesRegex(gate.GateError, "include-native L0-L5"):
                gate.ensure_plan(no_native, self.manifest)

    def test_ordinary_affected_plan_remains_affected_and_not_mandatory(self) -> None:
        changed = "backend/fwrouter_api/services/bootstrap.py"
        with mock.patch.object(gate, "git", return_value="c" * 40), \
             mock.patch.object(gate, "changed_paths", return_value=[changed]):
            plan = gate.make_plan("b" * 40, self.manifest, [changed], include_native=True)
            gate.ensure_plan(plan, self.manifest)
        self.assertFalse(plan["mandatory_suite_required"])
        self.assertFalse(plan["full_suite_required"])
        self.assertLess(len(plan["selected_files"]), len([
            row for row in self.manifest["test_files"]
            if row["primary_level"] in {"L1", "L2", "L3", "L4"} and not row.get("destructive")
        ]))

    def test_identity_diagnostic_uses_available_main_base_without_rebasing_legacy_stages(self) -> None:
        workflow = (gate.ROOT / ".github/workflows/phase-d-validation.yml").read_text(encoding="utf-8")
        legacy_stage_base = (
            "((needs.select-stage.outputs.stage == 'identity-diagnostic' || "
            "needs.select-stage.outputs.stage == 'final-regressions-diagnostic' || "
            "needs.select-stage.outputs.stage == 'packet-diagnostic') && "
            "'1b302e91b3cd652cd4aab4f45bbdd88c088e5786' || "
            "'6bdf37e9c73b5728b8d50e971f34d848610eb345')"
        )
        self.assertEqual(2, workflow.count(legacy_stage_base))
        self.assertEqual(3, workflow.count(
            "github.ref == 'refs/heads/fix/proof-only-legacy-admission' && "
            "'9a48562ab347f6e5324f24504cec1ecbc9f48f7c'"))
        self.assertIn(
            "needs.select-stage.outputs.stage == 'startup-comparison' && "
            "'47cfd63fc220f2890e140da27ff54c91a8a2c7a1'",
            workflow,
        )
        self.assertIn(
            "PHASE_D_BASE_SHA: ${{ github.ref == 'refs/heads/fix/proof-only-legacy-admission' && "
            "'9a48562ab347f6e5324f24504cec1ecbc9f48f7c' || "
            "'6bdf37e9c73b5728b8d50e971f34d848610eb345' }}", workflow)
        identity_step = workflow.split("Run subscription identity L1 subset", 1)[0]
        self.assertIn("git cat-file -e \"$PHASE_D_BASE_SHA^{commit}\"", identity_step)
        self.assertIn('git diff --check "$PHASE_D_BASE_SHA...HEAD"', identity_step)
        self.assertIn('python tests/gates/gate.py plan --base "$PHASE_D_BASE_SHA"', workflow)

        selector = workflow.split("markers = {", 1)[1].split("\n          }", 1)[0]
        self.assertEqual(1, selector.count('"ci:validate-packet": "packet-diagnostic"'))
        self.assertEqual(1, selector.count('"ci:validate-final-fixes": "final-regressions-diagnostic"'))

    def test_final_regressions_diagnostic_is_fixed_four_node_kernel_suite(self) -> None:
        launcher_path = gate.ROOT / "tests/acceptance/launcher.py"
        spec = importlib.util.spec_from_file_location("fwrouter_final_regressions_launcher", launcher_path)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        launcher = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(launcher)
        suite = "final-regressions-diagnostic"
        expected = {
            "tests/application_acceptance/test_database_application.py::"
            "test_provider_v23_shape_initialization_reaches_schema25_and_preserves_state",
            *launcher._RECOVERY_DIAGNOSTIC_NODEIDS,
            "tests/application_acceptance/test_browser_locale.py::"
            "test_real_chromium_xray_client_editor_uses_api_jobs_and_native_readback",
        }
        self.assertEqual(expected, launcher.expected_acceptance_nodeids(suite))
        self.assertEqual(expected, set(launcher._FINAL_REGRESSIONS_DIAGNOSTIC_NODEIDS))
        self.assertIn(suite, launcher._DIAGNOSTIC_SUITES)
        self.assertIn(suite, launcher._KERNEL_DATAPLANE_SUITES)
        self.assertNotIn(suite, launcher._KERNEL_SUITES)
        with self.assertRaises(launcher.NotRun):
            launcher.expected_acceptance_nodeids(f"{suite}-arbitrary")

        import ast
        launcher_tree = ast.parse(launcher_path.read_text(encoding="utf-8"), filename=str(launcher_path))
        suite_choices = [
            ast.literal_eval(keyword.value)
            for node in ast.walk(launcher_tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "add_argument"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == "--suite"
            for keyword in node.keywords if keyword.arg == "choices"
        ]
        self.assertEqual(1, len(suite_choices))
        self.assertIn(suite, suite_choices[0])
        self.assertEqual(4, len(expected))

        scenarios = json.loads((gate.ROOT / "tests/acceptance/scenarios.json").read_text())
        catalog = {row["nodeid"]: row for row in scenarios["scenarios"]}
        self.assertTrue(expected <= set(catalog))
        self.assertTrue(all(catalog[nodeid]["suite"] == "functional"
                            and catalog[nodeid]["level"] == "L3" for nodeid in expected))

        workflow = (gate.ROOT / ".github/workflows/phase-d-validation.yml").read_text(encoding="utf-8")
        self.assertIn('"ci:validate-final-fixes": "final-regressions-diagnostic"', workflow)
        self.assertIn("needs.select-stage.outputs.stage == 'final-regressions-diagnostic'", workflow)
        self.assertIn(
            'if [[ "$VALIDATION_STAGE" == final-regressions-diagnostic ]]; then suite=final-regressions-diagnostic; fi',
            workflow,
        )
        self.assertEqual(3, workflow.count(
            "github.ref == 'refs/heads/fix/proof-only-legacy-admission' && "
            "'9a48562ab347f6e5324f24504cec1ecbc9f48f7c'"))
        self.assertIn("needs.select-stage.outputs.stage == 'final-regressions-diagnostic'", workflow)

        deletion_step = workflow.split(
            "- name: Run final deletion and verified-reentry unit regressions", 1,
        )[1].split("\n      - name:", 1)[0]
        deletion_nodeids = {
            "tests/test_xray.py::test_disabled_subscription_profile_hard_delete_preserves_other_account",
            "tests/test_xray.py::test_subscription_profile_delete_rolls_back_projection_cleanup_on_account_mismatch",
            "tests/test_xray.py::test_subscription_profile_delete_reconcile_failure_does_not_revoke_identity_credentials",
            "tests/test_db_migrations.py::test_v24_to25_adds_identity_tables_without_changing_legacy_rows_or_admitting_downgrade",
            "tests/test_provider_recovery.py::test_verified_reentry_prepare_skips_reconcile_only_with_installed_policy_and_current_intent",
            "tests/test_provider_recovery.py::test_mihomo_reentry_ready_requires_native_policy_installed_boolean[False]",
            "tests/test_provider_recovery.py::test_mihomo_reentry_ready_requires_native_policy_installed_boolean[None]",
            "tests/test_provider_recovery.py::test_mihomo_reentry_ready_requires_native_policy_installed_boolean[true]",
            "tests/test_provider_recovery.py::test_mihomo_reentry_ready_requires_routing_parity_even_with_installed_policy",
            "tests/test_provider_recovery.py::test_mihomo_reentry_ready_accepts_native_policy_and_matching_routing",
            "tests/test_provider_recovery.py::test_verified_reentry_prepare_reconciles_before_probe_and_preserves_direct_on_failure",
            "tests/test_provider_recovery.py::test_verified_reentry_prepare_reconcile_precedes_probe_and_allows_only_incarnation_change",
            "tests/test_provider_recovery.py::test_verified_reentry_prepare_refuses_to_rebase_changed_fences[active_target_id]",
            "tests/test_provider_recovery.py::test_verified_reentry_prepare_refuses_to_rebase_changed_fences[binding]",
            "tests/test_provider_recovery.py::test_verified_reentry_prepare_refuses_to_rebase_changed_fences[credential_fingerprint]",
            "tests/test_provider_recovery.py::test_verified_reentry_prepare_refuses_to_rebase_changed_fences[desired_mode]",
            "tests/test_provider_recovery.py::test_verified_reentry_prepare_refuses_to_rebase_changed_fences[eligible]",
            "tests/test_provider_recovery.py::test_verified_reentry_prepare_refuses_to_rebase_changed_fences[exclusive]",
            "tests/test_provider_recovery.py::test_verified_reentry_prepare_refuses_to_rebase_changed_fences[path_key]",
            "tests/test_provider_recovery.py::test_verified_reentry_prepare_refuses_to_rebase_changed_fences[pending]",
            "tests/test_provider_recovery.py::test_verified_reentry_prepare_refuses_to_rebase_changed_fences[pool]",
            "tests/test_provider_recovery.py::test_verified_reentry_prepare_refuses_to_rebase_changed_fences[selection_fence]",
            "tests/test_provider_recovery.py::test_verified_reentry_accepts_only_exact_owned_native_revision_receipt",
            "tests/test_provider_recovery.py::test_verified_reentry_rejects_unreceipted_runtime_incarnation_change",
            "tests/test_provider_recovery.py::test_verified_reentry_rejects_unowned_or_unrelated_context_change[foreign-job]",
            "tests/test_provider_recovery.py::test_verified_reentry_rejects_unowned_or_unrelated_context_change[foreign-operation]",
            "tests/test_provider_recovery.py::test_verified_reentry_rejects_unowned_or_unrelated_context_change[revision-jump]",
            "tests/test_provider_recovery.py::test_verified_reentry_rejects_unowned_or_unrelated_context_change[native-proof-false]",
            "tests/test_provider_recovery.py::test_verified_reentry_rejects_unowned_or_unrelated_context_change[publication-false]",
            "tests/test_provider_recovery.py::test_verified_reentry_rejects_unowned_or_unrelated_context_change[active-hash-mismatch]",
            "tests/test_provider_recovery.py::test_verified_reentry_rejects_unowned_or_unrelated_context_change[runtime-mismatch]",
            "tests/test_provider_recovery.py::test_verified_reentry_rejects_unowned_or_unrelated_context_change[unrelated-binding-change]",
            "tests/test_subscription_refresh_outcomes.py::test_mihomo_reconcile_rolls_back_when_postapply_selection_readback_fails",
            "tests/test_provider_recovery.py::test_verified_reentry_accepts_reconcile_with_native_policy_when_fast_status_is_conservative",
            "tests/test_provider_recovery.py::test_verified_reentry_rejects_reconcile_without_native_policy_proof",
            "tests/test_provider_recovery.py::test_real_reentry_orchestration_defers_after_probe_fence_change[pre-selection_revision]",
            "tests/test_provider_recovery.py::test_real_reentry_orchestration_defers_after_probe_fence_change[pre-exclusive]",
            "tests/test_provider_recovery.py::test_real_reentry_orchestration_defers_after_probe_fence_change[pre-incarnation]",
            "tests/test_provider_recovery.py::test_real_reentry_orchestration_defers_after_probe_fence_change[pre-binding]",
            "tests/test_provider_recovery.py::test_real_reentry_orchestration_defers_after_probe_fence_change[post-selection_revision]",
            "tests/test_provider_recovery.py::test_real_reentry_orchestration_defers_after_probe_fence_change[post-exclusive]",
            "tests/test_provider_recovery.py::test_real_reentry_orchestration_defers_after_probe_fence_change[post-incarnation]",
            "tests/test_provider_recovery.py::test_real_reentry_orchestration_defers_after_probe_fence_change[post-binding]",
        }
        self.assertIn("needs.select-stage.outputs.stage == 'final-regressions-diagnostic'", deletion_step)
        self.assertIn("tests/gates/run_exact_nodes.py", deletion_step)
        observed_deletion_nodeids = set()
        for line in deletion_step.splitlines():
            tokens = shlex.split(line.strip().rstrip("\\").strip())
            if tokens and tokens[0].startswith("tests/"):
                self.assertEqual(1, len(tokens), "each exact node ID must be one shell argument")
                observed_deletion_nodeids.add(tokens[0])
        self.assertEqual(deletion_nodeids, observed_deletion_nodeids)
        self.assertEqual(43, len(deletion_nodeids))
        self.assertIn('--junit "$root/final-regressions-unit.xml"', deletion_step)
        self.assertIn('--report "$root/final-regressions-unit-report.json"', deletion_step)
        self.assertIn('--log "$root/final-regressions-unit.log"', deletion_step)
        exact_runner = (gate.ROOT / "tests/gates/run_exact_nodes.py").read_text(encoding="utf-8")
        self.assertIn('counts["skipped"] == 0', exact_runner)
        self.assertIn('report["junit_sanitized"]', exact_runner)

    def test_changed_python_static_check_only_lints_changed_python_with_root_rules(self) -> None:
        relative = str(Path(__file__).relative_to(gate.ROOT))
        with tempfile.TemporaryDirectory(prefix="fwrouter-f821-contract-") as temp, \
             mock.patch.object(gate.shutil, "which", return_value="/locked/bin/ruff"), \
             mock.patch.object(gate, "run_process", side_effect=[
                 (0, "ruff 0.17.0\n", False), (0, "", False),
             ]) as runner:
            self.assertEqual([], gate.check_changed_python_rules(
                ["README.md", relative, relative], Path(temp)))
        argv = runner.call_args.args[0]
        self.assertEqual("/locked/bin/ruff", argv[0])
        self.assertEqual(["check", "--config", str(gate.RUFF_CONFIG), "--select", "F,E9"], argv[1:6])
        self.assertIn("--ignore-noqa", argv)
        self.assertIn("--no-cache", argv)
        self.assertEqual("--", argv[-2])
        self.assertEqual([relative], argv[-1:])

    def test_changed_python_static_check_missing_tool_truncation_and_diagnostics_fail_closed(self) -> None:
        relative = str(Path(__file__).relative_to(gate.ROOT))
        with tempfile.TemporaryDirectory(prefix="fwrouter-f821-contract-") as temp:
            with mock.patch.object(gate.shutil, "which", return_value=None):
                self.assertTrue(gate.check_changed_python_rules(
                    [relative], Path(temp)))
            with mock.patch.object(gate.shutil, "which", return_value="/locked/bin/ruff"), \
                 mock.patch.object(gate, "run_process", side_effect=[
                     (0, "ruff 0.17.0\n", False), (0, "", True),
                 ]):
                self.assertIn("output bound", " ".join(
                    gate.check_changed_python_rules([relative], Path(temp))))
            with mock.patch.object(gate.shutil, "which", return_value="/locked/bin/ruff"), \
                 mock.patch.object(gate, "run_process", side_effect=[
                     (0, "ruff 0.17.0\n", False),
                     (1, "fixture.py:1:1: F401 `json` imported but unused", False),
                 ]):
                self.assertIn("F,E9", " ".join(
                    gate.check_changed_python_rules([relative], Path(temp))))

    def test_changed_python_static_check_requires_exact_locked_ruff_version(self) -> None:
        relative = str(Path(__file__).relative_to(gate.ROOT))
        with tempfile.TemporaryDirectory(prefix="fwrouter-f821-contract-") as temp, \
             mock.patch.object(gate.shutil, "which", return_value="/other/bin/ruff"), \
             mock.patch.object(gate, "run_process", return_value=(0, "ruff 0.16.10\n", False)) as runner:
            problems = gate.check_changed_python_rules([relative], Path(temp))
        self.assertIn("hash-locked 0.17.0", " ".join(problems))
        runner.assert_called_once()

    def test_static_analysis_configuration_and_dependency_pins_are_aligned(self) -> None:
        import tomllib

        root_config = tomllib.loads((gate.ROOT / "ruff.toml").read_text(encoding="utf-8"))
        backend_config = tomllib.loads((gate.ROOT / "backend/pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual("==0.17.0", root_config["required-version"])
        self.assertEqual(["F", "E9"], root_config["lint"]["select"])
        self.assertNotIn("ignore", root_config["lint"])
        self.assertNotIn("per-file-ignores", root_config["lint"])
        self.assertNotIn("extend-ignore", root_config["lint"])
        self.assertEqual("../ruff.toml", backend_config["tool"]["ruff"]["extend"])
        self.assertNotIn("select", backend_config["tool"]["ruff"].get("lint", {}))
        self.assertIn("ruff==0.17.0", backend_config["project"]["optional-dependencies"]["dev"])
        requirements = (gate.ROOT / "tests/gates/requirements-ci.txt").read_text(encoding="utf-8")
        uv_lock = tomllib.loads((gate.ROOT / "backend/uv.lock").read_text(encoding="utf-8"))
        self.assertIn("ruff==0.17.0 --hash=sha256:", requirements)
        ruff_rows = [row for row in uv_lock["package"] if row["name"] == "ruff"]
        self.assertEqual(["0.17.0"], [row["version"] for row in ruff_rows])

    def test_global_static_check_uses_only_tracked_maintained_python_and_full_rules(self) -> None:
        paths = ["backend/main.py", "tests/test_main.py"]
        with tempfile.TemporaryDirectory(prefix="fwrouter-global-f821-contract-") as temp, \
             mock.patch.object(gate, "tracked_maintained_python_paths", return_value=paths), \
             mock.patch.object(gate, "check_ruff_paths", return_value=[]) as checker:
            self.assertEqual([], gate.check_tracked_python_undefined_names(Path(temp)))
        checker.assert_called_once_with(
            paths, mock.ANY, rules="F,E9", label="F,E9",
        )

    def test_global_static_canaries_reject_unused_names_and_invalid_syntax(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix=".fwrouter-global-static-canary-", dir=gate.ROOT,
        ) as temp:
            root = Path(temp)
            fixtures = {
                "valid.py": "value = 1\nprint(value)\n",
                "unused_import.py": "import json  # noqa: F401\n",
                "unused_local.py": "def sample():\n    value = 1  # noqa: F841\n",
                "unbound_local.py": "value = 1\ndef sample():\n    value += 1\n",
                "syntax_error.py": "def incomplete(:\n    pass\n",
            }
            for name, source in fixtures.items():
                (root / name).write_text(source, encoding="utf-8")

            paths = [f"{root.name}/{name}" for name in fixtures]
            owned = root / "owned"
            owned.mkdir()
            with mock.patch.object(gate, "tracked_maintained_python_paths", return_value=paths):
                problems = gate.check_tracked_python_undefined_names(owned)

            detail = " ".join(problems)
            self.assertIn("F401", detail)
            self.assertIn("F841", detail)
            self.assertIn("F823", detail)
            self.assertIn("invalid-syntax", detail)

    def test_changed_python_inventory_rejects_symlinks_and_path_escape(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fwrouter-static-path-safety-") as temp:
            root = Path(temp)
            target = root / "target.py"
            target.write_text("value = 1\n", encoding="utf-8")
            link = root / "link.py"
            link.symlink_to(target)
            with self.assertRaises(gate.GateError):
                gate.validate_python_paths(["../outside.py"], root=root)
            with self.assertRaises(gate.GateError):
                gate.validate_python_paths(["link.py"], root=root)
            with self.assertRaises(gate.GateError):
                gate.validate_python_paths(["../missing.py"], root=root, require_existing=False)

    def test_changed_python_inventory_fails_closed_on_malformed_or_incomplete_git_output(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fwrouter-static-enumeration-") as temp, \
             tempfile.TemporaryDirectory(prefix="fwrouter-static-owned-") as owned:
            root = Path(temp)
            with mock.patch.object(gate, "run_process", side_effect=[
                (0, "../outside.py\0", False),
                (0, "../outside.py\0", False),
                (0, "", False),
            ]):
                with self.assertRaises(gate.GateError):
                    gate.changed_python_paths("a" * 40, Path(owned), root=root)
            for result in ((0, "partial.py", True), (2, "git error", False),
                           (0, "bad\ufffd.py\0", False)):
                with mock.patch.object(gate, "run_process", return_value=result):
                    with self.assertRaises(gate.GateError):
                        gate.changed_python_paths("a" * 40, Path(owned), root=root)
            with mock.patch.object(gate, "run_process", side_effect=[
                (0, "probe.py\0", False), (0, "", False), (0, "../outside.py\0", False),
            ]):
                with self.assertRaises(gate.GateError):
                    gate.changed_python_paths("a" * 40, Path(owned), root=root)

    def test_changed_python_inventory_excludes_only_explicit_local_deletions(self) -> None:
        def branch_repo(path: Path) -> str:
            subprocess.run(["git", "init", "--quiet"], cwd=path, check=True)
            subprocess.run(["git", "config", "user.name", "Gate Contract"], cwd=path, check=True)
            subprocess.run(["git", "config", "user.email", "gate-contract@example.invalid"], cwd=path, check=True)
            (path / "probe.py").write_text("value = 1\n", encoding="utf-8")
            subprocess.run(["git", "add", "probe.py"], cwd=path, check=True)
            subprocess.run(["git", "commit", "--quiet", "-m", "base"], cwd=path, check=True)
            base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=path, check=True,
                                  capture_output=True, text=True).stdout.strip()
            (path / "probe.py").write_text("value = 2\n", encoding="utf-8")
            subprocess.run(["git", "add", "probe.py"], cwd=path, check=True)
            subprocess.run(["git", "commit", "--quiet", "-m", "branch change"], cwd=path, check=True)
            return base

        for staged in (False, True):
            with self.subTest(staged=staged), tempfile.TemporaryDirectory(
                    prefix="fwrouter-local-delete-repo-") as repo_dir, \
                 tempfile.TemporaryDirectory(prefix="fwrouter-local-delete-owned-") as owned:
                repo = Path(repo_dir)
                base = branch_repo(repo)
                (repo / "probe.py").unlink()
                if staged:
                    subprocess.run(["git", "add", "-u", "--", "probe.py"], cwd=repo, check=True)
                self.assertEqual([], gate.changed_python_paths(base, Path(owned), root=repo))

        with tempfile.TemporaryDirectory(prefix="fwrouter-staged-rename-repo-") as repo_dir, \
             tempfile.TemporaryDirectory(prefix="fwrouter-staged-rename-owned-") as owned:
            repo = Path(repo_dir)
            base = branch_repo(repo)
            subprocess.run(["git", "mv", "probe.py", "renamed.py"], cwd=repo, check=True)
            self.assertEqual(["renamed.py"], gate.changed_python_paths(base, Path(owned), root=repo))

        with tempfile.TemporaryDirectory(prefix="fwrouter-unexpected-missing-repo-") as repo_dir, \
             tempfile.TemporaryDirectory(prefix="fwrouter-unexpected-missing-owned-") as owned:
            repo = Path(repo_dir)
            with mock.patch.object(gate, "run_process", side_effect=[
                (0, "probe.py\0", False), (0, "", False), (0, "", False),
            ]):
                with self.assertRaises(gate.GateError):
                    gate.changed_python_paths("a" * 40, Path(owned), root=repo)

    def test_tracked_python_inventory_excludes_untracked_and_knowledge_history(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fwrouter-python-inventory-") as temp:
            repo = Path(temp)
            subprocess.run(["git", "init", "--quiet"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "Gate Contract"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.email", "gate-contract@example.invalid"], cwd=repo, check=True)
            for name in ("app.py", "knowledge/audit.py", "deleted.py"):
                path = repo / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("value = 1\n", encoding="utf-8")
            subprocess.run(["git", "add", "app.py", "knowledge/audit.py", "deleted.py"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "--quiet", "-m", "base"], cwd=repo, check=True)
            (repo / "deleted.py").unlink()
            (repo / "untracked.py").write_text("value = 2\n", encoding="utf-8")
            with tempfile.TemporaryDirectory(prefix="fwrouter-inventory-owned-") as owned:
                paths = gate.tracked_maintained_python_paths(Path(owned), root=repo)
            self.assertEqual(["app.py"], paths)

    def test_changed_python_inventory_includes_committed_and_index_or_worktree_paths(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fwrouter-python-diff-") as temp:
            repo = Path(temp)
            subprocess.run(["git", "init", "--quiet"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.name", "Gate Contract"], cwd=repo, check=True)
            subprocess.run(["git", "config", "user.email", "gate-contract@example.invalid"], cwd=repo, check=True)
            (repo / "base.py").write_text("value = 1\n", encoding="utf-8")
            subprocess.run(["git", "add", "base.py"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "--quiet", "-m", "base"], cwd=repo, check=True)
            base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True,
                                  capture_output=True, text=True).stdout.strip()
            (repo / "committed.py").write_text("value = 2\n", encoding="utf-8")
            subprocess.run(["git", "add", "committed.py"], cwd=repo, check=True)
            subprocess.run(["git", "commit", "--quiet", "-m", "committed change"], cwd=repo, check=True)
            (repo / "base.py").write_text("value = 3\n", encoding="utf-8")
            (repo / "staged.py").write_text("value = 4\n", encoding="utf-8")
            subprocess.run(["git", "add", "staged.py"], cwd=repo, check=True)
            (repo / "intent-to-add.py").write_text("value = 6\n", encoding="utf-8")
            subprocess.run(["git", "add", "-N", "intent-to-add.py"], cwd=repo, check=True)
            (repo / "untracked.py").write_text("value = 5\n", encoding="utf-8")
            with tempfile.TemporaryDirectory(prefix="fwrouter-diff-owned-") as owned:
                paths = gate.changed_python_paths(base, Path(owned), root=repo)
            self.assertEqual(["base.py", "committed.py", "intent-to-add.py", "staged.py"], paths)

    def test_preflight_lint_precedes_immutable_plan_publication(self) -> None:
        workflow = (gate.ROOT / '.github/workflows/test-gates.yml').read_text()
        plan = workflow.split('  plan:', 1)[1].split('  build:', 1)[0]
        self.assertLess(plan.index('gate.py lint-changed --base'), plan.index('gate.py plan'))
        self.assertLess(plan.index('gate.py plan'), plan.index('parallel_ci.py configure'))
        self.assertIn('set -euo pipefail', plan)


    def test_provider_plan_expands_declared_domains_and_uses_l5_anchors_as_primary_levels(self) -> None:
        plan = gate.make_plan("HEAD", self.manifest, ["backend/fwrouter_api/services/provider_recovery.py"])
        self.assertIn("provider", plan["domains"])
        self.assertTrue(plan["regression_policy"]["anchors"])
        self.assertFalse(plan["full_suite_required"])
        self.assertFalse(plan["staging_required"])
        self.assertNotIn("L5", plan["required_levels"])
        self.assertTrue(set(plan["selected_files"]) <= gate.manifest_paths(self.manifest))

    def test_partial_node_override_inherits_required_domain_metadata(self) -> None:
        row = {"path": "backend/tests/test_provider_configuration.py", "primary_level": "L2", "domain": "provider"}
        override = {"path": row["path"], "node": "test_partial_override", "primary_level": "L1"}
        metadata = gate_plugin._merged_node_metadata(row, override)
        self.assertEqual("L1", metadata["primary_level"])
        self.assertEqual("provider", metadata["domain"])

    def test_partial_node_override_without_base_domain_fails_closed(self) -> None:
        row = {"path": "backend/tests/test_provider_configuration.py", "primary_level": "L2"}
        override = {"path": row["path"], "node": "test_partial_override", "primary_level": "L1"}
        with self.assertRaisesRegex(ValueError, "missing required field\\(s\\): domain"):
            gate_plugin._merged_node_metadata(row, override)

    def test_partial_node_override_with_unknown_field_fails_closed(self) -> None:
        row = {"path": "backend/tests/test_provider_configuration.py", "primary_level": "L2", "domain": "provider"}
        override = {"path": row["path"], "node": "test_partial_override", "primary_level": "L1",
                    "unexpected": "native"}
        with self.assertRaisesRegex(ValueError, "unknown field\\(s\\): unexpected"):
            gate_plugin._merged_node_metadata(row, override)

    def test_exact_routine_node_overrides_carve_only_the_two_reviewed_xray_baseline_nodes(self) -> None:
        overrides = [row for row in self.manifest["node_overrides"] if "execution_profile" in row]
        self.assertEqual({
            ("backend/tests/test_xray.py", "test_legacy_subscription_get_is_read_only"),
            ("backend/tests/test_xray.py", "test_vpn_auto_invalid_stage_keeps_database_and_active_xray_unchanged"),
        }, {(row["path"], row["node"]) for row in overrides})
        self.assertTrue(all(row["execution_profile"] is None and row["primary_level"] == "L1"
                            for row in overrides))
        base = next(row for row in self.manifest["test_files"]
                    if row["path"] == "backend/tests/test_xray.py")
        self.assertEqual("qualified-child-process", base["execution_profile"])
        self.assertEqual(sorted(name for _path, name in gate.ROUTINE_PROFILE_CARVEOUTS),
                         gate.routine_node_override_names(self.manifest, "backend/tests/test_xray.py"))
        self.assertEqual([], gate.routine_node_override_names(
            self.manifest, "backend/tests/test_xray_native_readback.py"))
        for override in overrides:
            metadata = gate_plugin._merged_node_metadata(base, override)
            self.assertIsNone(metadata["execution_profile"])

    def test_exact_routine_node_override_cannot_clear_an_unprofiled_file_or_add_profile(self) -> None:
        unprofiled = {"path": "backend/tests/test_provider_configuration.py",
                      "primary_level": "L2", "domain": "provider"}
        with self.assertRaisesRegex(gate.GateError, "carve exact nodes out of a profiled file"):
            invalid = copy.deepcopy(self.manifest)
            invalid["node_overrides"].append({"path": unprofiled["path"], "node": "test_x",
                                                "primary_level": "L2", "execution_profile": None})
            gate.validate_manifest(invalid)
        invalid = copy.deepcopy(self.manifest)
        invalid["node_overrides"].append({
            "path": "backend/tests/test_xray.py", "node": "test_xray_writer_guard_serializes_processes",
            "primary_level": "L1", "execution_profile": "hosted-kernel-packet",
        })
        with self.assertRaisesRegex(gate.GateError, "carve exact nodes out of a profiled file"):
            gate.validate_manifest(invalid)
        invalid = copy.deepcopy(self.manifest)
        invalid["node_overrides"].append({
            "path": "backend/tests/test_xray.py", "node": "test_unreviewed_xray_case",
            "primary_level": "L1", "execution_profile": None,
        })
        with self.assertRaisesRegex(gate.GateError, "exact two reviewed routine Xray node carve-outs"):
            gate.validate_manifest(invalid)

    def test_fixed_unit_fixture_cohort_is_exact_catalogued_and_non_native(self) -> None:
        self.assertEqual(24, len(gate.UNIT_FIXTURE_PATHS))
        self.assertEqual(len(gate.UNIT_FIXTURE_PATHS), len(set(gate.UNIT_FIXTURE_PATHS)))
        self.assertEqual(
            {
                "backend/tests/test_reconcile_watchdog.py",
                "backend/tests/test_routing_global_wave1.py",
                "backend/tests/test_runtime_logs.py",
                "backend/tests/test_selector.py",
                "backend/tests/test_watchdog.py",
                "backend/tests/test_core_bypass.py",
            },
            set(gate.UNIT_FIXTURE_RESIDUAL_PATHS),
        )
        self.assertEqual(6, len(gate.UNIT_FIXTURE_RESIDUAL_PATHS))
        self.assertEqual(
            {"backend/tests/test_core_bypass.py"},
            set(gate.UNIT_FIXTURE_PATHS) & set(gate.UNIT_FIXTURE_RESIDUAL_PATHS),
        )
        self.assertEqual(
            {"default": gate.UNIT_FIXTURE_PATHS, "residual": gate.UNIT_FIXTURE_RESIDUAL_PATHS,
             "acceptance-tail": gate.UNIT_FIXTURE_ACCEPTANCE_TAIL_PATHS},
            gate.UNIT_FIXTURE_COHORTS,
        )
        self.assertEqual({
            "backend/tests/test_apply_pipeline.py", "installer/test-install.sh",
            "tests/gates/test_gate_contract.py", "tests/gates/test_acceptance_contract.py",
            "backend/tests/test_live_probe_cache.py", "backend/tests/test_runtime_prewarm.py",
            "backend/tests/test_main_startup_scrub.py", "backend/tests/test_ui_state.py",
            "backend/tests/test_state_routes.py",
        }, set(gate.UNIT_FIXTURE_ACCEPTANCE_TAIL_PATHS))
        self.assertEqual(9, len(gate.UNIT_FIXTURE_ACCEPTANCE_TAIL_PATHS))
        rows = {row["path"]: row for row in self.manifest["test_files"]}
        for paths in gate.UNIT_FIXTURE_COHORTS.values():
            self.assertTrue(set(paths) <= rows.keys())
            self.assertTrue(all(not rows[path].get("native") and not rows[path].get("execution_profile")
                                for path in paths))
        prewarm = rows["backend/tests/test_runtime_prewarm.py"]
        self.assertEqual(("L2", "runtime", False),
                         (prewarm["primary_level"], prewarm["domain"], prewarm["native"]))
        self.assertIsNone(prewarm.get("execution_profile"))

    def test_acceptance_tail_installer_is_a_strict_junit_runner_and_bundle_fails_fast(self) -> None:
        source = GATE_PATH.read_text(encoding="utf-8")
        self.assertIn('if path == "installer/test-install.sh":', source)
        with tempfile.TemporaryDirectory(prefix="fwrouter-installer-junit-contract-") as temp:
            report = Path(temp) / "installer.xml"
            path = "installer/test-install.sh"
            expected = {f"{path}::installer-staged-deploy-smoke"}
            passed = gate.write_installer_smoke_junit(report, path, 0, False)
            self.assertEqual({next(iter(expected)): "passed"}, passed)
            self.assertEqual(sorted(expected), gate.validate_junit_report(report, 4096, 1, expected))
            failed = gate.write_installer_smoke_junit(report, path, 1, False)
            self.assertEqual({next(iter(expected)): "failed"}, failed)
            with self.assertRaisesRegex(gate.GateError, "failed or skipped"):
                gate.validate_junit_report(report, 4096, 1, expected)
            truncated = gate.write_installer_smoke_junit(report, path, 0, True)
            self.assertEqual({next(iter(expected)): "failed"}, truncated)
            with self.assertRaisesRegex(gate.GateError, "failed or skipped"):
                gate.validate_junit_report(report, 4096, 1, expected)
        workflow = (gate.ROOT / ".github/workflows/phase-d-validation.yml").read_text(encoding="utf-8")
        self.assertIn('"ci:validate-unit-acceptance-tail": "unit-acceptance-tail"', workflow)
        affected = workflow.split("id: affected", 1)[1].split(
            "- name: Provision exact runtime inputs", 1)[0]
        self.assertRegex(affected, r"printf\s+['\"]unit_exit=%s\\n['\"]\s+['\"]\$result['\"]")
        self.assertLess(affected.index("unit_exit=%s\\n"), affected.index('exit "$result"'))
        self.assertLess(affected.index("GITHUB_ENV"), affected.index('exit "$result"'))
        self.assertNotIn("if: always()", affected)
        self.assertIn("- name: Provision exact runtime inputs", workflow)
        self.assertIn("- name: Run exact hosted functional profile bundle", workflow)
        self.assertIn('if: always()\n        env:\n          AFFECTED_OUTCOME:', workflow)
        self.assertIn('--unit-failure-report --unit-exit-code "$unit_exit"', workflow)
        self.assertIn('"ci:validate-identities": "identity-diagnostic"', workflow)
        self.assertIn("identity-diagnostic", workflow)
        full_bundle = workflow.split("  full-affected-functional:", 1)[1].split("    runs-on:", 1)[0]
        self.assertNotIn("identity-diagnostic", full_bundle)

    def test_main_push_smoke_reuses_ordinary_or_runs_one_exact_native_diagnostic(self) -> None:
        workflow = (gate.ROOT / ".github/workflows/test-gates.yml").read_text()
        source = (gate.ROOT / "tests/gates/parallel_ci.py").read_text()
        self.assertIn("--main-smoke", workflow)
        self.assertIn("'functional-ordinary' if requirements(plan)['provider'] else 'xray-diagnostic'", source)
        self.assertIn("require_main_smoke=args.main_smoke, main_smoke_path=smoke", source)
        self.assertIn("'xray-diagnostic'", source)
        self.assertIn("any(r['path'] == 'backend/tests/test_xray_native_readback.py' for r in rows)", source)
        self.assertIn("and r['path'] != 'backend/tests/test_xray_native_readback.py'", source)


    def test_main_wheel_build_is_explicit_source_and_checks_retained_project_identity(self) -> None:
        import yaml
        jobs = yaml.safe_load((gate.ROOT / ".github/workflows/test-gates.yml").read_text())["jobs"]
        helper = (gate.ROOT / "tests/gates/ci_inputs.py").read_text()
        verifier = (gate.ROOT / "tests/gates/verify_backend_wheel.py").read_text()
        self.assertEqual("plan", jobs["build"]["needs"])
        self.assertNotIn("if", jobs["build"])
        self.assertIn("ci_inputs.py prepare", str(jobs["build"]["steps"]))
        self.assertIn('"--no-build-isolation"', helper)
        self.assertIn('"--require-hashes"', helper)
        self.assertIn('"./backend"', helper)
        self.assertIn('project.get("project", {}).get("name")', verifier)
        self.assertIn('project.get("project", {}).get("version")', verifier)
        self.assertIn('payload_bytes[name] == source_files[name]', verifier)
        self.assertIn('"fwrouter_api/__init__.py"', verifier)
        self.assertIn('ci_inputs.verify', (gate.ROOT / 'tests/gates/parallel_ci.py').read_text())

    def test_ci_dependency_installs_are_minimal_and_owned_by_consumers(self) -> None:
        import yaml

        workflow = (gate.ROOT / ".github/workflows/test-gates.yml").read_text(encoding="utf-8")
        jobs = yaml.safe_load(workflow)["jobs"]

        def install_commands(role: str) -> list[str]:
            return [step.get("run", "") for step in jobs[role]["steps"]
                    if "pip install" in step.get("run", "")]

        preflight = (gate.ROOT / "tests/gates/requirements-preflight.txt").read_text(encoding="utf-8")
        full_lock = (gate.ROOT / "tests/gates/requirements-ci.txt").read_text(encoding="utf-8")
        build_helper = (gate.ROOT / "tests/gates/ci_inputs.py").read_text(encoding="utf-8")
        self.assertEqual(1, len(install_commands("plan")))
        self.assertIn("--require-hashes", install_commands("plan")[0])
        self.assertIn("requirements-preflight.txt", install_commands("plan")[0])
        self.assertIn("ruff==0.17.0 --hash=sha256:", preflight)
        self.assertEqual(1, len([line for line in preflight.splitlines()
                                 if line.strip() and not line.startswith("#")]))
        self.assertEqual([], install_commands("build"))
        self.assertIn('"--require-hashes"', build_helper)
        self.assertIn('str(lock_path)', build_helper)
        self.assertEqual(1, len(install_commands("unit")))
        self.assertEqual(1, len(install_commands("native")))
        for role in ("unit", "native"):
            self.assertIn("requirements-ci.txt", install_commands(role)[0])
        for role in ("hosted", "provider", "packet", "affected"):
            self.assertEqual([], install_commands(role), role)
        self.assertIn("ruff==0.17.0 --hash=sha256:", full_lock)
        self.assertIn("PyYAML==6.0.2 --hash=sha256:", full_lock)

        # These jobs invoke only stdlib host helpers; application test dependencies
        # are installed and run inside the pinned dependency image/container.
        script = (
            "import sys\n"
            "sys.path[:0] = ['tests/gates', 'tests/acceptance', 'installer']\n"
            "import parallel_ci, ci_inputs, qualify_recovery\n"
        )
        completed = subprocess.run(
            [__import__("sys").executable, "-S", "-c", script], cwd=gate.ROOT,
            capture_output=True, text=True, timeout=20, check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stderr)


    def test_wheel_verifier_checks_identity_exact_payload_and_single_wheel(self) -> None:
        source_payload = {
            "fwrouter_api/__init__.py": b"\"\"\"Synthetic package.\"\"\"\n",
            "fwrouter_api/module.py": b"VALUE = 'source'\n",
        }

        def write_wheel(directory: Path, filename: str, *, name: str = "fwrouter-api",
                        version: str = "1.2.3", payload: dict[str, bytes] | None = None) -> None:
            wheel = directory / filename
            with zipfile.ZipFile(wheel, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.writestr(
                    "fwrouter_api-1.2.3.dist-info/METADATA",
                    f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n\n",
                )
                for path, content in (source_payload if payload is None else payload).items():
                    archive.writestr(path, content)

        def run_case(mutate) -> tuple[bool, dict[str, object]]:
            with tempfile.TemporaryDirectory(prefix="fwrouter-wheel-verifier-contract-") as temp:
                root = Path(temp)
                backend = root / "backend"
                package = backend / "fwrouter_api"
                package.mkdir(parents=True)
                (backend / "pyproject.toml").write_text(
                    '[project]\nname = "fwrouter-api"\nversion = "1.2.3"\n', encoding="utf-8")
                for relative, content in source_payload.items():
                    (backend / relative).write_bytes(content)
                wheel_dir = root / "wheels"
                wheel_dir.mkdir()
                mutate(wheel_dir)
                output = root / "audit.json"
                with mock.patch.object(wheel_verifier, "ROOT", root):
                    passed = wheel_verifier.verify(wheel_dir, output)
                return passed, json.loads(output.read_text(encoding="utf-8"))

        passed, summary = run_case(
            lambda directory: write_wheel(directory, "fwrouter_api-1.2.3-py3-none-any.whl"))
        self.assertTrue(passed, "correct source wheel should pass identity and payload verification")
        self.assertTrue(summary["wheels"][0]["matches_current_source"])

        negative_cases = (
            ("wrong PyPI identity", lambda d: write_wheel(
                d, "backend-0.2.4.1-py3-none-any.whl", name="backend", version="0.2.4.1")),
            ("stale payload", lambda d: write_wheel(
                d, "fwrouter_api-1.2.3-py3-none-any.whl",
                payload={**source_payload, "fwrouter_api/module.py": b"VALUE = 'stale'\n"})),
            ("missing source payload", lambda d: write_wheel(
                d, "fwrouter_api-1.2.3-py3-none-any.whl",
                payload={"fwrouter_api/__init__.py": source_payload["fwrouter_api/__init__.py"]})),
            ("second wheel", lambda d: (
                write_wheel(d, "fwrouter_api-1.2.3-py3-none-any.whl"),
                write_wheel(d, "fwrouter_api-1.2.3-2-py3-none-any.whl"))),
        )
        for label, mutation in negative_cases:
            with self.subTest(case=label):
                passed, _ = run_case(mutation)
                self.assertFalse(passed, f"{label} must be rejected")

    def test_pr_and_main_workflow_event_policy_uses_only_event_bases(self) -> None:
        import yaml
        workflow = (gate.ROOT / ".github/workflows/test-gates.yml").read_text()
        jobs = yaml.safe_load(workflow)["jobs"]
        self.assertEqual("Affected L0-L5 with hosted profiles", jobs["affected"]["name"])
        self.assertEqual("always()", jobs["affected"]["if"])
        self.assertEqual(set(parallel_ci.ROLES), set(jobs["affected"]["needs"]))
        self.assertIn('BASE="$PR_BASE_SHA"', workflow)
        self.assertIn('BASE="$PUSH_BASE_SHA"', workflow)
        self.assertNotIn('BASE="HEAD^"', workflow)
        self.assertIn("github.event.pull_request.head.sha || github.sha", workflow)
        self.assertEqual(["plan"], jobs["unit"]["needs"])


    def test_unit_fixture_junit_contract_rejects_missing_and_skipped_cases(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fwrouter-unit-junit-contract-") as temp:
            path = Path(temp) / "report.xml"
            path.write_text('<testsuite tests="1" failures="0" errors="0" skipped="0">'
                            '<testcase name="ok"/></testsuite>', encoding="utf-8")
            self.assertEqual(["ok"], gate.validate_junit_report(path, 4096, 1))
            path.write_text('<testsuite tests="1" failures="0" errors="0" skipped="1">'
                            '<testcase name="skip"><skipped/></testcase></testsuite>', encoding="utf-8")
            with self.assertRaisesRegex(gate.GateError, "failed or skipped"):
                gate.validate_junit_report(path, 4096, 1)
            with self.assertRaisesRegex(gate.GateError, "case count"):
                gate.validate_junit_report(path, 4096, 2)

    def test_unit_fixture_junit_maps_exact_pytest_node_ids(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fwrouter-unit-junit-nodeid-contract-") as temp:
            path = Path(temp) / "report.xml"
            path.write_text('<testsuite tests="1" failures="0" errors="0" skipped="0">'
                            '<testcase classname="tests.test_example.TestExample" name="test_case"/>'
                            '</testsuite>', encoding="utf-8")
            expected = {"backend/tests/test_example.py::TestExample::test_case"}
            self.assertEqual(sorted(expected), gate.validate_junit_report(path, 4096, 1, expected))
            path.write_text('<testsuite tests="1" failures="0" errors="0" skipped="0">'
                            '<testcase classname="tests.test_example" name="unexpected"/>'
                            '</testsuite>', encoding="utf-8")
            with self.assertRaisesRegex(gate.GateError, "exact pytest node ID"):
                gate.validate_junit_report(path, 4096, 1, expected)

    def test_node_tap_file_wrapper_is_one_test_case_with_file_level_status(self) -> None:
        test_path = "ui/tests/settings-events.test.js"
        tap = ("TAP version 13\n# Subtest: ui/tests/settings-events.test.js\n"
               "ok 1 - ui/tests/settings-events.test.js\n  ---\n  duration_ms: 4\n  ...\n"
               "1..1\n# tests 1\n# pass 1\n# fail 0\n")
        self.assertEqual({test_path: "passed"}, gate.node_status_from_tap(
            tap, test_path, include_file_status=False))
        self.assertEqual({test_path: "passed"}, gate.node_status_from_tap(tap, test_path))
        with self.assertRaisesRegex(gate.GateError, "no observed test cases"):
            gate.node_status_from_tap("TAP version 13\n1..0\n# tests 0\n", test_path,
                                      include_file_status=False, require_observed=True)

    def test_node_tap_nested_file_wrapper_preserves_only_actual_case_identity(self) -> None:
        test_path = "ui/tests/settings-events.test.js"
        tap = ("TAP version 13\n# Subtest: ui/tests/settings-events.test.js\n"
               "    # Subtest: rejects malformed event\n"
               "    ok 1 - rejects malformed event\n"
               "    1..1\n"
               "ok 1 - ui/tests/settings-events.test.js\n1..1\n")
        cases, file_status = gate.node_tap_results(tap, test_path, require_observed=True)
        self.assertEqual({test_path + "::rejects malformed event": "passed"}, cases)
        self.assertEqual("passed", file_status)
        failed_wrapper = tap.replace("ok 1 - ui/tests/settings-events.test.js",
                                     "not ok 1 - ui/tests/settings-events.test.js")
        failed_cases, failed_file_status = gate.node_tap_results(
            failed_wrapper, test_path, require_observed=True)
        self.assertEqual({test_path + "::rejects malformed event": "passed"}, failed_cases)
        self.assertEqual("failed", failed_file_status)

    def test_suite_root_has_private_owned_tmp_directory(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fwrouter-suite-root-contract-") as temp:
            root, reports = gate.create_suite_root(Path(temp), "suite")
            self.assertTrue((root / ".fwrouter-gate-test-root-owned").is_file())
            self.assertTrue((root / "tmp").is_dir())
            self.assertEqual(0, (root / "tmp").stat().st_mode & 0o077)
            self.assertTrue(reports.is_relative_to(root))

    def test_published_junit_redacts_diagnostics_and_preserves_case_identity(self) -> None:
        raw = (b'<testsuite tests="1"><properties><property name="api_token" value="SECRET_VALUE"/>'
               b'</properties><testcase classname="tests.test_example" name="test_case">'
               b'<failure message="token=FAIL_VALUE">Trace token=TRACE_VALUE</failure>'
               b'<system-err>Bearer BEARER_VALUE</system-err></testcase></testsuite>')
        sanitized = gate.sanitize_junit_artifact(raw, 4096)
        self.assertNotIn(b"SECRET_VALUE", sanitized)
        self.assertNotIn(b"FAIL_VALUE", sanitized)
        self.assertNotIn(b"TRACE_VALUE", sanitized)
        self.assertNotIn(b"BEARER_VALUE", sanitized)
        import xml.etree.ElementTree as ET
        root = ET.fromstring(sanitized)
        case = root.find("testcase")
        self.assertIsNotNone(case)
        assert case is not None
        self.assertEqual({"classname": "tests.test_example", "name": "test_case"}, case.attrib)
        property_row = root.find("./properties/property")
        self.assertIsNotNone(property_row)
        assert property_row is not None
        self.assertEqual("[REDACTED]", property_row.get("value"))

    def test_three_profile_hosted_partition_sets_are_exact_and_exhaustive(self) -> None:
        spec = importlib.util.spec_from_file_location(
            "fwrouter_hosted_partition_contract", gate.ROOT / "tests/gates/aggregate_hosted.py")
        self.assertIsNotNone(spec)
        module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(module)
        partitions = module.hosted_partition_nodeids()
        self.assertEqual({"functional-ordinary", "provider-cohort", "packet-diagnostic"}, set(partitions))
        self.assertEqual((30, 12, 4), tuple(len(partitions[key]) for key in
                                             ("functional-ordinary", "provider-cohort", "packet-diagnostic")))
        self.assertEqual(46, len(set().union(*partitions.values())))
        self.assertFalse(partitions["functional-ordinary"] & partitions["provider-cohort"])
        self.assertFalse(partitions["packet-diagnostic"] & set().union(
            partitions["functional-ordinary"], partitions["provider-cohort"]))
        self.assertEqual(set(module.functional_nodeids()),
                         partitions["functional-ordinary"] | partitions["provider-cohort"])

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
        _, matched_rules = gate.domain_for_path("ui/static/js/settings.js", self.manifest)
        expected_files.update(path for rule in matched_rules for path in rule.get("required_native", []))
        self.assertEqual(sorted(expected_domains - set(plan["domains"])), plan["dependency_domains"])
        self.assertEqual(expected_files, set(plan["selected_files"]))

    def test_core_change_selects_shared_l5_anchors_for_writer_and_projection_contracts(self) -> None:
        plan = gate.make_plan("HEAD", self.manifest, ["backend/fwrouter_api/core/writer.py"])
        self.assertIn("core", plan["domains"])
        self.assertIn("core", plan["regression_policy"]["reasons"])
        for path in ("backend/tests/test_configuration_contract.py", "backend/tests/test_health_contract.py",
                     "backend/tests/test_state_routes.py", "backend/tests/test_db_migrations.py"):
            self.assertIn(path, plan["selected_files"])

    def test_core_settings_change_selects_shared_consumer_domains(self) -> None:
        plan = gate.make_plan("HEAD", self.manifest, ["backend/fwrouter_api/core/config.py"])
        required_domains = {"api-contract", "core", "provider", "runtime", "selector",
                            "state", "subscription", "watchdog"}
        planned_domains = set(plan["domains"]) | set(plan["dependency_domains"])
        self.assertTrue(required_domains <= planned_domains)
        selected_domains = {
            row["domain"] for row in self.manifest["test_files"]
            if row["path"] in set(plan["selected_files"])
        }
        self.assertTrue(required_domains <= selected_domains)
        self.assertTrue(plan["regression_policy"]["anchors"])

    def test_shared_database_paths_select_all_declared_backend_consumers(self) -> None:
        backend_domains = {
            row["domain"] for row in self.manifest["test_files"]
            if row["path"].startswith("backend/tests/")
            and row["domain"] != "test-infrastructure"
        }
        for path in ("backend/fwrouter_api/db/connection.py", "backend/fwrouter_api/db/schema.sql"):
            with self.subTest(path=path):
                plan = gate.make_plan("HEAD", self.manifest, [path])
                planned_domains = set(plan["domains"]) | set(plan["dependency_domains"])
                self.assertTrue(backend_domains <= planned_domains)
                selected_domains = {
                    row["domain"] for row in self.manifest["test_files"]
                    if row["path"] in set(plan["selected_files"])
                }
                self.assertTrue(backend_domains <= selected_domains)
                self.assertTrue(plan["regression_policy"]["anchors"])

    def test_shared_test_bootstrap_changes_select_all_backend_consumers(self) -> None:
        backend_domains = {
            row["domain"] for row in self.manifest["test_files"]
            if row["path"].startswith("backend/tests/")
        }
        for path in ("backend/tests/conftest.py", "backend/tests/_isolation_bootstrap.py",
                     "backend/tests/_test_support.py"):
            with self.subTest(path=path):
                plan = gate.make_plan("HEAD", self.manifest, [path])
                planned_domains = set(plan["domains"]) | set(plan["dependency_domains"])
                self.assertTrue(backend_domains <= planned_domains)
                selected_domains = {
                    row["domain"] for row in self.manifest["test_files"]
                    if row["path"] in set(plan["selected_files"])
                }
                self.assertTrue(backend_domains <= selected_domains)

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

    def test_hosted_acceptance_profile_stays_blocked_even_with_include_native(self) -> None:
        path = "tests/application_acceptance/test_xray_api.py"
        row = next(item for item in self.manifest["test_files"] if item["path"] == path)
        self.assertEqual("hosted-isolated-compose", row["execution_profile"])
        plan = {
            "source_commit": "c" * 40, "base_commit": "b" * 40,
            "manifest_version": self.manifest["version"],
            "manifest_digest": gate.canonical_digest(self.manifest),
            "plan_digest": "synthetic", "changed_paths": [path],
            "selected_files": [path], "required_levels": ["L0", row["primary_level"]],
            "required_execution_profiles": ["hosted-isolated-compose"],
            "required_native_suites": [], "include_native": True,
            "full_suite_required": False, "manual_subset": False,
        }
        with tempfile.TemporaryDirectory(prefix="fwrouter-block-hosted-profile-") as temp:
            plan_path = Path(temp) / "plan.json"
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            args = type("Args", (), {
                "manifest": str(gate.MANIFEST), "plan": str(plan_path),
                "manual_full_suite": False, "manual_subset": False,
                "include_native": True, "output": None,
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
        self.assertIn("hosted isolated Compose launcher", " ".join(report["blocked_reasons"]))
        self.assertEqual("blocked_missing_execution_profile", report["executions"][1]["status"])

    def test_provider_cohort_profile_binding_is_exact_and_does_not_elevate_file_plans(self) -> None:
        bindings = self.manifest["node_execution_profiles"]
        self.assertEqual(12, len(bindings))
        self.assertEqual(gate.PROVIDER_COHORT_NODEIDS, {row["nodeid"] for row in bindings})
        self.assertTrue(all(row["suite"] == "provider-cohort"
                            and row["execution_profile"] == "hosted-kernel-dataplane"
                            for row in bindings))
        for path in {nodeid.split("::", 1)[0] for nodeid in gate.PROVIDER_COHORT_NODEIDS}:
            row = next(item for item in self.manifest["test_files"] if item["path"] == path)
            self.assertEqual("hosted-isolated-compose", row["execution_profile"])
            self.assertNotEqual("hosted-kernel-dataplane", row["execution_profile"])
        plan = gate.make_plan("HEAD", self.manifest,
                              ["tests/application_acceptance/test_core_provider_mihomo.py"],
                              include_native=False)
        self.assertNotIn("hosted-kernel-dataplane", plan["required_execution_profiles"])

        missing = copy.deepcopy(self.manifest)
        missing["node_execution_profiles"].pop()
        with self.assertRaisesRegex(gate.GateError, "exact 12 provider-cohort"):
            gate.validate_manifest(missing)
        wrong_profile = copy.deepcopy(self.manifest)
        wrong_profile["node_execution_profiles"][0]["execution_profile"] = "hosted-kernel-packet"
        with self.assertRaisesRegex(gate.GateError, "exact hosted provider-cohort"):
            gate.validate_manifest(wrong_profile)
        path_profile = copy.deepcopy(self.manifest)
        native_row = next(row for row in path_profile["test_files"]
                          if row["path"] == "tests/application_acceptance/test_core_provider_mihomo.py")
        native_row["execution_profile"] = "hosted-kernel-dataplane"
        with self.assertRaisesRegex(gate.GateError, "reserved for exact provider-cohort node bindings"):
            gate.validate_manifest(path_profile)

    def test_packet_execution_profile_is_exact_and_deferred_to_its_hosted_launcher(self) -> None:
        path = gate.PACKET_EXECUTION_PATH
        row = next(item for item in self.manifest["test_files"] if item["path"] == path)
        self.assertEqual("hosted-kernel-packet", row["execution_profile"])
        scale_path = gate.PACKET_SCALE_EXECUTION_PATH
        scale_row = next(item for item in self.manifest["test_files"] if item["path"] == scale_path)
        self.assertEqual("application-runtime-scale", scale_row["id"])
        self.assertEqual("hosted-kernel-packet", scale_row["execution_profile"])
        # Exercise the runner's manual-subset contract with a manifest containing
        # only this row. A normal affected plan intentionally expands its domain,
        # which is outside this focused dispatch contract.
        packet_manifest = {**self.manifest, "test_files": [row]}
        selected = [row]
        plan = {
            "schema_version": 1,
            "created_utc": "2026-01-01T00:00:00+00:00",
            "source_commit": "c" * 40,
            "base_commit": "c" * 40,
            "manifest_version": packet_manifest["version"],
            "manifest_digest": gate.canonical_digest(packet_manifest),
            "changed_paths": ["manual-subset:application-acceptance:L3"],
            "domains": ["application-acceptance"],
            "selected_files": [path],
            "optional_suites": [],
            "deferred_staging_suites": [],
            "required_native_suites": [path],
            "required_execution_profiles": gate.required_execution_profiles(selected),
            "include_native": True,
            "required_levels": ["L0", "L3"],
            "regression_policy": {"level": "manual-subset", "anchors": []},
            "full_suite_required": False,
            "manual_subset": True,
            "manual_gate": "subset",
            "subset_selector": {"domain": "application-acceptance", "level": "L3"},
            "deploy_eligibility": False,
        }
        plan["plan_digest"] = gate.canonical_digest(plan)
        self.assertEqual([path], plan["selected_files"])
        self.assertEqual(["hosted-kernel-packet"], plan["required_execution_profiles"])

        widened = copy.deepcopy(self.manifest)
        unrelated = next(item for item in widened["test_files"]
                         if item["path"] not in {path, scale_path})
        unrelated["execution_profile"] = "hosted-kernel-packet"
        with self.assertRaisesRegex(gate.GateError, "reserved for the exact opt-in L3 packet suite"):
            gate.validate_manifest(widened)

        with tempfile.TemporaryDirectory(prefix="fwrouter-defer-packet-profile-") as temp:
            plan_path = Path(temp) / "plan.json"
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            args = type("Args", (), {
                "manifest": str(gate.MANIFEST), "plan": str(plan_path),
                "manual_full_suite": False, "manual_subset": True,
                "include_native": True, "output": None, "defer_hosted": True,
                "baseline": str(gate.ROOT / self.manifest["baseline_policy_file"]),
                "mihomo_binary": None, "xray_image": None,
            })()
            output = StringIO()
            with mock.patch.object(gate, "load_manifest", return_value=packet_manifest), \
                 mock.patch.object(gate, "git", return_value="c" * 40), \
                 mock.patch.object(gate, "run_l0", return_value=(True, [])), \
                 mock.patch.object(gate, "environment_evidence", return_value={"python": "test"}), \
                 mock.patch.object(gate, "run_process") as suite_runner, \
                 redirect_stdout(output):
                self.assertEqual(0, gate.command_run(args))
            suite_runner.assert_not_called()
            report = json.loads(output.getvalue())
            self.assertFalse(report["eligible"])
            self.assertIn(path, report["deferred_remote_suites"])
            execution = next(item for item in report["executions"] if item.get("path") == path)
            self.assertEqual("deferred_remote_hosted", execution["status"])
            self.assertEqual("hosted-kernel-packet", execution["execution_profile"])

            args.defer_hosted = False
            output = StringIO()
            with mock.patch.object(gate, "load_manifest", return_value=packet_manifest), \
                 mock.patch.object(gate, "git", return_value="c" * 40), \
                 mock.patch.object(gate, "run_l0", return_value=(True, [])), \
                 mock.patch.object(gate, "environment_evidence", return_value={"python": "test"}), \
                 mock.patch.object(gate, "run_process") as suite_runner, \
                 redirect_stdout(output):
                self.assertEqual(1, gate.command_run(args))
            suite_runner.assert_not_called()
        blocked = json.loads(output.getvalue())
        packet_entry = next(item for item in blocked["executions"] if item.get("path") == path)
        self.assertEqual("blocked_missing_execution_profile", packet_entry["status"])
        self.assertIn("exact hosted-kernel-packet launcher", " ".join(blocked["blocked_reasons"]))

    def test_acceptance_harness_source_is_test_infrastructure_and_native_suite_is_opt_in(self) -> None:
        domains, _ = gate.domain_for_path("tests/acceptance/launcher.py", self.manifest)
        self.assertEqual({"test-infrastructure"}, domains)
        acceptance = [row for row in self.manifest["test_files"]
                      if row["domain"] == "application-acceptance"]
        self.assertTrue(acceptance)
        packet_rows = [row for row in acceptance if row.get("execution_profile") == gate.PACKET_EXECUTION_PROFILE]
        self.assertEqual({gate.PACKET_EXECUTION_PATH, gate.PACKET_SCALE_EXECUTION_PATH},
                         {row["path"] for row in packet_rows})
        self.assertTrue(all(row.get("opt_in") is True and row.get("native") is True
                            and row.get("execution_profile") in {"hosted-isolated-compose",
                                                                  gate.PACKET_EXECUTION_PROFILE}
                            for row in acceptance))
        self.assertTrue(all(row["destructive"] == (row["primary_level"] == "L7")
                            for row in acceptance))

    def test_artifact_lifecycle_document_selects_test_infrastructure_contract(self) -> None:
        path = "docs/CI_ARTIFACT_LIFECYCLE.md"
        domains, _ = gate.domain_for_path(path, self.manifest)
        self.assertEqual({"test-infrastructure"}, domains)
        plan = gate.make_mandatory_suite_plan("0a0ce14e2868c88ea83b29ad31a9ec914800e1ad",
                                              self.manifest, [path])
        self.assertIn("tests/gates/test_gate_contract.py", plan["selected_files"])
        self.assertIn("tests/gates/test_evidence_bundle.py", plan["selected_files"])
        self.assertTrue(plan["required_execution_profiles"])
        self.assertTrue(plan["required_native_suites"])

    def test_dependency_consumer_helper_selects_its_stdlib_contract(self) -> None:
        path = "tests/acceptance/consume_dependency_image.py"
        domains, _ = gate.domain_for_path(path, self.manifest)
        self.assertEqual({"test-infrastructure"}, domains)
        plan = gate.make_plan("HEAD", self.manifest, [path])
        expected_contracts = {
            "tests/acceptance/test_dependency_image_contract.py",
            "tests/acceptance/test_consume_dependency_image_contract.py",
        }
        inherited_native = {
            "tests/application_acceptance/test_browser_locale.py",
            "tests/application_acceptance/test_core_provider_mihomo.py",
            "tests/application_acceptance/test_database_application.py",
            "tests/application_acceptance/test_xray_api.py",
            "tests/application_acceptance/test_xray_generation.py",
        }
        self.assertTrue(expected_contracts <= set(plan["selected_files"]))
        self.assertTrue(inherited_native <= set(plan["selected_files"]))
        self.assertTrue(inherited_native <= set(plan["required_native_suites"]))
        self.assertFalse(plan["full_suite_required"])
        self.assertNotIn("L7", plan["required_levels"])

    def test_shared_dependency_inputs_select_both_stdlib_contracts(self) -> None:
        expected = {
            "tests/acceptance/test_dependency_image_contract.py",
            "tests/acceptance/test_consume_dependency_image_contract.py",
        }
        inherited_native = {
            "tests/application_acceptance/test_browser_locale.py",
            "tests/application_acceptance/test_core_provider_mihomo.py",
            "tests/application_acceptance/test_database_application.py",
            "tests/application_acceptance/test_xray_api.py",
            "tests/application_acceptance/test_xray_generation.py",
        }
        for path in (
            "tests/acceptance/publish_dependency_image.py",
            "tests/acceptance/prepare_dependency_image.py",
            "tests/acceptance/dependency-image.lock.json",
        ):
            with self.subTest(path=path):
                domains, _ = gate.domain_for_path(path, self.manifest)
                self.assertEqual({"test-infrastructure"}, domains)
                plan = gate.make_plan("HEAD", self.manifest, [path])
                self.assertTrue(expected <= set(plan["selected_files"]))
                self.assertTrue(inherited_native <= set(plan["selected_files"]))
                self.assertTrue(inherited_native <= set(plan["required_native_suites"]))
                self.assertFalse(plan["full_suite_required"])
                self.assertNotIn("L7", plan["required_levels"])

    def test_shared_application_contracts_require_hosted_functional_but_never_l7(self) -> None:
        functional = {row["path"] for row in self.manifest["test_files"]
                      if row.get("execution_profile") == "hosted-isolated-compose" and row["primary_level"] != "L7"}
        recovery = {row["path"] for row in self.manifest["test_files"]
                    if row.get("execution_profile") == "hosted-isolated-compose" and row["primary_level"] == "L7"}
        for changed in ("backend/fwrouter_api/services/xray_subscription_service.py",
                        "backend/fwrouter_api/services/provider_recovery.py",
                        "backend/fwrouter_api/services/vpn_auto_runtime.py",
                        "backend/fwrouter_api/db/connection.py",
                        "tests/acceptance/launcher.py"):
            with self.subTest(changed=changed):
                plan = gate.make_plan("HEAD", self.manifest, [changed])
                self.assertTrue(functional <= set(plan["required_native_suites"]))
                self.assertTrue(functional <= set(plan["selected_files"]))
                self.assertFalse(recovery & set(plan["selected_files"]))
                self.assertNotIn("L7", plan["required_levels"])
                self.assertFalse(plan["full_suite_required"])
                self.assertIn("hosted-isolated-compose", plan["required_execution_profiles"])

    def test_include_native_keeps_l7_as_explicit_deferred_staging_not_routine_selection(self) -> None:
        l7_path = "tests/application_acceptance/test_xray_worker_crash_l7.py"
        plan = gate.make_plan(
            "HEAD", self.manifest, ["tests/application_acceptance/test_xray_api.py"], include_native=True)
        self.assertNotIn(l7_path, plan["selected_files"])
        self.assertIn(l7_path, plan["optional_suites"])
        self.assertEqual([l7_path], plan["deferred_staging_suites"])
        self.assertNotIn("L7", plan["required_levels"])
        self.assertFalse(plan["staging_required"])
        self.assertEqual({"deferred_staging_suites": [l7_path],
                          "staging_gate_status": "NOTRUN_RELEASE_GATE"},
                         gate._staging_gate_evidence(plan))

    def test_l0_yaml_allows_only_compose_override_data_tags(self) -> None:
        import yaml

        parsed = gate.parse_l0_yaml(
            "tests/acceptance/compose.packet.yaml",
            "services: !override {app: {cap_add: !override [NET_ADMIN, NET_RAW]}}\n",
        )
        self.assertEqual({"services": {"app": {"cap_add": ["NET_ADMIN", "NET_RAW"]}}}, parsed)
        with self.assertRaises(yaml.YAMLError):
            gate.parse_l0_yaml("tests/acceptance/compose.packet.yaml", "value: !unknown [x]\n")
        with self.assertRaises(yaml.YAMLError):
            gate.parse_l0_yaml("tests/acceptance/other.yaml", "services: !override {app: {}}\n")
        with self.assertRaises(yaml.YAMLError):
            gate.parse_l0_yaml("tests/acceptance/compose.packet.yaml", "value: !override scalar\n")

    def test_plan_rejects_forged_l7_staging_deferral(self) -> None:
        changed = "tests/application_acceptance/test_xray_api.py"
        with mock.patch.object(gate, "git", return_value="a" * 40), \
             mock.patch.object(gate, "changed_paths", return_value=[changed]):
            plan = gate.make_plan("b" * 40, self.manifest, [changed], include_native=True)
            gate.ensure_plan(plan, self.manifest)
            forged = copy.deepcopy(plan)
            forged["deferred_staging_suites"] = []
            forged["plan_digest"] = gate.canonical_digest(
                {key: value for key, value in forged.items() if key != "plan_digest"})
            with self.assertRaisesRegex(gate.GateError, "deferred_staging_suites"):
                gate.ensure_plan(forged, self.manifest)

    def test_required_native_rule_cannot_force_l7_or_destructive_suite(self) -> None:
        l7_path = "tests/application_acceptance/test_xray_worker_crash_l7.py"
        invalid = copy.deepcopy(self.manifest)
        invalid["path_rules"][0]["required_native"] = [l7_path]
        with self.assertRaisesRegex(gate.GateError, "cannot force an L7/destructive"):
            gate.validate_manifest(invalid)

    def test_l5_anchor_cannot_force_l7_or_destructive_suite(self) -> None:
        l7_path = "tests/application_acceptance/test_xray_worker_crash_l7.py"
        invalid = copy.deepcopy(self.manifest)
        invalid["policies"]["L5"]["anchors"].append(l7_path)
        with self.assertRaisesRegex(gate.GateError, "L5 anchor cannot force an L7/destructive"):
            gate.validate_manifest(invalid)

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

    def test_pr_main_and_l6_stop_after_unit_failure_and_persist_nonpromoting_report(self) -> None:
        import yaml
        workflow = (gate.ROOT / ".github/workflows/test-gates.yml").read_text()
        jobs = yaml.safe_load(workflow)["jobs"]
        self.assertEqual("always()", jobs['affected']['if'])
        self.assertEqual(set(parallel_ci.ROLES), set(jobs['affected']['needs']))
        self.assertIn('FWROUTER_CI_NEEDS: ${{ toJSON(needs) }}', workflow)
        self.assertNotIn('continue-on-error:', workflow)
        outcomes = {role: {'result': 'success'} for role in parallel_ci.ROLES}
        required = {role: True for role in parallel_ci.ROLES}
        for status in ('failure', 'cancelled', 'skipped', None):
            with self.subTest(status=status):
                mutated = copy.deepcopy(outcomes)
                mutated['unit']['result'] = status
                self.assertTrue(parallel_ci.validate_results(mutated, required))
        l6 = (gate.ROOT / '.github/workflows/test-full-suite.yml').read_text()
        import l6_ci
        self.assertEqual('always()', yaml.safe_load(l6)['jobs']['l6']['if'])
        for status in ('failure', 'cancelled', 'skipped', None):
            mutated = copy.deepcopy(outcomes)
            mutated['unit']['result'] = status
            with self.assertRaises(l6_ci.gate.GateError):
                l6_ci.check_jobs(mutated)

    def test_parallel_producer_envelope_rejects_wrong_identity_role_and_outcome(self) -> None:
        plan = {
            "source_commit": "a" * 40,
            "plan_digest": "b" * 64,
            "manifest_digest": "c" * 64,
            "manifest_version": "manifest-v1",
        }
        identity_fields = {
            "source_commit": "d" * 40,
            "plan_digest": "e" * 64,
            "manifest_digest": "f" * 64,
            "manifest_version": "manifest-v2",
            "run_id": "987654",
            "run_attempt": "9",
            "role": "provider",
            "outcome": "failure",
        }
        with tempfile.TemporaryDirectory(prefix="fwrouter-producer-identity-") as temp:
            root = Path(temp)
            (root / "receipt.json").write_text('{"eligible":true}\n', encoding="utf-8")
            with mock.patch.dict(os.environ, {"GITHUB_RUN_ID": "123456", "GITHUB_RUN_ATTEMPT": "2"}):
                for field, wrong in identity_fields.items():
                    with self.subTest(field=field):
                        envelope = parallel_ci.seal(root, "hosted", plan, "success")
                        envelope[field] = wrong
                        envelope["producer_digest"] = gate.canonical_digest(
                            {key: value for key, value in envelope.items() if key != "producer_digest"})
                        gate.atomic_json(root / "producer.json", envelope)
                        with self.assertRaisesRegex(parallel_ci.gate.GateError, "identity/status"):
                            parallel_ci.verify_producer(root, "hosted", plan)

    def test_parallel_producer_envelope_rejects_file_inventory_changes_and_symlinks(self) -> None:
        plan = {
            "source_commit": "a" * 40,
            "plan_digest": "b" * 64,
            "manifest_digest": "c" * 64,
            "manifest_version": "manifest-v1",
        }
        for change in ("missing", "extra", "symlink", "digest"):
            with self.subTest(change=change), tempfile.TemporaryDirectory(
                    prefix="fwrouter-producer-files-") as temp:
                root = Path(temp)
                payload = root / "receipt.json"
                payload.write_text('{"eligible":true}\n', encoding="utf-8")
                with mock.patch.dict(os.environ, {"GITHUB_RUN_ID": "123456", "GITHUB_RUN_ATTEMPT": "2"}):
                    envelope = parallel_ci.seal(root, "hosted", plan, "success")
                    if change == "missing":
                        payload.unlink()
                        message = "producer files"
                    elif change == "extra":
                        (root / "unexpected.txt").write_text("unexpected\n", encoding="utf-8")
                        message = "producer files"
                    elif change == "symlink":
                        payload.unlink()
                        (root / "outside.txt").write_text("outside\n", encoding="utf-8")
                        payload.symlink_to(root / "outside.txt")
                        message = "symlink"
                    else:
                        envelope["producer_digest"] = "0" * 64
                        gate.atomic_json(root / "producer.json", envelope)
                        message = "digest"
                    with self.assertRaisesRegex(parallel_ci.gate.GateError, message):
                        parallel_ci.verify_producer(root, "hosted", plan)

    def test_parallel_documentation_plan_preserves_l0_only_and_main_smoke(self) -> None:
        manifest = gate.load_manifest()
        with mock.patch.object(gate, "changed_paths", return_value=["README.md"]):
            plan = gate.make_plan(gate.git("rev-parse", "HEAD"), manifest, include_native=True)
            gate.ensure_plan(plan, manifest)
        self.assertEqual([], plan["selected_files"])
        ordinary = parallel_ci.requirements(plan)
        self.assertEqual({"plan", "build", "unit"},
                         {role for role, needed in ordinary.items() if needed})
        main = parallel_ci.requirements(plan, main_smoke=True)
        self.assertEqual({"plan", "build", "unit", "hosted"},
                         {role for role, needed in main.items() if needed})
        results = {role: {"result": "success" if needed else "skipped"}
                   for role, needed in ordinary.items()}
        self.assertEqual([], parallel_ci.validate_results(results, ordinary))

    def test_parallel_result_matrix_requires_success_and_only_allows_optional_skip(self) -> None:
        required = {role: True for role in parallel_ci.ROLES}
        successes = {role: {"result": "success"} for role in parallel_ci.ROLES}
        self.assertEqual([], parallel_ci.validate_results(successes, required))
        for role in parallel_ci.ROLES:
            for status in ("failure", "cancelled", "skipped", None):
                with self.subTest(role=role, status=status):
                    changed = copy.deepcopy(successes)
                    if status is None:
                        del changed[role]
                    else:
                        changed[role]["result"] = status
                    self.assertTrue(parallel_ci.validate_results(changed, required))

        optional = dict(required)
        optional["provider"] = False
        skipped = copy.deepcopy(successes)
        skipped["provider"]["result"] = "skipped"
        self.assertEqual([], parallel_ci.validate_results(skipped, optional))
        for status in ("success", "failure", "cancelled", None):
            with self.subTest(optional_provider=status):
                changed = copy.deepcopy(skipped)
                if status is None:
                    del changed["provider"]
                else:
                    changed["provider"]["result"] = status
                self.assertTrue(parallel_ci.validate_results(changed, optional))

    def test_parallel_aggregate_malformed_upstream_json_writes_failure_receipts(self) -> None:
        plan = {
            "source_commit": "a" * 40,
            "plan_digest": "b" * 64,
            "manifest_digest": "c" * 64,
            "manifest_version": "manifest-v1",
            "selected_files": [],
        }
        with tempfile.TemporaryDirectory(prefix="fwrouter-bad-needs-json-") as temp:
            root = Path(temp)
            output = root / "report"
            artifacts = root / "artifacts"
            artifacts.mkdir()
            plan_path = root / "plan.json"
            plan_path.write_text("{}\n", encoding="utf-8")
            args = type("AggregateArgs", (), {
                "root": output, "plan": plan_path, "artifacts": artifacts, "main_smoke": False,
            })()
            with mock.patch.dict(os.environ, {
                    "FWROUTER_CI_NEEDS": "{not-json", "GITHUB_RUN_ID": "123456",
                    "GITHUB_RUN_ATTEMPT": "2"}), \
                 mock.patch.object(parallel_ci, "load_plan", return_value=plan):
                self.assertEqual(1, parallel_ci.aggregate(args))
            result = json.loads((output / "required-result.json").read_text(encoding="utf-8"))
            report = json.loads((output / "fwrouter-aggregate.json").read_text(encoding="utf-8"))
            self.assertIs(result["eligible"], False)
            self.assertTrue(result["error"])
            self.assertIs(report["eligible"], False)

    @unittest.skipUnless(Path("/usr/bin/time").is_file(), "/usr/bin/time is required by the real child runner")
    def test_parallel_run_command_bounds_live_log_and_preserves_tail_and_exit(self) -> None:
        import sys

        with tempfile.TemporaryDirectory(prefix="fwrouter-bounded-child-log-") as temp:
            log = Path(temp) / "noisy-child.log"
            child = (
                "import os, sys, time\n"
                "os.write(1, b'A' * (5 * 1024 * 1024))\n"
                "time.sleep(0.15)\n"
                "size = os.path.getsize(os.environ['FWROUTER_TEST_CHILD_LOG'])\n"
                "sys.stdout.write(f'\\nON_DISK={size}\\n')\n"
                "sys.stdout.flush()\n"
                "time.sleep(0.15)\n"
                "sys.stdout.write('TAIL_SENTINEL')\n"
                "sys.stdout.flush()\n"
                "raise SystemExit(23)\n"
            )
            stdout = StringIO()
            with mock.patch.dict(os.environ, {"FWROUTER_TEST_CHILD_LOG": str(log)}), \
                 redirect_stdout(stdout):
                code, _ = parallel_ci.run_command([sys.executable, "-c", child], log)

            self.assertEqual(23, code)
            self.assertLessEqual(log.stat().st_size, 4 * 1024 * 1024)
            report_line = next(line for line in stdout.getvalue().splitlines()
                               if line.startswith("ON_DISK="))
            self.assertLessEqual(int(report_line.split("=", 1)[1]), 4 * 1024 * 1024)
            suffix = f"\n{report_line}\nTAIL_SENTINEL\n"
            self.assertEqual("A" * (65537 - len(suffix)) + suffix, stdout.getvalue())

    def test_parallel_aggregate_reports_preflight_status_without_plan_artifact(self) -> None:
        for status in ("failure", "cancelled", "skipped", None):
            with self.subTest(status=status), tempfile.TemporaryDirectory(
                    prefix="fwrouter-plan-preflight-status-") as temp:
                root = Path(temp)
                output = root / "report"
                artifacts = root / "artifacts"
                artifacts.mkdir()
                plan_path = root / "absent-plan.json"
                results = {} if status is None else {"plan": {"result": status}}
                args = type("AggregateArgs", (), {
                    "root": output, "plan": plan_path, "artifacts": artifacts, "main_smoke": False,
                })()
                with mock.patch.dict(os.environ, {
                        "FWROUTER_CI_NEEDS": json.dumps(results), "GITHUB_RUN_ID": "123456",
                        "GITHUB_RUN_ATTEMPT": "2"}):
                    self.assertEqual(1, parallel_ci.aggregate(args))
                result = json.loads((output / "required-result.json").read_text(encoding="utf-8"))
                self.assertIs(result["eligible"], False)
                self.assertEqual(results, result["upstream"])
                self.assertIn("plan preflight result", result["error"])
                self.assertIn(status or "missing", result["error"])
                report = json.loads((output / "fwrouter-aggregate.json").read_text(encoding="utf-8"))
                self.assertIs(report["eligible"], False)


    def test_workflow_unit_exit_normalization_preserves_success_and_fails_closed(self) -> None:
        # V3 consumes typed GitHub job conclusions instead of shell exit-code
        # strings. Preserve the same fail-closed property through the real L6
        # finalizer: malformed/absent/failed unit outcomes never promote.
        import l6_ci
        from types import SimpleNamespace
        for status in ('failure', 'cancelled', 'skipped', 'timed_out', '', None, '0', 0, 256):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as temp:
                outcomes = {role: {'result': 'success'} for role in l6_ci.ROLES}
                outcomes['unit']['result'] = status
                output = Path(temp) / 'required'
                args = SimpleNamespace(root=output, artifacts=Path(temp) / 'missing', scope='branch-regression-non-promotional')
                with mock.patch.dict(os.environ, {'FWROUTER_CI_NEEDS': json.dumps(outcomes)}, clear=False):
                    self.assertEqual(1, l6_ci.aggregate(args))
                report = json.loads((output / 'required-result.json').read_text())
                self.assertFalse(report['eligible'])
                self.assertEqual(outcomes, report['upstream'])
                self.assertIn('unit', report['error'])

    def test_push_fast_lane_includes_stage_but_excludes_main(self) -> None:
        workflow = (gate.ROOT / ".github/workflows/push-fast.yml").read_text(encoding="utf-8")
        trigger = workflow.split("on:", 1)[1].split("permissions:", 1)[0]
        self.assertIn("branches-ignore: [main]", trigger)
        self.assertNotIn("stage/test-architecture-ci-stabilization", trigger)

    def test_push_fast_lane_empty_diff_is_an_explicit_lint_only_noop(self) -> None:
        import yaml

        workflow = yaml.safe_load((gate.ROOT / ".github/workflows/push-fast.yml").read_text())
        steps = workflow["jobs"]["fast"]["steps"]
        step = next(item for item in steps if item.get("name") == "Plan and run routine affected L0-L1 only")
        script = step["run"]
        self.assertLess(script.index("gate.py lint-changed --base"), script.index("git diff --name-only -z"))
        self.assertLess(script.index("git diff --name-only -z"), script.index("gate.py plan"))
        self.assertIn("Test execution NOT RUN: source tree unchanged; no test/deploy qualification.", script)

        real_git = shutil.which("git")
        self.assertIsNotNone(real_git)

        def execute_case(change: str | None, *, before: str | None = None,
                         fail_diff: bool = False, lint_exit: str = "0", run_exit: str = "0"):
            temp_context = tempfile.TemporaryDirectory(prefix="fwrouter-push-fast-shell-")
            self.addCleanup(temp_context.cleanup)
            root = Path(temp_context.name)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run([real_git, "init", "--quiet"], cwd=repo, check=True)
            subprocess.run([real_git, "config", "user.name", "Gate Contract"], cwd=repo, check=True)
            subprocess.run([real_git, "config", "user.email", "gate-contract@example.invalid"], cwd=repo, check=True)
            source = repo / "source.py"
            source.write_text("value = 1\n", encoding="utf-8")
            subprocess.run([real_git, "add", "source.py"], cwd=repo, check=True)
            subprocess.run([real_git, "commit", "--quiet", "-m", "base"], cwd=repo, check=True)
            base = subprocess.run([real_git, "rev-parse", "HEAD"], cwd=repo, check=True,
                                  capture_output=True, text=True).stdout.strip()
            event_before = before or base
            if change == "content":
                source.write_text("value = 2\n", encoding="utf-8")
                subprocess.run([real_git, "add", "source.py"], cwd=repo, check=True)
            elif change == "mode":
                source.chmod(0o755)
                subprocess.run([real_git, "add", "source.py"], cwd=repo, check=True)
            elif change == "deletion":
                source.unlink()
                subprocess.run([real_git, "add", "-u"], cwd=repo, check=True)
            if change is not None:
                subprocess.run([real_git, "commit", "--quiet", "-m", change], cwd=repo, check=True)

            bin_dir = root / "bin"
            bin_dir.mkdir()
            python_stub = bin_dir / "python"
            python_stub.write_text(textwrap.dedent("""\
                #!/usr/bin/env bash
                set -eu
                printf '%s\\n' "$*" >> "$STUB_CALLS"
                if [[ "$1" == tests/gates/gate.py && "$2" == lint-changed ]]; then
                  exit "$STUB_LINT_EXIT"
                fi
                if [[ "$1" == tests/gates/gate.py && "$2" == plan ]]; then
                  while (($#)); do
                    if [[ "$1" == --output ]]; then shift; printf '{}\\n' > "$1"; break; fi
                    shift
                  done
                  exit 0
                fi
                if [[ "$1" == tests/gates/gate.py && "$2" == run ]]; then
                  while (($#)); do
                    if [[ "$1" == --output ]]; then shift; printf '{}\\n' > "$1"; break; fi
                    shift
                  done
                  echo STUB_TEST_RUN
                  exit "$STUB_RUN_EXIT"
                fi
                exit 91
            """), encoding="utf-8")
            python_stub.chmod(0o755)
            env = dict(os.environ)
            env.update({
                "PATH": str(bin_dir) + os.pathsep + env.get("PATH", ""),
                "EVENT_BEFORE": event_before,
                "RUNNER_TEMP": str(root / "runner-temp"),
                "GITHUB_STEP_SUMMARY": str(root / "summary.md"),
                "STUB_CALLS": str(root / "calls.log"),
                "STUB_LINT_EXIT": lint_exit,
                "STUB_RUN_EXIT": run_exit,
            })
            Path(env["RUNNER_TEMP"]).mkdir()
            if fail_diff:
                git_stub = bin_dir / "git"
                git_stub.write_text(textwrap.dedent(f"""\
                    #!/usr/bin/env bash
                    if [[ "$1" == diff ]]; then exit 77; fi
                    exec {shlex.quote(real_git)} "$@"
                """), encoding="utf-8")
                git_stub.chmod(0o755)
            completed = subprocess.run(["bash", "-c", script], cwd=repo, env=env,
                                       capture_output=True, text=True)
            calls = Path(env["STUB_CALLS"]).read_text(encoding="utf-8").splitlines() \
                if Path(env["STUB_CALLS"]).exists() else []
            summary = Path(env["GITHUB_STEP_SUMMARY"]).read_text(encoding="utf-8") \
                if Path(env["GITHUB_STEP_SUMMARY"]).exists() else ""
            artifacts = {path.name for path in Path(env["RUNNER_TEMP"]).iterdir()}
            return completed, calls, summary, artifacts

        completed, calls, summary, artifacts = execute_case(None)
        self.assertEqual(0, completed.returncode, completed.stderr)
        self.assertEqual(1, len(calls))
        self.assertIn("lint-changed", calls[0])
        self.assertIn("NOT RUN", summary)
        self.assertIn("no test/deploy qualification", summary)
        self.assertNotIn("fwrouter-fast-plan.json", artifacts)
        self.assertNotIn("fwrouter-fast-report.json", artifacts)
        self.assertNotIn("gate.py plan", "\n".join(calls))
        self.assertNotIn("gate.py run", "\n".join(calls))

        for kind in ("content", "mode", "deletion"):
            with self.subTest(change=kind):
                completed, calls, summary, artifacts = execute_case(kind)
                self.assertEqual(0, completed.returncode, completed.stderr)
                self.assertEqual(3, len(calls), calls)
                self.assertIn("lint-changed", calls[0])
                self.assertIn("gate.py plan", calls[1])
                self.assertIn("gate.py run", calls[2])
                self.assertNotIn("NOT RUN", summary)
                self.assertIn("fwrouter-fast-plan.json", artifacts)
                self.assertIn("fwrouter-fast-report.json", artifacts)

        completed, calls, summary, _artifacts = execute_case("content", run_exit="9")
        self.assertNotEqual(0, completed.returncode)
        self.assertIn("gate.py run", "\n".join(calls))

        completed, calls, _summary, _artifacts = execute_case(None, fail_diff=True)
        self.assertNotEqual(0, completed.returncode)
        self.assertEqual(1, len(calls))
        self.assertIn("lint-changed", calls[0])

        completed, calls, _summary, _artifacts = execute_case(None, lint_exit="7")
        self.assertNotEqual(0, completed.returncode)
        self.assertEqual(1, len(calls))

        invalid = "f" * 40
        completed, calls, _summary, _artifacts = execute_case(None, before=invalid)
        self.assertNotEqual(0, completed.returncode)
        self.assertEqual([], calls)

        zero_sha = "0" * 40
        completed, calls, _summary, _artifacts = execute_case(None, before=zero_sha)
        self.assertNotEqual(0, completed.returncode)
        self.assertEqual([], calls)

    def test_acceptance_tail_diagnostic_repetitions_are_bounded_sequential_and_isolated(self) -> None:
        workflow = (gate.ROOT / ".github/workflows/phase-d-validation.yml").read_text(encoding="utf-8")
        job = workflow.split("  unit-fixtures:", 1)[1].split("  bootstrap-and-fast:", 1)[0]
        runner = job.split("- name: Run exact fixed unit fixture cohort", 1)[1].split(
            "- name: Retain per-file JUnit", 1)[0]
        self.assertIn("max_attempts=1", runner)
        self.assertIn('if [ "$UNIT_FIXTURE_COHORT" = acceptance-tail ]; then max_attempts=5; fi', runner)
        self.assertIn('while [ "$attempt" -le "$max_attempts" ]; do', runner)
        self.assertIn('attempt_dir="$artifact_root/attempt-$attempt"', runner)
        self.assertIn('--output "$attempt_dir/unit-fixtures-report.json"', runner)
        self.assertIn('--artifact-dir "$attempt_dir"', runner)
        self.assertIn('> "$attempt_dir/attempt-result.txt"', runner)
        self.assertLess(runner.index('if [ "$result" -ne 0 ]'), runner.index('attempt=$((attempt + 1))'))
        self.assertIn('exit "$result"', runner)
        self.assertIn('${{ runner.temp }}/phase-d-unit-fixtures/**', job)


if __name__ == "__main__":
    unittest.main(verbosity=2)
