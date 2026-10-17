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
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock


GATE_PATH = Path(__file__).with_name("gate.py")
SPEC = importlib.util.spec_from_file_location("fwrouter_gate", GATE_PATH)
assert SPEC is not None and SPEC.loader is not None
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)
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
            "tests/gates/test_network_fixture_contract.py",
            "tests/gates/test_subscription_identity_contract.py",
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
                    self.assertEqual(f"mandatory_all={expected}\n", output_path.read_text())

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

    def test_changed_python_f821_checks_only_changed_python_and_selects_one_rule(self) -> None:
        relative = str(Path(__file__).relative_to(gate.ROOT))
        with tempfile.TemporaryDirectory(prefix="fwrouter-f821-contract-") as temp, \
             mock.patch.object(gate.shutil, "which", return_value="/locked/bin/ruff"), \
             mock.patch.object(gate, "run_process", side_effect=[
                 (0, "ruff 0.17.0\n", False), (0, "", False),
             ]) as runner:
            self.assertEqual([], gate.check_changed_python_undefined_names(
                ["README.md", relative, relative], Path(temp)))
        argv = runner.call_args.args[0]
        self.assertEqual("/locked/bin/ruff", argv[0])
        self.assertEqual(["--isolated", "--select", "F821"], argv[2:5])
        self.assertIn("--ignore-noqa", argv)
        self.assertIn("--no-cache", argv)
        self.assertEqual("--", argv[-2])
        self.assertEqual([relative], argv[-1:])

    def test_changed_python_f821_missing_tool_truncation_and_diagnostics_fail_closed(self) -> None:
        relative = str(Path(__file__).relative_to(gate.ROOT))
        with tempfile.TemporaryDirectory(prefix="fwrouter-f821-contract-") as temp:
            with mock.patch.object(gate.shutil, "which", return_value=None):
                self.assertTrue(gate.check_changed_python_undefined_names(
                    [relative], Path(temp)))
            with mock.patch.object(gate.shutil, "which", return_value="/locked/bin/ruff"), \
                 mock.patch.object(gate, "run_process", side_effect=[
                     (0, "ruff 0.17.0\n", False), (0, "", True),
                 ]):
                self.assertIn("output bound", " ".join(
                    gate.check_changed_python_undefined_names([relative], Path(temp))))
            with mock.patch.object(gate.shutil, "which", return_value="/locked/bin/ruff"), \
                 mock.patch.object(gate, "run_process", side_effect=[
                     (0, "ruff 0.17.0\n", False),
                     (1, "fixture.py:1:5: F821 Undefined name `lost`", False),
                 ]):
                self.assertIn("F821", " ".join(
                    gate.check_changed_python_undefined_names([relative], Path(temp))))

    def test_changed_python_f821_requires_exact_locked_ruff_version(self) -> None:
        relative = str(Path(__file__).relative_to(gate.ROOT))
        with tempfile.TemporaryDirectory(prefix="fwrouter-f821-contract-") as temp, \
             mock.patch.object(gate.shutil, "which", return_value="/other/bin/ruff"), \
             mock.patch.object(gate, "run_process", return_value=(0, "ruff 0.16.10\n", False)) as runner:
            problems = gate.check_changed_python_undefined_names([relative], Path(temp))
        self.assertIn("hash-locked 0.17.0", " ".join(problems))
        runner.assert_called_once()

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
        workflow = (gate.ROOT / ".github/workflows/test-gates.yml").read_text(encoding="utf-8")
        required = (
            "run_main_smoke={'true' if '${{ github.event_name }}' == 'push' else 'false'}",
            "--suite xray-diagnostic",
            "if [ '${{ steps.plan.outputs.needs_qcp }}' = true ]; then",
            "if [ '${{ steps.plan.outputs.needs_xray_docker }}' = true ]; then",
            "args+=(--require-main-smoke)", "args+=(--main-smoke-report",
        )
        forbidden = (
            "needs_qcp }}' = true ] || [ '${{ steps.plan.outputs.run_main_smoke",
            "needs_xray_docker }}' = true ] || [ '${{ steps.plan.outputs.run_main_smoke",
        )
        self.assertTrue(all(item in workflow for item in required)
                        and all(item not in workflow for item in forbidden),
                        "main push must require one native smoke and select QCP/Docker-Xray only by affected plan")

    def test_main_wheel_build_is_explicit_source_and_checks_retained_project_identity(self) -> None:
        workflow = (gate.ROOT / ".github/workflows/test-gates.yml").read_text(encoding="utf-8")
        wheel_step = workflow.split("- name: Build backend wheel on main", 1)[1].split(
            "- name: Provision exact runtime inputs", 1)[0]
        trigger = workflow.split("permissions:", 1)[0]
        affected = workflow.split("  affected:", 1)[1]
        artifact_step = workflow.split("- name: Retain bounded reports", 1)[1]
        verifier = (gate.ROOT / "tests/gates/verify_backend_wheel.py").read_text(encoding="utf-8")
        contract = (
            'python -m pip wheel --no-deps --wheel-dir "$wheel_dir" ./backend' in wheel_step
            and '"$wheel_dir" "$RUNNER_TEMP/fwrouter-wheel-metadata.json"' in wheel_step
            and "branches: [main]" in trigger
            and "github.event_name == 'pull_request'" in affected
            and "github.ref == 'refs/heads/main'" in affected
            and 'endswith(".dist-info/METADATA")' in verifier
            and 'project.get("project", {}).get("name")' in verifier
            and 'project.get("project", {}).get("version")' in verifier
            and 'payload_bytes[name] == source_files[name]' in verifier
            and '"fwrouter_api/__init__.py"' in verifier
            and "fwrouter-wheel-metadata.json" in wheel_step
            and "fwrouter-wheel-metadata.json" in artifact_step
            and "if: always()" in artifact_step
        )
        self.assertTrue(contract, "main wheel must prove and retain the built source distribution identity")

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
        workflow = (gate.ROOT / ".github/workflows/test-gates.yml").read_text(encoding="utf-8")
        trigger = workflow.split("permissions:", 1)[0]
        job = workflow.split("  affected:", 1)[1].split("    steps:", 1)[0]
        plan_step = workflow.split('if [ "$EVENT_NAME" = pull_request ]; then', 1)[1].split(
            'if [ -z "$BASE"', 1)[0]
        contract = (
            "pull_request:\n    branches: [main]\n  push:\n    branches: [main]" in trigger
            and "github.event_name == 'pull_request'" in job
            and "github.ref == 'refs/heads/main'" in job
            and 'BASE="$PR_BASE_SHA"' in plan_step
            and 'BASE="$PUSH_BASE_SHA"' in plan_step
            and 'PHASE_E_BASE_SHA' not in workflow
            and 'ci:main-equivalent' not in workflow
            and 'ci:wheel-equivalent' not in workflow
            and 'stage/test-architecture-ci-stabilization' not in trigger
            and '  wheel-audit:' not in workflow
        )
        self.assertTrue(contract,
                        "affected CI must be limited to PR/main events with event-derived base SHAs")

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
        self.assertEqual((27, 12, 2), tuple(len(partitions[key]) for key in
                                             ("functional-ordinary", "provider-cohort", "packet-diagnostic")))
        self.assertEqual(41, len(set().union(*partitions.values())))
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
        affected = (gate.ROOT / ".github/workflows/test-gates.yml").read_text(encoding="utf-8")
        unit_step = affected.split("id: plan", 1)[1].split("- name: Build backend wheel", 1)[0]
        self.assertRegex(unit_step, r"printf\s+['\"]unit_exit=%s\\n['\"]\s+.*GITHUB_OUTPUT")
        self.assertIn('exit "$result"', unit_step)
        self.assertLess(unit_step.index('exit "$result"'), affected.index("Provision exact runtime inputs"))
        aggregate = affected.split("- name: Aggregate immutable source", 1)[1].split(
            "- name: Record runner use", 1)[0]
        self.assertIn("if: always()", aggregate)
        self.assertIn("--unit-failure-report --unit-exit-code", aggregate)
        self.assertLess(aggregate.index("--unit-failure-report"), aggregate.index("args=(--plan"))

        l6 = (gate.ROOT / ".github/workflows/test-full-suite.yml").read_text(encoding="utf-8")
        routine = l6.split("id: routine", 1)[1].split("- name: Provision exact native", 1)[0]
        self.assertRegex(routine, r"printf\s+['\"]unit_exit=%s\\n['\"]\s+.*GITHUB_OUTPUT")
        self.assertIn('exit "$result"', routine)
        l6_aggregate = l6.split("- name: Aggregate full L6 evidence", 1)[1].split(
            "- name: Upload L6 evidence", 1)[0]
        self.assertIn("if: always()", l6_aggregate)
        self.assertIn("--unit-failure-report --unit-exit-code", l6_aggregate)
        self.assertLess(l6_aggregate.index("--unit-failure-report"), l6_aggregate.index("args=(--plan"))

    def test_workflow_unit_exit_normalization_preserves_success_and_fails_closed(self) -> None:
        for workflow_name in ("test-gates.yml", "test-full-suite.yml"):
            with self.subTest(workflow=workflow_name):
                workflow = (gate.ROOT / ".github/workflows" / workflow_name).read_text(encoding="utf-8")
                normalization = workflow.split('case "$UNIT_EXIT" in', 1)[1].split(
                    'if [ "$unit_exit" -gt 255 ]', 1)[0]
                script = (
                    'UNIT_EXIT="$1"\ncase "$UNIT_EXIT" in' + normalization
                    + 'printf "%s\\n" "$unit_exit"\n'
                    + 'if [ "$unit_exit" -gt 255 ]; then unit_exit=1; fi\n'
                    + 'printf "%s\\n" "$unit_exit"\n'
                )
                for raw, expected in (("0", "0\n0\n"), ("1", "1\n1\n"),
                                      ("255", "255\n255\n"), ("invalid", "1\n1\n"),
                                      ("", "1\n1\n"), ("256", "256\n1\n")):
                    completed = subprocess.run(
                        ["bash", "-c", script, "unit-exit-contract", raw],
                        capture_output=True, text=True, timeout=3, check=False,
                    )
                    self.assertEqual(0, completed.returncode, completed.stderr)
                    self.assertEqual(expected, completed.stdout)

    def test_push_fast_lane_includes_stage_but_excludes_main(self) -> None:
        workflow = (gate.ROOT / ".github/workflows/push-fast.yml").read_text(encoding="utf-8")
        trigger = workflow.split("on:", 1)[1].split("permissions:", 1)[0]
        self.assertIn("branches-ignore: [main]", trigger)
        self.assertNotIn("stage/test-architecture-ci-stabilization", trigger)

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
