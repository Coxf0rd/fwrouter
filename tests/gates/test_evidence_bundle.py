from __future__ import annotations

import argparse
import contextlib
import io
from unittest import mock
import importlib.util
import json
from pathlib import Path
import re
import shutil
import tempfile
import unittest
from unittest.mock import patch
from typing import Any


SCRIPT = Path(__file__).with_name("evidence_bundle.py")
SPEC = importlib.util.spec_from_file_location("fwrouter_evidence_bundle_tested", SCRIPT)
assert SPEC and SPEC.loader
bundle = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bundle)


class EvidenceBundleTests(unittest.TestCase):
    def test_main_smoke_is_derived_only_from_actual_main_push(self) -> None:
        self.assertTrue(bundle.main_smoke_for("push", "refs/heads/main"))
        self.assertFalse(bundle.main_smoke_for("pull_request", "refs/pull/35/merge"))
        self.assertFalse(bundle.main_smoke_for("push", "refs/heads/feature"))

    def test_hidden_bundle_paths_are_limited_to_pinned_workflow_source(self) -> None:
        self.assertEqual(".github/workflows/test-gates.yml",
                         bundle.safe_path(".github/workflows/test-gates.yml").as_posix())
        self.assertEqual("verifier-source/.github/workflows/test-gates.yml",
                         bundle.safe_path("verifier-source/.github/workflows/test-gates.yml").as_posix())
        with self.assertRaisesRegex(bundle.gate.GateError, "hidden paths"):
            bundle.safe_path("evidence/.unexpected/hosted-acceptance-report.json")

    def test_projection_must_match_plan_derived_roles_and_paths(self) -> None:
        plan = {"selected_files": []}
        needed = {"plan": True, "build": True, "unit": True, "hosted": False,
                  "provider": False, "packet": False, "native": False}
        expected = {"plan": ["fwrouter-affected-plan.json"], "build": sorted([
            "build/build-summary.json", "build/wheel-metadata.json", "build/fwrouter-1.0.whl"]),
            "unit": ["fwrouter-affected-unit.json"]}
        metadata = {"evidence_selection": expected}
        bundle.validate_projection_metadata(metadata, plan, needed, expected)
        for invalid in (
            {**expected, "hosted": ["hosted-acceptance-report.json"]},
            {**expected, "build": expected["build"][:-1]},
            {**expected, "plan": ["../fwrouter-affected-plan.json"]},
            {**expected, "unit": ["fwrouter-affected-unit.json", "extra.json"]},
        ):
            with self.subTest(invalid=invalid), self.assertRaises(bundle.gate.GateError):
                bundle.validate_projection_metadata({"evidence_selection": invalid}, plan, needed, expected)

    def test_content_manifest_detects_missing_extra_modified_and_symlink_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "bundle"
            root.mkdir()
            (root / "payload.json").write_text('{"ok":true}\n', encoding="utf-8")
            bundle.gate.atomic_json(root / "metadata.json", {"schema": bundle.SCHEMA})
            files = bundle.inventory(root)
            bundle.gate.atomic_json(root / "content-manifest.json", {
                "schema": "fwrouter-ci-evidence-content/v1",
                "metadata_sha256": bundle.sha256(root / "metadata.json"),
                "file_count": len(files), "total_bytes": sum(item["bytes"] for item in files), "files": files,
            })
            bundle.verify_content(root)
            (root / "payload.json").write_text('{"ok":false}\n', encoding="utf-8")
            with self.assertRaisesRegex(bundle.gate.GateError, "modified"):
                bundle.verify_content(root)
            (root / "payload.json").write_text('{"ok":true}\n', encoding="utf-8")
            (root / "unexpected.txt").write_text("extra", encoding="utf-8")
            with self.assertRaisesRegex(bundle.gate.GateError, "extra"):
                bundle.verify_content(root)
            (root / "unexpected.txt").unlink()
            (root / "payload.json").unlink()
            with self.assertRaisesRegex(bundle.gate.GateError, "missing"):
                bundle.verify_content(root)
            (root / "payload.json").symlink_to(root / "metadata.json")
            with self.assertRaisesRegex(bundle.gate.GateError, "symlink"):
                bundle.verify_content(root)

    def test_missing_or_expired_bundle_is_not_a_verification_or_deploy_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            args = argparse.Namespace(bundle=Path(temporary) / "expired.zip-extracted",
                                      expected_sha="a" * 40, event="push", ref="refs/heads/main",
                                      run_id="123", attempt="1", require_main=True)
            with self.assertRaisesRegex(bundle.gate.GateError, "metadata.json"):
                bundle.verify_bundle(args)

    def test_resealed_metadata_still_cannot_change_expected_sha(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "bundle"
            root.mkdir()
            bundle.gate.atomic_json(root / "metadata.json", {
                "schema": bundle.SCHEMA, "source_sha": "b" * 40, "run_id": "123", "run_attempt": "1",
                "event": "push", "ref": "refs/heads/main", "repository": bundle.REPOSITORY,
                "workflow": bundle.WORKFLOW_NAME, "workflow_path": ".github/workflows/test-gates.yml",
                "required_check": bundle.REQUIRED_CHECK, "strict_aggregate": "passed",
            })
            rows = bundle.inventory(root)
            bundle.gate.atomic_json(root / "content-manifest.json", {
                "schema": "fwrouter-ci-evidence-content/v1", "metadata_sha256": bundle.sha256(root / "metadata.json"),
                "file_count": len(rows), "total_bytes": sum(row["bytes"] for row in rows), "files": rows,
            })
            args = argparse.Namespace(bundle=root, expected_sha="a" * 40, event="push",
                                      ref="refs/heads/main", run_id="123", attempt="1", require_main=True)
            with self.assertRaisesRegex(bundle.gate.GateError, "source/run/attempt"):
                bundle.verify_bundle(args)

    def test_pr_bundle_cannot_satisfy_main_only_consumer(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "bundle"
            root.mkdir()
            bundle.gate.atomic_json(root / "metadata.json", {
                "schema": bundle.SCHEMA, "source_sha": "a" * 40, "run_id": "123", "run_attempt": "1",
                "event": "pull_request", "ref": "refs/pull/35/merge", "repository": bundle.REPOSITORY,
                "workflow": bundle.WORKFLOW_NAME, "workflow_path": ".github/workflows/test-gates.yml",
                "required_check": bundle.REQUIRED_CHECK, "strict_aggregate": "passed",
            })
            rows = bundle.inventory(root)
            bundle.gate.atomic_json(root / "content-manifest.json", {
                "schema": "fwrouter-ci-evidence-content/v1", "metadata_sha256": bundle.sha256(root / "metadata.json"),
                "file_count": len(rows), "total_bytes": sum(row["bytes"] for row in rows), "files": rows,
            })
            args = argparse.Namespace(bundle=root, expected_sha="a" * 40, event="pull_request",
                                      ref="refs/pull/35/merge", run_id="123", attempt="1", require_main=True)
            with self.assertRaisesRegex(bundle.gate.GateError, "actual main push"):
                bundle.verify_bundle(args)

    def test_handoff_seals_allowlisted_worker_output_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, destination = root / "unit-source", root / "unit-handoff"
            source.mkdir()
            plan_path = root / "plan.json"
            manifest = bundle.gate.load_manifest()
            plan = {"source_commit": "a" * 40, "plan_digest": "b" * 64,
                    "manifest_digest": bundle.gate.canonical_digest(manifest),
                    "manifest_version": manifest["version"], "selected_files": []}
            report = bundle.gate._routine_shard_report(
                plan, manifest, "core-state", [], [], {}, [], l0_owned=True, l0_ok=True,
                l0_elapsed=0.1, l0_problems=[])
            report.update({"run_id": "123", "run_attempt": "1",
                           "resources_before": {"cpu_count": 2, "memory": {}, "disk_total_bytes": 4,
                                                 "disk_used_bytes": 1, "disk_free_bytes": 3},
                           "resources_after": {"cpu_count": 2, "memory": {}, "disk_total_bytes": 4,
                                                "disk_used_bytes": 1, "disk_free_bytes": 3},
                           "process_peak": {"max_rss_kib": 12345},
                           "cleanup": "verified_owned_gate_temp_removed"})
            report.pop("shard_digest", None)
            report["shard_digest"] = bundle.gate.canonical_digest(report)
            (source / "shard.json").write_text(json.dumps(report) + "\n", encoding="utf-8")
            (source / "timings.json").write_text('[{"seconds":1}]\n', encoding="utf-8")
            (source / "runner.log").write_text("verbose output", encoding="utf-8")
            (source / "capture.pcap").write_bytes(b"raw packet data")
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            args = argparse.Namespace(source=source, destination=destination, plan=plan_path, role="unit",
                                      shard_group="core-state", outcome="success", event="pull_request",
                                      ref="refs/pull/35/merge")
            needs = {"plan": True, "build": True, "unit": True, "hosted": False,
                     "provider": False, "packet": False, "native": False}
            with patch.object(bundle.gate, "ensure_plan"), patch.object(
                    bundle.parallel_ci, "requirements", return_value=needs), patch.dict(
                        "os.environ", {"GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1"}, clear=False):
                result = bundle.handoff(args)
                bundle.parallel_ci.verify_producer(destination, "unit", plan)
            self.assertEqual("success", result["outcome"])
            self.assertEqual({"shard.json", "producer.json"},
                             {path.name for path in destination.iterdir()})
            self.assertTrue((source / "runner.log").exists())
            self.assertTrue((source / "capture.pcap").exists())
            self.assertNotIn("timings.json", result["copied"])

            failed_destination = root / "failed-handoff"
            failed_args = argparse.Namespace(source=source, destination=failed_destination, plan=plan_path,
                                             role="unit", shard_group="core-state", outcome="failure", event="pull_request",
                                             ref="refs/pull/35/merge")
            with patch.object(bundle.gate, "ensure_plan"), patch.object(
                    bundle.parallel_ci, "requirements", return_value=needs), patch.dict(
                        "os.environ", {"GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1"}, clear=False):
                failed = bundle.handoff(failed_args)
                failed_envelope = bundle.read_json(failed_destination / "producer.json")
                self.assertEqual("failure", failed_envelope["outcome"])
                self.assertEqual({"shard.json", "producer.json"},
                                 {path.name for path in failed_destination.iterdir()})
            self.assertEqual("failure", failed["outcome"])

    def test_native_handoff_preserves_qcp_and_docker_xray_profile_closure(self) -> None:
        # Synthetic contract fixture: this calls the real strict native receipt
        # validators, but is not a native-runtime qualification result.
        provision = bundle.strict.provision
        manifest = bundle.gate.load_manifest()
        plan = {
            "source_commit": "a" * 40, "plan_digest": "b" * 64,
            "manifest_digest": bundle.gate.canonical_digest(manifest),
            "manifest_version": manifest["version"],
            "selected_files": [
                "backend/tests/test_protocol_native_validation.py",
                "backend/tests/test_xray_native_readback.py",
            ],
        }
        native = {
            name: {"version": provision.INPUTS[name]["version"],
                   "source_archive_sha256": provision.INPUTS[name]["sha256"],
                   "binary_sha256": provision.BINARY_SHA256[name]}
            for name in ("xray", "mihomo")
        }
        runtime_inputs = {
            "schema": "fwrouter-hosted-inputs/v1", "base_image": provision.BASE_IMAGE,
            "playwright": provision.PLAYWRIGHT_VERSION, "native": native,
            "chromium": {"version": provision.CHROMIUM_VERSION,
                         "source_archive_sha256": provision.INPUTS["chromium"]["sha256"],
                         "bundle_sha256": provision.CHROMIUM_BUNDLE_SHA256,
                         "executable_sha256": provision.CHROMIUM_BINARY_SHA256},
        }

        def archived_profile(value: dict[str, Any]) -> str:
            return bundle.hashlib.sha256((json.dumps(value, sort_keys=True, indent=2) + "\n").encode()).hexdigest()

        scenario = bundle.strict.read_json(bundle.ROOT / "tests/acceptance/qualified-child-scenarios.json")
        qcp_nonce = "1" * 32
        qcp_profile = {
            "schema": "fwrouter-qualified-child-profile/v1", "profile": "qualified-child-process",
            "source_revision": plan["source_commit"], "plan_digest": plan["plan_digest"],
            "suite_nonce": qcp_nonce,
            "mihomo": {"version": provision.INPUTS["mihomo"]["version"],
                        "sha256": provision.BINARY_SHA256["mihomo"]},
        }
        qcp_statuses = {nodeid: "passed" for nodeid in scenario["nodeids"]}
        qcp = {
            "schema": "fwrouter-qualified-child-receipt/v1", "scope": "qualified-child-process",
            "status": "passed", "source_revision": plan["source_commit"],
            "plan_digest": plan["plan_digest"], "suite_nonce": qcp_nonce,
            "cleanup": "owned_resources_removed", "expected_ids": list(scenario["nodeids"]),
            "profile_sha256": archived_profile(qcp_profile),
            "tests": {"node_status": qcp_statuses, "tests": bundle.strict.QCP_EXPECTED_TOTAL,
                      "failed": 0, "skipped": 0, "contract_valid": True},
        }
        xray_nonce = "2" * 32
        image_id = "sha256:" + "c" * 64
        xray_profile = {
            "schema": "fwrouter-qualified-docker-xray-profile/v1", "profile": "qualified-docker-xray",
            "source_revision": plan["source_commit"], "plan_digest": plan["plan_digest"],
            "suite_nonce": xray_nonce, "image_id": image_id,
            "xray_binary_sha256": provision.BINARY_SHA256["xray"],
            "xray_version": provision.INPUTS["xray"]["version"],
        }
        xray_nodeid = "backend/tests/test_xray_native_readback.py::test_loaded_client_readback_against_isolated_xray_26_2_6"
        xray = {
            "schema": "fwrouter-qualified-docker-xray-receipt/v1", "scope": "qualified-docker-xray",
            "status": "passed", "source_revision": plan["source_commit"],
            "plan_digest": plan["plan_digest"], "suite_nonce": xray_nonce,
            "cleanup": "owned_resources_removed", "expected_ids": [xray_nodeid],
            "tests": {"node_status": {xray_nodeid: "passed"}, "passed": 1, "failed": 0,
                      "skipped": 0, "contract_valid": True},
            "profile_sha256": archived_profile(xray_profile), "image_id": image_id,
            "xray_binary_sha256": provision.BINARY_SHA256["xray"],
        }

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, destination = root / "native-source", root / "native-handoff"
            (source / "qcp").mkdir(parents=True)
            (source / "xray").mkdir(parents=True)
            (source / "runtime-inputs.json").write_text(json.dumps(runtime_inputs), encoding="utf-8")
            (source / "qcp/qualified-child-receipt.json").write_text(json.dumps(qcp), encoding="utf-8")
            (source / "qcp/profile.json").write_text(json.dumps(qcp_profile), encoding="utf-8")
            (source / "xray/qualified-xray-docker-receipt.json").write_text(json.dumps(xray), encoding="utf-8")
            (source / "xray/profile.json").write_text(json.dumps(xray_profile), encoding="utf-8")
            plan_path = root / "plan.json"
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            args = argparse.Namespace(source=source, destination=destination, plan=plan_path,
                                      role="native", outcome="success", event="pull_request",
                                      ref="refs/pull/1/merge")
            with patch.object(bundle.gate, "ensure_plan"), patch.object(
                    bundle.parallel_ci, "requirements", return_value={"native": True}), patch.dict(
                        "os.environ", {"GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1"}, clear=False):
                bundle.handoff(args)

            qcp_path = destination / "qcp/qualified-child-receipt.json"
            xray_path = destination / "xray/qualified-xray-docker-receipt.json"
            self.assertTrue(qcp_path.with_name("profile.json").is_file())
            self.assertTrue(xray_path.with_name("profile.json").is_file())
            self.assertEqual(([], qcp_statuses), bundle.strict.validate_qcp_receipt(qcp_path, plan, runtime_inputs))
            self.assertEqual(([], {xray_nodeid: "passed"}),
                             bundle.strict.validate_xray_docker_receipt(xray_path, plan, runtime_inputs))

            evidence = root / "evidence"
            evidence.mkdir()
            for profile, receipt_name in (("qcp", "qualified-child-receipt.json"),
                                          ("xray", "qualified-xray-docker-receipt.json")):
                target = evidence / f"fwrouter-v2-native-{profile}-123-1"
                (target / profile).mkdir(parents=True)
                shutil.copyfile(destination / "runtime-inputs.json", target / "runtime-inputs.json")
                shutil.copyfile(destination / profile / receipt_name, target / profile / receipt_name)
                shutil.copyfile(destination / profile / "profile.json", target / profile / "profile.json")
                with patch.dict("os.environ", {"GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1"},
                                clear=False):
                    bundle.parallel_ci.seal(target, "native", plan, "success")
                self.assertTrue((target / "producer.json").is_file())
            with patch.dict("os.environ", {"GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1"},
                            clear=False):
                native_evidence = bundle.parallel_ci.combine_native_profiles(evidence, plan, ["qcp", "xray"])
                bundle.parallel_ci.seal(native_evidence, "native", plan, "success")
            with patch.dict("os.environ", {"GITHUB_RUN_ID": "123", "GITHUB_RUN_ATTEMPT": "1"},
                            clear=False):
                selected = bundle.expected_evidence_paths(evidence, plan, {"native": True})["native"]
                self.assertIn("profiles/qcp/qcp/profile.json", selected)
                self.assertIn("profiles/xray/xray/profile.json", selected)
                bundle.verify_native_profiles(evidence, plan)
                unexpected = native_evidence / "profiles/qcp/diagnostic.log"
                unexpected.write_text("synthetic", encoding="utf-8")
                bundle.parallel_ci.seal(native_evidence / "profiles/qcp", "native", plan, "success")
                bundle.parallel_ci.seal(native_evidence, "native", plan, "success")
                with self.assertRaisesRegex(bundle.gate.GateError, "unapproved sealed files"):
                    bundle.expected_evidence_paths(evidence, plan, {"native": True})
                unexpected.unlink()
                bundle.parallel_ci.seal(native_evidence / "profiles/qcp", "native", plan, "success")
                bundle.parallel_ci.seal(native_evidence, "native", plan, "success")
                (native_evidence / "runtime-inputs.json").write_text("{}", encoding="utf-8")
                with self.assertRaisesRegex(bundle.gate.GateError, "input manifest differs"):
                    bundle.verify_native_profiles(evidence, plan)
            self.assertIn("tests/acceptance/qualified-child-scenarios.json", bundle.SOURCE_FILES)

    def test_runtime_identity_ignores_only_validated_measurement_timings(self) -> None:
        provision = bundle.strict.provision
        measurements = {
            "xray": {"source_archive_bytes": 1, "executable_bytes": 2,
                     "download_seconds": 1.0, "extract_seconds": 2.0},
            "mihomo": {"source_archive_bytes": 3, "executable_bytes": 4,
                       "download_seconds": 3.0, "extract_seconds": 4.0},
            "chromium": {"source_archive_bytes": 5, "normalized_bundle_bytes": 6,
                         "expanded_content_bytes": 7, "chrome_executable_bytes": 8,
                         "download_seconds": 5.0, "normalize_seconds": 6.0},
        }
        self.assertTrue(provision.valid_asset_measurements(measurements))
        baseline = {"native": {"mihomo": {"sha256": "a"}}, "asset_measurements": measurements}
        changed_timing = json.loads(json.dumps(baseline))
        changed_timing["asset_measurements"]["xray"]["download_seconds"] = 19.0
        self.assertEqual(bundle.parallel_ci.runtime_identity(baseline),
                         bundle.parallel_ci.runtime_identity(changed_timing))
        changed_size = json.loads(json.dumps(baseline))
        changed_size["asset_measurements"]["xray"]["source_archive_bytes"] = 99
        self.assertNotEqual(bundle.parallel_ci.runtime_identity(baseline),
                            bundle.parallel_ci.runtime_identity(changed_size))
        changed_pin = json.loads(json.dumps(baseline))
        changed_pin["native"]["mihomo"]["sha256"] = "b"
        self.assertNotEqual(bundle.parallel_ci.runtime_identity(baseline),
                            bundle.parallel_ci.runtime_identity(changed_pin))

    def test_eligibility_requires_exact_run_attempt_and_all_required_job_successes(self) -> None:
        manifest = bundle.gate.load_manifest()
        plan = {"source_commit": "a" * 40, "plan_digest": "b" * 64,
                "manifest_digest": bundle.gate.canonical_digest(manifest), "manifest_version": manifest["version"],
                "selected_files": [], "changed_paths": ["synthetic"]}

        def results_for(main: bool) -> dict[str, dict[str, str]]:
            needed = bundle.parallel_ci.requirements(plan, main)
            return {role: {"result": "success" if required else "skipped"}
                    for role, required in needed.items()}

        def fixtures(root: Path, event: str, ref: str, results: dict[str, dict[str, str]], attempt: str):
            evidence = root / "evidence"
            plan_dir = evidence / "plan"
            plan_dir.mkdir(parents=True)
            (plan_dir / "fwrouter-affected-plan.json").write_text(json.dumps(plan), encoding="utf-8")
            required = root / "required"
            required.mkdir(parents=True)
            bundle.gate.atomic_json(required / "required-result.json", {
                "schema": "fwrouter-parallel-required/v1", "source_commit": plan["source_commit"],
                "plan_digest": plan["plan_digest"], "manifest_digest": plan["manifest_digest"],
                "manifest_version": plan["manifest_version"], "run_id": "456", "run_attempt": attempt,
                "eligible": True, "upstream": results, "safe_recovery_required": False,
            })
            bundle.gate.atomic_json(required / "fwrouter-aggregate.json", {
                "eligible": True, "source_commit": plan["source_commit"], "plan_digest": plan["plan_digest"],
                "manifest_version": plan["manifest_version"], "main_smoke_required": bundle.main_smoke_for(event, ref),
                "blocked_reasons": [], "missing_levels": [],
            })
            return evidence, required

        with tempfile.TemporaryDirectory() as temporary, patch.object(bundle.gate, "ensure_plan"):
            base = Path(temporary)
            for event, ref in (("pull_request", "refs/pull/35/merge"), ("push", "refs/heads/main")):
                results = results_for(bundle.main_smoke_for(event, ref))
                evidence, required = fixtures(base / event, event, ref, results, "2")
                bundle.check_eligibility(evidence, required, event=event, ref=ref,
                                         run_id="456", attempt="2", upstream=results)

            for failed in ("failure", "cancelled", "skipped", "missing"):
                results = results_for(False)
                if failed == "missing":
                    results.pop("unit")
                else:
                    results["unit"] = {"result": failed}
                evidence, required = fixtures(base / f"bad-{failed}", "pull_request", "refs/pull/35/merge", results, "2")
                with self.subTest(job_result=failed), self.assertRaisesRegex(bundle.gate.GateError, "not eligible"):
                    bundle.check_eligibility(evidence, required, event="pull_request", ref="refs/pull/35/merge",
                                             run_id="456", attempt="2", upstream=results)

            results = results_for(False)
            evidence, required = fixtures(base / "stale-attempt", "pull_request", "refs/pull/35/merge", results, "1")
            with self.assertRaisesRegex(bundle.gate.GateError, "identity/status mismatch"):
                bundle.check_eligibility(evidence, required, event="pull_request", ref="refs/pull/35/merge",
                                         run_id="456", attempt="2", upstream=results)

            evidence, required = fixtures(base / "recovery-downgrade", "pull_request", "refs/pull/35/merge", results, "2")
            marker = bundle.read_json(required / "required-result.json")
            marker["safe_recovery_required"] = True
            bundle.gate.atomic_json(required / "required-result.json", marker)
            with self.assertRaisesRegex(bundle.gate.GateError, "safe-recovery requirement"):
                bundle.check_eligibility(evidence, required, event="pull_request", ref="refs/pull/35/merge",
                                         run_id="456", attempt="2", upstream=results)

    def test_diagnostics_keep_allowlisted_receipts_and_exclude_raw_payloads(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence, required, destination = root / "evidence", root / "required", root / "diagnostics"
            (evidence / "hosted").mkdir(parents=True)
            required.mkdir()
            producer_root = evidence / "hosted"
            (producer_root / "producer.json").write_text('{"schema":"safe"}\n', encoding="utf-8")
            (producer_root / "hosted-acceptance-report.json").write_text('{"status":"failed"}\n', encoding="utf-8")
            (producer_root / "secrets.json").write_text('{"token":"never-copy"}\n', encoding="utf-8")
            (producer_root / "run.log").write_text("private output", encoding="utf-8")
            (producer_root / "capture.pcap").write_bytes(b"pcap")
            jobs = root / "jobs.json"
            jobs.write_text(json.dumps({"hosted": {"result": "failure"}}), encoding="utf-8")
            args = argparse.Namespace(destination=destination, required=required, jobs=jobs, evidence=evidence,
                                      event="pull_request", ref="refs/pull/1/merge", run_id="123", attempt="1",
                                      source_sha="a" * 40)
            result = bundle.diagnostics(args)
            content = sorted(path.relative_to(destination).as_posix()
                             for path in destination.rglob("*") if path.is_file())
            self.assertIn("receipts/hosted/hosted-acceptance-report.json.diagnostic-projection.json", content)
            self.assertIn("receipts/hosted/producer.json.diagnostic-projection.json", content)
            self.assertNotIn("receipts/hosted/secrets.json.diagnostic-projection.json", content)
            self.assertNotIn("run.log", " ".join(content))
            self.assertNotIn("capture.pcap", " ".join(content))
            self.assertEqual("failed_or_incomplete", result["status"])
            projected = bundle.read_json(destination / "receipts/hosted/hosted-acceptance-report.json.diagnostic-projection.json")
            self.assertEqual({"redacted": True, "qualification_evidence": False,
                              "source_receipt_validated": False}, projected["_diagnostic_projection"])

    def test_diagnostics_redact_nested_secrets_from_artifacts_and_stdout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence, required, destination = root / "evidence", root / "required", root / "diagnostics"
            (evidence / "hosted").mkdir(parents=True)
            required.mkdir()
            required_result = {
                "schema": "fwrouter-parallel-required/v1", "eligible": False,
                "upstream": {"hosted": {"result": "failure", "error": "token=synthetic-secret"}},
                "blocked_reasons": ["authorization: SyntheticAuthSecret"],
                "nested": {"api_token": "SyntheticApiKeySecret", "message": "password=SyntheticPasswordSecret"},
            }
            aggregate = {"eligible": False, "failure_diagnostics": [
                {"error": "client_secret=SyntheticClientSecret", "details": {"access_key": "SyntheticAccessSecret"}}
            ]}
            producer = {"schema": "producer/v1", "notes": ["secret SyntheticNestedSecret"],
                        "credentials": {"value": "SyntheticCredentialSecret"}}
            source_paths = {
                required / "required-result.json": required_result,
                required / "fwrouter-aggregate.json": aggregate,
                evidence / "hosted" / "producer.json": producer,
            }
            originals = {}
            for path, value in source_paths.items():
                path.write_text(json.dumps(value), encoding="utf-8")
                originals[path] = path.read_bytes()
            jobs = root / "jobs.json"
            jobs.write_text(json.dumps({"hosted": {"result": "failure"}}), encoding="utf-8")
            args = argparse.Namespace(destination=destination, required=required, jobs=jobs, evidence=evidence,
                                      event="pull_request", ref="refs/pull/1/merge", run_id="123", attempt="1",
                                      source_sha="a" * 40)
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                with mock.patch.object(bundle.sys, "argv", [
                    "evidence_bundle.py", "diagnostics", "--required", str(required),
                    "--jobs", str(jobs), "--destination", str(destination),
                    "--event", args.event, "--ref", args.ref, "--run-id", args.run_id,
                    "--attempt", args.attempt, "--source-sha", args.source_sha,
                    "--evidence", str(evidence),
                ]):
                    self.assertEqual(0, bundle.main())

            output = stdout.getvalue()
            all_files = [path for path in destination.rglob("*") if path.is_file()]
            artifact_text = "\n".join(path.read_text(encoding="utf-8") for path in all_files)
            for secret in (
                "synthetic-secret", "SyntheticAuthSecret", "SyntheticApiKeySecret",
                "SyntheticPasswordSecret", "SyntheticClientSecret", "SyntheticAccessSecret",
                "SyntheticNestedSecret", "SyntheticCredentialSecret",
            ):
                self.assertNotIn(secret, output)
                self.assertNotIn(secret, artifact_text)
            self.assertIn("[REDACTED]", artifact_text)
            self.assertIn("qualification_evidence", artifact_text)
            for path, original in originals.items():
                self.assertEqual(original, path.read_bytes())

    def test_summary_fails_if_aggregate_passes_but_publication_or_receipt_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            required = root / "required"
            required.mkdir()
            bundle.gate.atomic_json(required / "required-result.json", {
                "eligible": True, "source_commit": "a" * 40, "upstream": {},
            })
            for bundle_outcome, publication, artifact_id, digest in (
                ("success", "failure", "", ""), ("failure", "skipped", "", ""),
                ("success", "success", "", ""), ("success", "success", "55", "not-a-digest"),
                ("success", "success", "55", "a" * 64),
            ):
                output = root / f"summary-{publication}-{artifact_id}-{len(digest)}.md"
                args = argparse.Namespace(aggregate_outcome="success", bundle_outcome=bundle_outcome,
                                          publish_outcome=publication, artifact_id=artifact_id,
                                          artifact_digest=digest, required=required, output=output,
                                          run_id="123", attempt="1")
                self.assertEqual(2, bundle.summary(args))
                self.assertIn("Overall result: **FAIL / INCOMPLETE**", output.read_text(encoding="utf-8"))

    def test_workflow_preserves_eight_jobs_required_name_and_no_write_permission(self) -> None:
        workflow = (bundle.ROOT / ".github/workflows/test-gates.yml").read_text(encoding="utf-8")
        self.assertNotIn("actions: write", workflow)
        self.assertIn("name: Affected L0-L5 with hosted profiles", workflow)
        self.assertIn("Publish successful CI evidence bundle", workflow)
        self.assertIn("retention-days: 1", workflow)
        self.assertIn("retention-days: 3", workflow)
        self.assertIn("retention-days: 7", workflow)
        self.assertIn("steps.publish_bundle.outputs.artifact-id", workflow)
        self.assertIn("steps.publish_bundle.outputs.artifact-digest", workflow)
        self.assertIn("ref: ${{ github.event.pull_request.head.sha || github.sha }}", workflow)
        diagnostics_block = workflow.split("Build bounded failed-run diagnostics", 1)[1].split(
            "Publish bounded failed-run diagnostics", 1)[0]
        self.assertIn("SOURCE_SHA: ${{ github.event.pull_request.head.sha || github.sha }}", diagnostics_block)
        final_upload = workflow.split("Publish successful CI evidence bundle", 1)[1].split(
            "Build bounded failed-run diagnostics", 1)[0]
        self.assertIn("include-hidden-files: true", final_upload)
        job_names = re.findall(r"^    name: [^\n]+$", workflow, flags=re.MULTILINE)
        self.assertEqual(8, len(job_names))


if __name__ == "__main__":
    unittest.main()
