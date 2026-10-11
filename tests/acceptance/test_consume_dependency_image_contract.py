"""Source-only, standard-library contracts for anonymous GHCR consumption."""
from __future__ import annotations

import ast
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
ACCEPTANCE = ROOT / "tests" / "acceptance"
sys.path.insert(0, str(ACCEPTANCE))
import consume_dependency_image as consumer
import prepare_dependency_image as prepare
import publish_dependency_image as publisher


class ConsumeDependencyImageContractTests(unittest.TestCase):
    def _lock(self):
        value = json.loads((ACCEPTANCE / "dependency-image.lock.json").read_text(encoding="utf-8"))
        return prepare.validate_lock(value, root=ROOT)

    def _reference(self, lock):
        return {
            "schema": consumer.REFERENCE_SCHEMA,
            "image_ref": f"{publisher.REGISTRY_IMAGE}@sha256:{'a' * 64}",
            "lock_sha256": prepare._canonical_digest(lock),
            "platform": lock["platform"],
        }

    def test_consumer_reference_is_exact_digest_and_current_lock_bound(self):
        lock = self._lock()
        reference = self._reference(lock)
        self.assertEqual(reference["image_ref"], consumer.validate_consumer_reference(reference, lock=lock))
        invalid = (
            dict(reference, image_ref=f"{publisher.REGISTRY_IMAGE}:deps-current"),
            dict(reference, image_ref="ghcr.io/other/image@sha256:" + "a" * 64),
            dict(reference, lock_sha256="0" * 64),
            dict(reference, platform="linux/arm64"),
            dict(reference, extra="not-locked"),
        )
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(consumer.ConsumerError):
                consumer.validate_consumer_reference(value, lock=lock)

    def test_hosted_admission_rejects_docker_overrides_before_any_child_command(self):
        env = {"GITHUB_ACTIONS": "true", "RUNNER_ENVIRONMENT": "github-hosted", "PATH": "/usr/bin"}
        consumer.validate_consumer_environment(env, system="Linux", machine="x86_64")
        for override in ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_AUTH_CONFIG", "DOCKER_CONFIG",
                         "DOCKER_CERT_PATH", "DOCKER_TLS", "DOCKER_TLS_VERIFY", "CONTAINER_HOST",
                         "BUILDKIT_HOST"):
            for value in ("/unreviewed", ""):
                with self.subTest(override=override, value=value), self.assertRaises(consumer.ConsumerError):
                    consumer.validate_consumer_environment({**env, override: value},
                                                           system="Linux", machine="x86_64")
        for system, machine, runner in (("Linux", "x86_64", "self-hosted"),
                                        ("Darwin", "x86_64", "github-hosted"),
                                        ("Linux", "aarch64", "github-hosted")):
            with self.subTest(system=system, machine=machine), self.assertRaises(consumer.ConsumerError):
                consumer.validate_consumer_environment(
                    {**env, "RUNNER_ENVIRONMENT": runner}, system=system, machine=machine)

    def test_consume_rejects_remote_docker_endpoint_before_starting_any_command(self):
        lock = self._lock()
        reference = self._reference(lock)
        env = {"GITHUB_ACTIONS": "true", "RUNNER_ENVIRONMENT": "github-hosted", "PATH": "/usr/bin",
               "DOCKER_HOST": "tcp://unowned.example:2375"}
        with (tempfile.TemporaryDirectory(prefix="fwrouter-consumer-preflight-") as temp,
              mock.patch.object(consumer.platform, "system", return_value="Linux"),
              mock.patch.object(consumer.platform, "machine", return_value="x86_64"),
              mock.patch.object(consumer, "_run_bounded") as run):
            with self.assertRaisesRegex(consumer.ConsumerError, "Docker override"):
                consumer.consume(reference, runner_temp=Path(temp), docker="/usr/bin/docker", env=env)
        run.assert_not_called()

    def test_runner_temp_rejects_symlinked_ancestors(self):
        with tempfile.TemporaryDirectory(prefix="fwrouter-consumer-path-") as temp:
            root = Path(temp)
            real = root / "real"
            real.mkdir()
            runner_temp = real / "runner-temp"
            runner_temp.mkdir(mode=0o700)
            alias = root / "alias"
            alias.symlink_to(real, target_is_directory=True)
            with self.assertRaisesRegex(consumer.ConsumerError, "must not contain symlinks"):
                consumer._validated_runner_temp(alias / "runner-temp")

    def test_consumption_is_anonymous_digest_pinned_and_native_readback_is_offline(self):
        lock = self._lock()
        reference = self._reference(lock)
        image_ref = reference["image_ref"]
        lock_digest = reference["lock_sha256"]
        digest = image_ref.split("@", 1)[1]
        image = {
            "Id": "sha256:" + "b" * 64,
            "Size": 2_500_000_000,
            "Os": "linux",
            "Architecture": "amd64",
            "RepoDigests": [image_ref],
            "Config": {
                "Labels": {
                    publisher.OWNER_LABEL: lock_digest,
                    publisher.BASE_LABEL: lock["base_image"],
                },
                "User": "10001:10001",
                "WorkingDir": "/opt/fwrouter-test",
                "Entrypoint": None,
                "Cmd": ["python3"],
                "Env": ["PYTHONDONTWRITEBYTECODE=1"],
            },
        }
        manifest = {
            "schema": "fwrouter-acceptance-dependency-image/v1",
            "lock_sha256": lock_digest,
            "base_image": lock["base_image"],
            "platform": lock["platform"],
            "debian_snapshot": lock["debian_snapshot"],
            "installed_packages": ["base-files=1", "python3=3"],
            "requirements": lock["requirements"],
            "assets": lock["assets"],
            "playwright_version": lock["playwright_version"],
        }
        env = {
            "GITHUB_ACTIONS": "true", "RUNNER_ENVIRONMENT": "github-hosted", "PATH": "/usr/bin",
            "HOME": "/runner/home", "GITHUB_TOKEN": "must-not-enter-docker-child",
            "GHCR_TOKEN": "must-not-enter-docker-child", "FWROUTER_TEST_SECRET": "must-not-enter-child",
        }
        observed_calls = []

        def fake_docker(argv, *, env, timeout, output_limit=consumer.MAX_DOCKER_OUTPUT_BYTES,
                        expected_returncode=0):
            observed_calls.append((argv, dict(env), timeout, output_limit, expected_returncode))
            if "pull" in argv:
                return f"Digest: {digest}\n".encode(), b"", 0.25
            if "inspect" in argv:
                if not any("pull" in prior for prior, *_rest in observed_calls[:-1]):
                    return b"", b"", 0.02
                return json.dumps([image]).encode(), b"", 0.1
            if "run" in argv:
                return json.dumps(manifest).encode(), b"", 0.2
            self.fail("consumer issued an unreviewed Docker command")

        with (tempfile.TemporaryDirectory(prefix="fwrouter-consumer-contract-") as temp,
              mock.patch.object(consumer.platform, "system", return_value="Linux"),
              mock.patch.object(consumer.platform, "machine", return_value="x86_64"),
              mock.patch.object(consumer, "_run_bounded", side_effect=fake_docker)):
            runner_temp = Path(temp)
            runner_temp.chmod(0o700)
            receipt = consumer.consume(reference, runner_temp=runner_temp, docker="/usr/bin/docker",
                                       env=env, expect_cold=True)

            self.assertEqual("ready", receipt["status"])
            self.assertEqual(image_ref, receipt["image_ref"])
            self.assertEqual(lock_digest, receipt["lock_sha256"])
            self.assertTrue(receipt["anonymous"])
            self.assertTrue(receipt["cold_cache_absent"])
            self.assertEqual(2_500_000_000, receipt["verified_image_bytes"])
            self.assertEqual(0.25, receipt["pull_seconds"])
            self.assertEqual(0.1, receipt["inspect_seconds"])
            self.assertEqual(0.2, receipt["manifest_readback_seconds"])
            self.assertEqual(4, len(observed_calls))
            self.assertEqual(1, observed_calls[0][4])
            configs = set()
            for argv, child_env, _timeout, _limit, _expected_returncode in observed_calls:
                self.assertEqual(consumer.MAX_DOCKER_OUTPUT_BYTES, _limit)
                self.assertEqual("/usr/bin/docker", argv[0])
                self.assertEqual("--config", argv[1])
                configs.add(argv[2])
                self.assertNotIn("GITHUB_TOKEN", child_env)
                self.assertNotIn("GHCR_TOKEN", child_env)
                self.assertNotIn("FWROUTER_TEST_SECRET", child_env)
                self.assertEqual(argv[2], child_env["DOCKER_CONFIG"])
            self.assertEqual(1, len(configs))
            config = Path(configs.pop())
            self.assertTrue(config.parent == runner_temp)
            self.assertFalse(config.exists())
            run_call = next(argv for argv, _env, _timeout, _limit, _expected in observed_calls if "run" in argv)
            for required in ("--pull=never", "--network=none", "--read-only", "10001:10001",
                             "--memory=256m", "--memory-swap=256m", "--cpus=0.5",
                             "--pids-limit=32", "--cap-drop=ALL", "--security-opt=no-new-privileges"):
                self.assertIn(required, run_call)
            self.assertNotIn("--network=host", run_call)
            self.assertNotIn("--privileged", run_call)
            self.assertNotIn("must-not-enter", json.dumps(receipt))

    def test_consumer_reference_loader_fails_closed_until_published_digest_is_checked_in(self):
        with tempfile.TemporaryDirectory(prefix=".consumer-ref-contract-", dir=ACCEPTANCE) as temp:
            missing = Path(temp) / "reference.json"
            with self.assertRaises(consumer.ConsumerError):
                consumer.load_consumer_reference(missing)

    def test_consumer_receipt_is_bound_to_owned_runner_temp_and_rejects_symlinks(self):
        with tempfile.TemporaryDirectory(prefix="fwrouter-consumer-receipt-") as temp:
            root = Path(temp)
            root.chmod(0o700)
            receipt = root / "fwrouter-dependency-consumer-cold.json"
            self.assertEqual(receipt, consumer._validated_receipt_path(receipt, root))
            for target in (root.parent / "outside.json", root / "receipt.txt"):
                with self.subTest(target=target), self.assertRaises(consumer.ConsumerError):
                    consumer._validated_receipt_path(target, root)
            link = root / "fwrouter-dependency-consumer-warm.json"
            link.symlink_to(receipt)
            with self.assertRaises(consumer.ConsumerError):
                consumer._validated_receipt_path(link, root)

    def test_consumer_rejects_invalid_image_size_before_manifest_and_cleans_docker_config(self):
        lock = self._lock()
        reference = self._reference(lock)
        image_ref = reference["image_ref"]
        digest = image_ref.split("@", 1)[1]
        lock_digest = prepare._canonical_digest(lock)
        base_image = {
            "Id": "sha256:" + "b" * 64,
            "Os": "linux",
            "Architecture": "amd64",
            "RepoDigests": [image_ref],
            "Config": {"Labels": {
                publisher.OWNER_LABEL: lock_digest,
                publisher.BASE_LABEL: lock["base_image"],
            }},
        }
        env = {"GITHUB_ACTIONS": "true", "RUNNER_ENVIRONMENT": "github-hosted", "PATH": "/usr/bin"}
        invalid_sizes = (None, 0, consumer.MAX_IMAGE_BYTES + 1)
        for image_size in invalid_sizes:
            with self.subTest(image_size=image_size), tempfile.TemporaryDirectory(
                    prefix="fwrouter-consumer-size-") as temp:
                runner_temp = Path(temp)
                runner_temp.chmod(0o700)
                image = dict(base_image)
                if image_size is not None:
                    image["Size"] = image_size
                calls = []

                def fake_docker(argv, *, env, timeout, output_limit=consumer.MAX_DOCKER_OUTPUT_BYTES,
                                expected_returncode=0):
                    calls.append((argv, dict(env)))
                    if "pull" in argv:
                        return f"Digest: {digest}\n".encode(), b"", 0.1
                    if "inspect" in argv:
                        return json.dumps([image]).encode(), b"", 0.1
                    raise AssertionError("invalid image size reached manifest execution")

                with (mock.patch.object(consumer.platform, "system", return_value="Linux"),
                      mock.patch.object(consumer.platform, "machine", return_value="x86_64"),
                      mock.patch.object(consumer, "_run_bounded", side_effect=fake_docker)):
                    with self.assertRaisesRegex(consumer.ConsumerError, "image size"):
                        consumer.consume(reference, runner_temp=runner_temp, docker="/usr/bin/docker", env=env)
                self.assertEqual(2, len(calls))
                config = Path(calls[0][0][2])
                self.assertFalse(config.exists())

    def test_cold_consumer_refuses_a_cached_image_before_pull_and_cleans_config(self):
        lock = self._lock()
        reference = self._reference(lock)
        env = {"GITHUB_ACTIONS": "true", "RUNNER_ENVIRONMENT": "github-hosted", "PATH": "/usr/bin"}
        with tempfile.TemporaryDirectory(prefix="fwrouter-consumer-cold-cache-") as temp:
            runner_temp = Path(temp)
            runner_temp.chmod(0o700)
            calls = []

            def cached_image(argv, *, env, timeout, output_limit=consumer.MAX_DOCKER_OUTPUT_BYTES,
                             expected_returncode=0):
                calls.append(argv)
                self.assertIn("image", argv)
                self.assertIn("inspect", argv)
                self.assertEqual(1, expected_returncode)
                raise consumer.ConsumerError("cold cache check found a cached image")

            with (mock.patch.object(consumer.platform, "system", return_value="Linux"),
                  mock.patch.object(consumer.platform, "machine", return_value="x86_64"),
                  mock.patch.object(consumer, "_run_bounded", side_effect=cached_image)):
                with self.assertRaisesRegex(consumer.ConsumerError, "cached image"):
                    consumer.consume(reference, runner_temp=runner_temp, docker="/usr/bin/docker",
                                     env=env, expect_cold=True)
            self.assertEqual(1, len(calls))
            self.assertNotIn("pull", calls[0])
            self.assertFalse(Path(calls[0][2]).exists())

    def test_dependency_workflow_has_separate_anonymous_cold_and_warm_consumer(self):
        workflow = (ROOT / ".github/workflows/publish-acceptance-dependencies.yml").read_text(
            encoding="utf-8")
        job = workflow.split("  publish-dependencies:", 1)[0]
        self.assertIn("ci:validate-dependencies", job)
        self.assertIn("runs-on: ubuntu-24.04", job)
        self.assertIn("permissions:\n      contents: read", job)
        self.assertNotIn("packages: write", job)
        self.assertNotIn("docker login", job)
        self.assertIn("--expect-cold", job)
        self.assertIn("test_dependency_image_contract.py", job)
        self.assertIn("test_consume_dependency_image_contract.py", job)
        self.assertIn("fwrouter-dependency-consumer-cold.json", job)
        self.assertIn("fwrouter-dependency-consumer-warm.json", job)
        self.assertIn("if: always()", job)
        self.assertIn("retention-days: 14", job)
        self.assertIn("dependency-image-consumer.lock.json", (ACCEPTANCE / "consume_dependency_image.py")
                      .read_text(encoding="utf-8"))

    def test_consumer_uses_only_standard_library_and_the_existing_lock_qualifiers(self):
        source = (ACCEPTANCE / "consume_dependency_image.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported = {alias.name.split(".")[0] for node in ast.walk(tree)
                    if isinstance(node, ast.Import) for alias in node.names}
        imported |= {node.module.split(".")[0] for node in ast.walk(tree)
                     if isinstance(node, ast.ImportFrom) and node.module}
        self.assertTrue(imported.isdisjoint({"backend", "fwrouter_api", "fastapi"}))
        self.assertIn("validate_dependency_image_metadata", source)
        self.assertIn("validate_dependency_manifest", source)
        self.assertIn("dependency-image-consumer.lock.json", source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
