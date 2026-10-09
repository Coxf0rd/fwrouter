"""Source-only, standard-library contracts for the GHCR dependency publisher."""
from __future__ import annotations

import ast
import hashlib
import json
import sys
import unittest
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

    def test_publisher_refuses_existing_lock_tag_before_a_cached_build(self):
        source = (ACCEPTANCE / "publish_dependency_image.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        publish_function = next(node for node in tree.body
                                if isinstance(node, ast.FunctionDef) and node.name == "publish")
        publish_source = ast.get_source_segment(source, publish_function) or ""
        self.assertLess(publish_source.index('"docker", "manifest", "inspect"'),
                        publish_source.index('"docker", "build"'))
        self.assertIn("dependency lock tag already exists in GHCR", publish_source)
        self.assertNotIn("--no-cache", publish_source)

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
        auth_position = workflow.index("Authenticate Docker on the hosted publisher only")
        self.assertLess(contract_position, auth_position)
        self.assertIn("--input-receipt", workflow)
        self.assertIn("docker logout ghcr.io", workflow)
        self.assertIn("Write bounded failure receipt", workflow)
        self.assertIn("step_outcomes", workflow)


if __name__ == "__main__":
    unittest.main(verbosity=2)
