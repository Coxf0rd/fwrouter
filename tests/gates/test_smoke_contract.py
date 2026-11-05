from __future__ import annotations

import ast
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

PATH = Path(__file__).with_name("smoke.py")
SPEC = importlib.util.spec_from_file_location("fwrouter_smoke", PATH)
assert SPEC and SPEC.loader
smoke = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(smoke)


class _LocalApiServer(ThreadingHTTPServer):
    daemon_threads = True


class _LocalApiHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        spec = self.server.responses.get(self.path)
        if spec is None:
            spec = {"status": 404, "body": b"not found"}
        delay = spec.get("delay_before_headers", 0)
        if delay:
            time.sleep(delay)
        self.send_response(spec.get("status", 200))
        headers = list(spec.get("headers", []))
        if spec.get("content_length", "auto") == "auto" and not any(
                key.lower() == "content-length" for key, _value in headers):
            headers.append(("Content-Length", str(len(spec.get("body", b"")))))
        for key, value in headers:
            self.send_header(key, value)
        self.end_headers()
        body = spec.get("body", b"")
        delay = spec.get("trickle_delay", 0)
        try:
            if delay:
                for byte in body:
                    self.wfile.write(bytes((byte,)))
                    self.wfile.flush()
                    time.sleep(delay)
            else:
                self.wfile.write(body)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, _format, *args):
        return


@contextmanager
def _serve_api(responses):
    server = _LocalApiServer(("127.0.0.1", 0), _LocalApiHandler)
    server.responses = responses
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _body_of_exact_size(payload: dict, size: int, *, padding_path: tuple[str, ...]) -> bytes:
    value = payload
    for key in padding_path[:-1]:
        value = value[key]
    value[padding_path[-1]] = ""
    initial = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    value[padding_path[-1]] = "x" * (size - len(initial))
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    if len(body) != size:
        raise AssertionError(f"could not construct exact body size: {len(body)} != {size}")
    return body


