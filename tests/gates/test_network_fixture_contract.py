"""Pure source contracts for the hosted packet fixture; imports no FWRouter code."""
from __future__ import annotations

import ast
import copy
import hashlib
import importlib.util
import io
import ipaddress
import json
import os
import re
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "acceptance" / "network_fixture.py"
PACKET_TEST = ROOT / "tests" / "application_acceptance" / "test_packet_dataplane.py"
LAUNCHER = ROOT / "tests" / "acceptance" / "launcher.py"


def _source() -> str:
    return FIXTURE.read_text(encoding="utf-8")


def _provider_bridge_source() -> str:
    return (ROOT / "tests" / "application_acceptance" / "joined_support.py").read_text(encoding="utf-8")


def _tree() -> ast.Module:
    return ast.parse(_source())


def _fixture_module():
    spec = importlib.util.spec_from_file_location("packet_fixture_contract_target", FIXTURE)
    if spec is None or spec.loader is None:
        raise AssertionError("packet fixture module could not be loaded for its pure contract checks")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _function(name: str) -> ast.FunctionDef:
    found = [node for node in ast.walk(_tree()) if isinstance(node, ast.FunctionDef) and node.name == name]
    if len(found) != 1:
        raise AssertionError(f"expected one source function named {name}")
    return found[0]


def _function_source(name: str) -> str:
    lines = _source().splitlines()
    node = _function(name)
    return "\n".join(lines[node.lineno - 1:node.end_lineno])


def _method_source(class_name: str, method_name: str) -> str:
    tree = _tree()
    classes = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name]
    if len(classes) != 1:
        raise AssertionError(f"expected one source class named {class_name}")
    methods = [node for node in classes[0].body
               if isinstance(node, ast.FunctionDef) and node.name == method_name]
    if len(methods) != 1:
        raise AssertionError(f"expected one method {class_name}.{method_name}")
    lines = _source().splitlines()
    return "\n".join(lines[methods[0].lineno - 1:methods[0].end_lineno])


def _literal_assignments() -> dict[str, object]:
    result: dict[str, object] = {}
    for node in _tree().body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            try:
                result[node.targets[0].id] = ast.literal_eval(node.value)
            except (ValueError, TypeError):
                pass
    return result


