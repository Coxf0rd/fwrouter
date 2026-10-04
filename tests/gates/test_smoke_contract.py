from __future__ import annotations

import importlib.util
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

PATH = Path(__file__).with_name("smoke.py")
SPEC = importlib.util.spec_from_file_location("fwrouter_smoke", PATH)
assert SPEC and SPEC.loader
smoke = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(smoke)


class SmokeContractTests(unittest.TestCase):
    def test_isolated_uses_owned_target_sanitized_env_and_fake_readonly_api(self):
        commands = []
        def runner(argv, **kwargs):
            commands.append((argv, kwargs))
            return SimpleNamespace(returncode=0, stdout="secret must not be retained", stderr="")
        def http(url, timeout):
            self.assertTrue(url.startswith("http://127.0.0.1:5000/api/v2/"))
            fields = ({"service":"fwrouter-api", "status":"healthy", "database_status":"healthy", "schema_ok":True}
                      if url.endswith("health") else {"critical_state_present":True})
            return {"status_code": 200, "api_ok": True, "fields": fields, "body": "secret"}
        result = smoke.run_smoke("isolated", http_get=http, command_runner=runner,
                                 validators=[{"name":"native", "kind":"mihomo", "binary":"/missing", "config":"/tmp/config", "sha256":"0"*64}])
        self.assertEqual("blocked_not_verified", result["status"])
        self.assertTrue(result["isolation"]["target_owned_temp"])
        self.assertFalse(result["isolation"]["production_env_imported"])
        self.assertEqual(1, len(commands))
        argv, kwargs = commands[0]
        target = Path(argv[-1])
        self.assertEqual("--target", argv[-2])
        self.assertTrue(target.name == "target" and target.parent.name.startswith("fwrouter-smoke-"))
        self.assertNotIn("FWROUTER_ENV_FILE", kwargs["env"])
        self.assertNotIn("SECRET", repr(result))
        self.assertNotIn("stdout", repr(result))

    def test_isolated_requires_http_evidence_and_reports_installer_failure(self):
        def fail(argv, **kwargs):
            return SimpleNamespace(returncode=7, stdout="credential", stderr="provider payload")
        result = smoke.run_smoke("isolated", command_runner=fail)
        self.assertEqual("failed", result["status"])
        checks = {c["name"]: c for c in result["checks"]}
        self.assertEqual("failed", checks["installer_deploy_to_owned_temp_target"]["status"])
        self.assertIn(checks["health_api"]["status"], {"passed", "blocked_not_verified"})
        self.assertNotIn("credential", repr(result))
        self.assertNotIn("provider payload", repr(result))

    def test_live_requires_explicit_optin_reason_and_paths(self):
        blocked = smoke.run_smoke("live-readonly")
        self.assertEqual("blocked_not_verified", blocked["status"])
        with self.assertRaises(smoke.SmokeError):
            smoke.run_smoke("live-readonly", opt_in=True, reason="reviewed")
        with self.assertRaises(smoke.SmokeError):
            smoke.run_smoke("live-readonly", opt_in=True, reason="reviewed",
                            db_path="/tmp/db", api_base_url="http://example.org")

    def test_live_loopback_get_smoke_uses_readonly_temp_database(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / "state.db"
            smoke._create_current_schema(db)
            con = sqlite3.connect(db)
            con.execute("CREATE TABLE marker(value TEXT)")
            con.execute("INSERT INTO marker VALUES ('must-not-leak')")
            con.commit(); con.close()
            calls = []
            def http(url, timeout):
                calls.append((url, timeout))
                fields = ({"service":"fwrouter-api", "status":"healthy", "database_status":"healthy", "schema_ok":True}
                          if url.endswith("health") else {"critical_state_present":True})
                return {"status_code": 200, "api_ok": True, "fields": fields}
            result = smoke.run_smoke("live-readonly", opt_in=True, reason="test-only",
                                     db_path=db, api_base_url="http://127.0.0.1:5000",
                                     http_get=http, validators=[])
            self.assertEqual("blocked_not_verified", result["status"])
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            with self.assertRaises(sqlite3.OperationalError):
                con.execute("INSERT INTO marker VALUES ('x')")
            con.close()
            self.assertEqual(2, len(calls))
            self.assertTrue(all(url.startswith("http://127.0.0.1:5000/api/v2/") for url, _ in calls))
            self.assertNotIn("must-not-leak", repr(result))
            self.assertIn("GET", repr(result))

    def test_readonly_database_rejects_noncanonical_schema_version(self):
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / "empty.db"
            con = sqlite3.connect(db)
            con.execute("CREATE TABLE sample(value TEXT)")
            con.execute("CREATE TABLE schema_meta(key TEXT, value TEXT)")
            con.execute("INSERT INTO schema_meta VALUES('schema_version', 'old')")
            con.commit(); con.close()
            self.assertEqual("failed", smoke._readonly_db(db)["status"])
            self.assertFalse(smoke._readonly_db(db)["schema_version_valid"])

    def test_native_validator_hash_failure_never_runs_binary(self):
        with tempfile.TemporaryDirectory() as temp:
            binary = Path(temp) / "native"
            config = Path(temp) / "config.yaml"
            binary.write_bytes(b"not-a-real-binary")
            config.write_text("synthetic", encoding="utf-8")
            invoked = []
            checks = smoke._native_checks([{"name":"fake", "kind":"mihomo", "binary":str(binary),
                                            "config":str(config), "sha256":"0"*64}],
                                         lambda *a, **k: invoked.append(a), {}, Path(temp))
            self.assertEqual("blocked_not_verified", checks[0]["status"])
            self.assertEqual([], invoked)

    def test_isolated_real_fastapi_testclient_routes_use_temp_state_when_dependencies_exist(self):
        def runner(argv, **kwargs):
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        result = smoke.run_smoke("isolated", command_runner=runner)
        checks = {row["name"]: row for row in result["checks"]}
        self.assertEqual("local_fastapi_testclient", result["api_evidence_mode"])
        for name in ("health_api", "critical_state_api"):
            self.assertIn(checks[name]["status"], {"passed", "blocked_not_verified"})
            if checks[name]["status"] == "passed":
                self.assertIn("fastapi_testclient", checks[name]["evidence_mode"])
        # Component L4 explicitly records native work as out of scope.
        self.assertEqual("not_requested", checks["native_validation"]["status"])
        self.assertEqual("blocked_not_verified", result["status"])  # fake installer is not evidence

    def test_live_generated_mounted_parity_mismatch_blocks_native_execution(self):
        with tempfile.TemporaryDirectory() as temp:
            binary = Path(temp) / "binary"
            config = Path(temp) / "generated.yaml"
            mounted = Path(temp) / "mounted.yaml"
            binary.write_bytes(b"pinned")
            config.write_text("generated", encoding="utf-8")
            mounted.write_text("different", encoding="utf-8")
            invoked = []
            checks = smoke._native_checks([{"name":"native", "kind":"mihomo", "binary":str(binary),
                                            "config":str(config), "generated_path":str(config),
                                            "mounted_path":str(mounted), "sha256":smoke.hashlib.sha256(b"pinned").hexdigest()}],
                                         lambda *a, **k: invoked.append(a), {}, Path(temp), require_parity=True)
            parity = next(row for row in checks if row["name"] == "native_mihomo")
            self.assertEqual("failed", parity["status"])
            self.assertEqual("generated_mounted_hash_parity_failed", parity["reason"])
            self.assertEqual([], invoked)

    def test_cli_requires_explicit_json_paths_and_forwards_empty_allowlist(self):
        import contextlib
        import io
        import json
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temp:
            validators = Path(temp) / "validators.json"
            allowlist = Path(temp) / "failed-units.json"
            validators.write_text("[]", encoding="utf-8")
            allowlist.write_text("[]", encoding="utf-8")
            with patch.object(smoke, "run_smoke", return_value={"status":"passed", "checks":[]}) as run:
                stdout = io.StringIO()
                with patch.object(smoke.sys, "argv", ["smoke.py", "--profile", "live-readonly",
                        "--reason", "test-contract", "--opt-in", "--api-base-url", "http://127.0.0.1:5000",
                        "--db-path", "/tmp/state.db", "--validators-json", str(validators),
                        "--failed-units-allowlist-json", str(allowlist), "--complete-native"]), contextlib.redirect_stdout(stdout):
                    self.assertEqual(0, smoke.main())
                self.assertEqual([], run.call_args.kwargs["validators"])
                self.assertEqual([], run.call_args.kwargs["failed_units_allowlist"])
                self.assertTrue(run.call_args.kwargs["require_native"])
                self.assertNotIn("/tmp/state.db", stdout.getvalue())

    def test_native_scope_and_report_fail_closed_statuses(self):
        self.assertEqual("passed", smoke._report("isolated", [{"status":"not_requested"}])["status"])
        self.assertEqual("failed", smoke._report("isolated", [{"status":"output_limit"}])["status"])
        self.assertEqual("failed", smoke._report("isolated", [{"status":"invented"}])["status"])
        full = smoke.run_smoke("isolated", require_native=True,
                               command_runner=lambda *a, **k: SimpleNamespace(returncode=0, stdout="", stderr=""),
                               http_get=lambda url, timeout: {"status_code":200,"api_ok":True,
                                   "fields":{"service":"fwrouter-api","status":"healthy","database_status":"healthy","schema_ok":True}}
                                   if url.endswith("health") else {"status_code":200,"api_ok":True,"fields":{"critical_state_present":True}})
        names = {row["name"]:row for row in full["checks"]}
        self.assertEqual("blocked_not_verified", names["native_mihomo"]["status"])
        self.assertEqual("blocked_not_verified", names["native_xray"]["status"])
        self.assertEqual("blocked_not_verified", full["status"])

    def test_default_process_helper_caps_output_and_times_out_process_group(self):
        with tempfile.TemporaryDirectory() as temp:
            status, code, output = smoke._execute(
                [smoke.sys.executable, "-c", "print('x' * 50000)"], timeout=5,
                env={"PATH":smoke.os.defpath}, cwd=Path(temp), runner=smoke.subprocess.run)
            self.assertEqual("output_limit", status)
            self.assertEqual(125, code)
            self.assertEqual("", output)
            code = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); time.sleep(30)"
            import time
            started = time.monotonic()
            status, _returncode, _output = smoke._execute(
                [smoke.sys.executable, "-c", code], timeout=1,
                env={"PATH":smoke.os.defpath}, cwd=Path(temp), runner=smoke.subprocess.run)
            self.assertEqual("timeout", status)
            self.assertLess(time.monotonic() - started, 8)

    def test_timeout_is_bounded_and_does_not_leak_exception_text(self):
        def timeout(*args, **kwargs):
            raise smoke.subprocess.TimeoutExpired("command-with-secret", 1, output=b"secret")
        result = smoke._safe_run(["/bin/true"], timeout=1, env={}, cwd=Path("/tmp"), runner=timeout)
        self.assertEqual({"status":"timeout", "returncode":None, "evidence_mode":"injected_test_runner"}, result)


if __name__ == "__main__":
    unittest.main()