class SmokeContractTests(unittest.TestCase):
    def test_guarded_fastapi_child_source_parses_without_app_import(self):
        source = ast.parse(PATH.read_text(encoding="utf-8"), filename=str(PATH))
        function = next(node for node in source.body if isinstance(node, ast.FunctionDef)
                        and node.name == "_isolated_fastapi_checks")
        child = next(node.value for node in function.body if isinstance(node, ast.Assign)
                     and any(isinstance(target, ast.Name) and target.id == "child"
                             for target in node.targets))
        self.assertIsInstance(child, ast.Constant)
        ast.parse(child.value, filename="guarded-fastapi-smoke-child.py")

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

    def test_default_live_get_ignores_ambient_proxy_and_rejects_redirects(self):
        api_paths = []
        proxy_paths = []

        class ApiHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                api_paths.append(self.path)
                if self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", "/health")
                    self.end_headers()
                    return
                payload = {
                    "ok": True,
                    "data": {"service": "fwrouter-api", "status": "healthy",
                             "database": {"status": "healthy", "schema": {"ok": True}}},
                }
                body = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format, *args):
                return

        class ProxyHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                proxy_paths.append(self.path)
                self.send_error(502)

            def log_message(self, _format, *args):
                return

        api = ThreadingHTTPServer(("127.0.0.1", 0), ApiHandler)
        proxy = ThreadingHTTPServer(("127.0.0.1", 0), ProxyHandler)
        threads = [threading.Thread(target=server.serve_forever, daemon=True)
                   for server in (api, proxy)]
        for thread in threads:
            thread.start()
        try:
            proxy_url = f"http://127.0.0.1:{proxy.server_port}"
            ambient = {key: proxy_url for key in
                       ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy",
                        "ALL_PROXY", "all_proxy")}
            with patch.dict(smoke.os.environ, ambient, clear=True):
                base = f"http://127.0.0.1:{api.server_port}"
                result = smoke._default_http_get(base + "/health", 2, "health")
                redirected = smoke._default_http_get(base + "/redirect", 2, "health")
            self.assertEqual([], proxy_paths)
            self.assertEqual(200, result["status_code"])
            self.assertTrue(result["api_ok"])
            self.assertEqual(["/health", "/redirect"], api_paths)
            self.assertEqual(302, redirected["status_code"])
            self.assertFalse(redirected["api_ok"])
            self.assertEqual("redirect_rejected", redirected["reason"])
        finally:
            for server in (api, proxy):
                server.shutdown()
                server.server_close()
            for thread in threads:
                thread.join(timeout=2)

    def test_default_http_get_preserves_health_cap_and_bounds_state_at_observed_scale(self):
        health = {"ok": True, "data": {"service": "fwrouter-api", "status": "healthy",
                  "database": {"status": "healthy", "schema": {"ok": True}}, "padding": ""}}
        state = {"ok": True, "data": {"state": {"padding": ""}}}
        production_size = 1_363_679
        responses = {}
        bodies = {
            "/health-exact": _body_of_exact_size(health, smoke.MAX_HTTP_BODY, padding_path=("data", "padding")),
            "/health-over": _body_of_exact_size(health, smoke.MAX_HTTP_BODY + 1, padding_path=("data", "padding")),
            "/state-production": _body_of_exact_size(state, production_size, padding_path=("data", "state", "padding")),
            "/state-exact": _body_of_exact_size(state, smoke.MAX_STATE_HTTP_BODY, padding_path=("data", "state", "padding")),
            "/state-over": _body_of_exact_size(state, smoke.MAX_STATE_HTTP_BODY + 1, padding_path=("data", "state", "padding")),
        }
        for path, body in bodies.items():
            responses[path] = {"status": 200, "headers": [("Content-Type", "application/json")], "body": body}
        with _serve_api(responses) as base:
            health_exact = smoke._default_http_get(base + "/health-exact", 10, "health")
            health_over = smoke._default_http_get(base + "/health-over", 10, "health")
            state_production = smoke._default_http_get(base + "/state-production", 10, "state")
            state_exact = smoke._default_http_get(base + "/state-exact", 10, "state")
            state_over = smoke._default_http_get(base + "/state-over", 10, "state")
        self.assertTrue(smoke._api_payload_check(health_exact, "health"))
        self.assertEqual("body_too_large", health_over["reason"])
        self.assertTrue(smoke._api_payload_check(state_production, "state"))
        self.assertTrue(smoke._api_payload_check(state_exact, "state"))
        self.assertEqual("body_too_large", state_over["reason"])
        self.assertNotIn("padding", repr(state_production))
        self.assertNotIn("padding", repr(state_exact))

    def test_default_http_get_rejects_content_length_and_json_failures_safely(self):
        valid_health = json.dumps({"ok": True, "data": {"service": "fwrouter-api", "status": "healthy",
                              "database": {"status": "healthy", "schema": {"ok": True}}}}).encode()
        valid_state = json.dumps({"ok": True, "data": {"state": {}}}).encode()
        responses = {
            "/missing-length": {"status": 200, "headers": [("Content-Type", "application/json")],
                                 "content_length": None, "body": valid_state},
            "/bad-length": {"status": 200, "headers": [("Content-Length", "not-a-number")],
                             "content_length": None, "body": valid_state},
            "/duplicate-length": {"status": 200, "headers": [("Content-Length", str(len(valid_state))),
                                      ("Content-Length", str(len(valid_state)))],
                                   "content_length": None, "body": valid_state},
            "/declared-too-large": {"status": 200, "headers": [("Content-Length", str(smoke.MAX_STATE_HTTP_BODY + 1))],
                                     "content_length": None, "body": b""},
            "/truncated": {"status": 200, "headers": [("Content-Length", str(len(valid_state) + 8))],
                            "content_length": None, "body": valid_state},
            "/lying-short": {"status": 200, "headers": [("Content-Length", str(len(valid_state) - 1))],
                              "content_length": None, "body": valid_state},
            "/malformed-json": {"status": 200, "headers": [("Content-Type", "application/json")],
                                "body": b'{"ok":true,"data":'},
            "/nonfinite-json": {"status": 200, "headers": [("Content-Type", "application/json")],
                                 "body": b'{"ok":NaN,"data":{"state":{}}}'},
            "/bad-shape": {"status": 200, "headers": [("Content-Type", "application/json")],
                            "body": b'{"ok":true,"data":[]}'},
            "/bad-status": {"status": 503, "headers": [("Content-Type", "application/json")],
                             "body": valid_health},
        }
        with _serve_api(responses) as base:
            results = {path: smoke._default_http_get(base + path, 5,
                       "health" if path == "/bad-status" else "state") for path in responses}
        self.assertTrue(smoke._api_payload_check(results["/missing-length"], "state"))
        self.assertEqual("invalid_content_length", results["/bad-length"]["reason"])
        self.assertEqual("invalid_content_length", results["/duplicate-length"]["reason"])
        self.assertEqual("body_too_large", results["/declared-too-large"]["reason"])
        self.assertEqual("content_length_mismatch", results["/truncated"]["reason"])
        self.assertEqual("invalid_json", results["/lying-short"]["reason"])
        self.assertEqual("invalid_json", results["/malformed-json"]["reason"])
        self.assertEqual("invalid_json", results["/nonfinite-json"]["reason"])
        self.assertEqual("invalid_json_shape", results["/bad-shape"]["reason"])
        self.assertEqual("unexpected_http_status", results["/bad-status"]["reason"])
        self.assertNotIn("not-a-number", repr(results))
        self.assertNotIn("NaN", repr(results))
        self.assertNotIn('"data"', json.dumps(results["/malformed-json"]))

    def test_default_http_get_enforces_total_deadline_for_delayed_headers_and_trickled_body(self):
        valid = json.dumps({"ok": True, "data": {"state": {"status": "healthy"}}}).encode()
        responses = {
            "/slow-headers": {"status": 200, "headers": [("Content-Type", "application/json")],
                              "body": valid, "delay_before_headers": 2},
            "/trickle-body": {"status": 200, "headers": [("Content-Type", "application/json"),
                                 ("Content-Length", "64")], "content_length": None,
                               "body": b"{}", "trickle_delay": 0.75},
        }
        with _serve_api(responses) as base:
            for path in responses:
                started = time.monotonic()
                result = smoke._default_http_get(base + path, 1, "state")
                elapsed = time.monotonic() - started
                self.assertEqual("timeout", result["reason"])
                self.assertLess(elapsed, 7)

    def test_http_check_propagates_only_safe_actionable_reasons(self):
        timeout = smoke._http_check(lambda _url, _timeout: {
            "status_code": None, "api_ok": False, "fields": {}, "reason": "timeout"},
            "http://127.0.0.1/health", 1, "health")
        untrusted = smoke._http_check(lambda _url, _timeout: {
            "status_code": 200, "api_ok": False, "fields": {}, "reason": "secret-body-value"},
            "http://127.0.0.1/health", 1, "health")
        self.assertEqual({"status": "failed", "status_code": None,
                          "contract": "health_schema_and_database", "reason": "timeout"}, timeout)
        self.assertEqual("api_contract_mismatch", untrusted["reason"])
        self.assertNotIn("secret-body-value", repr(untrusted))

    def test_cli_json_reports_live_smoke_pass_and_actionable_failure(self):
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as temp:
            db = Path(temp) / "state.db"
            smoke._create_current_schema(db)
            allowlist = Path(temp) / "failed-units.json"
            allowlist.write_text("[]", encoding="utf-8")
            health = json.dumps({"ok": True, "data": {"service": "fwrouter-api", "status": "healthy",
                           "database": {"status": "healthy", "schema": {"ok": True}}}}).encode()
            small_state = json.dumps({"ok": True, "data": {"state": {}}}).encode()
            oversized_state = _body_of_exact_size({"ok": True, "data": {"state": {"padding": ""}}},
                                                   smoke.MAX_STATE_HTTP_BODY + 1,
                                                   padding_path=("data", "state", "padding"))
            responses = []
            for state_body in (small_state, oversized_state):
                with _serve_api({
                    "/api/v2/health": {"status": 200, "headers": [("Content-Type", "application/json")], "body": health},
                    "/api/v2/state/system": {"status": 200, "headers": [("Content-Type", "application/json")], "body": state_body},
                }) as base:
                    stdout = io.StringIO()
                    args = ["smoke.py", "--profile", "live-readonly", "--opt-in", "--reason", "test-only",
                            "--api-base-url", base, "--db-path", str(db),
                            "--failed-units-allowlist-json", str(allowlist)]
                    execute = smoke._execute
                    def controlled_command(argv, **kwargs):
                        if argv and argv[0] == "systemctl":
                            return "passed", 0, ""
                        return execute(argv, **kwargs)
                    with patch.object(smoke.sys, "argv", args), \
                            patch.object(smoke, "_native_checks", return_value=[
                                {"name": "native_mihomo", "status": "passed"},
                                {"name": "native_xray", "status": "passed"},
                            ]), \
                            patch.object(smoke, "_execute", side_effect=controlled_command), \
                            contextlib.redirect_stdout(stdout):
                        code = smoke.main()
                    responses.append((code, json.loads(stdout.getvalue())))
        pass_code, passed = responses[0]
        fail_code, failed = responses[1]
        self.assertEqual(0, pass_code, json.dumps(passed, sort_keys=True))
        self.assertEqual("passed", passed["status"])
        self.assertEqual(1, fail_code)
        self.assertEqual("failed", failed["status"])
        critical = next(row for row in failed["checks"] if row["name"] == "critical_state_api")
        self.assertEqual("body_too_large", critical["reason"])
        self.assertNotIn("padding", repr(failed))

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

    def test_isolated_fastapi_smoke_requires_a_real_guarded_child_runner(self):
        def runner(argv, **kwargs):
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        result = smoke.run_smoke("isolated", command_runner=runner)
        checks = {row["name"]: row for row in result["checks"]}
        self.assertEqual("blocked_injected_process_runner", result["api_evidence_mode"])
        for name in ("health_api", "critical_state_api"):
            self.assertEqual("blocked_not_verified", checks[name]["status"])
            self.assertEqual("guarded_fastapi_child_unavailable", checks[name]["reason"])
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
