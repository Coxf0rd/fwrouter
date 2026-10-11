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

        def fake_docker(argv, *, env, timeout, output_limit=consumer.MAX_DOCKER_OUTPUT_BYTES):
            observed_calls.append((argv, dict(env), timeout, output_limit))
            if "pull" in argv:
                return f"Digest: {digest}\n".encode(), b"", 0.25
            if "inspect" in argv:
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
            receipt = consumer.consume(reference, runner_temp=runner_temp, docker="/usr/bin/docker", env=env)

            self.assertEqual("ready", receipt["status"])
            self.assertEqual(image_ref, receipt["image_ref"])
            self.assertEqual(lock_digest, receipt["lock_sha256"])
            self.assertTrue(receipt["anonymous"])
            self.assertEqual(0.25, receipt["pull_seconds"])
            self.assertEqual(0.1, receipt["inspect_seconds"])
            self.assertEqual(0.2, receipt["manifest_readback_seconds"])
            self.assertEqual(3, len(observed_calls))
            configs = set()
            for argv, child_env, _timeout, _limit in observed_calls:
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
            run_call = next(argv for argv, _env, _timeout, _limit in observed_calls if "run" in argv)
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
