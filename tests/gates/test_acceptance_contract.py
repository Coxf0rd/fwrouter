from __future__ import annotations

import importlib.util
import ast
import hashlib
import json
import tarfile
import tempfile
import unittest
import zipfile
import stat
import sys
from unittest import mock
import io
from pathlib import Path
import xml.etree.ElementTree as ET
from types import SimpleNamespace


LAUNCHER_PATH = Path(__file__).parents[1] / "acceptance" / "launcher.py"
SPEC = importlib.util.spec_from_file_location("fwrouter_acceptance_launcher", LAUNCHER_PATH)
assert SPEC and SPEC.loader
launcher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(launcher)
PROFILE_PATH = Path(__file__).parents[1] / "application_acceptance" / "profile.py"
PROFILE_SPEC = importlib.util.spec_from_file_location("fwrouter_acceptance_profile_contract", PROFILE_PATH)
assert PROFILE_SPEC and PROFILE_SPEC.loader
acceptance_profile = importlib.util.module_from_spec(PROFILE_SPEC)
PROFILE_SPEC.loader.exec_module(acceptance_profile)
PROVISION_PATH = Path(__file__).parents[1] / "acceptance" / "provision.py"
PROVISION_SPEC = importlib.util.spec_from_file_location("fwrouter_acceptance_provision_contract", PROVISION_PATH)
assert PROVISION_SPEC and PROVISION_SPEC.loader
provision = importlib.util.module_from_spec(PROVISION_SPEC)
PROVISION_SPEC.loader.exec_module(provision)
AGGREGATE_PATH = Path(__file__).with_name("aggregate_hosted.py")
AGGREGATE_SPEC = importlib.util.spec_from_file_location("fwrouter_hosted_aggregate_contract", AGGREGATE_PATH)
assert AGGREGATE_SPEC and AGGREGATE_SPEC.loader
aggregate_hosted = importlib.util.module_from_spec(AGGREGATE_SPEC)
AGGREGATE_SPEC.loader.exec_module(aggregate_hosted)
NATIVE_RUNNER_PATH = Path(__file__).parents[1] / "application_acceptance" / "native_runner.py"
NATIVE_SPEC = importlib.util.spec_from_file_location("fwrouter_native_runner_contract", NATIVE_RUNNER_PATH)
assert NATIVE_SPEC and NATIVE_SPEC.loader
native_runner = importlib.util.module_from_spec(NATIVE_SPEC)
NATIVE_SPEC.loader.exec_module(native_runner)


