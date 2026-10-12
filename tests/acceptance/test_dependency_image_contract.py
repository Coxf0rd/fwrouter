"""Source-only, standard-library contracts for the GHCR dependency publisher."""
from __future__ import annotations

import ast
import hashlib
import json
import sys
import tempfile
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ACCEPTANCE = ROOT / "tests" / "acceptance"
sys.path.insert(0, str(ACCEPTANCE))
import prepare_dependency_image as prepare
import publish_dependency_image as publisher


class DependencyImageContractTests(unittest.TestCase):
    def test_lock_and_docker_recipe_are_pinned_and_keep_apt_signatures_required(self):
        lock = json.loads((ACCEPTANCE / "dependency-image.lock.json").read_text(encoding="utf-8"))
        checked = prepare.validate_lock(lock, root=ROOT)
        self.assertEqual("linux/amd64", checked["platform"])
        self.assertEqual("apt-secure-required", checked["debian_snapshot"]["signature_verification"])
        self.assertFalse(checked["debian_snapshot"]["check_valid_until"])
        self.assertEqual(sorted(checked["apt_packages"]), checked["apt_packages"])
        self.assertEqual(prepare.KERNEL_PACKET_TOOL_VERSIONS,
                         prepare.locked_package_versions(checked["apt_packages"]))
        self.assertEqual(
            hashlib.sha256((ACCEPTANCE / "Dependencies.Dockerfile").read_bytes()).hexdigest(),
            checked["dockerfile_sha256"],
        )
        self.assertNotIn("image_digest", checked)
        self.assertNotIn("registry_image", checked)
        recipe = (ACCEPTANCE / "Dependencies.Dockerfile").read_text(encoding="utf-8")
        self.assertIn("APT::Get::AllowUnauthenticated=false", recipe)
        self.assertIn("Acquire::AllowInsecureRepositories=false", recipe)
        self.assertIn("Acquire::Check-Valid-Until=false", recipe)
        self.assertIn("--require-hashes", recipe)
        self.assertNotIn("/workspace/backend", recipe)

    def test_locked_package_versions_require_unique_exact_inventory(self):
        versions = prepare.KERNEL_PACKET_TOOL_VERSIONS
        inventory = [f"{name}={version}" for name, version in versions.items()]
        self.assertTrue(prepare.installed_packages_match_lock(inventory, inventory))
        self.assertFalse(prepare.installed_packages_match_lock(inventory[:-1], inventory))
        self.assertFalse(prepare.installed_packages_match_lock(inventory, inventory + [inventory[0]]))
        self.assertFalse(prepare.installed_packages_match_lock(inventory, inventory + ["nftables=1.0.5"]))
        lock = json.loads((ACCEPTANCE / "dependency-image.lock.json").read_text(encoding="utf-8"))
        invalid = dict(lock)
        invalid["apt_packages"] = sorted([*lock["apt_packages"], "nftables=1.0.5"])
        with self.assertRaises(prepare.PreparationError):
            prepare.validate_lock(invalid, root=ROOT)

    def test_preparer_exports_only_the_fixed_source_free_input_inventory(self):
        source = (ACCEPTANCE / "prepare_dependency_image.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        prepare_function = next(node for node in tree.body
                                if isinstance(node, ast.FunctionDef) and node.name == "prepare")
        sources_assignment = next(node for node in ast.walk(prepare_function)
                                  if isinstance(node, ast.Assign)
                                  and any(isinstance(target, ast.Name) and target.id == "sources"
                                          for target in node.targets))
        keys = {key.value for key in sources_assignment.value.keys if isinstance(key, ast.Constant)}
        self.assertEqual({
            "native/xray", "native/mihomo", "native/chromium.tar", "requirements-ci.txt",
            "requirements-browser.txt", "dependency-image.lock.json", "Dockerfile",
        }, keys)
        for path in (ACCEPTANCE / "prepare_dependency_image.py", ACCEPTANCE / "publish_dependency_image.py",
                     ACCEPTANCE / "test_dependency_image_contract.py"):
            parsed = ast.parse(path.read_text(encoding="utf-8"))
            imported = {alias.name.split(".")[0] for node in ast.walk(parsed)
                        if isinstance(node, ast.Import) for alias in node.names}
            imported |= {node.module.split(".")[0] for node in ast.walk(parsed)
                         if isinstance(node, ast.ImportFrom) and node.module}
            self.assertTrue(imported.isdisjoint({"backend", "fwrouter_api", "fastapi"}))

    def test_publisher_environment_fails_closed_outside_authorized_hosted_push(self):
        trusted = {
            "GITHUB_ACTIONS": "true", "RUNNER_ENVIRONMENT": "github-hosted",
            "GITHUB_REPOSITORY": "Coxf0rd/fwrouter",
            "GITHUB_REF": "refs/heads/stage/test-architecture-ci-stabilization",
            "GITHUB_EVENT_NAME": "push", "FWROUTER_DEPENDENCY_PUBLISH_MARKER": "ci:publish-dependencies",
            "GITHUB_TOKEN": "synthetic-host-token", "RUNNER_TEMP": "/tmp",
        }
        publisher.validate_publisher_environment(trusted)
        for field, value in (
            ("GITHUB_REPOSITORY", "fork/fwrouter"),
            ("GITHUB_REF", "refs/heads/main"),
            ("GITHUB_EVENT_NAME", "pull_request"),
            ("FWROUTER_DEPENDENCY_PUBLISH_MARKER", ""),
            ("RUNNER_ENVIRONMENT", "self-hosted"),
            ("GITHUB_TOKEN", ""),
        ):
            rejected = dict(trusted)
            rejected[field] = value
            with self.subTest(field=field), self.assertRaises(publisher.PublishError):
                publisher.validate_publisher_environment(rejected)

    def test_measurement_schema_keeps_bytes_and_durations_bounded_and_typed(self):
        measurements = {
            "xray": {"source_archive_bytes": 10, "executable_bytes": 20,
                     "download_seconds": 1.25, "extract_seconds": 0.2},
            "mihomo": {"source_archive_bytes": 11, "executable_bytes": 21,
                       "download_seconds": 1.5, "extract_seconds": 0.3},
            "chromium": {"source_archive_bytes": 100, "normalized_bundle_bytes": 90,
                         "expanded_content_bytes": 200, "chrome_executable_bytes": 50,
                         "download_seconds": 3.0, "normalize_seconds": 2.0},
        }
        self.assertTrue(publisher._valid_asset_measurements(measurements))
        invalid = json.loads(json.dumps(measurements))
        invalid["xray"]["download_seconds"] = float("inf")
        self.assertFalse(publisher._valid_asset_measurements(invalid))
        invalid = json.loads(json.dumps(measurements))
        invalid["chromium"]["expanded_content_bytes"] = True
        self.assertFalse(publisher._valid_asset_measurements(invalid))
        runner = next(node for node in ast.parse(
            (ACCEPTANCE / "publish_dependency_image.py").read_text(encoding="utf-8")).body
                      if isinstance(node, ast.FunctionDef) and node.name == "_run_output")
        runner_source = ast.get_source_segment(
            (ACCEPTANCE / "publish_dependency_image.py").read_text(encoding="utf-8"), runner) or ""
        self.assertIn("MAX_DIAGNOSTIC_BYTES", runner_source)
        self.assertIn("selectors.DefaultSelector", runner_source)
        self.assertNotIn("capture_output=True", runner_source)

    def test_prepared_receipt_uses_unprefixed_sha256_hex_from_preparer(self):
        measurements = {
            "xray": {"source_archive_bytes": 10, "executable_bytes": 20,
                     "download_seconds": 1.25, "extract_seconds": 0.2},
            "mihomo": {"source_archive_bytes": 11, "executable_bytes": 21,
                       "download_seconds": 1.5, "extract_seconds": 0.3},
            "chromium": {"source_archive_bytes": 100, "normalized_bundle_bytes": 90,
                         "expanded_content_bytes": 200, "chrome_executable_bytes": 50,
                         "download_seconds": 3.0, "normalize_seconds": 2.0},
        }
        digest = "a" * 64
        receipt = {
            "schema": "fwrouter-acceptance-dependency-inputs/v1",
            "status": "prepared",
            "lock_sha256": "b" * 64,
            "provision_manifest_sha256": digest,
            "asset_measurements": measurements,
            "image_digest": None,
        }
        self.assertTrue(publisher._prepared_input_receipt_matches_lock(receipt, "b" * 64))
        receipt["provision_manifest_sha256"] = f"sha256:{digest}"
        self.assertFalse(publisher._prepared_input_receipt_matches_lock(receipt, "b" * 64))
        receipt["provision_manifest_sha256"] = digest
        self.assertFalse(publisher._prepared_input_receipt_matches_lock(receipt, "c" * 64))

        with tempfile.TemporaryDirectory(prefix="dependency-member-") as temp:
            path = Path(temp) / "member.bin"
            content = b"owned source-free fixture bytes"
            path.write_bytes(content)
            member_digest, member_size = prepare._file_digest(path)
            member = {"path": "native/member.bin", "sha256": member_digest, "bytes": member_size}
            self.assertTrue(publisher._valid_context_member_metadata(member))
            self.assertEqual(64, len(member_digest))
            self.assertFalse(member_digest.startswith("sha256:"))
            self.assertEqual(len(content), member_size)
            self.assertFalse(publisher._valid_context_member_metadata(
                {**member, "sha256": f"sha256:{member_digest}"}))

    def test_existing_publisher_tag_is_verified_and_reused_without_push(self):
        source = (ACCEPTANCE / "publish_dependency_image.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        publish_function = next(node for node in tree.body
                                if isinstance(node, ast.FunctionDef) and node.name == "publish")
        publish_source = ast.get_source_segment(source, publish_function) or ""
        self.assertLess(publish_source.index('"docker", "manifest", "inspect"'),
                        publish_source.index('"docker", "build"'))
        self.assertIn("_reuse_existing_locked_tag", publish_source)
        self.assertIn("reused_pull_seconds", publish_source)
        self.assertIn('"docker", "push"', publish_source)
        self.assertNotIn("--no-cache", publish_source)
        existing_guard = next(node for node in ast.walk(publish_function)
                              if isinstance(node, ast.If) and isinstance(node.test, ast.Compare)
                              and isinstance(node.test.left, ast.Name) and node.test.left.id == "existing"
                              and isinstance(node.test.ops[0], ast.IsNot)
                              and isinstance(node.test.comparators[0], ast.Constant)
                              and node.test.comparators[0].value is None)
        reused_calls = {call.func.id for call in ast.walk(ast.Module(body=existing_guard.body, type_ignores=[]))
                        if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)}
        fresh_calls = {call.func.id for call in ast.walk(ast.Module(body=existing_guard.orelse, type_ignores=[]))
                       if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)}
        self.assertIn("_reuse_existing_locked_tag", reused_calls)
        self.assertNotIn("_run_output", reused_calls)
        self.assertIn("_run_output", fresh_calls)

        reuse = next(node for node in tree.body
                     if isinstance(node, ast.FunctionDef) and node.name == "_reuse_existing_locked_tag")
        reuse_source = ast.get_source_segment(source, reuse) or ""
        self.assertIn('["docker", "pull", "--platform", lock["platform"], image_tag]', reuse_source)
        self.assertIn("validate_dependency_image_metadata", reuse_source)
        self.assertIn("image_ref=immutable_ref", reuse_source)
        self.assertIn("_check_local_image", reuse_source)
        self.assertIn("_check_local_image(immutable_ref", reuse_source)
        self.assertIn("_read_image_manifest", reuse_source)
        self.assertNotIn('"docker", "push"', reuse_source)

        workflow = (ROOT / ".github/workflows/publish-acceptance-dependencies.yml").read_text(encoding="utf-8")
        self.assertIn("group: fwrouter-acceptance-dependency-publisher", workflow)
        self.assertIn("cancel-in-progress: false", workflow)

        readback = next(node for node in tree.body
                        if isinstance(node, ast.FunctionDef) and node.name == "_read_image_manifest")
        readback_source = ast.get_source_segment(source, readback) or ""
        for limit in ("--network=none", "--read-only", "--memory=256m", "--memory-swap=256m",
                      "--cpus=0.5", "--pids-limit=32", "--cap-drop=ALL"):
            self.assertIn(limit, readback_source)

        anonymous_pull = next(node for node in tree.body
                              if isinstance(node, ast.FunctionDef) and node.name == "_anonymous_pull")
        anonymous_source = ast.get_source_segment(source, anonymous_pull) or ""
        self.assertIn('"pull"', anonymous_source)
        self.assertIn('"--config"', anonymous_source)
        self.assertIn('"DOCKER_CONFIG"', anonymous_source)
        self.assertIn("shutil.rmtree(config", anonymous_source)

    def test_existing_tag_pull_digest_parser_and_verification_are_strict(self):
        digest = "sha256:" + "b" * 64
        self.assertEqual(digest, publisher._digest_from_pull_output(f"Status: Image is up to date\nDigest: {digest}\n"))
        for output in ("Status: Image is up to date\n", f"Digest: {digest}\nDigest: {digest}\n",
                       "Digest: sha256:NOT_A_DIGEST\n"):
            with self.subTest(output=output), self.assertRaises(publisher.PublishError):
                publisher._digest_from_pull_output(output)

        lock = json.loads((ACCEPTANCE / "dependency-image.lock.json").read_text(encoding="utf-8"))
        lock_digest = publisher._canonical_digest(lock)
        image_tag = f"{publisher.REGISTRY_IMAGE}:deps-{lock_digest}"
        immutable_ref = f"{publisher.REGISTRY_IMAGE}@{digest}"
        image = {
            "Id": "sha256:" + "c" * 64,
            "Os": "linux",
            "Architecture": "amd64",
            "RepoDigests": [immutable_ref],
            "Config": {"Labels": {
                publisher.OWNER_LABEL: lock_digest,
                publisher.BASE_LABEL: lock["base_image"],
            }},
        }
        manifest = {"schema": "fwrouter-acceptance-dependency-image/v1"}
        with (mock.patch.object(publisher, "_run_output", return_value=f"Digest: {digest}\n") as run_output,
              mock.patch.object(publisher, "_run_json", return_value=[image]) as run_json,
              mock.patch.object(publisher, "_check_local_image", return_value=image) as check_image,
              mock.patch.object(publisher, "_read_image_manifest", return_value=manifest) as read_manifest,
              mock.patch.object(publisher.time, "monotonic", side_effect=(10.0, 12.5))):
            observed_ref, observed_image, observed_manifest, observed_pull_seconds = publisher._reuse_existing_locked_tag(
                image_tag, lock=lock, lock_digest=lock_digest)
        self.assertEqual(immutable_ref, observed_ref)
        self.assertIs(image, observed_image)
        self.assertIs(manifest, observed_manifest)
        self.assertEqual(2.5, observed_pull_seconds)
        run_output.assert_called_once_with(["docker", "pull", "--platform", lock["platform"], image_tag],
                                           timeout=600)
        run_json.assert_called_once_with(["docker", "image", "inspect", image_tag])
        check_image.assert_called_once_with(immutable_ref, lock=lock, lock_digest=lock_digest)
        read_manifest.assert_called_once_with(immutable_ref, lock=lock, lock_digest=lock_digest)

        with (mock.patch.object(publisher, "_run_output", return_value=f"Digest: {digest}\n"),
              mock.patch.object(publisher, "_run_json", return_value=[{**image, "RepoDigests": []}]),
              mock.patch.object(publisher, "_check_local_image") as check_image,
              mock.patch.object(publisher, "_read_image_manifest") as read_manifest,
              self.assertRaisesRegex(publisher.PublishError, "immutable repository digest")):
            publisher._reuse_existing_locked_tag(image_tag, lock=lock, lock_digest=lock_digest)
        check_image.assert_not_called()
        read_manifest.assert_not_called()

    def test_workflow_runs_contracts_before_host_registry_auth_and_never_on_pr(self):
        workflow = (ROOT / ".github/workflows/publish-acceptance-dependencies.yml").read_text(encoding="utf-8")
        self.assertIn("branches: [stage/test-architecture-ci-stabilization]", workflow)
        self.assertIn("github.repository == 'Coxf0rd/fwrouter'", workflow)
        self.assertIn("github.ref == 'refs/heads/stage/test-architecture-ci-stabilization'", workflow)
        self.assertIn("contains(github.event.head_commit.message, 'ci:publish-dependencies')", workflow)
        self.assertIn("packages: write", workflow)
        self.assertNotIn("pull_request", workflow)
        self.assertIn("persist-credentials: false", workflow)
        contract_position = workflow.index("test_dependency_image_contract.py")
        consumer_contract_position = workflow.index("test_consume_dependency_image_contract.py")
        auth_position = workflow.index("Authenticate Docker on the hosted publisher only")
        self.assertLess(contract_position, auth_position)
        self.assertLess(consumer_contract_position, auth_position)
        self.assertIn("--input-receipt", workflow)
        self.assertIn("docker logout ghcr.io", workflow)
        self.assertIn("Write bounded failure receipt", workflow)
        self.assertIn("step_outcomes", workflow)


if __name__ == "__main__":
    unittest.main(verbosity=2)