class NetworkFixtureContractTests(unittest.TestCase):
    def test_packet_capture_copy_failure_retains_only_bounded_redacted_stderr(self):
        tree = ast.parse(LAUNCHER.read_text(encoding="utf-8"))
        function = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                        and node.name == "export_validated_packet_capture")
        seen_limits = []

        def redact(value, *, limit):
            seen_limits.append(limit)
            return str(value)[:limit]

        class NotRun(Exception):
            pass

        namespace = {
            "Any": object, "Path": Path, "stat": stat, "os": os, "NotRun": NotRun,
            "mark_cleanup_unconfirmed": lambda receipt, reason: None,
            "validate_packet_capture": lambda *args, **kwargs: self.fail("failed copy must not validate"),
            "_redact_public": redact,
        }
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(LAUNCHER), "exec"), namespace)
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            owned, artifacts = base / "owned", base / "artifacts"
            owned.mkdir()
            artifacts.mkdir()
            result = namespace["export_validated_packet_capture"](
                lambda source, target: {"copied": False, "exit_code": 1, "method": "docker-cp",
                                        "stderr": "bounded diagnostic"},
                "/tmp/fixed.pcap", owned / "capture.tmp", artifacts / "capture.pcap",
                owned_root=owned, artifact_root=artifacts, protocol=6, snaplen=54,
                allowed_destinations={}, receipt={}, name="tcp")
        self.assertEqual(result["copy_stderr"], "bounded diagnostic")
        self.assertEqual(result["exit_code"], 1)
        self.assertEqual(seen_limits, [2048])

    def test_tmpfs_capture_export_uses_only_fixed_root_owned_bounded_file(self):
        tree = ast.parse(LAUNCHER.read_text(encoding="utf-8"))
        functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
        namespace = {"PACKET_CAPTURE_SNAPLEN": {"tcp": 54, "udp": 42}, "Any": object,
                     "NotRun": type("NotRun", (Exception,), {})}
        for name in ("packet_capture_copy_code", "_docker_copy_capture"):
            exec(compile(ast.Module(body=[functions[name]], type_ignores=[]), str(LAUNCHER), "exec"), namespace)
        worker = namespace["packet_capture_copy_code"]("tcp")
        worker_tree = ast.parse(worker)
        worker_text = ast.unparse(worker_tree)
        assignments = {target.id: node.value for node in worker_tree.body if isinstance(node, ast.Assign)
                       for target in node.targets if isinstance(target, ast.Name)}
        self.assertEqual(ast.literal_eval(assignments["root_path"]), "/tmp/fwrouter-packet-evidence")
        self.assertIn("capture_name", worker_text)
        self.assertIn(".pcap", worker_text)
        self.assertEqual(ast.unparse(assignments["maximum"]), "24 + 512 * (16 + 54)")
        self.assertIn("os.O_NOFOLLOW", worker_text)
        self.assertIn("root_info.st_uid != 0", worker_text)
        self.assertIn("stat.S_IMODE(root_info.st_mode) != 448", worker_text)
        self.assertIn("before.st_uid != 0", worker_text)
        self.assertIn("stat.S_IMODE(before.st_mode) != 384", worker_text)
        self.assertIn("before.st_size > maximum", worker_text)
        self.assertNotIn("sys.argv", worker_text)
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            destination = base / "packet-tcp.pcap.quarantine"
            pcap_header = b"\xd4\xc3\xb2\xa1" + b"\0" * 20
            calls = []

            class Completed:
                def __init__(self, *, returncode, stdout=b"", stderr=b""):
                    self.returncode, self.stdout, self.stderr = returncode, stdout, stderr

            def run(argv, **kwargs):
                calls.append(argv)
                if len(calls) == 1:
                    return Completed(returncode=1, stderr=b"tmpfs source unavailable")
                return Completed(returncode=0, stdout=pcap_header)

            namespace.update({"Path": Path, "ROOT": base, "subprocess": type("Subprocess", (), {
                                  "run": staticmethod(run), "TimeoutExpired": TimeoutError}),
                              "_redact_public": lambda value, *, limit: value.decode()[:limit]
                              if isinstance(value, bytes) else str(value)[:limit],
                              "PACKET_CAPTURE_SOURCES": {"/tmp/fwrouter-packet-evidence/tcp.pcap": "tcp"},
                              "PACKET_CAPTURE_LIMIT": 512, "os": os, "stat": stat})
            result = namespace["_docker_copy_capture"](
                "docker", "fixed-container", "/tmp/fwrouter-packet-evidence/tcp.pcap", destination, env={})
            self.assertTrue(result["copied"])
            self.assertEqual(result["method"], "docker-exec-bounded-stdout")
            self.assertEqual(result["docker_cp_exit_code"], 1)
            self.assertEqual(result["docker_cp_stderr"], "tmpfs source unavailable")
            self.assertEqual(destination.read_bytes(), pcap_header)
            self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)
            self.assertEqual(calls[1][0:4], ["docker", "exec", "fixed-container", "python"])
            self.assertIn("packet capture export refused", calls[1][5])

    def test_fixture_is_stdlib_only_and_roles_have_no_arbitrary_arguments(self):
        tree = _tree()
        source = _source()
        imported = {alias.name.split(".")[0] for node in ast.walk(tree)
                    if isinstance(node, ast.Import) for alias in node.names}
        imported |= {node.module.split(".")[0] for node in ast.walk(tree)
                     if isinstance(node, ast.ImportFrom) and node.module}
        self.assertTrue(imported.isdisjoint({"backend", "fwrouter_api", "fastapi", "requests", "httpx"}))
        main = _function_source("main")
        self.assertIn('parser.add_argument("role", choices=("client", "endpoint"))', main)
        self.assertNotIn("url", main.lower())
        self.assertNotIn("shell=True", _source())
        self.assertIn('profile["profile_owner_uid"] != profile_info.st_uid', source)

    def test_fixture_failure_diagnostics_are_redacted_and_byte_bounded(self):
        fixture = _fixture_module()
        detail = fixture._fixture_failure_detail(
            fixture.FixtureError("provider token=secret-value " + "x" * fixture.MAX_OUTPUT))
        self.assertIn("token=[REDACTED]", detail)
        self.assertLessEqual(len(detail.encode("utf-8")), fixture.MAX_DIAGNOSTIC_OUTPUT)
        self.assertNotIn("secret-value", detail)

    def test_endpoint_readiness_uses_the_control_servers_complete_listener_set(self):
        fixture = _fixture_module()

        class FakeSocket:
            def __init__(self, address, *, tcp):
                self.address = address
                self.tcp = tcp

            def getsockopt(self, level, option):
                if (level, option) != (fixture.socket.SOL_SOCKET, fixture.socket.SO_ACCEPTCONN):
                    raise AssertionError("unexpected socket readiness query")
                return 1 if self.tcp else 0

            def getsockname(self):
                return self.address

        class FakeServer:
            def __init__(self, address, *, tcp):
                self.server_address = address
                self.socket = FakeSocket(address, tcp=tcp)

        servers = (
            FakeServer((fixture.SERVICE_VIP, fixture.HTTP_PORT), tcp=True),
            FakeServer((fixture.ENDPOINT_WAN_IP, fixture.ENDPOINT_CONTROL_PORT), tcp=True),
            FakeServer((fixture.SERVICE_VIP, fixture.UDP_ECHO_PORT), tcp=False),
            FakeServer((fixture.SERVICE_VIP, fixture.DNS_PORT), tcp=False),
        )
        control = servers[1]
        control.role_servers = servers

        class FakeRuntime:
            running = True

            def snapshot(self):
                return {"running": True}

        with mock.patch.object(fixture, "_tcp_listener_present", return_value=True):
            ready = fixture._endpoint_status(control, FakeRuntime())
            self.assertTrue(ready["ready"])
            self.assertTrue(all(ready["sockets"].values()))

            del control.role_servers
            missing = fixture._endpoint_status(control, FakeRuntime())
            self.assertFalse(missing["ready"])
            self.assertFalse(missing["sockets"]["http_tcp_9080"])
            self.assertFalse(missing["sockets"]["udp_echo_9081"])
            self.assertFalse(missing["sockets"]["dns_udp_5353"])

            control.role_servers = (servers[0], servers[1],
                                    FakeServer((fixture.SERVICE_VIP, fixture.DNS_PORT), tcp=False), servers[3])
            wrong_udp = fixture._endpoint_status(control, FakeRuntime())
            self.assertFalse(wrong_udp["ready"])
            self.assertFalse(wrong_udp["sockets"]["udp_echo_9081"])

            control.role_servers = (servers[0], servers[1], servers[2],
                                    FakeServer((fixture.SERVICE_VIP, fixture.UDP_ECHO_PORT), tcp=False))
            wrong_dns = fixture._endpoint_status(control, FakeRuntime())
            self.assertFalse(wrong_dns["ready"])
            self.assertFalse(wrong_dns["sockets"]["dns_udp_5353"])

        serve = _function_source("_serve")
        self.assertIn("servers[1].role_servers = tuple(servers)", serve)

    def test_launcher_captures_packet_fixture_exit_status_with_bounded_pipe_logs(self):
        tree = ast.parse(LAUNCHER.read_text(encoding="utf-8"))
        selected = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                    and node.name in {"packet_fixture_supervisor_code", "packet_role_diagnostics_code",
                                      "packet_role_readiness_code"}]
        self.assertEqual({node.name for node in selected},
                         {"packet_fixture_supervisor_code", "packet_role_diagnostics_code",
                          "packet_role_readiness_code"})
        namespace = {"PACKET_ROLE_DIAGNOSTIC_ROOT": "/tmp/fwrouter-packet-diagnostics",
                     "PACKET_ROLE_DIAGNOSTIC_LIMIT": 4096,
                     "NotRun": RuntimeError}
        exec(compile(ast.Module(body=selected, type_ignores=[]), str(LAUNCHER), "exec"), namespace)
        supervisor = namespace["packet_fixture_supervisor_code"]("client")
        supervisor_source = supervisor
        self.assertIn("stdout=subprocess.PIPE", supervisor_source)
        self.assertIn("stderr=subprocess.STDOUT", supervisor_source)
        self.assertIn("child.wait()", supervisor_source)
        self.assertIn("role+\".status.json\"", supervisor_source)
        self.assertIn("os.O_EXCL", supervisor_source)
        self.assertIn("os.O_NOFOLLOW", supervisor_source)
        self.assertNotIn('getattr(os,"O_NOFOLLOW",0)', supervisor_source)
        self.assertNotIn("setrlimit", supervisor_source,
                         "fixture/native child must not inherit a global output file-size limit")
        diagnostics = namespace["packet_role_diagnostics_code"]("endpoint")
        self.assertIn("os.O_NOFOLLOW", diagnostics)
        self.assertNotIn('getattr(os,"O_NOFOLLOW",0)', diagnostics)
        self.assertIn("os.fstat(fd)", diagnostics)
        self.assertIn("root_info.st_uid==0", diagnostics)
        self.assertIn("info.st_size>limit", diagnostics)
        readiness = namespace["packet_role_readiness_code"]()
        self.assertIn("time.monotonic()+10.0", readiness)
        self.assertIn("timeout=0.4", readiness)
        self.assertIn('"last_error"', readiness)
        self.assertIn('"last_http_status"', readiness)

    def test_packet_capture_worker_has_real_bounded_libpcap_contract(self):
        launcher_tree = ast.parse(LAUNCHER.read_text(encoding="utf-8"))
        generators = {node.name: node for node in launcher_tree.body if isinstance(node, ast.FunctionDef)
                      and node.name in {"packet_capture_start_code", "packet_capture_stop_code"}}
        namespace = {}
        for name, node in generators.items():
            exec(compile(ast.Module(body=[node], type_ignores=[]), str(LAUNCHER), "exec"), namespace)
        start = namespace["packet_capture_start_code"]()
        stop = namespace["packet_capture_stop_code"]()
        start_tree, stop_tree = ast.parse(start), ast.parse(stop)
        self.assertIn("packet_capture_worker.py", start)
        self.assertIn("pass_fds=(write_fd,)", start)
        self.assertIn("deadline=time.monotonic()+10.0", start)
        self.assertIn("selector.select(max(0.0,deadline-time.monotonic()))", start)
        self.assertIn("os.O_NOFOLLOW", start)
        self.assertIn("worker_error", start)
        self.assertIn("stderr=subprocess.PIPE", start)
        self.assertIn("stderr_total+=len(block)", start)
        self.assertIn("worker_stderr=re.sub", start)
        self.assertIn("pidfd_open", stop)
        self.assertIn("pidfd_send_signal", stop)
        self.assertIn("signal.SIGINT", stop)
        self.assertIn("/proc/{pid}/cmdline", stop)
        self.assertNotIn("tcpdump", start.lower() + stop.lower())
        self.assertNotIn("packet payload", start.lower() + stop.lower())
        self.assertFalse([node for node in start_tree.body if isinstance(node, ast.Raise)
                          and isinstance(node.exc, ast.Call)
                          and isinstance(node.exc.func, ast.Name) and node.exc.func.id == "SystemExit"])
        self.assertTrue(any(isinstance(node, ast.Raise) for node in ast.walk(stop_tree)),
                        "stop path must fail closed on invalid ownership metadata")

        worker_path = ROOT / "tests/acceptance/packet_capture_worker.py"
        worker_source = worker_path.read_text(encoding="utf-8")
        worker_tree = ast.parse(worker_source)
        packet_out = next(node for node in worker_tree.body if isinstance(node, ast.Assign)
                          and any(isinstance(target, ast.Name) and target.id == "PCAP_D_OUT"
                                  for target in node.targets))
        self.assertEqual(ast.literal_eval(packet_out.value), 2,
                         "libpcap 1.10.3 defines PCAP_D_OUT as enum value 2")
        functions = {node.name: node for node in worker_tree.body if isinstance(node, ast.FunctionDef)}
        pure_names = ("_version_prefix_matches", "_classic_header_matches", "_captured_lengths_valid",
                      "_redact_native_text")
        pure_namespace = {"struct": __import__("struct"), "DLT_EN10MB": 1, "re": re}
        for name in pure_names:
            exec(compile(ast.Module(body=[functions[name]], type_ignores=[]), str(worker_path), "exec"), pure_namespace)
        accepts_version = pure_namespace["_version_prefix_matches"]
        self.assertTrue(accepts_version("libpcap version 1.10.3", "libpcap version 1.10.3"))
        self.assertTrue(accepts_version("libpcap version 1.10.3 (with TPACKET)", "libpcap version 1.10.3"))
        self.assertFalse(accepts_version("libpcap version 1.10.30", "libpcap version 1.10.3"))
        good_header = __import__("struct").pack("<IHHIIII", 0xA1B2C3D4, 2, 4, 0, 0, 54, 1)
        self.assertTrue(pure_namespace["_classic_header_matches"](good_header, 54)[0])
        self.assertFalse(pure_namespace["_classic_header_matches"](good_header[:20], 54)[0])
        self.assertFalse(pure_namespace["_classic_header_matches"](
            __import__("struct").pack("<IHHIIII", 0xA1B2C3D4, 2, 3, 0, 0, 54, 1), 54)[0])
        self.assertTrue(pure_namespace["_captured_lengths_valid"](42, 1500, 54))
        self.assertFalse(pure_namespace["_captured_lengths_valid"](55, 1500, 54))
        self.assertFalse(pure_namespace["_captured_lengths_valid"](42, 41, 54))
        redacted = pure_namespace["_redact_native_text"](
            "device error token=hunter2 user@example.test 123e4567-e89b-12d3-a456-426614174000")
        self.assertLessEqual(len(redacted.encode("utf-8")), 256)
        for secret in ("hunter2", "user@example.test", "123e4567-e89b-12d3-a456-426614174000"):
            self.assertNotIn(secret, redacted)

        configure = functions["_configure_flow"]
        configure_calls = {node.func.attr for node in ast.walk(configure)
                           if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
        self.assertTrue({"pcap_setnonblock", "pcap_getnonblock", "pcap_setdirection",
                         "pcap_compile", "pcap_setfilter"}.issubset(configure_calls))
        self.assertIn("PCAP_D_OUT", ast.unparse(configure))
        self.assertIn("activation != 0", ast.unparse(configure))
        run_source = ast.unparse(functions["_run"])
        self.assertIn("_captured_lengths_valid", run_source)
        self.assertIn("callback_errors", run_source)
        self.assertIn("capture_final_flush", run_source)
        self.assertIn("pcap_dump_flush", run_source)
        self.assertIn("PcapDumpFlushFailed", run_source)
        self.assertIn("_require_package_owned_library", ast.unparse(functions["_libpcap"]))
        package_owner = ast.unparse(functions["_require_package_owned_library"])
        self.assertIn("dpkg-query", package_owner)
        self.assertIn("-L", package_owner)
        self.assertIn("path not in package_paths", package_owner)
        mapped = ast.unparse(functions["_mapped_library_path"])
        self.assertIn("(deleted)", mapped)
        self.assertIn("/usr/lib", mapped)

        schema = json.loads((ROOT / "tests/acceptance/kernel_packet_profile_schema.json").read_text())
        self.assertEqual(schema["properties"]["packet_capture"]["required"],
                         ["libpcap_package_version", "libpcap_api_version_prefix", "capture_worker_sha256"])
        dockerfile = (ROOT / "tests/acceptance/Dockerfile").read_text()
        self.assertIn("libpcap0.8=1.10.3-1", dockerfile)
        self.assertIn('receipt["packet_capture_runtime"] = capture_result',
                      LAUNCHER.read_text(encoding="utf-8"))

    def test_packet_application_scenario_requires_real_core_counter_deltas_and_header_only_flows(self):
        source = PACKET_TEST.read_text(encoding="utf-8")
        tree = ast.parse(source)
        scenario = next((node for node in tree.body if isinstance(node, ast.FunctionDef)
                         and node.name == "test_core_vpn_emergency_direct_reentry_forwards_owned_tcp_udp_dns_without_leaks"), None)
        self.assertIsNotNone(scenario)
        core_chains = (ROOT / "backend" / "fwrouter_api" / "services" /
                       "dataplane_nft_chains.py").read_text(encoding="utf-8")
        core_render = (ROOT / "backend" / "fwrouter_api" / "services" /
                       "dataplane_nft_render.py").read_text(encoding="utf-8")
        self.assertIn('"/usr/sbin/nft", "-j", "list", "table", "inet", CORE_NFT_TABLE', source)
        self.assertIn('chain == "fwrouter_direct" and comment == "global direct path"', source)
        self.assertIn('chain == "fwrouter_vpn_full" and comment.startswith("fwrouter vpn mark tcp:5204")', source)
        self.assertIn('chain == "fwrouter_vpn_full" and comment.startswith("fwrouter vpn mark udp:5205")', source)
        self.assertIn('chain == "prerouting"\n              and comment.startswith("fwrouter full-vpn tproxy handoff udp:5205")', source)
        self.assertIn('len(counters["vpn_udp_handoff"]) > 1', source)
        self.assertIn('before["vpn_udp_handoff"] is None and after["vpn_udp_handoff"] is None', source)
        self.assertIn('classify_lines.append(\'        goto fwrouter_vpn_full comment "global vpn v1"\')', core_chains)
        self.assertIn('chain_name="fwrouter_vpn_full"', core_render)
        self.assertIn('fwrouter full-vpn tproxy handoff udp:{full_vpn_tproxy_port}', core_chains)
        self.assertIn("full_vpn_redir_port = 5204", core_render)
        self.assertIn("full_vpn_tproxy_port = 5205", core_render)
        self.assertIn('counter return comment "global direct path"', core_chains)
        self.assertIn('meta l4proto tcp tcp dport { 22 } goto fwrouter_direct comment "management tcp ingress direct"', core_chains)
        self.assertIn('sock.connect((ENDPOINT_WAN_IP, 22))', _function_source("_probe_router_forward_leak"))
        self.assertIn('tcp dport 22 accept', _function_source("_install_guard"))
        self.assertIn('"vpn_lan_input":', source)
        self.assertIn('"direct_lan_forward":', source)
        self.assertIn('"vpn_tcp_classified"', source)
        self.assertIn('"vpn_udp_classified"', source)
        self.assertIn('"vpn_udp_handoff_packets"', source)
        self.assertIn('"router_stub_drop"', source)
        self.assertIn('"router_egress_drop"', source)
        self.assertIn('"router_forward_drop"', source)
        guard_helper = next((node for node in tree.body if isinstance(node, ast.FunctionDef)
                             and node.name == "_probe_router_egress_guards"), None)
        probe_phase = next((node for node in tree.body if isinstance(node, ast.FunctionDef)
                            and node.name == "_probe_phase"), None)
        self.assertIsNotNone(guard_helper)
        self.assertIsNotNone(probe_phase)
        forward_probe_calls = [node for node in ast.walk(guard_helper)
                               if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                               and node.func.id == "_role_json" and node.args]
        self.assertTrue(any(
            isinstance(call.args[0], ast.JoinedStr)
            and len(call.args[0].values) == 2
            and isinstance(call.args[0].values[0], ast.FormattedValue)
            and isinstance(call.args[0].values[0].value, ast.Name)
            and call.args[0].values[0].value.id == "CLIENT_CONTROL"
            and isinstance(call.args[0].values[1], ast.Constant)
            and call.args[0].values[1].value == "/probe/router-forward-leak"
            and any(keyword.arg == "method" and isinstance(keyword.value, ast.Constant)
                    and keyword.value.value == "POST" for keyword in call.keywords)
            for call in forward_probe_calls),
            "router guard helper must POST to the fixed client role's router-forward leak probe")
        self.assertTrue(any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                            and node.func.id == "_probe_router_egress_guards"
                            for node in ast.walk(probe_phase)))
        self.assertTrue(any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                            and node.func.id == "_probe_phase"
                            for node in ast.walk(scenario)))
        packet_path_proof = next((node for node in tree.body if isinstance(node, ast.FunctionDef)
                                  and node.name == "_mihomo_packet_path_proof"), None)
        self.assertIsNotNone(packet_path_proof)
        path_proof_source = ast.unparse(packet_path_proof)
        for proof in ("full_vpn_udp_listener_count", "vless_udp_enabled_count",
                      "transparent_udp_sessions_count", "vpn_auto_selects_non_direct"):
            self.assertIn(proof, path_proof_source)
        self.assertTrue(any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                            and node.func.id == "_mihomo_packet_path_proof"
                            for node in ast.walk(probe_phase)))
        self.assertIn('"core_counter_delta": core_counters', ast.unparse(probe_phase))
        self.assertIn('"endpoint_observation_delta"', ast.unparse(probe_phase))
        self.assertIn('sock.connect((ENDPOINT_IP, 65000))', source)
        self.assertIn('sock.sendto(b"fwrouter-packet-guard", ("127.0.0.11", 53))', source)
        self.assertIn('stub_send_error = type(exc).__name__', source)
        self.assertIn('linktype != 1', source)
        self.assertIn('inotify_init1', source)
        self.assertIn('_wait_packet_counts', source)
        self.assertNotIn('time.sleep(', source)
        self.assertIn('"vpn-after-reentry"', source)
        self.assertTrue(any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                            and node.func.id == "_core_counter_snapshot" for node in ast.walk(scenario)))
        summary_calls = [node for node in ast.walk(scenario)
                         if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                         and node.func.id == "_write_capture_summary"]
        self.assertEqual(len(summary_calls), 1)
        summary_arg = summary_calls[0].args[0]
        counter_mapping_node = next(value for key, value in zip(summary_arg.keys, summary_arg.values)
                                     if isinstance(key, ast.Constant) and key.value == "core_counter_mapping")
        counter_mapping = ast.literal_eval(counter_mapping_node)
        self.assertEqual(counter_mapping["direct_lan_forward"],
                         "inet fwrouter_v2 chain fwrouter_direct / global direct path (exercised by fixed LAN packet phase)")
        vpn_mapping = counter_mapping["vpn_lan_input"]
        for proof in ("fwrouter_vpn_full", "TCP 5204 REDIR", "UDP 5205 TProxy", "global vpn v1"):
            self.assertIn(proof, vpn_mapping)
        self.assertNotIn("chain fwrouter_vpn TCP", vpn_mapping)
        self.assertEqual(counter_mapping["vpn_udp_handoff_packets"],
                         "inet fwrouter_v2 chain prerouting / fwrouter full-vpn tproxy handoff udp:5205 counter; proves marked full-VPN UDP reached the Core TProxy handoff")
        self.assertNotIn("os.system(", _source())

    def test_xray_incarnation_evidence_reads_only_bounded_current_worker_log_append(self):
        source_path = ROOT / "tests" / "application_acceptance" / "test_xray_generation.py"
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        selected_names = {"_PROVIDER_WORKER_LOG_PREFIX", "_PROVIDER_WORKER_LOG_MAX_FILES",
                          "_PROVIDER_WORKER_LOG_MAX_APPEND_BYTES"}
        selected_functions = {"_snapshot_provider_worker_logs", "_provider_worker_records_since"}
        selected = []
        for node in tree.body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                if node.targets[0].id in selected_names:
                    selected.append(node)
            elif isinstance(node, ast.FunctionDef) and node.name in selected_functions:
                selected.append(node)
        self.assertEqual(selected_names, {node.targets[0].id for node in selected if isinstance(node, ast.Assign)})
        self.assertEqual(selected_functions, {node.name for node in selected if isinstance(node, ast.FunctionDef)})
        namespace = {"json": json, "os": os, "re": re, "stat": stat}
        exec(compile(ast.Module(body=selected, type_ignores=[]), str(source_path), "exec"), namespace)
        snapshot_logs = namespace["_snapshot_provider_worker_logs"]
        records_since = namespace["_provider_worker_records_since"]
        prefix = namespace["_PROVIDER_WORKER_LOG_PREFIX"]
        old_record = {"event": "refresh_result", "marker": "before-action", "targeted_source_match": True,
                      "summary": {"error": {"code": "XRAY_GENERATION_RUNTIME_READBACK_FAILED"}}}
        current_record = {"event": "refresh_result", "marker": "current-action", "targeted_source_match": True,
                          "summary": {"error": {"code": "XRAY_GENERATION_RUNTIME_READBACK_FAILED"}}}
        old_line = (prefix + json.dumps(old_record, separators=(",", ":")) + "\n").encode("ascii")
        current_line = (prefix + json.dumps(current_record, separators=(",", ":")) + "\n").encode("ascii")
        with tempfile.TemporaryDirectory(prefix="xray-log-offset-contract-") as raw_root:
            root = Path(raw_root)
            existing = root / "uvicorn-8123.log"
            existing.write_bytes(old_line)
            before = snapshot_logs(root)
            self.assertEqual(records_since(root, before), [], "pre-action matching record must be excluded")
            with existing.open("ab") as stream:
                stream.write(current_line)
            self.assertEqual(records_since(root, before), [current_record],
                             "current targeted native failure record must be retained")

            replacement_before = snapshot_logs(root)
            existing.rename(root / "uvicorn-8123.log.rotated")
            existing.write_bytes(current_line)
            with self.assertRaises(AssertionError):
                records_since(root, replacement_before)

            existing.unlink()
            existing.write_bytes(old_line)
            truncated_before = snapshot_logs(root)
            existing.write_bytes(b"x")
            with self.assertRaises(AssertionError):
                records_since(root, truncated_before)

            bounded_before = snapshot_logs(root)
            with existing.open("ab") as stream:
                stream.write(b"x" * (namespace["_PROVIDER_WORKER_LOG_MAX_APPEND_BYTES"] + 1))
            with self.assertRaises(AssertionError):
                records_since(root, bounded_before)

            new_log_before = snapshot_logs(root)
            new_log = root / "uvicorn-8124.log"
            new_log.write_bytes(current_line)
            self.assertEqual(records_since(root, new_log_before), [current_record],
                             "new suite-owned worker log must be read from offset zero")

        scenario = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                        and node.name == "test_xray_generation_fence_rejects_replaced_native_incarnation")
        scenario_source = "\n".join(source_path.read_text(encoding="utf-8").splitlines()[
            scenario.lineno - 1:scenario.end_lineno])
        self.assertIn('record.get("targeted_source_match") is True', scenario_source)
        self.assertIn('== "XRAY_GENERATION_RUNTIME_READBACK_FAILED"', scenario_source)

    def test_provider_bridge_packet_target_is_closed_and_default_remains_loopback(self):
        tree = ast.parse(_provider_bridge_source())
        bridges = [node for node in tree.body if isinstance(node, ast.ClassDef)
                   and node.name == "ProviderHttpTestBridge"]
        self.assertEqual(len(bridges), 1)
        constructors = [node for node in bridges[0].body if isinstance(node, ast.FunctionDef)
                        and node.name == "__init__"]
        self.assertEqual(len(constructors), 1)
        source = "\n".join(_provider_bridge_source().splitlines()[constructors[0].lineno - 1:
                                                              constructors[0].end_lineno])
        self.assertIn("packet_endpoint: bool = False", source)
        self.assertIn('"198.18.240.2:5301" if packet_endpoint else "127.0.0.1:5301"', source)
        self.assertIn("if not isinstance(packet_endpoint, bool)", source)
        self.assertNotIn("urlparse", source)

    def test_topology_and_service_addresses_are_fixed_reserved_literals(self):
        values = _literal_assignments()
        expected = {
            "CLIENT_NET": "10.240.0.0/29", "CLIENT_IP": "10.240.0.2",
            "ROUTER_LAN_IP": "10.240.0.1", "ROUTER_WAN_IP": "198.18.240.1",
            "ENDPOINT_WAN_IP": "198.18.240.2", "WAN_NET": "198.18.240.0/29",
            "SERVICE_VIP": "203.0.113.53", "ENDPOINT_XRAY_PORT": 5301,
            "HTTP_PORT": 9080, "UDP_ECHO_PORT": 9081, "DNS_PORT": 5353,
            "CLIENT_CONTROL_PORT": 8081, "ENDPOINT_CONTROL_PORT": 8082,
            "DNS_NAME": "probe.fwrouter.test", "DNS_NEGATIVE_NAME": "missing.fwrouter.test",
            "LEAK_DNS_TARGET": "127.0.0.11", "LEAK_RESERVED_TARGET": "192.0.2.1",
            "LEAK_RESERVED_PORT": 9,
        }
        for key, value in expected.items():
            self.assertEqual(value, values[key], key)
        self.assertRegex(values["XRAY_ID"], r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
        vip = ipaddress.ip_address(values["SERVICE_VIP"])
        for network in ("10.0.0.0/8", "100.64.0.0/10", "172.16.0.0/12", "192.168.0.0/16",
                        "127.0.0.0/8", "169.254.0.0/16", "224.0.0.0/4", "240.0.0.0/4"):
            self.assertNotIn(vip, ipaddress.ip_network(network))
        self.assertNotIn(vip, ipaddress.ip_network(values["CLIENT_NET"]))
        self.assertNotIn(vip, ipaddress.ip_network(values["WAN_NET"]))

    def test_profile_capability_and_namespace_checks_precede_fixed_network_commands(self):
        context = _function_source("_validate_owned_context")
        for invariant in ("DOCKER_MARKER.is_file()", "PRODUCTION_MARKERS", "_mount_is_read_only(PROFILE_PATH)",
                          'set(block) != {"role", "host_netns_inode_sha256"}',
                          "_namespace_inode_hash(Path(\"/proc/self/ns/net\"))",
                          "_cap_eff() != NET_ADMIN_BIT", "_sha256(XRAY_PATH) != xray[\"sha256\"]"):
            self.assertIn(invariant, context)
        main = _function_source("main")
        expected_order = ["_read_json", "_validate_owned_context", "_bootstrap_owned_root",
                          "_validate_owned_root", "_validate_role_network", "_install_guard", "_serve"]
        positions = [main.index(name + "(") for name in expected_order]
        self.assertEqual(sorted(positions), positions)
        self.assertLess(main.index("_install_guard(args.role)"), main.index("_serve(args.role, profile)"))

    def test_guard_is_private_owned_drop_default_and_blocks_stub_before_loopback(self):
        guard = _function_source("_install_guard")
        self.assertIn('"fwrouter_packet_fixture"', _source())
        self.assertIn("add table inet {TABLE_NAME}", guard)
        self.assertIn("policy drop", guard)
        self.assertIn("meta nfproto ipv6", guard)
        self.assertLess(guard.index("ip daddr 127.0.0.11 drop"),
                        guard.index('output oifname "lo" accept'))
        self.assertIn("leak_dns_stub", guard)
        self.assertIn("leak_reserved", guard)
        self.assertIn("ct state established,related", guard)
        self.assertIn('output ip daddr {{ {ROUTER_LAN_IP}, {CLIENT_IP}, {ENDPOINT_WAN_IP}, {SERVICE_VIP} }}', guard)
        self.assertIn('output ip daddr {ENDPOINT_WAN_IP} tcp dport {ENDPOINT_XRAY_PORT}', guard)
        self.assertIn('output ip daddr {ENDPOINT_WAN_IP} tcp dport 22 accept', guard)
        self.assertIn('input ip saddr {{ {ROUTER_LAN_IP}, {ENDPOINT_WAN_IP}, {SERVICE_VIP} }}', guard)
        endpoint_input = guard.split('else:', 1)[1]
        self.assertNotIn('tcp dport 22 accept', endpoint_input)
        self.assertIn('hook input priority 0; policy drop;', guard)
        self.assertIn('hook output priority 0; policy drop;', guard)
        self.assertNotIn("fwrouter_v2", guard)
        self.assertIn('"nft", "delete", "table", "inet", TABLE_NAME', _function_source("_remove_guard"))

    def test_only_fixed_probe_protocols_and_no_recursive_dns_or_url_fetch(self):
        source = _source()
        client = _method_source("_ClientHandler", "do_POST")
        for path in ("/probe/tcp", "/probe/udp", "/probe/dns", "/probe/dns-negative",
                     "/probe/leak", "/probe/router-forward-leak"):
            self.assertIn(path, client)
        self.assertIn("Transfer-Encoding", client)
        self.assertIn("_guard_counter", _function_source("_probe_leak"))
        self.assertIn("_dns_response", source)
        leak_probe = _function_source("_probe_leak")
        self.assertIn("except OSError as exc:", leak_probe)
        self.assertIn('if after_dns <= before_dns or after_reserved <= before_reserved:', leak_probe)
        self.assertNotIn("socket.gethostbyname", source)
        self.assertNotIn("getaddrinfo(", source)
        self.assertNotIn("urlopen(", source)
        self.assertNotIn("requests.get(", source)

    def test_endpoint_config_is_synthetic_bounded_and_native_cli_checked(self):
        config = _function_source("_expected_xray_config")
        start = _function_source("_start_xray")
        self.assertIn("XRAY_ID", config)
        self.assertIn('"protocol": "vless"', config)
        self.assertIn('"protocol": "freedom"', config)
        self.assertIn('"rules": []', config)
        self.assertIn("_assert_xray_config(config)", start)
        self.assertIn("os.O_EXCL | os.O_NOFOLLOW", start)
        self.assertIn('"run", "-test", "-config"', start)
        self.assertIn("_redact(checked.stderr)", start)
        self.assertIn("stderr=subprocess.PIPE", start)
        self.assertIn("binary_sha256", _method_source("_XrayRuntime", "snapshot"))

    def test_resource_bounds_and_owned_cleanup_are_explicit(self):
        values = _literal_assignments()
        self.assertLessEqual(values["MAX_OUTPUT"], 16 * 1024)
        self.assertLessEqual(values["MAX_HTTP_BODY"], 2 * 1024)
        self.assertEqual(values["MAX_JSON_RESPONSE"], 8 * 1024)
        self.assertLessEqual(values["READINESS_TIMEOUT"], 10.0)
        self.assertIn("BoundedSemaphore(8)", _source())
        self.assertIn("timeout=5", _function_source("terminate"))
        cleanup = _function_source("_cleanup_owned_root")
        self.assertIn('"xray-endpoint.json"', cleanup)
        self.assertIn("OWNED_MARKER", cleanup)
        self.assertIn("OWNED_ROOT.rmdir()", cleanup)

    def test_json_response_limit_preserves_bounded_readiness_and_rejects_oversize(self):
        fixture = _fixture_module()

        class FakeResponseHandler:
            def __init__(self):
                self.output = io.BytesIO()
                self.wfile = self.output
                self.sent = []

            def send_response(self, status):
                self.sent.append(("status", status))

            def send_header(self, name, value):
                self.sent.append((name, value))

            def end_headers(self):
                return None

            def send_error(self, status):
                self.sent.append(("error", status))

        bounded = FakeResponseHandler()
        bounded._send_json = fixture._QuietHandler._send_json.__get__(bounded)
        bounded._send_json(200, {"ready": True, "xray": {"stderr_tail": "x" * 3000}})
        self.assertNotIn(("error", 500), bounded.sent)
        self.assertLessEqual(len(bounded.output.getvalue()), fixture.MAX_JSON_RESPONSE)

        oversized = FakeResponseHandler()
        oversized._send_json = fixture._QuietHandler._send_json.__get__(oversized)
        oversized._send_json(200, {"too_large": "x" * (fixture.MAX_JSON_RESPONSE + 1)})
        self.assertIn(("error", 500), oversized.sent)

    def test_dns_protocol_answers_fixed_a_record_and_closed_nxdomain_without_recursion(self):
        fixture = _fixture_module()
        positive_query = fixture._dns_query()
        positive = fixture._dns_response(positive_query, fixture.CLIENT_IP)
        self.assertEqual(positive[:2], positive_query[:2])
        self.assertEqual(positive[2:4], b"\x81\x00")
        self.assertEqual(positive[4:8], b"\x00\x01\x00\x01")
        self.assertEqual(positive[-4:], ipaddress.ip_address(fixture.DNS_IP).packed)

        negative_query = fixture._dns_query(fixture.DNS_NEGATIVE_NAME)
        negative = fixture._dns_response(negative_query, fixture.CLIENT_IP)
        self.assertEqual(negative[:2], negative_query[:2])
        self.assertEqual(negative[2:4], b"\x81\x03")
        self.assertEqual(negative[4:8], b"\x00\x01\x00\x00")
        for malformed in (b"", positive_query[:11], b"\x00" * 17,
                          positive_query[:2] + b"\x02\x00" + positive_query[4:], b"x" * 513):
            self.assertEqual(fixture._dns_response(malformed, fixture.CLIENT_IP), b"")

    def test_client_control_rejects_unknown_routes_and_request_bodies_before_network_io(self):
        fixture = _fixture_module()

        class FakeHandler:
            path = "/probe/arbitrary"
            headers = {}
            response = None

            def _send_json(self, status, body):
                self.response = (status, body)

        unknown = FakeHandler()
        fixture._ClientHandler.do_POST(unknown)
        self.assertEqual(unknown.response, (404, {"error": "not_found"}))

        body = FakeHandler()
        body.path = "/probe/tcp"
        body.headers = {"Content-Length": "1"}
        fixture._ClientHandler.do_POST(body)
        self.assertEqual(body.response, (400, {"error": "body_not_allowed"}))

    def test_fixed_http_peer_contract_accepts_the_owned_endpoint_vip_and_rejects_other_peers(self):
        fixture = _fixture_module()

        class FakeConnection:
            def __init__(self, peer):
                self.response = (b"HTTP/1.1 200 OK\r\nContent-Length: 30\r\nConnection: close\r\n\r\n"
                                 + (f'{{"ok":true,"peer":"{peer}"}}').encode("ascii"))

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def settimeout(self, _timeout):
                return None

            def sendall(self, _data):
                return None

            def recv(self, size):
                data, self.response = self.response[:size], self.response[size:]
                return data

        with mock.patch.object(fixture.socket, "create_connection",
                               side_effect=lambda _target, timeout: FakeConnection(fixture.SERVICE_VIP)):
            result = fixture._probe_http()
        self.assertEqual(result["peer"], fixture.SERVICE_VIP)

        with mock.patch.object(fixture.socket, "create_connection",
                               side_effect=lambda _target, timeout: FakeConnection("192.0.2.1")):
            with self.assertRaises(fixture.FixtureError):
                fixture._probe_http()

    def test_observation_snapshot_keeps_bounded_history_and_exact_latest_peer(self):
        fixture = _fixture_module()
        observations = fixture._Observations()
        observations.record("http", fixture.CLIENT_IP)
        observations.record("http", fixture.SERVICE_VIP)
        for index in range(12):
            observations.record("udp", f"198.18.0.{index + 1}")
        snapshot = observations.snapshot()
        self.assertEqual(snapshot["counts"], {"http": 2, "udp": 12, "dns": 0})
        self.assertEqual(snapshot["peers"]["http"], [fixture.CLIENT_IP, fixture.SERVICE_VIP])
        self.assertEqual(len(snapshot["peers"]["udp"]), 8)
        self.assertEqual(snapshot["last_peers"]["http"], fixture.SERVICE_VIP)
        self.assertEqual(snapshot["last_peers"]["udp"], "198.18.0.12")
        self.assertIsNone(snapshot["last_peers"]["dns"])

    def test_endpoint_health_and_control_reads_do_not_count_as_service_http_probes(self):
        fixture = _fixture_module()

        class FakeEndpointHandler:
            server = type("Server", (), {"server_address": (fixture.ENDPOINT_WAN_IP,
                                                                fixture.ENDPOINT_CONTROL_PORT)})()
            path = "/observations"
            client_address = (fixture.CLIENT_IP, 12345)
            response = None

            def _send_json(self, status, body):
                self.response = (status, body)

        handler = FakeEndpointHandler()
        fixture._EndpointHandler.do_GET(handler)
        self.assertEqual(handler.response[0], 200)
        self.assertEqual(fixture._OBS.snapshot()["counts"], {"http": 0, "udp": 0, "dns": 0})

        self.assertEqual(handler.response[1]["last_peers"]["http"], None)

    def test_health_control_is_exact_and_generate_204_reports_availability_without_probe_counts(self):
        fixture = _fixture_module()

        class FakeHealthHandler:
            def __init__(self, address, path, payload=None):
                self.server = type("Server", (), {"server_address": address})()
                self.path = path
                self.headers = {"Content-Length": str(len(payload or b""))}
                self.rfile = io.BytesIO(payload or b"")
                self.response = None
                self.headers_out = []

            def _send_json(self, status, body):
                self.response = (status, body)

            def send_response(self, status):
                self.response = (status, None)

            def send_header(self, name, value):
                self.headers_out.append((name, value))

            def end_headers(self):
                return None

            def _send_generate_204(self):
                return fixture._EndpointHandler._send_generate_204(self)

        with mock.patch.object(fixture, "_HEALTH_AVAILABLE", True):
            for available, status in ((False, 200), (True, 200)):
                payload = json.dumps({"available": available}, separators=(",", ":")).encode("ascii")
                handler = FakeHealthHandler((fixture.ENDPOINT_WAN_IP, fixture.ENDPOINT_CONTROL_PORT),
                                           "/health", payload)
                fixture._EndpointHandler.do_POST(handler)
                self.assertEqual(handler.response, (status, {"available": available}))
                for method in (fixture._EndpointHandler.do_GET, fixture._EndpointHandler.do_HEAD):
                    probe = FakeHealthHandler((fixture.SERVICE_VIP, fixture.HTTP_PORT), "/generate_204")
                    method(probe)
                    self.assertEqual(probe.response[0], 204 if available else 503)
                    self.assertIn(("Content-Length", "0"), probe.headers_out)
                    self.assertIn(("Connection", "close"), probe.headers_out)
                self.assertEqual(fixture._OBS.snapshot()["counts"], {"http": 0, "udp": 0, "dns": 0})

            for payload in (b'{"available":1}', b'{"available":true,"extra":0}', b"not-json"):
                handler = FakeHealthHandler((fixture.ENDPOINT_WAN_IP, fixture.ENDPOINT_CONTROL_PORT),
                                            "/health", payload)
                fixture._EndpointHandler.do_POST(handler)
                self.assertEqual(handler.response[0], 400)

        self.assertIn('self.path == "/generate_204"', _method_source("_EndpointHandler", "do_GET"))
        self.assertIn('self.path == "/generate_204"', _method_source("_EndpointHandler", "do_HEAD"))

    def test_negative_dns_and_leak_probe_do_not_enter_positive_service_observations(self):
        fixture = _fixture_module()

        class FakeClientHandler:
            path = "/probe/dns-negative"
            headers = {"Content-Length": "0"}
            response = None

            def _send_json(self, status, body):
                self.response = (status, body)

        for path, probe in (("/probe/dns-negative", "_probe_dns_negative"),
                            ("/probe/leak", "_probe_leak")):
            handler = FakeClientHandler()
            handler.path = path
            with mock.patch.object(fixture, probe, return_value={"service": "test", "peer": fixture.CLIENT_IP}):
                fixture._ClientHandler.do_POST(handler)
            self.assertEqual(handler.response[0], 200)
            self.assertEqual(fixture._OBS.snapshot()["counts"], {"http": 0, "udp": 0, "dns": 0})

    def test_public_fixture_redaction_masks_uuid_email_and_credential_values(self):
        fixture = _fixture_module()
        rendered = fixture._redact(
            "uuid=88c7ce2a-465e-4e72-9c56-2a9e2fc84a51 email=user@example.test token=synthetic-secret",
            limit=256,
        )
        self.assertNotIn("88c7ce2a-465e-4e72-9c56-2a9e2fc84a51", rendered)
        self.assertNotIn("user@example.test", rendered)
        self.assertNotIn("synthetic-secret", rendered)
        self.assertIn("[UUID]", rendered)
        self.assertIn("[EMAIL]", rendered)
        self.assertIn("[REDACTED]", rendered)

    def test_closed_profile_validation_accepts_only_patched_owned_boundary_facts(self):
        fixture = _fixture_module()
        with tempfile.TemporaryDirectory(prefix="packet-profile-contract-") as raw_root:
            root = Path(raw_root)
            marker = root / ".dockerenv"
            marker.write_text("owned test marker\n", encoding="ascii")
            source_root = root / "workspace"
            source_root.mkdir()
            xray = root / "xray"
            xray.write_bytes(b"synthetic pinned binary bytes\n")
            profile_path = root / "profile.json"
            profile_path.write_text("{}\n", encoding="ascii")
            profile_path.chmod(0o444)
            xray_digest = hashlib.sha256(xray.read_bytes()).hexdigest()
            valid = {
                "schema": fixture.PROFILE_SCHEMA,
                "profile": fixture.PROFILE_NAME,
                "suite_nonce": "a" * 32,
                "profile_owner_uid": profile_path.stat().st_uid,
                "network_testbed": {"role": "client", "host_netns_inode_sha256": "f" * 64},
                "xray": {"path": str(xray), "sha256": xray_digest},
            }
            patches = (
                mock.patch.object(fixture, "DOCKER_MARKER", marker),
                mock.patch.object(fixture, "SOURCE_ROOT", source_root),
                mock.patch.object(fixture, "PRODUCTION_MARKERS", ()),
                mock.patch.object(fixture, "PROFILE_PATH", profile_path),
                mock.patch.object(fixture, "XRAY_PATH", xray),
                mock.patch.object(fixture, "_tmpfs_is_bounded", return_value=True),
                mock.patch.object(fixture, "_mount_is_read_only", return_value=True),
                mock.patch.object(fixture, "_namespace_inode_hash", return_value="e" * 64),
                mock.patch.object(fixture, "_cap_eff", return_value=fixture.NET_ADMIN_BIT),
            )
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], patches[8]:
                self.assertEqual(fixture._validate_owned_context("client", valid), valid["network_testbed"])
                with mock.patch.object(fixture, "DOCKER_MARKER", root / "missing-dockerenv"):
                    with self.assertRaises(fixture.FixtureError):
                        fixture._validate_owned_context("client", valid)
                invalid_profiles = []
                wrong_role = copy.deepcopy(valid)
                wrong_role["network_testbed"]["role"] = "endpoint"
                invalid_profiles.append(wrong_role)
                extra_block_key = copy.deepcopy(valid)
                extra_block_key["network_testbed"]["extra"] = True
                invalid_profiles.append(extra_block_key)
                bad_pin = copy.deepcopy(valid)
                bad_pin["xray"]["sha256"] = "0" * 64
                invalid_profiles.append(bad_pin)
                bad_owner = copy.deepcopy(valid)
                bad_owner["profile_owner_uid"] = valid["profile_owner_uid"] + 1
                invalid_profiles.append(bad_owner)
                for candidate in invalid_profiles:
                    with self.assertRaises(fixture.FixtureError):
                        fixture._validate_owned_context("client", candidate)
            with mock.patch.object(fixture, "DOCKER_MARKER", marker), \
                 mock.patch.object(fixture, "SOURCE_ROOT", source_root), \
                 mock.patch.object(fixture, "PRODUCTION_MARKERS", ()), \
                 mock.patch.object(fixture, "PROFILE_PATH", profile_path), \
                 mock.patch.object(fixture, "XRAY_PATH", xray), \
                 mock.patch.object(fixture, "_tmpfs_is_bounded", return_value=True), \
                 mock.patch.object(fixture, "_mount_is_read_only", return_value=True), \
                 mock.patch.object(fixture, "_namespace_inode_hash", return_value="e" * 64), \
                 mock.patch.object(fixture, "_cap_eff", return_value=fixture.NET_ADMIN_BIT | 1):
                with self.assertRaises(fixture.FixtureError):
                    fixture._validate_owned_context("client", valid)


if __name__ == "__main__":
    unittest.main()