class AcceptanceContractTests(unittest.TestCase):
    def test_native_diagnostic_redaction_and_output_bounds(self):
        raw = (b'{"api_key":"do-not-publish","privateKey":"also-private",'
               b'"pre_shared_key":"psk-value"} Bearer abc.def.ghi\n'
               b'-----BEGIN PRIVATE KEY-----\nmaterial\n-----END PRIVATE KEY-----')
        cleaned = native_runner._redact_diagnostic(raw, limit=1024)
        for secret in ("do-not-publish", "also-private", "psk-value", "abc.def.ghi", "material"):
            self.assertNotIn(secret, cleaned)
        capped = native_runner._redact_diagnostic(b"x" * 10000, limit=127)
        self.assertLessEqual(len(capped.encode()), 127)

        with tempfile.TemporaryDirectory(prefix="fwrouter-bounded-stderr-") as temp:
            child = native_runner._bounded_child(
                [sys.executable, "-c", "import sys; sys.stderr.write('z'*50000); sys.exit(23)"],
                cwd=Path(temp), env={"PATH": "/usr/bin:/bin"}, timeout=5)
            self.assertEqual(23, child.returncode)
            self.assertLessEqual(len(child.stderr), native_runner.DIAGNOSTIC_STREAM_LIMIT)

    def test_native_rpc_output_preserves_identity_while_artifact_redacts_it(self):
        uuid = "123e4567-e89b-42d3-a456-426614174000"
        email = "native-user@example.org"
        stdout = json.dumps({"users": [{"id": uuid, "email": email}]}).encode()
        stderr = b"e" * 5000
        proc = native_runner.subprocess.CompletedProcess([], 0, stdout, stderr)
        native = native_runner.NativeXrayProcess.__new__(native_runner.NativeXrayProcess)
        native._guard = mock.MagicMock()
        native._command_history = []

        with mock.patch.object(native_runner, "_bounded_child", return_value=proc):
            completed = native._native_cli("xray", Path("/opt/xray"), ["-api"], timeout=1, env={})

        rpc_result = native_runner.NativeXrayProcess._completed(completed)
        self.assertEqual(json.loads(stdout.decode()), json.loads(rpc_result["details"]["stdout"]))
        self.assertIn(uuid, rpc_result["details"]["stdout"])
        self.assertIn(email, rpc_result["details"]["stdout"])
        self.assertLessEqual(len(rpc_result["details"]["stdout"].encode()),
                             native_runner.DIAGNOSTIC_STREAM_LIMIT)
        self.assertEqual("e" * 4096, rpc_result["details"]["stderr"])
        self.assertLessEqual(len(rpc_result["details"]["stderr"].encode()), 4096)

        artifact_command = native._command_history[0]
        self.assertNotIn(uuid, artifact_command["stdout"])
        self.assertNotIn(email, artifact_command["stdout"])
        self.assertIn("[UUID]", artifact_command["stdout"])
        self.assertIn("[EMAIL]", artifact_command["stdout"])

    def test_native_xray_candidate_uses_private_json_copy_without_mutating_source(self):
        with tempfile.TemporaryDirectory(prefix="fwrouter-native-xray-candidate-") as temp:
            root = Path(temp)
            candidate = root / "state" / "xray" / "config.json.candidate"
            candidate.parent.mkdir(parents=True)
            for payload, expected_exit in ((b'{"inbounds":[]}', 0),
                                           (b"{ controlled invalid candidate", 23)):
                candidate.write_bytes(payload)
                native = native_runner.NativeXrayProcess.__new__(native_runner.NativeXrayProcess)
                native.root = root
                observed = {}

                def run_cli(args, *, timeout):
                    observed["args"] = list(args)
                    observed["timeout"] = timeout
                    observed["copied_bytes"] = Path(args[-1]).read_bytes()
                    observed["copy_mode"] = stat.S_IMODE(Path(args[-1]).stat().st_mode)
                    observed["copy_exists_during_validation"] = Path(args[-1]).exists()
                    return native_runner.subprocess.CompletedProcess(args, expected_exit, b"", b"")

                with mock.patch.object(native, "_xray_cli", side_effect=run_cli):
                    result = native._test_xray_candidate(candidate)
                self.assertEqual(expected_exit == 0, result["ok"])
                self.assertEqual(None if expected_exit == 0 else f"XRAY_NATIVE_EXIT_{expected_exit}",
                                 result["error_code"])
                self.assertEqual(payload, candidate.read_bytes())
                self.assertEqual(payload, observed["copied_bytes"])
                self.assertTrue(observed["args"][-1].endswith(".json"))
                self.assertEqual(0o600, observed["copy_mode"])
                self.assertTrue(observed["copy_exists_during_validation"])
                self.assertFalse(Path(observed["args"][-1]).exists())
            candidate.write_bytes(b"x" * (native_runner.MAX_REQUEST * 64 + 1))
            native = native_runner.NativeXrayProcess.__new__(native_runner.NativeXrayProcess)
            native.root = root
            with mock.patch.object(native, "_xray_cli") as run_cli:
                with self.assertRaisesRegex(ValueError, "outside acceptance state"):
                    native._test_xray_candidate(candidate)
                run_cli.assert_not_called()

    def test_hosted_receipt_aggregation_rejects_source_plan_and_skipped_node_mismatch(self):
        expected = aggregate_hosted.functional_nodeids()
        source = "a" * 40
        plan_digest = "b" * 64
        nonce = "c" * 32
        profile = {
            "schema": "fwrouter-acceptance-profile/v2", "source_revision": source,
            "plan_digest": plan_digest,
            "xray": {"version": provision.INPUTS["xray"]["version"],
                     "sha256": provision.BINARY_SHA256["xray"]},
            "mihomo": {"version": provision.INPUTS["mihomo"]["version"],
                       "sha256": provision.BINARY_SHA256["mihomo"]},
            "chromium": {"version": provision.CHROMIUM_VERSION,
                         "bundle_sha256": provision.CHROMIUM_BUNDLE_SHA256,
                         "sha256": provision.CHROMIUM_BINARY_SHA256},
            "playwright_python": provision.PLAYWRIGHT_VERSION,
        }
        profile_sha = hashlib.sha256((json.dumps(profile, sort_keys=True, indent=2) + "\n").encode()).hexdigest()
        nodeids = sorted(expected)
        rows = [{"nodeid": nodeid, "status": "passed",
                 "phases": {phase: "passed" for phase in ("setup", "call", "teardown")}}
                for nodeid in nodeids]
        receipt = {
            "schema_version": 1, "scope": "hosted-native-process", "status": "passed",
            "suite": "functional", "source_revision": source, "plan_digest": plan_digest,
            "container_confinement": "passed", "runtime_preflight": "passed",
            "cleanup": "owned_resources_removed", "temporary_artifacts_removed": True,
            "suite_nonce": nonce, "profile_sha256": profile_sha,
            "tests": {"nodeids": nodeids, "tests": len(expected), "failures": 0, "errors": 0, "skipped": 0},
            "profile": profile,
            "application_receipt": {
                "schema": "fwrouter-application-acceptance-receipt/v2", "scope": "hosted-native-process",
                "source_revision": source, "plan_digest": plan_digest, "suite_nonce": nonce,
                "profile_sha256": profile_sha, "status": "passed", "exit_status": 0,
                "cleanup_errors": [], "tests": rows,
            },
        }
        plan = {"source_commit": source, "plan_digest": plan_digest}
        runtime_inputs = {
            "schema": "fwrouter-hosted-inputs/v1", "base_image": provision.BASE_IMAGE,
            "playwright": provision.PLAYWRIGHT_VERSION,
            "native": {
                "xray": {"version": provision.INPUTS["xray"]["version"],
                         "source_archive_sha256": provision.INPUTS["xray"]["sha256"],
                         "binary_sha256": provision.BINARY_SHA256["xray"]},
                "mihomo": {"version": provision.INPUTS["mihomo"]["version"],
                           "source_archive_sha256": provision.INPUTS["mihomo"]["sha256"],
                           "binary_sha256": provision.BINARY_SHA256["mihomo"]},
            },
            "chromium": {"version": provision.CHROMIUM_VERSION,
                         "source_archive_sha256": provision.INPUTS["chromium"]["sha256"],
                         "bundle_sha256": provision.CHROMIUM_BUNDLE_SHA256,
                         "executable_sha256": provision.CHROMIUM_BINARY_SHA256},
        }
        with tempfile.TemporaryDirectory(prefix="fwrouter-aggregate-contract-") as temp:
            path = Path(temp) / "receipt.json"
            path.write_text(json.dumps(receipt), encoding="utf-8")
            self.assertEqual([], aggregate_hosted.validate_functional_receipt(path, plan, expected, runtime_inputs))
            repeat = json.loads(json.dumps(receipt))
            repeat["suite_nonce"] = "f" * 32
            repeat["application_receipt"]["suite_nonce"] = repeat["suite_nonce"]
            path.write_text(json.dumps(repeat), encoding="utf-8")
            self.assertEqual([], aggregate_hosted.validate_functional_receipt(path, plan, expected, runtime_inputs))
            for field, value in (("source_revision", "e" * 40), ("plan_digest", "f" * 64)):
                changed = dict(receipt)
                changed[field] = value
                path.write_text(json.dumps(changed), encoding="utf-8")
                self.assertTrue(aggregate_hosted.validate_functional_receipt(path, plan, expected, runtime_inputs))
            changed = json.loads(json.dumps(receipt))
            changed["application_receipt"]["tests"][0]["phases"]["teardown"] = "skipped"
            path.write_text(json.dumps(changed), encoding="utf-8")
            self.assertTrue(aggregate_hosted.validate_functional_receipt(path, plan, expected, runtime_inputs))
            changed = json.loads(json.dumps(receipt))
            changed["profile"]["xray"]["version"] = "latest"
            path.write_text(json.dumps(changed), encoding="utf-8")
            self.assertTrue(aggregate_hosted.validate_functional_receipt(path, plan, expected, runtime_inputs))

    def test_provision_inputs_are_exact_versioned_https_assets(self):
        self.assertRegex(provision.BASE_IMAGE, r"^python:3\.11-bookworm@sha256:[0-9a-f]{64}$")
        self.assertEqual("1.55.0", provision.PLAYWRIGHT_VERSION)
        self.assertEqual("1187", provision.CHROMIUM_REVISION)
        for name, item in provision.INPUTS.items():
            with self.subTest(asset=name):
                self.assertRegex(item["url"], r"^https://")
                self.assertRegex(item["sha256"], r"^[0-9a-f]{64}$")
                self.assertNotIn("latest", item["url"].lower())
                self.assertTrue(item["version"])

    def test_provision_download_rejects_digest_mismatch_and_oversize(self):
        payload = b"pinned public fixture bytes"

        class Response(io.BytesIO):
            def geturl(self):
                return "https://fixture.invalid/pinned-asset"

        original = provision.INPUTS["xray"]
        old_limit = provision.MAX_DOWNLOAD["xray"]
        try:
            provision.INPUTS["xray"] = {"url": "https://fixture.invalid/pinned-asset",
                                         "sha256": hashlib.sha256(payload).hexdigest()}
            provision.MAX_DOWNLOAD["xray"] = len(payload)
            with tempfile.TemporaryDirectory(prefix="fwrouter-provision-digest-") as temp:
                target = Path(temp) / "asset"
                with mock.patch.object(provision.urllib.request, "urlopen", return_value=Response(payload)):
                    provision.download("xray", target)
                self.assertEqual(payload, target.read_bytes())
                provision.INPUTS["xray"]["sha256"] = "0" * 64
                with mock.patch.object(provision.urllib.request, "urlopen", return_value=Response(payload)):
                    with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
                        provision.download("xray", Path(temp) / "wrong-digest")
                provision.INPUTS["xray"]["sha256"] = hashlib.sha256(payload).hexdigest()
                provision.MAX_DOWNLOAD["xray"] = len(payload) - 1
                with mock.patch.object(provision.urllib.request, "urlopen", return_value=Response(payload)):
                    with self.assertRaisesRegex(ValueError, "byte limit"):
                        provision.download("xray", Path(temp) / "oversized")
                self.assertFalse((Path(temp) / "wrong-digest.partial").exists())
                self.assertFalse((Path(temp) / "oversized.partial").exists())
        finally:
            provision.INPUTS["xray"] = original
            provision.MAX_DOWNLOAD["xray"] = old_limit

    def test_provision_chromium_bundle_normalizes_and_hashes_complete_archive(self):
        with tempfile.TemporaryDirectory(prefix="fwrouter-provision-chromium-") as temp:
            source = Path(temp) / "chromium.zip"
            target = Path(temp) / "chromium.tar"
            with zipfile.ZipFile(source, "w") as archive:
                directory = zipfile.ZipInfo("chrome-linux/")
                directory.external_attr = (stat.S_IFDIR | 0o755) << 16
                archive.writestr(directory, b"")
                browser = zipfile.ZipInfo("chrome-linux/chrome")
                browser.external_attr = (stat.S_IFREG | 0o755) << 16
                archive.writestr(browser, b"browser executable fixture")
                resource = zipfile.ZipInfo("chrome-linux/resources.pak")
                resource.external_attr = (stat.S_IFREG | 0o644) << 16
                archive.writestr(resource, b"browser resource fixture")
            observed = provision.make_chromium_bundle(source, target)
            self.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(), observed)
            with tarfile.open(target, "r:") as bundle:
                self.assertEqual({"chrome-linux64", "chrome-linux64/chrome", "chrome-linux64/resources.pak"},
                                 set(bundle.getnames()))
                self.assertTrue(bundle.getmember("chrome-linux64/chrome").mode & 0o111)

    def test_source_catalog_expands_literal_ids_without_importing_tests(self):
        path = LAUNCHER_PATH.with_name("source_catalog.py")
        spec = importlib.util.spec_from_file_location("fwrouter_static_acceptance_catalog", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory(prefix="fwrouter-static-nodes-") as temp:
            root = Path(temp)
            tests = root / "tests/application_acceptance"
            tests.mkdir(parents=True)
            sample = tests / "test_example.py"
            sample.write_text("raise RuntimeError('must never import this test')\n"
                              "@pytest.mark.parametrize('stage', ['a','b'], ids=['apply','persist'])\n"
                              "def test_contract(stage): pass\n"
                              "@pytest.mark.l7\n"
                              "def test_crash(): pass\n")
            rows = module.collect_source_nodes(root)
            self.assertEqual(["test_contract[apply]", "test_contract[persist]", "test_crash"],
                             [row["nodeid"].split("::")[1] for row in rows])
            self.assertEqual(["functional", "functional", "recovery"], [row["suite"] for row in rows])
            sample.write_text("@pytest.mark.parametrize('stage', ['a','b'])\ndef test_contract(stage): pass\n")
            with self.assertRaises(module.CatalogError):
                module.collect_source_nodes(root)
            for unsupported in ("class TestHidden:\n def test_hidden(self): pass\n",
                                "pytestmark = pytest.mark.l7\ndef test_hidden(): pass\n"):
                sample.write_text(unsupported)
                with self.assertRaises(module.CatalogError):
                    module.collect_source_nodes(root)

    def test_source_catalog_rejects_stale_or_incomplete_registry(self):
        path = LAUNCHER_PATH.with_name("source_catalog.py")
        spec = importlib.util.spec_from_file_location("fwrouter_static_catalog_stale", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory(prefix="fwrouter-static-stale-") as temp:
            root = Path(temp)
            tests = root / "tests/application_acceptance"
            tests.mkdir(parents=True)
            (tests / "test_sample.py").write_text("def test_required(): pass\n")
            registry = root / "tests/acceptance/scenarios.json"
            registry.parent.mkdir()
            registry.write_text(json.dumps({"schema": "fwrouter-acceptance-scenarios/v1", "scenarios": []}))
            with self.assertRaisesRegex(module.CatalogError, "differs"):
                module.read_catalog(root)
            registry.write_text(json.dumps({"schema": "fwrouter-acceptance-scenarios/v1", "scenarios": module.collect_source_nodes(root)}))
            self.assertEqual(1, len(module.read_catalog(root)))
            (tests / "test_sample.py").write_text("def test_required(): pass\ndef test_new_required(): pass\n")
            with self.assertRaises(module.CatalogError):
                module.read_catalog(root)

    def test_mihomo_fence_observer_installs_after_normal_runtime_import(self):
        worker_path = Path(__file__).parents[1] / "application_acceptance" / "worker.py"
        module = ast.parse(worker_path.read_text(encoding="utf-8"))
        build_app = next(node for node in module.body
                         if isinstance(node, ast.FunctionDef) and node.name == "build_app")
        calls = [node for node in ast.walk(build_app)
                 if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)]
        bind_line = min(node.lineno for node in calls if node.func.id == "_bind_acceptance_mihomo")
        observer_line = min(node.lineno for node in calls
                            if node.func.id == "_install_acceptance_mihomo_fence_observer")
        self.assertLess(bind_line, observer_line)

    def test_launcher_import_is_stdlib_only_and_default_cli_is_nonexecuting(self):
        self.assertNotIn("fwrouter_api", launcher.sys.modules)
        self.assertNotIn("fastapi", launcher.sys.modules)
        self.assertIn("--run", launcher.main.__code__.co_consts)

    def test_generated_preflight_compiles_and_stopped_compose_lookup_includes_all(self):
        compile(launcher.runtime_preflight_code(), "acceptance-runtime-preflight.py", "exec")
        source = LAUNCHER_PATH.read_text(encoding="utf-8")
        self.assertIn('"ps", "--all", "-q", "application"', source)

    def test_unconfirmed_cleanup_downgrades_pass_and_partial_results(self):
        for status in ("passed", "partial"):
            receipt = {"status": status}
            launcher.mark_cleanup_unconfirmed(receipt, "synthetic")
            self.assertEqual("failed", receipt["status"])
            self.assertEqual("cleanup_unconfirmed", receipt["cleanup"])
        receipt = {"status": "NOTRUN"}
        launcher.mark_cleanup_unconfirmed(receipt, "synthetic")
        self.assertEqual("NOTRUN", receipt["status"])

    def test_junit_requires_exact_suite_node_ids_without_skips_or_duplicates(self):
        for suite in ("functional", "recovery"):
            with self.subTest(suite=suite), tempfile.TemporaryDirectory(prefix="fwrouter-acceptance-junit-") as temp:
                expected = launcher.expected_acceptance_nodeids(suite)
                root = ET.Element("testsuite")
                for nodeid in sorted(expected):
                    path, _, name = nodeid.partition("::")
                    case = ET.SubElement(root, "testcase", {
                        "classname": path[:-3].replace("/", "."), "name": name,
                    })
                junit = Path(temp) / "junit.xml"
                ET.ElementTree(root).write(junit, encoding="utf-8", xml_declaration=True)
                observed = launcher.validate_junit(junit, suite)
                self.assertEqual(sorted(expected), observed["nodeids"])
                rows = [{"nodeid": nodeid, "status": "passed",
                         "phases": {phase: "passed" for phase in ("setup", "call", "teardown")}}
                        for nodeid in sorted(expected)]
                launcher.validate_suite_node_receipt(rows, suite, observed["nodeids"])
                with self.assertRaises(launcher.NotRun):
                    launcher.validate_suite_node_receipt(rows[:-1], suite, observed["nodeids"])
                rows[0]["phases"].pop("teardown")
                with self.assertRaises(launcher.NotRun):
                    launcher.validate_suite_node_receipt(rows, suite, observed["nodeids"])
                duplicate = ET.SubElement(root, "testcase", {
                    "classname": next(iter(sorted(expected))).split("::", 1)[0][:-3].replace("/", "."),
                    "name": next(iter(sorted(expected))).split("::", 1)[1],
                })
                ET.ElementTree(root).write(junit, encoding="utf-8", xml_declaration=True)
                with self.assertRaises(launcher.NotRun):
                    launcher.validate_junit(junit, suite)

    def test_junit_skipped_node_is_not_a_pass(self):
        with tempfile.TemporaryDirectory(prefix="fwrouter-acceptance-skip-") as temp:
            nodeid = sorted(launcher.expected_acceptance_nodeids("recovery"))[0]
            path, _, name = nodeid.partition("::")
            root = ET.Element("testsuite")
            case = ET.SubElement(root, "testcase", {"classname": path[:-3].replace("/", "."), "name": name})
            ET.SubElement(case, "skipped")
            junit = Path(temp) / "junit.xml"
            ET.ElementTree(root).write(junit, encoding="utf-8", xml_declaration=True)
            with self.assertRaises(launcher.NotRun):
                launcher.validate_junit(junit, "recovery")

    def test_source_allowlist_rejects_secrets_state_logs_archives_and_traversal(self):
        for path in (
            "backend/tests/.env.production", "backend/tests/secrets/token.json",
            "backend/tests/state.db", "backend/tests/output.log", "backend/tests/dump.tar.gz",
            "tests/acceptance/../../backend/.env",
        ):
            with self.subTest(path=path):
                self.assertFalse(launcher._safe_source(path))
        self.assertTrue(launcher._safe_source("tests/acceptance/profile_schema.json"))
        self.assertTrue(launcher._safe_source("tests/application_acceptance/fixtures/xray.initial.json"))

    def test_environment_labels_do_not_qualify_root_or_minisk(self):
        env = {"GITHUB_ACTIONS": "true", "RUNNER_ENVIRONMENT": "github-hosted",
               "GITHUB_WORKSPACE": "/tmp/work", "RUNNER_TEMP": "/tmp/temp"}
        root_reasons = launcher.qualify_host(env, {"uid": 0, "hostname": "runner", "hosted_image_marker": True}, markers=())
        self.assertTrue(any("root" in reason for reason in root_reasons))
        minisk_reasons = launcher.qualify_host(env, {"uid": 1000, "hostname": "minisk",
            "hosted_image_marker": True}, markers=())
        self.assertTrue(any("minisk" in reason for reason in minisk_reasons))
        self.assertTrue(any("outside" in reason for reason in minisk_reasons))

    def test_application_profile_qualification_predicate_is_fail_closed_without_side_effects(self):
        facts = {"uid": 10001, "gid": 10001, "hostname": "acceptance-container",
                 "docker_marker_regular": True, "production_api_absent": True,
                 "readonly_root": True, "readonly_profile_mount": True, "fixed_workspace": True}
        self.assertIsNone(acceptance_profile._qualification_error(facts))
        for key in ("docker_marker_regular", "production_api_absent", "readonly_root",
                    "readonly_profile_mount", "fixed_workspace"):
            with self.subTest(key=key):
                rejected = dict(facts, **{key: False})
                self.assertIsNotNone(acceptance_profile._qualification_error(rejected))
        self.assertIsNotNone(acceptance_profile._qualification_error(dict(facts, hostname="minisk")))
        self.assertIsNotNone(acceptance_profile._qualification_error(dict(facts, uid=0)))
        self.assertIsNotNone(acceptance_profile._qualification_error(dict(facts, gid=0)))

    def test_export_hashes_copied_source_and_writes_provenance_sidecar(self):
        with tempfile.TemporaryDirectory(prefix="fwrouter-acceptance-export-") as temp:
            root = Path(temp) / "root"
            dest = Path(temp) / "context"
            (root / "ui").mkdir(parents=True)
            (root / "tests/acceptance").mkdir(parents=True)
            (root / "ui/index.html").write_bytes(b"synthetic ui")
            (root / "tests/acceptance/Dockerfile").write_text("FROM example\n", encoding="utf-8")
            (root / "tests/acceptance/compose.yaml").write_text("services: {}\n", encoding="utf-8")
            xray = Path(temp) / "xray"
            mihomo = Path(temp) / "mihomo"
            bundle = Path(temp) / "chromium.tar"
            xray.write_bytes(b"fake xray executable")
            mihomo.write_bytes(b"fake mihomo executable")
            with tarfile.open(bundle, "w") as archive:
                info = tarfile.TarInfo("chrome-linux64/chrome")
                info.mode = 0o755
                info.size = 10
                import io
                archive.addfile(info, io.BytesIO(b"fakechrome"))
            result = launcher.export_build_context(
                root, dest,
                tracked=["tests/acceptance/Dockerfile", "tests/acceptance/compose.yaml", "ui/index.html"],
                binaries={"xray": xray, "mihomo": mihomo}, browser_bundle=bundle,
                source_revision="a" * 40,
            )
            sidecar = json.loads((dest / ".fwrouter-acceptance-revision").read_text())
            self.assertEqual(result["source_manifest_sha256"], sidecar["source_manifest_sha256"])
            self.assertEqual(result["ui_tree_sha256"], sidecar["ui_tree_sha256"])
            self.assertEqual("a" * 40, sidecar["source_revision"])
            self.assertEqual(0o444, (dest / ".fwrouter-acceptance-revision").stat().st_mode & 0o777)
            self.assertEqual("fakechrome", (dest / "native/chromium/chrome-linux64/chrome").read_text())
            digest = hashlib.sha256()
            for relative in sorted(("tests/acceptance/Dockerfile", "tests/acceptance/compose.yaml", "ui/index.html")):
                digest.update(relative.encode("utf-8") + b"\0")
                digest.update((dest / relative).read_bytes())
            self.assertEqual(digest.hexdigest(), result["source_manifest_sha256"])

    def test_dockerfile_copies_the_full_acceptance_tree_used_by_preflight_digest(self):
        dockerfile = (LAUNCHER_PATH.parent / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("COPY tests/acceptance/ /workspace/tests/acceptance/", dockerfile)
        self.assertIn("COPY tests/application_acceptance/ /workspace/tests/application_acceptance/", dockerfile)
        self.assertIn("COPY tests/gates/requirements-ci.txt /workspace/tests/gates/requirements-ci.txt", dockerfile)

    def test_browser_archive_rejects_traversal_and_links(self):
        with tempfile.TemporaryDirectory(prefix="fwrouter-acceptance-tar-") as temp:
            root = Path(temp)
            archive_path = root / "bad.tar"
            with tarfile.open(archive_path, "w") as archive:
                info = tarfile.TarInfo("../escape")
                info.size = 1
                import io
                archive.addfile(info, io.BytesIO(b"x"))
            with self.assertRaises(launcher.NotRun):
                launcher._extract_browser_bundle(archive_path, root / "extracted")
            self.assertFalse((root / "escape").exists())

    def test_runtime_inspection_rejects_unexpected_mounts_env_and_network_attachments(self):
        profile = Path("/tmp/profile.json")
        base = {
            "Id": "a" * 64,
            "Config": {"User": "10001:10001", "Labels": {
                launcher.OWNER_LABEL: launcher.OWNER_VALUE,
                launcher.RUN_LABEL: "run", "com.docker.compose.project": "project",
            }, "Env": ["PATH=/usr/bin", "FWROUTER_ENVIRONMENT=test"],
                "Entrypoint": ["python"], "Cmd": ["-c", "import signal; signal.pause()"]},
            "Image": "sha256:" + "b" * 64,
            "HostConfig": {
                "ReadonlyRootfs": True, "Privileged": False, "NetworkMode": "project_isolated",
                "CapAdd": [], "Devices": [], "PidMode": "", "IpcMode": "private",
                "CapDrop": ["ALL"], "SecurityOpt": ["no-new-privileges:true"],
                "PortBindings": {}, "PidsLimit": 256, "Memory": 2 * 1024**3,
                "MemorySwap": 2 * 1024**3,
                "NanoCpus": 2_000_000_000,
                "Tmpfs": {"/tmp": "rw,noexec,nosuid,nodev,size=512m,mode=1777"},
            },
            "State": {"Status": "created"},
            "Mounts": [
                {"Type": "bind", "Destination": "/run/fwrouter-acceptance/profile.json",
                 "Source": str(profile), "RW": False},
                {"Type": "tmpfs", "Destination": "/tmp", "RW": True,
                 "Mode": "rw,noexec,nosuid,nodev"},
            ],
            "NetworkSettings": {"Networks": {"project_isolated": {}}},
        }
        with tempfile.TemporaryDirectory(prefix="fwrouter-acceptance-inspect-") as temp:
            profile = Path(temp) / "profile.json"
            profile.write_text("{}")
            base["Mounts"][0]["Source"] = str(profile)
            launcher.validate_container_inspect(base, project="project", run_id="run", profile_path=profile,
                                                image_id=base["Image"])
            extra_mount = json.loads(json.dumps(base))
            extra_mount["Mounts"].append({"Type": "volume", "Destination": "/data"})
            with self.assertRaises(launcher.NotRun):
                launcher.validate_container_inspect(extra_mount, project="project", run_id="run", profile_path=profile,
                                                    image_id=base["Image"])
            credential_env = json.loads(json.dumps(base))
            credential_env["Config"]["Env"].append("AWS_SECRET_ACCESS_KEY=leak")
            with self.assertRaises(launcher.NotRun):
                launcher.validate_container_inspect(credential_env, project="project", run_id="run", profile_path=profile,
                                                    image_id=base["Image"])
        network = {"Name": "project_isolated", "Labels": {
            launcher.OWNER_LABEL: launcher.OWNER_VALUE, launcher.RUN_LABEL: "run",
        }, "Internal": True, "EnableIPv6": False, "Options": {}, "Containers": {"other-id": {}}}
        with self.assertRaises(launcher.NotRun):
            launcher.validate_network_inspect(network, project="project", run_id="run", container_id="owned-id")
        empty_network = json.loads(json.dumps(network))
        empty_network["Containers"] = {}
        launcher.validate_network_inspect(empty_network, project="project", run_id="run", container_id="owned-id")
        with self.assertRaises(launcher.NotRun):
            launcher.validate_network_inspect(empty_network, project="project", run_id="run",
                                              container_id="owned-id", require_container_attached=True)

    def test_compose_config_rejects_host_integration_and_extra_environment(self):
        run_id = "run-id"
        with tempfile.TemporaryDirectory(prefix="fwrouter-acceptance-compose-") as temp:
            profile = Path(temp) / "profile.json"
            profile.write_text("{}")
            service = {
                "cap_drop": ["ALL"], "read_only": True, "user": "10001:10001", "restart": "no",
                "init": True, "security_opt": ["no-new-privileges:true"], "memswap_limit": "2g",
                "tmpfs": ["/tmp:rw,noexec,nosuid,nodev,size=512m,mode=1777"],
                "command": ["-c", "import signal; signal.pause()"],
                "labels": {launcher.OWNER_LABEL: launcher.OWNER_VALUE, launcher.RUN_LABEL: run_id},
                "networks": ["isolated"], "volumes": [{"source": str(profile),
                    "target": "/run/fwrouter-acceptance/profile.json", "read_only": True}],
                "mem_limit": "2g", "pids_limit": 256, "cpus": 2.0,
                "environment": {
                    "FWROUTER_ENVIRONMENT": "test", "FWROUTER_STATE_DIR": "/tmp/fwrouter-acceptance-state",
                    "FWROUTER_STARTUP_TASKS_ENABLED": "0", "FWROUTER_ACCEPTANCE_PROFILE": "/run/fwrouter-acceptance/profile.json",
                    "FWROUTER_XRAY_BINARY": "/opt/fwrouter-test/bin/xray",
                    "FWROUTER_MIHOMO_BINARY": "/opt/fwrouter-test/bin/mihomo",
                    "FWROUTER_BROWSER_EXECUTABLE": "/opt/fwrouter-test/chromium/chrome-linux64/chrome",
                    "FWROUTER_CHROMIUM_BINARY": "/opt/fwrouter-test/chromium/chrome-linux64/chrome",
                    "FWROUTER_APPLICATION_ACCEPTANCE_ROOT": "/tmp/fwrouter-application-acceptance",
                    "FWROUTER_ACCEPTANCE_RECEIPT_PATH": "/tmp/fwrouter-receipts/application-acceptance.json",
                    "PLAYWRIGHT_BROWSERS_PATH": "/opt/fwrouter-test/playwright-browsers",
                    "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTHONDONTWRITEBYTECODE": "1",
                    "HOME": "/tmp/fwrouter-home",
                },
            }
            config = {"services": {"application": service}, "networks": {"isolated": {
                "internal": True, "labels": {launcher.OWNER_LABEL: launcher.OWNER_VALUE,
                                               launcher.RUN_LABEL: run_id}}}}
            launcher.validate_compose_config(config, run_id=run_id, profile_path=profile)
            polluted = json.loads(json.dumps(config))
            polluted["services"]["application"]["environment"]["HTTP_PROXY"] = "http://example.invalid"
            with self.assertRaises(launcher.NotRun):
                launcher.validate_compose_config(polluted, run_id=run_id, profile_path=profile)
            host_port = json.loads(json.dumps(config))
            host_port["services"]["application"]["ports"] = ["10085:10085"]
            with self.assertRaises(launcher.NotRun):
                launcher.validate_compose_config(host_port, run_id=run_id, profile_path=profile)

    def test_profile_schema_has_no_unbound_authorization_or_port_fields(self):
        schema = json.loads((LAUNCHER_PATH.parent / "profile_schema.json").read_text())
        required = set(schema["required"])
        self.assertEqual({"schema", "profile", "source_revision", "plan_digest", "xray", "mihomo", "chromium",
                          "playwright_python", "baseline_xray_config_sha256", "ui_tree_sha256", "suite_nonce"}, required)
        self.assertNotIn("loopback_ports", schema["properties"])
        self.assertNotIn("authorization", schema["properties"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
