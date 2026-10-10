from __future__ import annotations

import importlib.util
import ast
import hashlib
import json
import tarfile
import tempfile
import threading
import unittest
import zipfile
import stat
import sys
import socket
from unittest import mock
import io
from pathlib import Path
import xml.etree.ElementTree as ET
from types import ModuleType, SimpleNamespace

sys.path.insert(0, str(Path(__file__).parents[1]))
from application_acceptance.xray_support import parse_inbound_users_reply
from application_acceptance.joined_support import ProviderHttpTestBridge
sys.path.insert(0, str(Path(__file__).parents[1] / "acceptance"))
import qualified_child
import qualified_xray_docker


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
WORKER_PATH = Path(__file__).parents[1] / "application_acceptance" / "worker.py"


class AcceptanceContractTests(unittest.TestCase):
    def test_packet_runtime_tmpfs_accepts_exact_role_size_in_bytes_or_units(self):
        expected = {"router": 512 * 1024 ** 2, "client": 32 * 1024 ** 2,
                    "endpoint": 64 * 1024 ** 2}
        for role, size in expected.items():
            with self.subTest(role=role, representation="bytes"):
                raw = f"rw,noexec,nosuid,nodev,mode=1777,size={size}"
                self.assertEqual(size, launcher.validate_packet_runtime_tmpfs({"/tmp": raw}, role))
            suffix = "512m" if role == "router" else "32m" if role == "client" else "64m"
            with self.subTest(role=role, representation="suffix"):
                raw = f"rw,noexec,nosuid,nodev,mode=1777,size={suffix}"
                self.assertEqual(size, launcher.validate_packet_runtime_tmpfs({"/tmp": raw}, role))
        with self.assertRaisesRegex(launcher.NotRun, "unknown_option_count=1") as raised:
            launcher.validate_packet_runtime_tmpfs(
                {"/tmp": "rw,noexec,nosuid,nodev,mode=1777,size=512m,opaque=VALUE_SENTINEL"}, "router")
        self.assertNotIn("VALUE_SENTINEL", str(raised.exception))
        with self.assertRaises(launcher.NotRun):
            launcher.validate_packet_runtime_tmpfs(
                {"/tmp": "rw,noexec,nosuid,nodev,mode=1777,size=511m"}, "router")
        with self.assertRaisesRegex(launcher.NotRun, "observed_flags"):
            launcher.validate_packet_runtime_tmpfs(
                {"/tmp": "rw,noexec,nosuid,mode=1777,size=512m"}, "router")
        with self.assertRaises(launcher.NotRun):
            launcher.validate_packet_runtime_tmpfs(
                {"/tmp": "rw,rw,noexec,nosuid,nodev,mode=1777,size=512m"}, "router")

    def test_packet_runtime_mount_inventory_allows_engine_tmpfs_omission_but_denies_other_writable_mounts(self):
        profile = Path("/owned/router-profile.json")
        bind = {"Type": "bind", "Source": str(profile),
                "Destination": "/run/fwrouter-acceptance/profile.json", "RW": False}
        launcher.validate_packet_runtime_mounts([bind], profile)
        tmpfs = {"Type": "tmpfs", "Source": "tmpfs", "Destination": "/tmp", "RW": True,
                 "Mode": "rw,noexec,nosuid,nodev"}
        launcher.validate_packet_runtime_mounts([bind, tmpfs], profile)
        with self.assertRaises(launcher.NotRun):
            launcher.validate_packet_runtime_mounts([bind, tmpfs, {"Type": "volume", "RW": True}], profile)
        with self.assertRaises(launcher.NotRun):
            launcher.validate_packet_runtime_mounts([dict(bind, RW=True)], profile)
        with self.assertRaises(launcher.NotRun):
            launcher.validate_packet_runtime_mounts([bind, dict(tmpfs, RW=False)], profile)

    def test_packet_tmpfs_in_namespace_probe_reads_mountinfo_for_each_fixed_role(self):
        code = launcher.packet_tmpfs_mount_probe_code()
        ast.parse(code, filename="packet_tmpfs_mount_probe")
        for token in ("/proc/self/mountinfo", '"router":512*1024**2',
                      '"client":32*1024**2', '"endpoint":64*1024**2',
                      '"mode=1777"', '"noexec"', '"nosuid"', '"nodev"'):
            self.assertIn(token, code)
        module = ast.parse(LAUNCHER_PATH.read_text(encoding="utf-8"), filename=str(LAUNCHER_PATH))
        hosted = next(node for node in module.body
                      if isinstance(node, ast.FunctionDef) and node.name == "run_hosted_acceptance")
        probe_calls = [node for node in ast.walk(hosted)
                       if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                       and node.func.id == "packet_tmpfs_mount_probe_code"]
        topology_calls = [node for node in ast.walk(hosted)
                          if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                          and node.func.id == "packet_namespace_setup"]
        self.assertEqual(1, len(probe_calls), "all three packet roles need actual /proc mount readback")
        self.assertEqual(1, len(topology_calls))
        self.assertLess(probe_calls[0].lineno, topology_calls[0].lineno,
                        "mount policy must be proven before packet topology changes")

        expected = {"router": (512 * 1024 ** 2, "524288k"),
                    "client": (32 * 1024 ** 2, "32768k"),
                    "endpoint": (64 * 1024 ** 2, "65536k")}
        for role, (size_bytes, rendered_size) in expected.items():
            mountinfo = ("36 25 0:32 / /tmp rw,nosuid,nodev,noexec,relatime "
                         f"- tmpfs tmpfs rw,size={rendered_size},mode=1777\n")
            with self.subTest(role=role), mock.patch.object(sys, "argv", ["probe", role]), \
                    mock.patch.object(Path, "read_text", return_value=mountinfo), \
                    mock.patch("builtins.print") as print_result:
                exec(code, {"__name__": "__main__"})
                observed = json.loads(print_result.call_args.args[0])
                self.assertEqual({"status": "passed", "role": role, "size_bytes": size_bytes,
                                  "flags": ["mode=1777", "noexec", "nodev", "nosuid", "rw"]}, observed)

        failing_mountinfos = {
            "missing-flag": "36 25 0:32 / /tmp rw,nodev,noexec,relatime - tmpfs tmpfs rw,size=524288k,mode=1777\n",
            "wrong-size": "36 25 0:32 / /tmp rw,nosuid,nodev,noexec,relatime - tmpfs tmpfs rw,size=1m,mode=1777\n",
            "duplicate-mount": (
                "36 25 0:32 / /tmp rw,nosuid,nodev,noexec,relatime - tmpfs tmpfs rw,size=524288k,mode=1777\n"
                "37 25 0:33 / /tmp rw,nosuid,nodev,noexec,relatime - tmpfs tmpfs rw,size=524288k,mode=1777\n"),
        }
        for failure, mountinfo in failing_mountinfos.items():
            with self.subTest(failure=failure), mock.patch.object(sys, "argv", ["probe", "router"]), \
                    mock.patch.object(Path, "read_text", return_value=mountinfo), \
                    self.assertRaises(SystemExit):
                exec(code, {"__name__": "__main__"})

    def test_packet_network_inspect_targets_both_exact_owned_networks(self):
        project = "fwrouter-acceptance-0123456789abcdef0123456789abcdef"
        self.assertEqual(["/usr/bin/docker", "network", "inspect", f"{project}_lan"],
                         launcher.packet_network_inspect_argv("/usr/bin/docker", project, "lan"))
        self.assertEqual(["/usr/bin/docker", "network", "inspect", f"{project}_wan"],
                         launcher.packet_network_inspect_argv("/usr/bin/docker", project, "wan"))
        with self.assertRaises(launcher.NotRun):
            launcher.packet_network_inspect_argv("/usr/bin/docker", project, "isolated")

        module = ast.parse(LAUNCHER_PATH.read_text(encoding="utf-8"), filename=str(LAUNCHER_PATH))
        hosted = next(node for node in module.body
                      if isinstance(node, ast.FunctionDef) and node.name == "run_hosted_acceptance")
        network_loop = next((node for node in ast.walk(hosted)
                             if isinstance(node, ast.For)
                             and isinstance(node.target, ast.Name) and node.target.id == "name"
                             and isinstance(node.iter, ast.Tuple)
                             and ast.literal_eval(node.iter) == ("lan", "wan")), None)
        if network_loop is None:
            self.fail("packet startup must inspect both expected network names")
        loop_source = ast.Module(body=network_loop.body, type_ignores=[])
        loop_calls = [node for node in ast.walk(loop_source) if isinstance(node, ast.Call)]
        self.assertTrue(any(isinstance(call.func, ast.Name) and call.func.id == "_docker_json"
                            and call.args and isinstance(call.args[0], ast.Call)
                            and isinstance(call.args[0].func, ast.Name)
                            and call.args[0].func.id == "packet_network_inspect_argv"
                            for call in loop_calls))
        self.assertTrue(any(isinstance(call.func, ast.Name) and call.func.id == "validate_packet_network_inspect"
                            for call in loop_calls))
        self.assertFalse(any(isinstance(node, ast.IfExp) and isinstance(node.body, ast.Constant)
                             and node.body.value is None for node in ast.walk(hosted)),
                         "packet startup must not leave network inspect as an empty placeholder")

    def test_packet_compose_normalized_null_entrypoint_is_allowed_but_override_is_not(self):
        role_keys = {
            "application": {"build", "cap_add", "cap_drop", "command", "cpus", "environment", "image", "init",
                            "labels", "mem_limit", "memswap_limit", "networks", "pids_limit", "read_only",
                            "restart", "security_opt", "sysctls", "tmpfs", "user", "volumes"},
            "lanclient": {"cap_add", "cap_drop", "command", "cpus", "environment", "image", "init", "labels",
                          "mem_limit", "memswap_limit", "networks", "pids_limit", "read_only", "restart",
                          "security_opt", "tmpfs", "user", "volumes"},
            "endpoint": {"cap_add", "cap_drop", "command", "cpus", "environment", "image", "init", "labels",
                         "mem_limit", "memswap_limit", "networks", "pids_limit", "read_only", "restart",
                         "security_opt", "tmpfs", "user", "volumes"},
        }
        for role, expected_keys in role_keys.items():
            with self.subTest(role=role):
                normalized = dict.fromkeys(expected_keys, None)
                normalized["entrypoint"] = None
                launcher.validate_packet_service_fields(role, normalized, expected_keys)
                override = dict(normalized, entrypoint=["/bin/sh", "-c", "unexpected"])
                with self.assertRaisesRegex(launcher.NotRun, "inherit the fixed image entrypoint"):
                    launcher.validate_packet_service_fields(role, override, expected_keys)

    def test_packet_compose_contract_reports_only_bounded_service_field_names(self):
        run_id = "packet-contract-run"
        profile_paths = {role: Path(f"/owned/{role}-profile.json")
                         for role in ("router", "client", "endpoint")}
        unexpected = {f"normalized-field-{index:02d}": "SENSITIVE_VALUE_MUST_NOT_APPEAR"
                      for index in range(40)}
        unexpected["bad\nFIELDNAME_SENTINEL"] = "SENSITIVE_VALUE_MUST_NOT_APPEAR"
        config = {
            "services": {
                "application": {"image": f"fwrouter-acceptance:{run_id}", **unexpected},
                "lanclient": {"image": f"fwrouter-acceptance:{run_id}"},
                "endpoint": {"image": f"fwrouter-acceptance:{run_id}"},
            },
            "networks": {"lan": {}, "wan": {}},
        }
        with self.assertRaises(launcher.NotRun) as raised:
            launcher.validate_packet_compose_config(config, run_id=run_id, profile_paths=profile_paths)
        message = str(raised.exception)
        self.assertIn("role=application", message)
        self.assertIn("normalized-field-00", message)
        self.assertNotIn("normalized-field-39", message)
        self.assertNotIn("FIELDNAME_SENTINEL", message)
        self.assertNotIn("SENSITIVE_VALUE_MUST_NOT_APPEAR", message)
        self.assertIn("unexpected_count=41", message)

    def test_packet_host_snapshot_uses_noninteractive_sudo_for_read_only_nft_query(self):
        route_result = SimpleNamespace(returncode=0, stdout=b"[]")
        nft_result = SimpleNamespace(returncode=0, stdout=b'{"nftables":[]}')
        with (mock.patch.object(launcher.shutil, "which", side_effect=("/usr/sbin/ip", "/usr/sbin/nft",
                                                                         "/usr/bin/sudo")) as which,
              mock.patch.object(launcher.subprocess, "run", side_effect=(route_result, nft_result)) as run,
              mock.patch.object(launcher.os, "stat", return_value=SimpleNamespace(st_ino=1234))):
            result = launcher.packet_host_preflight()
        self.assertEqual("passed", result["overlap_check"])
        self.assertEqual(["/usr/sbin/ip", "-j", "-4", "route", "show", "table", "all"], run.call_args_list[0].args[0])
        self.assertEqual(["/usr/bin/sudo", "-n", "/usr/sbin/nft", "-j", "list", "ruleset"],
                         run.call_args_list[1].args[0])
        self.assertTrue(all(call.kwargs.get("timeout") in {5, 8} for call in run.call_args_list))
        self.assertEqual(3, which.call_count)

    def test_packet_capture_validation_quarantines_rejections_and_never_publishes_failed_copy(self):
        with tempfile.TemporaryDirectory(prefix="fwrouter-packet-capture-quarantine-") as temp:
            root = Path(temp) / "owned"
            artifacts = Path(temp) / "artifacts"
            root.mkdir(mode=0o700)
            artifacts.mkdir(mode=0o700)
            quarantine = root / "tcp.pcap.quarantine"
            published = artifacts / "packet-tcp.pcap"
            raw_marker = b"RAW_PACKET_PAYLOAD_MUST_NOT_BE_PUBLISHED"

            def invalid_capture(_source, target):
                target.write_bytes(raw_marker)
                return {"copied": True, "exit_code": 0, "method": "synthetic"}

            receipt = {"status": "passed"}
            result = launcher.export_validated_packet_capture(
                invalid_capture, "/fixed/tcp.pcap", quarantine, published,
                owned_root=root, artifact_root=artifacts, protocol=6, snaplen=54,
                allowed_destinations={("203.0.113.53", 6): {9080}},
                receipt=receipt, name="tcp")
            self.assertFalse(result["published"])
            self.assertNotIn(raw_marker.decode(), json.dumps(result))
            self.assertFalse(quarantine.exists())
            self.assertFalse(published.exists())
            self.assertEqual("cleanup_unconfirmed", receipt["cleanup"])
            self.assertEqual("failed", receipt["status"])

            udp_quarantine = root / "udp.pcap.quarantine"
            udp_published = artifacts / "packet-udp.pcap"
            failed_receipt = {"status": "passed"}
            failed = launcher.export_validated_packet_capture(
                lambda _source, _target: {"copied": False, "exit_code": 1, "method": "synthetic"},
                "/fixed/udp.pcap", udp_quarantine, udp_published,
                owned_root=root, artifact_root=artifacts, protocol=17, snaplen=42,
                allowed_destinations={("203.0.113.53", 17): {9081}},
                receipt=failed_receipt, name="udp")
            self.assertFalse(failed["published"])
            self.assertFalse(udp_quarantine.exists())
            self.assertFalse(udp_published.exists())
            self.assertEqual("cleanup_unconfirmed", failed_receipt["cleanup"])
            self.assertEqual("failed", failed_receipt["status"])

            precondition_receipt = {"status": "passed"}
            precondition = launcher.export_validated_packet_capture(
                lambda _source, _target: self.fail("copy must not run for an external quarantine path"),
                "/fixed/tcp.pcap", Path(temp) / "outside.pcap", artifacts / "blocked.pcap",
                owned_root=root, artifact_root=artifacts, protocol=6, snaplen=54,
                allowed_destinations={("203.0.113.53", 6): {9080}},
                receipt=precondition_receipt, name="tcp")
            self.assertFalse(precondition["published"])
            self.assertFalse((artifacts / "blocked.pcap").exists())
            self.assertEqual("cleanup_unconfirmed", precondition_receipt["cleanup"])
            self.assertEqual("failed", precondition_receipt["status"])

    def test_packet_router_guard_generates_fixed_nft_sets_and_order(self):
        generated = launcher.packet_router_guard_setup_code("eth0", "eth1")
        # Evaluate only the deterministic rule construction prefix. Do not run
        # nft or mutate the host/container namespace in this contract test.
        prefix = generated.split("present=subprocess.run", 1)[0]
        namespace = {}
        with mock.patch.object(launcher.subprocess, "run", side_effect=AssertionError("runtime command forbidden")):
            exec(prefix, namespace)
        rules = namespace["rules"]
        self.assertTrue(any("udp dport { 9081, 5353 } accept" in rule and "output" in rule for rule in rules))
        stub_rule = next(rule for rule in rules if "ip daddr 127.0.0.11 counter name router_stub_drop drop" in rule)
        self.assertLess(rules.index(stub_rule),
                        rules.index(next(rule for rule in rules if 'oifname "lo" accept' in rule)))
        self.assertTrue(any("udp dport { 9081, 5353 } accept" in rule and "input" in rule for rule in rules))
        self.assertTrue(any("tcp dport { 5202, 5204 } accept" in rule for rule in rules))
        self.assertTrue(any("udp dport { 5203, 5205 } accept" in rule for rule in rules))
        self.assertTrue(any("udp dport { 9081, 5353 } accept" in rule and "forward" in rule for rule in rules))
        self.assertFalse(any("(9081, 5353)" in rule or "(5202, 5204)" in rule for rule in rules))

    def test_kernel_acceptance_worker_path_exposes_admin_tools_only_in_kernel_profiles(self):
        conftest = (LAUNCHER_PATH.parents[1] / "application_acceptance/conftest.py").read_text(encoding="utf-8")
        self.assertIn('profile.get("profile") in {"hosted-kernel-dataplane", "hosted-kernel-packet"}', conftest)
        self.assertIn('"/opt/fwrouter-test/bin:/usr/sbin:/sbin:/usr/bin:/bin" if kernel_profile', conftest)
        self.assertIn('else "/opt/fwrouter-test/bin:/usr/bin:/bin"', conftest)

    def test_owned_worker_rewrites_only_copied_logical_health_check_groups(self):
        from application_acceptance import worker

        original = [
            {"name": "fallback-group", "type": "fallback", "url": "https://www.gstatic.com/generate_204",
             "proxies": ["member-a"]},
            {"name": "url-test-group", "type": "url-test", "url": "https://www.gstatic.com/generate_204",
             "proxies": ["member-b"]},
            {"name": "selector", "type": "select", "proxies": ["member-a", "member-b"]},
        ]
        probe_url = "http://127.0.0.1:52044/generate_204"
        observed = worker._owned_logical_health_check_groups(original, probe_url)
        self.assertEqual(probe_url, observed[0]["url"])
        self.assertEqual(probe_url, observed[1]["url"])
        self.assertEqual(original[2], observed[2])
        self.assertIsNot(original, observed)
        self.assertIsNot(original[0], observed[0])
        self.assertIsNot(original[0]["proxies"], observed[0]["proxies"])
        self.assertEqual("https://www.gstatic.com/generate_204", original[0]["url"])
        self.assertEqual("https://www.gstatic.com/generate_204", original[1]["url"])
        health_checks = [group for group in observed if group["type"] in {"fallback", "url-test"}]
        self.assertTrue(all(group["url"] == probe_url for group in health_checks))
        for unsafe in (
            "https://www.gstatic.com/generate_204",
            "http://127.0.0.1:52044/generate_204?url=https://example.invalid",
            "http://user@127.0.0.1:52044/generate_204",
        ):
            with self.subTest(probe_url=unsafe), self.assertRaises(RuntimeError):
                worker._owned_logical_health_check_groups(original, unsafe)
        packet_profile = {"profile": "hosted-kernel-packet"}
        packet_url = "http://203.0.113.53:9080/generate_204"
        packet_groups = worker._owned_logical_health_check_groups(
            original, packet_url, profile=packet_profile)
        self.assertEqual(packet_url, packet_groups[0]["url"])
        self.assertEqual(packet_url, packet_groups[1]["url"])
        for unsafe in (
            "http://203.0.113.54:9080/generate_204",
            "http://203.0.113.53:9081/generate_204",
            "http://203.0.113.53:9080/other",
            "http://user@203.0.113.53:9080/generate_204",
            "http://203.0.113.53:9080/generate_204?url=http://127.0.0.1",
        ):
            with self.subTest(packet_probe_url=unsafe), self.assertRaises(RuntimeError):
                worker._owned_logical_health_check_groups(original, unsafe, profile=packet_profile)
        with self.assertRaises(RuntimeError):
            worker._owned_logical_health_check_groups(original, packet_url)
        normal_groups = worker._owned_logical_health_check_groups(original, probe_url)
        self.assertEqual(probe_url, normal_groups[0]["url"])
        source = WORKER_PATH.read_text(encoding="utf-8")
        self.assertIn("mihomo_config_proxies._logical_profile_groups = logical_profile_groups_on_owned_bridge", source)
        self.assertIn("generated = original_logical_profile_groups()", source)

    def test_acceptance_recovery_routes_build_fresh_production_controller(self):
        tree = ast.parse(WORKER_PATH.read_text(encoding="utf-8"))
        build_app = next(node for node in tree.body
                         if isinstance(node, ast.FunctionDef) and node.name == "build_app")
        helper = next(node for node in ast.walk(build_app)
                      if isinstance(node, ast.FunctionDef)
                      and node.name == "acceptance_provider_recovery_controller")
        calls = [node for node in ast.walk(helper) if isinstance(node, ast.Call)]
        self.assertTrue(any(isinstance(call.func, ast.Attribute)
                            and call.func.attr == "active_watchdog_vpn_adapter"
                            and isinstance(call.func.value, ast.Name) and call.func.value.id == "deps"
                            for call in calls))
        factory = next(call for call in calls if isinstance(call.func, ast.Attribute)
                       and call.func.attr == "get_vpn_runtime_controller"
                       and isinstance(call.func.value, ast.Name) and call.func.value.id == "deps")
        self.assertTrue(factory.args and isinstance(factory.args[0], ast.Name)
                        and factory.args[0].id == "vpn_adapter")
        routing = next(keyword.value for keyword in factory.keywords if keyword.arg == "routing")
        self.assertIsInstance(routing, ast.Call)
        self.assertIsInstance(routing.func, ast.Attribute)
        self.assertEqual("load_routing_state", routing.func.attr)
        self.assertTrue(any(isinstance(call.func, ast.Name)
                            and call.func.id == "_capture_controller_selection_fence"
                            and call.args and isinstance(call.args[0], ast.Name)
                            and call.args[0].id == "controller" for call in calls))

        recovery_route = next(node for node in ast.walk(build_app)
                              if isinstance(node, ast.FunctionDef) and node.name == "recover_provider_path")
        reentry_route = next(node for node in ast.walk(build_app)
                              if isinstance(node, ast.FunctionDef) and node.name == "reenter_provider_path")
        for route, service in ((recovery_route, "confirmed_provider_recovery"),
                               (reentry_route, "try_verified_reentry")):
            route_calls = [node for node in ast.walk(route) if isinstance(node, ast.Call)]
            service_call = next(call for call in route_calls if isinstance(call.func, ast.Name)
                                and call.func.id == service)
            if service == "confirmed_provider_recovery":
                controller = next(keyword.value for keyword in service_call.keywords
                                  if keyword.arg == "controller")
            else:
                controller = service_call.args[0]
            self.assertIsInstance(controller, ast.Call)
            self.assertIsInstance(controller.func, ast.Name)
            self.assertEqual("acceptance_provider_recovery_controller", controller.func.id)
        self.assertNotIn("mihomo_controller", ast.unparse(build_app))

    def test_owned_vpn_auto_direct_fault_route_is_fixed_and_readback_bound(self):
        tree = ast.parse(WORKER_PATH.read_text(encoding="utf-8"))
        build_app = next(node for node in tree.body
                         if isinstance(node, ast.FunctionDef) and node.name == "build_app")
        route = next(node for node in ast.walk(build_app)
                     if isinstance(node, ast.FunctionDef)
                     and node.name == "set_owned_vpn_auto_selector_direct")
        self.assertEqual([], route.args.args)
        calls = [node for node in ast.walk(route) if isinstance(node, ast.Call)]
        selector_reads = [call for call in calls if isinstance(call.func, ast.Attribute)
                          and call.func.attr == "get_proxy_state"
                          and call.args and isinstance(call.args[0], ast.Constant)
                          and call.args[0].value == "vpn-auto"]
        self.assertEqual(2, len(selector_reads))
        put = next(call for call in calls if isinstance(call.func, ast.Attribute)
                   and call.func.attr == "_put_json")
        self.assertEqual("/proxies/vpn-auto", put.args[0].value)
        payload = put.args[1]
        self.assertIsInstance(payload, ast.Dict)
        self.assertEqual(["name"], [key.value for key in payload.keys])
        self.assertEqual(["DIRECT"], [value.value for value in payload.values])
        self.assertTrue(any(isinstance(node, ast.Compare)
                            and any(isinstance(comparator, ast.Constant)
                                    and comparator.value == "DIRECT" for comparator in node.comparators)
                            for node in ast.walk(route)))
        self.assertTrue(any(isinstance(node, ast.Constant)
                            and node.value == "/tmp/fwrouter-application-acceptance"
                            for node in ast.walk(route)))
        forbidden_calls = {"db_session", "commit_active_selection", "advance_selection_revision"}
        self.assertFalse(any(isinstance(call.func, ast.Name) and call.func.id in forbidden_calls
                             for call in calls))
        registration = next(call for call in ast.walk(build_app)
                            if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                            and call.func.attr == "add_api_route" and call.args
                            and isinstance(call.args[0], ast.Constant)
                            and call.args[0].value == "/api/v2/__acceptance/selection/runtime-direct")
        methods = next(keyword.value for keyword in registration.keywords if keyword.arg == "methods")
        self.assertEqual(["POST"], [item.value for item in methods.elts])

    def test_provider_bridge_generate_204_supports_mihomo_head_and_get(self):
        import http.client

        bridge = ProviderHttpTestBridge().start()
        try:
            host, port = bridge._server.server_address
            for method in ("HEAD", "GET"):
                connection = http.client.HTTPConnection(host, port, timeout=3)
                try:
                    connection.request(method, "/generate_204")
                    response = connection.getresponse()
                    self.assertEqual(204, response.status)
                    self.assertEqual("0", response.getheader("Content-Length"))
                    self.assertEqual(b"", response.read())
                finally:
                    connection.close()

            bridge.set_probe_available(False)
            for method in ("HEAD", "GET"):
                connection = http.client.HTTPConnection(host, port, timeout=3)
                try:
                    connection.request(method, "/generate_204")
                    with self.assertRaises(http.client.RemoteDisconnected):
                        connection.getresponse()
                finally:
                    connection.close()
            self.assertEqual([204, 204], bridge.snapshot_probe_responses())
            summary = bridge.snapshot_probe_summary()
            self.assertEqual(4, summary["event_count"])
            self.assertEqual(2, summary["transport_closed_count"])
            self.assertEqual([
                {"kind": "http_response", "status": 204},
                {"kind": "http_response", "status": 204},
                {"kind": "transport_closed", "status": None},
                {"kind": "transport_closed", "status": None},
            ], summary["recent_events"])
        finally:
            bridge.close()

    def test_provider_probe_unavailable_closes_transport_without_http_response(self):
        bridge_source = Path(__file__).parents[1] / "application_acceptance" / "joined_support.py"
        tree = ast.parse(bridge_source.read_text(encoding="utf-8"))
        owner = next(node for node in tree.body
                     if isinstance(node, ast.ClassDef) and node.name == "ProviderHttpTestBridge")
        start = next(node for node in owner.body
                     if isinstance(node, ast.FunctionDef) and node.name == "start")
        handler = next(node for node in ast.walk(start)
                       if isinstance(node, ast.ClassDef) and node.name == "Handler")
        handle = next(node for node in handler.body
                      if isinstance(node, ast.FunctionDef) and node.name == "_handle")

        class Bridge:
            def __init__(self):
                self._lock = threading.Lock()
                self._hold_probe_count = 0
                self.probe_requests = 0
                self.probe_available = False
                self.probe_events = []
                self.probe_responses = []
                self.mode = "normal"
                self.calls = []
                self.request_entered = threading.Event()
                self._release_response = threading.Event()

        class Connection:
            def __init__(self):
                self.shutdown_how = []

            def shutdown(self, how):
                self.shutdown_how.append(how)

        class Request:
            def __init__(self, path):
                self.headers = {"Content-Length": "0"}
                self.rfile = io.BytesIO()
                self.path = path
                self.command = "GET"
                self.connection = Connection()
                self.close_connection = False
                self.protocol_calls = []

            def _respond(self, status, payload, headers=None):
                self.protocol_calls.append(("response", status, payload, headers))

            def send_response(self, status):
                self.protocol_calls.append(("send_response", status))

            def send_header(self, name, value):
                self.protocol_calls.append(("send_header", name, value))

            def end_headers(self):
                self.protocol_calls.append(("end_headers",))

            def send_error(self, status, message=None, explain=None):
                self.protocol_calls.append(("send_error", status, message, explain))

        bridge = Bridge()
        namespace = {"__builtins__": __builtins__, "bridge": bridge, "json": json,
                     "urlparse": __import__("urllib.parse", fromlist=["urlparse"]).urlparse,
                     "socket": socket}
        exec(compile(ast.Module(body=[handle], type_ignores=[]), str(bridge_source), "exec"), namespace)
        unavailable = Request("/generate_204")
        namespace["_handle"](unavailable)
        self.assertEqual([], unavailable.protocol_calls)
        self.assertTrue(unavailable.close_connection)
        self.assertEqual([socket.SHUT_RDWR], unavailable.connection.shutdown_how)
        self.assertEqual([], bridge.probe_responses)
        self.assertEqual([{"kind": "transport_closed", "status": None}], bridge.probe_events)
        self.assertEqual(1, bridge.probe_requests)

        for mode, expected_status in (("429", 429), ("503", 503)):
            bridge.mode = mode
            api_request = Request("/configs")
            namespace["_handle"](api_request)
            self.assertEqual([("response", expected_status, {"status": False, "data": None},
                               {"Retry-After": "30"} if mode == "429" else None)],
                             api_request.protocol_calls)

    def test_phase_d_functional_repeat_status_starts_green_and_preserves_failures(self):
        workflow = (Path(__file__).parents[2] / ".github/workflows/phase-d-validation.yml").read_text(
            encoding="utf-8")
        start = workflow.index("      - name: Run all 37 functional application/browser scenarios")
        end = workflow.index("      - name: Aggregate exact full-phase", start)
        step = workflow[start:end]
        self.assertRegex(step, r"(?m)^\s+first=0$")
        self.assertRegex(step, r"(?m)^\s+/usr/bin/time -v python tests/acceptance/launcher\.py --run --suite functional\s*\\\s*$")
        self.assertIn("|| first=$?", step)
        self.assertIn('if [ "$first" -eq 0 ]; then', step)
        self.assertRegex(step, r"(?m)^\s+second=0$")
        self.assertIn("|| second=$?", step)
        self.assertIn('test "$second" -eq 0', step)
        self.assertIn('exit "$first"', step)

    def test_qualified_child_diagnostics_and_mihomo_banner_are_bounded_and_pinned(self):
        generated = qualified_child._preflight_code()
        ast.parse(generated)
        self.assertIn("assert m['version']=='1.19.31'", generated)
        self.assertIn(
            r"r'^(?:1\.19\.31|v1\.19\.31|Mihomo Meta v1\.19\.31)(?:\s|$)'", generated)
        self.assertIn("assert h==m['sha256']", generated)
        self.assertIn("/tmp/fwrouter-qcp-test-root", generated)
        self.assertIn("FWROUTER_GATE_TEST_ROOT_V1", generated)
        self.assertIn("configured_tmpfs=['rw','exec','nosuid','nodev','size=512m','mode=1777']", generated)
        self.assertIn("'mount_type':tmp_type,'mount_source':tmp_source", generated)
        self.assertIn("tmp_type=='tmpfs'", generated)
        self.assertIn("'noexec' not in tmp_options", generated)
        self.assertNotIn("'exec' in tmp_options", generated)
        self.assertIn("shutil').which('jq',path='/usr/bin:/bin')", generated)
        self.assertIn("'jq_version':jq_version.stdout.strip()", generated)
        self.assertIn("/tmp:rw,exec,nosuid,nodev,size=512m,mode=1777",
                      qualified_child.COMPOSE.read_text(encoding="utf-8"))

        qualified_child._validate_tmpfs_options(
            "/tmp:rw,exec,nosuid,nodev,size=512m,mode=1777", compose_entry=True)
        qualified_child._validate_tmpfs_options("rw,exec,nosuid,nodev,size=512m,mode=1777")
        for invalid in (
            "/tmp:rw,nosuid,nodev,size=512m,mode=1777",
            "/tmp:rw,exec,noexec,nosuid,nodev,size=512m,mode=1777",
            "/tmp:rw,exec,suid,nodev,size=512m,mode=1777",
            "/tmp:rw,exec,nodev,size=512m,mode=1777",
            "/tmp:rw,exec,nosuid,nodev,size=256m,mode=1777",
            "/var/tmp:rw,exec,nosuid,nodev,size=512m,mode=1777",
        ):
            with self.subTest(tmpfs=invalid), self.assertRaises(qualified_child.launcher.NotRun):
                qualified_child._validate_tmpfs_options(invalid, compose_entry=True)

        evidence = qualified_child._command_failure_evidence(
            ["/usr/bin/docker", "exec", "container-id", "python", "-c", "print('password=hidden')"],
            exit_code=1, stdout=b'{"api_key":"hidden"}' * 1000,
            stderr=b"Bearer abc.def.ghi 123e4567-e89b-42d3-a456-426614174000 secret@example.org",
            reason="nonzero exit",
        )
        self.assertEqual(1, evidence["exit_code"])
        self.assertEqual("<redacted-argument>", evidence["command"][-1])
        self.assertNotIn("password=hidden", str(evidence["command"]))
        self.assertNotIn("hidden", evidence["stdout"])
        self.assertNotIn("abc.def.ghi", evidence["stderr"])
        self.assertNotIn("123e4567-e89b-42d3-a456-426614174000", evidence["stderr"])
        self.assertNotIn("secret@example.org", evidence["stderr"])
        self.assertLessEqual(len(evidence["stdout"].encode()), qualified_child.DIAGNOSTIC_LIMIT)
        self.assertLessEqual(len(evidence["stderr"].encode()), qualified_child.DIAGNOSTIC_LIMIT)
        failed_command = qualified_child.subprocess.CompletedProcess(
            ["docker", "exec"], 17, b"safe stdout", b"preflight denied: secret=hidden",
        )
        with mock.patch.object(qualified_child.subprocess, "run", return_value=failed_command):
            with self.assertRaises(qualified_child._SmallCommandFailure) as captured:
                qualified_child._small(["docker", "exec", "container-id"], env={})
        self.assertEqual(17, captured.exception.evidence["exit_code"])
        self.assertIn("safe stdout", captured.exception.evidence["stdout"])
        self.assertNotIn("hidden", captured.exception.evidence["stderr"])

    def test_qualified_child_junit_normalizes_only_expected_tests_prefix(self):
        scenarios = json.loads(qualified_child.ROOT.joinpath(
            "tests/acceptance/qualified-child-scenarios.json").read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory(prefix="fwrouter-qualified-child-junit-") as temp:
            path = Path(temp) / "report.xml"
            root = ET.Element("testsuite")
            for nodeid in scenarios["nodeids"]:
                identity, name = nodeid.split("::", 1)
                classname = Path(identity).stem
                ET.SubElement(root, "testcase", {
                    "classname": f"tests.{classname}", "name": name,
                })
            ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)
            observed = qualified_child._read_junit(path)
            self.assertTrue(observed["contract_valid"])
            self.assertEqual(set(scenarios["nodeids"]), set(observed["node_status"]))

            root = ET.Element("testsuite")
            ET.SubElement(root, "testcase", {
                "classname": "foreign.test_xray", "name": "test_writer_guard_serializes_processes",
            })
            ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)
            with self.assertRaises(qualified_child.launcher.NotRun):
                qualified_child._read_junit(path)

    def test_qualified_child_pytest_exec_uses_backend_selector_directory(self):
        source = (qualified_child.ROOT / "tests/acceptance/qualified_child.py").read_text(
            encoding="utf-8")
        self.assertEqual("/workspace/backend", qualified_child.PYTEST_WORKDIR)
        self.assertIn('[docker, "exec", "--workdir", PYTEST_WORKDIR', source)
        self.assertIn('"tests/test_protocol_native_validation.py"', source)
        self.assertIn('"tests/test_xray_default_runner_archive.py"', source)

    def test_qualified_xray_junit_normalizes_only_observed_tests_prefix(self):
        test_name = "test_loaded_client_readback_against_isolated_xray_26_2_6"
        with tempfile.TemporaryDirectory(prefix="fwrouter-qualified-xray-junit-") as temp:
            path = Path(temp) / "report.xml"
            root = ET.Element("testsuite")
            ET.SubElement(root, "testcase", {
                "classname": "tests.test_xray_native_readback", "name": test_name,
            })
            ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)
            observed = qualified_xray_docker._validate_junit(path)
            self.assertTrue(observed["contract_valid"])
            self.assertEqual([qualified_xray_docker.NODE_ID], observed["expected_ids"])

            root = ET.Element("testsuite")
            ET.SubElement(root, "testcase", {"classname": "foreign.test_xray_native_readback", "name": test_name})
            ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)
            self.assertFalse(qualified_xray_docker._validate_junit(path)["contract_valid"])


    def test_native_inbound_users_response_accepts_omitted_empty_field_only(self):
        def reply(payload, *, ok=True):
            return {"ok": ok, "details": {"stdout": json.dumps(payload)}}

        # Pinned Xray 26.2.6 can omit the repeated field for a successful empty response.
        self.assertEqual([], parse_inbound_users_reply(reply({})))
        self.assertEqual([], parse_inbound_users_reply(reply({"users": []})))
        valid_user = {"account": {"_TypedMessage_": "xray.proxy.vless.Account", "id": "id"},
                      "email": "user@example.org"}
        self.assertEqual([valid_user], parse_inbound_users_reply(reply({"users": [valid_user]})))

        for invalid in (
            reply({}, ok=False),
            reply([]),
            reply({"users": None}),
            reply({"users": {}}),
            reply({"error": "unexpected native response"}),
            reply({"users": [], "extra": True}),
            {"ok": True, "details": {"stdout": "{"}},
        ):
            with self.subTest(invalid=invalid), self.assertRaises((AssertionError, ValueError)):
                parse_inbound_users_reply(invalid)


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

    def test_bounded_pipe_retains_available_chunk_before_eof(self):
        class FakePipe:
            def __init__(self):
                self.first_read = True
                self.second_read = threading.Event()
                self.release_eof = threading.Event()

            def read1(self, _size):
                if self.first_read:
                    self.first_read = False
                    return b"live native warning"
                self.second_read.set()
                self.release_eof.wait(timeout=2)
                return b""

            def close(self):
                pass

        fake = FakePipe()
        pipe = native_runner._BoundedPipe(fake)
        try:
            self.assertTrue(fake.second_read.wait(timeout=1))
            self.assertEqual(b"live native warning", pipe.snapshot())
        finally:
            fake.release_eof.set()
            pipe.close()

    def test_native_rpc_output_preserves_identity_while_artifact_redacts_it(self):
        uuid = "123e4567-e89b-42d3-a456-426614174000"
        email = "native-user@example.org"
        stdout = json.dumps({"users": [{"id": uuid, "email": email}]}).encode()
        stderr = b"e" * 5000
        proc = native_runner.subprocess.CompletedProcess([], 0, stdout, stderr)
        native = native_runner.NativeXrayProcess.__new__(native_runner.NativeXrayProcess)
        native.root = Path("/tmp")
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

    def test_provider_verification_observer_redacts_phase_and_reraises(self):
        worker_path = Path(__file__).parents[1] / "application_acceptance" / "worker.py"
        parsed = ast.parse(worker_path.read_text(encoding="utf-8"))
        observer = next(
            node for node in parsed.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "_install_acceptance_provider_verification_observer"
        )
        namespace = {
            "__builtins__": __builtins__, "__package__": "application_acceptance",
            "os": __import__("os"), "sys": sys, "json": json,
            "threading": __import__("threading"), "Any": __import__("typing").Any,
        }
        exec(compile(ast.Module(body=[observer], type_ignores=[]), str(worker_path), "exec"), namespace)

        native_module = ModuleType("application_acceptance.native_runner")
        native_module._redact_diagnostic = native_runner._redact_diagnostic
        failure = RuntimeError("native validation rejected password=private-value for client@example.test")
        phase = {"value": "runtime"}

        class FailingOperations:
            def get_logical_group_state(self, _runtime_name):
                if phase["value"] == "verify":
                    return {"ok": True}
                raise failure

        refresh_result = {
            "ok": False, "stage": "config_validation", "runtime_verified": False,
            "error": {"code": "MIHOMO_CONFIG_VALIDATION_FAILED", "message": "password=private-value"},
            "refresh": {"batch": {"targeted_source_ref": "source-private"}},
            "source_ref": "source-private", "candidate": {"candidate_path": "/private/config.yaml"},
        }
        provider_managed = SimpleNamespace(_refresh_with_material=lambda *_args, **_kwargs: refresh_result)
        runtime_adapters = SimpleNamespace(runtime_adapter_operations=lambda _adapter: FailingOperations())
        server_ping = SimpleNamespace()

        def fake_verify(*_args, **_kwargs):
            if phase["value"] == "runtime":
                return runtime_adapters.runtime_adapter_operations({}).get_logical_group_state("owned")
            if phase["value"] == "probe":
                return server_ping.check_server_delay("owned")
            runtime_adapters.runtime_adapter_operations({}).get_logical_group_state("owned")
            raise failure

        def fail_probe(*_args, **_kwargs):
            raise failure

        provider_managed.verify_provider_handoff = fake_verify
        server_ping.check_server_delay = fail_probe
        services_module = ModuleType("fwrouter_api.services")
        services_module.provider_managed = provider_managed
        services_module.runtime_adapters = runtime_adapters
        services_module.server_ping = server_ping
        observed = io.StringIO()
        with mock.patch.dict(sys.modules, {
            "application_acceptance.native_runner": native_module,
            "fwrouter_api.services": services_module,
        }), mock.patch.dict(__import__("os").environ, {
            "FWROUTER_ENVIRONMENT": "test",
            "FWROUTER_APPLICATION_ACCEPTANCE_ROOT": "/tmp/fwrouter-application-acceptance",
        }), mock.patch("sys.stderr", observed):
            previous_trace = sys.gettrace()
            namespace["_install_acceptance_provider_verification_observer"]()
            returned_refresh = provider_managed._refresh_with_material({"source_ref": "source-private"})
            self.assertIs(refresh_result, returned_refresh)
            with self.assertRaises(RuntimeError) as runtime_error:
                provider_managed.verify_provider_handoff()
            self.assertIs(failure, runtime_error.exception)
            phase["value"] = "probe"
            with self.assertRaises(RuntimeError) as probe_error:
                provider_managed.verify_provider_handoff()
            self.assertIs(failure, probe_error.exception)
            phase["value"] = "verify"
            with self.assertRaises(RuntimeError) as verify_error:
                provider_managed.verify_provider_handoff()
            self.assertIs(failure, verify_error.exception)
            self.assertIs(previous_trace, sys.gettrace())

        diagnostic = observed.getvalue()
        self.assertIn('"phase":"provider_managed.verify_provider_handoff"', diagnostic)
        records = [json.loads(line.split(" ", 1)[1]) for line in diagnostic.splitlines()]
        self.assertEqual(8, len(records))
        self.assertEqual("observer_installed", records[0].get("event"))
        refresh_record = next(record for record in records if record.get("event") == "refresh_result")
        self.assertTrue(refresh_record["targeted_source_match"])
        self.assertEqual("MIHOMO_CONFIG_VALIDATION_FAILED",
                         refresh_record["summary"]["error"]["code"])
        self.assertEqual(3, sum(record.get("event") == "verify_enter" for record in records))
        verify_records = [record for record in records
                          if record.get("phase") == "provider_managed.verify_provider_handoff"]
        self.assertEqual(3, len(verify_records))
        self.assertTrue(all(isinstance(record.get("line"), int) for record in verify_records))
        self.assertIn("RuntimeError", diagnostic)
        self.assertNotIn("private-value", diagnostic)
        self.assertNotIn("client@example.test", diagnostic)
        self.assertIn("[REDACTED]", diagnostic)
        self.assertIn("[EMAIL]", diagnostic)
        self.assertNotIn("source-private", diagnostic)
        self.assertNotIn("/private/config.yaml", diagnostic)

    def test_mihomo_candidate_rpc_uses_owned_default_and_keeps_native_path_guard(self):
        worker_path = Path(__file__).parents[1] / "application_acceptance" / "worker.py"
        parsed = ast.parse(worker_path.read_text(encoding="utf-8"))
        binder = next(node for node in parsed.body
                      if isinstance(node, ast.FunctionDef) and node.name == "_bind_acceptance_mihomo")
        default_candidate_assignment = next(
            node for node in binder.body if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "default_candidate"
                    for target in node.targets)
        )
        candidate_path_guard = next(
            node for node in binder.body if isinstance(node, ast.If)
            and "default_candidate" in ast.dump(node.test)
        )
        validator = next(node for node in binder.body
                         if isinstance(node, ast.FunctionDef) and node.name == "validate_candidate")
        state = Path("/tmp/fwrouter-application-acceptance/state")
        calls = []

        def rpc_call(_socket_path, action, payload):
            calls.append((action, payload))
            return {"ok": True, "details": {}}

        owned_default = str(state / "generated" / "mihomo" / "config.next.yaml")
        namespace = {"__builtins__": __builtins__, "state": state, "_rpc_call": rpc_call,
                     "socket_path": Path("/unused"), "Path": Path,
                     "_resolved_candidate_config_path": lambda: owned_default,
                     "expected_candidate": Path(owned_default).resolve(strict=False)}
        assignment_module = ast.Module(body=[default_candidate_assignment], type_ignores=[])
        exec(compile(assignment_module, str(worker_path), "exec"), namespace)
        guard_module = ast.Module(body=[candidate_path_guard], type_ignores=[])
        exec(compile(guard_module, str(worker_path), "exec"), namespace)
        namespace["_resolved_candidate_config_path"] = lambda: "/outside/config.next.yaml"
        exec(compile(assignment_module, str(worker_path), "exec"), namespace)
        with self.assertRaisesRegex(RuntimeError, "escapes the owned acceptance state"):
            exec(compile(guard_module, str(worker_path), "exec"), namespace)
        namespace["_resolved_candidate_config_path"] = lambda: owned_default
        exec(compile(assignment_module, str(worker_path), "exec"), namespace)
        exec(compile(ast.Module(body=[validator], type_ignores=[]), str(worker_path), "exec"), namespace)
        validate = namespace["validate_candidate"]
        validate()
        self.assertEqual(("mihomo_test_config", {"path": owned_default}), calls[0])
        explicit = "/outside/explicit-candidate.yaml"
        validate(explicit)
        self.assertEqual(("mihomo_test_config", {"path": explicit}), calls[1])

        with tempfile.TemporaryDirectory(prefix="fwrouter-mihomo-path-contract-") as temp:
            base = Path(temp)
            owned_root = base / "owned"
            owned_root.mkdir()
            foreign_candidate = base / "foreign.yaml"
            foreign_candidate.write_text("candidate", encoding="utf-8")
            native = native_runner.NativeXrayProcess.__new__(native_runner.NativeXrayProcess)
            native.root = owned_root
            native._guard = __import__("threading").Lock()
            native._hold_action = None
            native._fail_action = None
            with self.assertRaisesRegex(ValueError, "outside acceptance state"):
                native._dispatch({"action": "mihomo_test_config", "payload": {"path": str(foreign_candidate)}})

    def test_generation_callback_observer_records_bounded_redacted_exception_and_reraises(self):
        worker_path = Path(__file__).parents[1] / "application_acceptance" / "worker.py"
        parsed = ast.parse(worker_path.read_text(encoding="utf-8"))
        observer = next(node for node in parsed.body
                        if isinstance(node, ast.FunctionDef)
                        and node.name == "_install_acceptance_generation_callback_observer")
        namespace = {
            "__builtins__": __builtins__, "__package__": "application_acceptance",
            "os": __import__("os"), "sys": sys, "json": json,
            "threading": __import__("threading"), "Path": Path,
            "Any": __import__("typing").Any,
        }
        exec(compile(ast.Module(body=[observer], type_ignores=[]), str(worker_path), "exec"), namespace)

        native_module = ModuleType("application_acceptance.native_runner")
        native_module._redact_diagnostic = native_runner._redact_diagnostic
        failure = RuntimeError("generation callback failed token=private-value for client@example.test")

        def invoke(callback, *, operation_id, expected_revision):
            return callback(operation_id=operation_id, expected_selection_revision=expected_revision)

        mihomo_reconcile = SimpleNamespace(_invoke_verification_callback=invoke)
        services_module = ModuleType("fwrouter_api.services")
        services_module.mihomo_reconcile = mihomo_reconcile
        observed = io.StringIO()

        def verify_callback(**_kwargs):
            raise failure

        selector_result = {
            "ok": False,
            "status": "provider_target_unconfirmed",
            "selector": {
                "error_code": "VPN_AUTO_SELECTION_RUNTIME_IDENTITY_UNAVAILABLE",
                "selection_outcome": "deferred",
                "selection_basis": "on-demand successful latency check",
                "reason": "subscription_refresh_auto_select",
                "applied": False,
                "candidates_count": 1,
                "selection_revision": 9,
                "runtime_adapter_id": "mihomo",
                "on_demand": {
                    "checked_count": 1, "success_count": 0, "failed_count": 1,
                    "results": [{
                        "status": "failed", "error_code": "PROBE_TIMEOUT",
                        "error_message": "probe for user@example.test UUID 88c7ce2a-465e-4e72-9c56-2a9e2fc84a51 failed",
                        "server_id": "private-logical-id", "runtime_target": "private-target",
                    }],
                },
                "selected_server_id": "private-logical-id",
            },
        }

        def return_selector_result(**_kwargs):
            return selector_result

        with mock.patch.dict(sys.modules, {
            "application_acceptance.native_runner": native_module,
            "fwrouter_api.services": services_module,
        }), mock.patch.dict(__import__("os").environ, {
            "FWROUTER_ENVIRONMENT": "test",
            "FWROUTER_APPLICATION_ACCEPTANCE_ROOT": "/tmp/fwrouter-application-acceptance",
        }), mock.patch("sys.stderr", observed):
            namespace["_install_acceptance_generation_callback_observer"]()
            with self.assertRaises(RuntimeError) as raised:
                mihomo_reconcile._invoke_verification_callback(
                    verify_callback, operation_id="private-operation", expected_revision=7,
                )
            returned = mihomo_reconcile._invoke_verification_callback(
                return_selector_result, operation_id="private-operation", expected_revision=7,
            )
            self.assertIs(selector_result, returned)
        self.assertIs(failure, raised.exception)
        diagnostic = observed.getvalue()
        self.assertNotIn("private-value", diagnostic)
        self.assertNotIn("client@example.test", diagnostic)
        self.assertNotIn("private-operation", diagnostic)
        self.assertNotIn("private-logical-id", diagnostic)
        records = [json.loads(line.split(" ", 1)[1]) for line in diagnostic.splitlines()]
        self.assertEqual(4, len(records))
        self.assertEqual("callback_enter", records[0]["event"])
        self.assertEqual("callback_exception", records[1]["event"])
        self.assertEqual("verify_callback", records[1]["callback"])
        self.assertEqual("RuntimeError", records[1]["exception_type"])
        self.assertTrue(any(frame["function"] == "verify_callback" and isinstance(frame["line"], int)
                            for frame in records[1]["frames"]))
        self.assertEqual("callback_result", records[3]["event"])
        self.assertEqual("provider_target_unconfirmed", records[3]["status"])
        self.assertEqual("VPN_AUTO_SELECTION_RUNTIME_IDENTITY_UNAVAILABLE",
                         records[3]["selector"]["error_code"])
        self.assertEqual({"checked_count": 1, "success_count": 0, "failed_count": 1},
                         {key: value for key, value in records[3]["selector"]["on_demand"].items()
                          if key != "results"})
        result_rows = records[3]["selector"]["on_demand"]["results"]
        self.assertEqual(1, len(result_rows))
        self.assertEqual("failed", result_rows[0]["status"])
        self.assertEqual("PROBE_TIMEOUT", result_rows[0]["error_code"])
        self.assertNotIn("user@example.test", result_rows[0]["error_message"])
        self.assertNotIn("88c7ce2a-465e-4e72-9c56-2a9e2fc84a51", result_rows[0]["error_message"])
        self.assertNotIn("private-target", diagnostic)
        self.assertIn("[EMAIL]", result_rows[0]["error_message"])
        self.assertIn("[UUID]", result_rows[0]["error_message"])

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

    def test_native_provider_upstream_summary_contains_only_fixture_presence_counts(self):
        summarize = native_runner.NativeXrayProcess._provider_upstream_summary
        value = summarize(json.dumps({"inbounds": [{
            "tag": "acceptance-provider-upstream",
            "settings": {"clients": [{
                "id": "88c7ce2a-465e-4e72-9c56-2a9e2fc84a51",
                "email": "acceptance-provider-upstream",
            }]},
        }]}).encode())
        self.assertEqual({
            "inbound_present": True, "client_count": 1, "fixture_identity_present": True,
        }, value)
        self.assertNotIn("88c7ce2a-465e-4e72-9c56-2a9e2fc84a51", json.dumps(value))
        self.assertNotIn("acceptance-provider-upstream", json.dumps(value))

    def test_acceptance_upstream_direct_rule_is_unique_and_preserved_by_generation(self):
        fixture_path = Path(__file__).parents[1] / "application_acceptance" / "fixtures" / "xray.initial.json"
        fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
        rules = fixture["routing"]["rules"]
        upstream_rule = {
            "type": "field", "inboundTag": ["acceptance-provider-upstream"], "outboundTag": "direct",
        }
        self.assertEqual(1, rules.count(upstream_rule))
        api_rule = {"type": "field", "inboundTag": ["fwrouter-api"], "outboundTag": "fwrouter-api"}
        self.assertEqual(1, rules.count(api_rule))
        self.assertFalse(any(
            rule.get("inboundTag") == ["vless-ws"] for rule in rules if isinstance(rule, dict)
        ))

        adapter_path = Path(__file__).parents[2] / "backend" / "fwrouter_api" / "adapters" / "xray_real.py"
        source = ast.parse(adapter_path.read_text(encoding="utf-8"))
        adapter_class = next(node for node in source.body
                             if isinstance(node, ast.ClassDef) and node.name == "RealXrayAdapter")
        method = next(node for node in adapter_class.body
                      if isinstance(node, ast.FunctionDef) and node.name == "_is_managed_rule")
        namespace = {
            "Any": __import__("typing").Any,
            "XRAY_API_TAG": "fwrouter-api",
            "XRAY_FALLBACK_OUTBOUND_TAG": "blocked-until-fwrouter-dataplane",
            "XRAY_EXPLICIT_DIRECT_OUTBOUND_TAG": "fwrouter-explicit-direct",
            "XRAY_MANAGED_DNS_OUTBOUND_TAG": "fwrouter-dns-out",
            "XRAY_MANAGED_EGRESS_PREFIX": "fwrouter-egress-",
            "XRAY_INBOUND_TAG": "vless-ws",
        }
        exec(compile(ast.Module(body=[method], type_ignores=[]), str(adapter_path), "exec"), namespace)
        self.assertFalse(namespace["_is_managed_rule"](SimpleNamespace(), upstream_rule))
        self.assertTrue(namespace["_is_managed_rule"](SimpleNamespace(), api_rule))

    def test_mihomo_delay_error_observer_redacts_body_and_preserves_raise(self):
        worker_path = Path(__file__).parents[1] / "application_acceptance" / "worker.py"
        parsed = ast.parse(worker_path.read_text(encoding="utf-8"))
        observer = next(node for node in parsed.body
                        if isinstance(node, ast.FunctionDef)
                        and node.name == "_install_acceptance_mihomo_delay_error_observer")

        class FakeURL:
            def __init__(self, host, port, path):
                self.host, self.port, self.path = host, port, path

        class FakeResponse:
            def __init__(self, url, status, content, failure=None):
                self.request = SimpleNamespace(url=url)
                self.status_code = status
                self.content = content
                self.failure = failure

            def raise_for_status(self):
                if self.failure is not None:
                    raise self.failure

        httpx_stub = SimpleNamespace(Response=FakeResponse)
        native_module = ModuleType("application_acceptance.native_runner")
        native_module._redact_diagnostic = native_runner._redact_diagnostic
        namespace = {
            "__builtins__": __builtins__, "__package__": "application_acceptance",
            "os": __import__("os"), "sys": sys, "json": json,
            "threading": __import__("threading"), "Any": __import__("typing").Any,
        }
        exec(compile(ast.Module(body=[observer], type_ignores=[]), str(worker_path), "exec"), namespace)
        original_method = FakeResponse.raise_for_status
        failure = RuntimeError("same Mihomo HTTP failure")
        body = json.dumps({"message": "dial failed https://private.example/target user@example.test 127.0.0.1:34607"}).encode()
        owned = FakeResponse(FakeURL("127.0.0.1", 5200, "/group/secret-provider/delay"), 504, body, failure)
        foreign_error = RuntimeError("foreign response")
        foreign = FakeResponse(FakeURL("127.0.0.1", 5201, "/group/secret-provider/delay"), 504, body, foreign_error)
        observed = io.StringIO()
        try:
            with mock.patch.dict(sys.modules, {
                "application_acceptance.native_runner": native_module,
                "httpx": httpx_stub,
            }), mock.patch.dict(__import__("os").environ, {
                "FWROUTER_ENVIRONMENT": "test",
                "FWROUTER_APPLICATION_ACCEPTANCE_ROOT": "/tmp/fwrouter-application-acceptance",
            }), mock.patch("sys.stderr", observed):
                namespace["_install_acceptance_mihomo_delay_error_observer"]()
                with self.assertRaises(RuntimeError) as raised:
                    owned.raise_for_status()
                self.assertIs(failure, raised.exception)
                with self.assertRaises(RuntimeError) as foreign_raised:
                    foreign.raise_for_status()
                self.assertIs(foreign_error, foreign_raised.exception)
        finally:
            FakeResponse.raise_for_status = original_method

        records = [json.loads(line.split(" ", 1)[1]) for line in observed.getvalue().splitlines()]
        self.assertEqual(1, len(records))
        self.assertEqual(504, records[0]["status"])
        self.assertEqual("group_delay", records[0]["path_category"])
        diagnostic = json.dumps(records[0])
        for private in ("private.example", "/target", "user@example.test", "127.0.0.1:34607", "secret-provider"):
            self.assertNotIn(private, diagnostic)
        self.assertIn("[URL]", records[0]["error_message"])
        self.assertIn("[EMAIL]", records[0]["error_message"])
        self.assertIn("[ENDPOINT]", records[0]["error_message"])

    def test_mihomo_delay_samples_are_bounded_and_first_503_snapshots_proxy_state(self):
        worker_path = Path(__file__).parents[1] / "application_acceptance" / "worker.py"
        parsed = ast.parse(worker_path.read_text(encoding="utf-8"))
        observer = next(node for node in parsed.body
                        if isinstance(node, ast.FunctionDef)
                        and node.name == "_install_acceptance_mihomo_delay_error_observer")

        class FakeURL:
            def __init__(self, host, port, path, scheme="http"):
                self.host, self.port, self.path, self.scheme = host, port, path, scheme

            def copy_with(self, *, path, query):
                self.query = query
                return FakeURL(self.host, self.port, path, self.scheme)

        class FakeResponse:
            def __init__(self, url, status, content, failure=None, headers=None):
                self.request = SimpleNamespace(url=url, headers=headers or {})
                self.status_code = status
                self.content = content
                self.failure = failure

            def raise_for_status(self):
                if self.failure is not None:
                    raise self.failure

        state_body = json.dumps({
            "alive": True,
            "history": [{"time": "2026-10-09T13:20:00.000Z", "delay": 0}],
            "name": "secret-member-name",
            "id": "secret-runtime-id",
        }).encode()
        snapshot_response = FakeResponse(
            FakeURL("127.0.0.1", 5200, "/proxies/secret-member-name"), 200, state_body,
        )
        diagnostic_gets = []

        class FakeClient:
            def __init__(self, *, timeout, trust_env):
                self.timeout, self.trust_env = timeout, trust_env

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def get(self, url, *, headers):
                diagnostic_gets.append((url, headers, self.timeout, self.trust_env))
                return snapshot_response

        httpx_stub = SimpleNamespace(Response=FakeResponse, Client=FakeClient)
        native_module = ModuleType("application_acceptance.native_runner")
        native_module._redact_diagnostic = native_runner._redact_diagnostic
        namespace = {
            "__builtins__": __builtins__, "__package__": "application_acceptance",
            "os": __import__("os"), "sys": sys, "json": json,
            "threading": __import__("threading"), "Any": __import__("typing").Any,
        }
        exec(compile(ast.Module(body=[observer], type_ignores=[]), str(worker_path), "exec"), namespace)
        original_method = FakeResponse.raise_for_status
        request_headers = {"authorization": "synthetic-secret"}
        failed = RuntimeError("original 503")
        observed = io.StringIO()
        try:
            with mock.patch.dict(sys.modules, {
                "application_acceptance.native_runner": native_module,
                "httpx": httpx_stub,
            }), mock.patch.dict(__import__("os").environ, {
                "FWROUTER_ENVIRONMENT": "test",
                "FWROUTER_APPLICATION_ACCEPTANCE_ROOT": "/tmp/fwrouter-application-acceptance",
            }), mock.patch("sys.stderr", observed):
                namespace["_install_acceptance_mihomo_delay_error_observer"]()
                samples = [
                    FakeResponse(FakeURL("127.0.0.1", 5200, "/group/secret-group/delay"), 200,
                                 json.dumps({"secret-uuid-alias": 0, "private-member-name": 1}).encode()),
                    FakeResponse(FakeURL("127.0.0.1", 5200, "/proxies/private-proxy/delay"), 200,
                                 json.dumps({"delay": 3}).encode()),
                    FakeResponse(FakeURL("127.0.0.1", 5200, "/group/another-private-group/delay"), 200,
                                 json.dumps({"other-secret-member": 4}).encode()),
                    FakeResponse(FakeURL("127.0.0.1", 5200, "/group/fourth-private-group/delay"), 200,
                                 json.dumps({"member-five": 5}).encode()),
                    FakeResponse(FakeURL("127.0.0.1", 5200, "/group/fifth-private-group/delay"), 200,
                                 json.dumps({"member-six": 6}).encode()),
                ]
                for response in samples:
                    response.raise_for_status()
                failure = FakeResponse(
                    FakeURL("127.0.0.1", 5200, "/proxies/secret-member-name/delay"), 503,
                    b'{"message":"generic delay failure"}', failure=failed, headers=request_headers,
                )
                with self.assertRaises(RuntimeError) as raised:
                    failure.raise_for_status()
                self.assertIs(failed, raised.exception)
                second_failure = FakeResponse(
                    FakeURL("127.0.0.1", 5200, "/proxies/other-private-member/delay"), 503,
                    b'{"message":"generic delay failure"}', failure=RuntimeError("second 503"),
                    headers=request_headers,
                )
                with self.assertRaisesRegex(RuntimeError, "second 503"):
                    second_failure.raise_for_status()
        finally:
            FakeResponse.raise_for_status = original_method

        lines = observed.getvalue().splitlines()
        samples_out = [json.loads(line.split(" ", 1)[1]) for line in lines
                       if line.startswith("FWROUTER_ACCEPTANCE_MIHOMO_DELAY_SAMPLE ")]
        errors_out = [json.loads(line.split(" ", 1)[1]) for line in lines
                      if line.startswith("FWROUTER_ACCEPTANCE_MIHOMO_DELAY_ERROR ")]
        states_out = [json.loads(line.split(" ", 1)[1]) for line in lines
                      if line.startswith("FWROUTER_ACCEPTANCE_MIHOMO_PROXY_STATE ")]
        self.assertEqual([0, 1], samples_out[0]["delays_ms"])
        self.assertEqual(3, samples_out[1]["delay_ms"])
        self.assertEqual([4], samples_out[2]["delays_ms"])
        self.assertEqual([5], samples_out[3]["delays_ms"])
        self.assertEqual(4, len(samples_out))
        self.assertTrue(all(row["status"] == 200 for row in samples_out))
        self.assertEqual(2, len(errors_out))
        self.assertEqual(1, len(states_out))
        self.assertEqual({
            "path_category": "proxy_state", "status": 200, "alive": True,
            "latest_history_delay_ms": 0,
        }, {key: states_out[0][key] for key in (
            "path_category", "status", "alive", "latest_history_delay_ms",
        )})
        self.assertIn("snapshot_requested_at_utc", states_out[0])
        self.assertIn("latest_history_time_utc", states_out[0])
        self.assertIn("history_age_at_snapshot_ms", states_out[0])
        self.assertEqual(1, len(diagnostic_gets))
        url, headers, timeout, trust_env = diagnostic_gets[0]
        self.assertEqual("/proxies/secret-member-name", url.path)
        self.assertEqual(request_headers, headers)
        self.assertEqual(1.5, timeout)
        self.assertFalse(trust_env)
        private = "\n".join(lines)
        for secret in ("secret-member-name", "secret-uuid-alias", "private-member-name",
                       "another-private-group", "other-secret-member", "secret-runtime-id",
                       "synthetic-secret"):
            self.assertNotIn(secret, private)

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
            observed, expanded_bytes = provision.make_chromium_bundle(source, target)
            self.assertEqual(hashlib.sha256(target.read_bytes()).hexdigest(), observed)
            self.assertEqual(len(b"browser executable fixture") + len(b"browser resource fixture"), expanded_bytes)
            executable_sha256, executable_bytes = provision.chromium_executable_digest(target)
            self.assertEqual(hashlib.sha256(b"browser executable fixture").hexdigest(), executable_sha256)
            self.assertEqual(len(b"browser executable fixture"), executable_bytes)
            with tarfile.open(target, "r:") as bundle:
                self.assertEqual({"chrome-linux64", "chrome-linux64/chrome", "chrome-linux64/resources.pak"},
                                 set(bundle.getnames()))
                self.assertTrue(bundle.getmember("chrome-linux64/chrome").mode & 0o111)

    def test_provision_asset_measurements_are_bounded_numeric_and_path_free(self):
        measurements = {
            "xray": {"source_archive_bytes": 100, "executable_bytes": 200,
                     "download_seconds": 0.25, "extract_seconds": 0.1},
            "mihomo": {"source_archive_bytes": 110, "executable_bytes": 210,
                       "download_seconds": 0.3, "extract_seconds": 0.12},
            "chromium": {"source_archive_bytes": 120, "normalized_bundle_bytes": 130,
                         "expanded_content_bytes": 140, "chrome_executable_bytes": 150,
                         "download_seconds": 0.4, "normalize_seconds": 0.2},
        }
        self.assertTrue(provision.valid_asset_measurements(measurements))
        self.assertNotIn("https://", json.dumps(measurements))
        self.assertNotIn("/tmp/", json.dumps(measurements))
        for asset, field, value in (
            ("xray", "source_archive_bytes", True),
            ("mihomo", "extract_seconds", float("nan")),
            ("chromium", "chrome_executable_bytes", -1),
        ):
            invalid = json.loads(json.dumps(measurements))
            invalid[asset][field] = value
            with self.subTest(asset=asset, field=field):
                self.assertFalse(provision.valid_asset_measurements(invalid))
        invalid = json.loads(json.dumps(measurements))
        invalid["xray"]["source_url"] = "https://assets.example.invalid/private"
        self.assertFalse(provision.valid_asset_measurements(invalid))

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
                              "def test_crash(): pass\n"
                              "@pytest.mark.packet\n"
                              "def test_packet(): pass\n")
            rows = module.collect_source_nodes(root)
            self.assertEqual(["test_contract[apply]", "test_contract[persist]", "test_crash", "test_packet"],
                             [row["nodeid"].split("::")[1] for row in rows])
            self.assertEqual(["functional", "functional", "recovery", "packet"], [row["suite"] for row in rows])
            self.assertEqual("L3", rows[-1]["level"])
            sample.write_text("@pytest.mark.packet\n@pytest.mark.l7\ndef test_conflicting(): pass\n")
            with self.assertRaisesRegex(module.CatalogError, "exclusive"):
                module.collect_source_nodes(root)
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
        kernel_code = launcher.kernel_preflight_code()
        compile(kernel_code, "acceptance-kernel-preflight.py", "exec")
        runtime_code = launcher.runtime_preflight_code()
        self.assertIn("hosted-kernel-dataplane", runtime_code)
        self.assertIn("os.geteuid() == 0 and os.getegid() == 0", runtime_code)
        self.assertIn('[nft,"-c","add","rule"', kernel_code)
        self.assertIn('initial_objects == final_objects', kernel_code)
        self.assertIn('("inet", table) not in final_tables', kernel_code)
        compose_args = ["-f", "/suite/compose.yaml", "-f", "/suite/compose.packet.yaml"]
        services = {"router": "application", "client": "lanclient", "endpoint": "endpoint"}
        for role, service in services.items():
            with self.subTest(role=role):
                self.assertEqual(
                    ["/usr/bin/docker", "compose", *compose_args, "-p", "owned-project",
                     "ps", "--all", "-q", service],
                    launcher.compose_service_lookup_argv(
                        "/usr/bin/docker", compose_args, "owned-project", service),
                )
        with self.assertRaises(launcher.NotRun):
            launcher.compose_service_lookup_argv("docker", compose_args, "owned-project", "other")
        source = LAUNCHER_PATH.read_text(encoding="utf-8")
        self.assertIn("hosted-kernel-preflight", source)

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

    def test_provider_diagnostic_is_one_fixed_functional_node(self):
        nodeid = (
            "tests/application_acceptance/test_core_provider_mihomo.py::"
            "test_core_subscription_provider_discovery_exclusive_intent_and_real_mihomo_child"
        )
        self.assertEqual({nodeid}, launcher.expected_acceptance_nodeids("provider-diagnostic"))
        with self.assertRaises(launcher.NotRun):
            launcher.expected_acceptance_nodeids("provider-diagnostic-anything")
        failed_diagnostic = [{"nodeid": nodeid, "status": "failed",
                             "phases": {"setup": "passed", "call": "failed", "teardown": "passed"}}]
        launcher.validate_suite_node_receipt(failed_diagnostic, "provider-diagnostic", [nodeid])
        source = LAUNCHER_PATH.read_text(encoding="utf-8")
        self.assertIn('"provider-diagnostic": "tests/application_acceptance/test_core_provider_mihomo.py::', source)
        tree = ast.parse(source, filename=str(LAUNCHER_PATH))
        suite_choices = [
            ast.literal_eval(keyword.value)
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "add_argument"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == "--suite"
            for keyword in node.keywords if keyword.arg == "choices"
        ]
        self.assertEqual([("functional", "recovery", "xray-diagnostic", "provider-diagnostic",
                           "browser-diagnostic", "provider-cohort", "fence-diagnostic", "target-diagnostic",
                           "recovery-diagnostic", "kernel-preflight", "kernel-recovery-diagnostic",
                           "packet-diagnostic")], suite_choices)

    def test_recovery_diagnostic_is_exactly_the_two_fixed_recovery_nodes(self):
        expected = {
            "tests/application_acceptance/test_core_provider_mihomo.py::test_confirmed_provider_failure_applies_emergency_direct_and_failed_reentry_stays_direct",
            "tests/application_acceptance/test_core_provider_mihomo.py::test_provider_handoff_rejects_stale_selection_after_real_selector_wins_probe_race",
        }
        self.assertEqual(expected, launcher.expected_acceptance_nodeids("recovery-diagnostic"))
        with self.assertRaises(launcher.NotRun):
            launcher.expected_acceptance_nodeids("recovery-diagnostic-extra")
        failed = [{"nodeid": nodeid, "status": "failed",
                   "phases": {"setup": "passed", "call": "failed", "teardown": "passed"}}
                  for nodeid in sorted(expected)]
        launcher.validate_suite_node_receipt(failed, "recovery-diagnostic", sorted(expected))
        skipped = [dict(row, status="skipped") for row in failed]
        with self.assertRaises(launcher.NotRun):
            launcher.validate_suite_node_receipt(skipped, "recovery-diagnostic", sorted(expected))

        launcher_source = LAUNCHER_PATH.read_text(encoding="utf-8")
        for nodeid in expected:
            self.assertIn(nodeid, launcher_source)
        workflow = (Path(__file__).parents[2] / ".github/workflows/phase-d-validation.yml").read_text(
            encoding="utf-8")
        self.assertIn('"ci:validate-recovery": "recovery-diagnostic"', workflow)
        self.assertIn('if [[ "$VALIDATION_STAGE" == recovery-diagnostic ]]; then suite=recovery-diagnostic; fi', workflow)

        worker_source = WORKER_PATH.read_text(encoding="utf-8")
        worker_tree = ast.parse(worker_source, filename=str(WORKER_PATH))
        observer = next(node for node in worker_tree.body
                        if isinstance(node, ast.FunctionDef)
                        and node.name == "_install_acceptance_recovery_apply_observer")
        observer_source = ast.get_source_segment(worker_source, observer) or ""
        for required in ("FWROUTER_ENVIRONMENT", "FWROUTER_APPLICATION_ACCEPTANCE_ROOT",
                         "enter_emergency_direct", "_apply_override_under_policy",
                         "_run_pipeline_for_state", "select_vpn_auto_server",
                         "override_pipeline_caught_exception", "exception_type", "limit = 32",
                         "The real Core apply result nests its bounded taxonomy under dataplane.",
                         'dataplane.get("error_code")', 'for key in ("stage", "error_stage")',
                         '"dataplane_capability", "enforcement_level"'):
            self.assertIn(required, observer_source)
        self.assertNotIn("str(exc)", observer_source)
        self.assertNotIn("error_message", observer_source)

    def test_packet_diagnostic_is_one_fixed_l3_node_and_is_registered(self):
        nodeid = (
            "tests/application_acceptance/test_packet_dataplane.py::"
            "test_core_vpn_emergency_direct_reentry_forwards_owned_tcp_udp_dns_without_leaks"
        )
        self.assertEqual({nodeid}, launcher.expected_acceptance_nodeids("packet-diagnostic"))
        with self.assertRaises(launcher.NotRun):
            launcher.expected_acceptance_nodeids("packet-diagnostic-extra")
        failed = [{"nodeid": nodeid, "status": "failed",
                   "phases": {"setup": "passed", "call": "failed", "teardown": "passed"}}]
        launcher.validate_suite_node_receipt(failed, "packet-diagnostic", [nodeid])
        skipped = [dict(failed[0], status="skipped")]
        with self.assertRaises(launcher.NotRun):
            launcher.validate_suite_node_receipt(skipped, "packet-diagnostic", [nodeid])
        row = next(item for item in launcher.expected_acceptance_nodeids("packet-diagnostic"))
        self.assertEqual(nodeid, row)
        workflow = (Path(__file__).parents[2] / ".github/workflows/phase-d-validation.yml").read_text(
            encoding="utf-8")
        self.assertIn('"ci:validate-packet": "packet-diagnostic"', workflow)
        self.assertIn('if [[ "$VALIDATION_STAGE" == packet-diagnostic ]]; then suite=packet-diagnostic; fi', workflow)

    def test_browser_diagnostic_is_one_fixed_functional_node_and_rejects_skips(self):
        nodeid = (
            "tests/application_acceptance/test_browser_locale.py::"
            "test_real_chromium_xray_client_editor_uses_api_jobs_and_native_readback"
        )
        self.assertEqual({nodeid}, launcher.expected_acceptance_nodeids("browser-diagnostic"))
        with self.assertRaises(launcher.NotRun):
            launcher.expected_acceptance_nodeids("browser-diagnostic-anything")
        failed_diagnostic = [{"nodeid": nodeid, "status": "failed",
                             "phases": {"setup": "passed", "call": "failed", "teardown": "passed"}}]
        launcher.validate_suite_node_receipt(failed_diagnostic, "browser-diagnostic", [nodeid])
        skipped_diagnostic = [{"nodeid": nodeid, "status": "skipped",
                               "phases": {"setup": "passed", "call": "skipped", "teardown": "passed"}}]
        with self.assertRaises(launcher.NotRun):
            launcher.validate_suite_node_receipt(skipped_diagnostic, "browser-diagnostic", [nodeid])
        source = LAUNCHER_PATH.read_text(encoding="utf-8")
        self.assertIn('"browser-diagnostic": "tests/application_acceptance/test_browser_locale.py::', source)
        workflow = (Path(__file__).parents[2] / ".github/workflows/phase-d-validation.yml").read_text(
            encoding="utf-8")
        self.assertIn('"ci:validate-browser": "browser"', workflow)
        self.assertIn('if [[ "$VALIDATION_STAGE" == browser ]]; then suite=browser-diagnostic; fi', workflow)

    def test_target_diagnostic_is_exact_fence_and_browser_union_and_rejects_skips(self):
        expected = {
            "tests/application_acceptance/test_core_provider_mihomo.py::test_confirmed_recovery_typed_provider_api_errors_are_unknown_not_member_down[recovery-unavailable]",
            "tests/application_acceptance/test_xray_generation.py::test_real_core_selection_cas_miss_reconciles_after_native_readback_without_provider_retry",
            "tests/application_acceptance/test_browser_locale.py::test_real_chromium_xray_client_editor_uses_api_jobs_and_native_readback",
        }
        self.assertEqual(expected, launcher.expected_acceptance_nodeids("target-diagnostic"))
        with self.assertRaises(launcher.NotRun):
            launcher.expected_acceptance_nodeids("target-diagnostic-extra")
        failed = [{"nodeid": nodeid, "status": "failed",
                   "phases": {"setup": "passed", "call": "failed", "teardown": "passed"}}
                  for nodeid in expected]
        launcher.validate_suite_node_receipt(failed, "target-diagnostic", sorted(expected))
        skipped = [dict(row, status="skipped") for row in failed]
        with self.assertRaises(launcher.NotRun):
            launcher.validate_suite_node_receipt(skipped, "target-diagnostic", sorted(expected))

        workflow = (Path(__file__).parents[2] / ".github/workflows/phase-d-validation.yml").read_text(
            encoding="utf-8")
        self.assertIn('"ci:validate-targets": "targets"', workflow)
        self.assertIn('if [[ "$VALIDATION_STAGE" == targets ]]; then suite=target-diagnostic; fi', workflow)
        self.assertIn("if: needs.select-stage.outputs.stage == 'targets'", workflow)
        self.assertIn("needs: [select-stage, minimal-xray-diagnostic, target-readmodel-fixtures]", workflow)

        fixture_nodes = {
            "tests/test_ui_state.py::test_ui_readmodels_share_subject_health_from_projection",
            "tests/test_ui_state.py::test_list_ui_clients_includes_traffic_and_filters_internal_xray",
            "tests/test_ui_state.py::test_system_visibility_filters_ui_clients_and_inventory",
            "tests/test_ui_state.py::test_ui_settings_inventory_is_loaded_separately",
            "tests/test_ui_state.py::test_ui_settings_inventory_external_client_exposes_subscription_url",
            "tests/test_ui_state.py::test_xray_subscription_profiles_are_grouped_by_client",
            "tests/test_ui_state.py::test_disabled_xray_subscription_profile_remains_visible_with_separate_runtime_state",
            "tests/test_ui_state.py::test_opaque_xray_subscription_profile_nodes_are_hidden",
            "tests/test_ui_state.py::test_subscription_display_name_different_from_token_stays_in_inventory",
            "tests/test_ui_state.py::test_list_ui_clients_reuses_cached_traffic_and_effective_state",
            "tests/test_runtime_summary.py::test_runtime_summary_does_not_probe_external_ingress_without_connection",
            "tests/test_runtime_summary.py::test_runtime_summary_exposes_dataplane_capability",
            "tests/test_runtime_summary.py::test_runtime_summary_includes_scoped_egress_diagnostics",
            "tests/test_runtime_summary.py::test_system_summary_reports_runtime_status_instead_of_skeleton",
            "tests/test_runtime_summary.py::test_system_summary_uses_external_ingress_taxonomy_names",
            "tests/test_runtime_summary.py::test_runtime_summary_uses_persisted_subscription_state",
            "tests/test_runtime_summary.py::test_runtime_summary_exposes_automation_flags",
            "tests/test_isolation_bootstrap.py::test_isolated_host_observations_keep_external_source_probe_unavailable",
        }
        self.assertEqual(18, len(fixture_nodes))
        self.assertTrue(all(nodeid in workflow for nodeid in fixture_nodes))

    def test_zero_stage_is_exact_mihomo_unit_nodes_without_native_launcher(self):
        expected = {
            "tests/test_mihomo_adapter.py::test_mihomo_zero_history_is_healthy_only_when_native_alive_and_well_formed",
            "tests/test_mihomo_adapter.py::test_mihomo_group_probe_accepts_zero_and_preserves_last_good_for_invalid_samples",
            "tests/test_mihomo_adapter.py::test_mihomo_member_zero_503_requires_fresh_exact_url_state_readback",
            "tests/test_mihomo_adapter.py::test_mihomo_member_zero_503_without_exact_fresh_proof_stays_failed",
            "tests/test_mihomo_adapter.py::test_mihomo_delay_zero_fallback_does_not_probe_on_other_errors",
        }
        workflow = (Path(__file__).parents[2] / ".github/workflows/phase-d-validation.yml").read_text(
            encoding="utf-8")
        self.assertIn('"ci:validate-zero": "zero"', workflow)
        self.assertIn("if: needs.select-stage.outputs.stage == 'zero'", workflow)
        self.assertIn("python tests/gates/run_exact_nodes.py", workflow)
        for nodeid in expected:
            self.assertIn(nodeid, workflow)
        self.assertEqual(5, len(expected))
        zero_step_start = workflow.index("Run fixed Mihomo zero-delay contract nodes")
        zero_step_end = workflow.find("      - name:", zero_step_start)
        zero_step = workflow[zero_step_start:] if zero_step_end < 0 else workflow[zero_step_start:zero_step_end]
        self.assertNotIn("launcher.py --run", zero_step)

    def test_image_measurements_require_bounded_types_and_exclude_paths(self):
        measurements = {
            "base_image_present_before_build": False,
            "base_image_inspect_seconds": 0.12,
            "compose_build_seconds": 7.5,
            "built_image_bytes": 123456,
        }
        self.assertTrue(launcher.valid_image_measurements(measurements))
        for field, value in (
            ("base_image_present_before_build", 1),
            ("base_image_inspect_seconds", True),
            ("compose_build_seconds", float("nan")),
            ("built_image_bytes", True),
            ("built_image_bytes", -1),
        ):
            invalid = dict(measurements, **{field: value})
            with self.subTest(field=field, value=value):
                self.assertFalse(launcher.valid_image_measurements(invalid))
        self.assertFalse(launcher.valid_image_measurements(
            {**measurements, "private_path": "/tmp/private-bundle"}))
        self.assertFalse(launcher.valid_image_measurements(
            {**measurements, "download_url": "https://assets.example.invalid/private"}))

    def test_provider_cohort_is_the_fixed_prior_member_delay_failures(self):
        expected = {
            "tests/application_acceptance/test_browser_locale.py::test_real_chromium_provider_exclusive_control_persists_and_excludes_auto_candidate",
            "tests/application_acceptance/test_core_provider_mihomo.py::test_core_subscription_provider_discovery_exclusive_intent_and_real_mihomo_child",
            "tests/application_acceptance/test_core_provider_mihomo.py::test_confirmed_provider_failure_applies_emergency_direct_and_failed_reentry_stays_direct",
            "tests/application_acceptance/test_core_provider_mihomo.py::test_provider_handoff_rejects_stale_selection_after_real_selector_wins_probe_race",
            "tests/application_acceptance/test_core_provider_mihomo.py::test_provider_reentry_rejects_old_probe_after_real_mihomo_incarnation_change",
            "tests/application_acceptance/test_core_provider_mihomo.py::test_provider_reentry_fences_concurrent_public_exclusive_intent_change",
            "tests/application_acceptance/test_core_provider_mihomo.py::test_confirmed_recovery_typed_provider_api_errors_are_unknown_not_member_down[recovery-timeout]",
            "tests/application_acceptance/test_core_provider_mihomo.py::test_confirmed_recovery_typed_provider_api_errors_are_unknown_not_member_down[recovery-rate-limited]",
            "tests/application_acceptance/test_core_provider_mihomo.py::test_confirmed_recovery_typed_provider_api_errors_are_unknown_not_member_down[recovery-malformed]",
            "tests/application_acceptance/test_core_provider_mihomo.py::test_provider_status_controls_real_core_apply_and_native_mihomo_parity[unknown-status-neutral]",
            "tests/application_acceptance/test_xray_generation.py::test_xray_generation_fence_rejects_replaced_native_incarnation",
        }
        self.assertEqual(11, len(expected))
        self.assertEqual(expected, launcher.expected_acceptance_nodeids("provider-cohort"))
        failed_cohort = [{"nodeid": nodeid, "status": "failed",
                          "phases": {"setup": "passed", "call": "failed", "teardown": "passed"}}
                         for nodeid in expected]
        launcher.validate_suite_node_receipt(failed_cohort, "provider-cohort", sorted(expected))
        skipped_cohort = [dict(row, status="skipped") for row in failed_cohort]
        with self.assertRaises(launcher.NotRun):
            launcher.validate_suite_node_receipt(skipped_cohort, "provider-cohort", sorted(expected))

    def test_fence_diagnostic_is_exactly_the_two_prior_fence_failures(self):
        expected = {
            "tests/application_acceptance/test_core_provider_mihomo.py::test_confirmed_recovery_typed_provider_api_errors_are_unknown_not_member_down[recovery-unavailable]",
            "tests/application_acceptance/test_xray_generation.py::test_real_core_selection_cas_miss_reconciles_after_native_readback_without_provider_retry",
        }
        self.assertEqual(expected, launcher.expected_acceptance_nodeids("fence-diagnostic"))
        failed = [{"nodeid": nodeid, "status": "failed",
                   "phases": {"setup": "passed", "call": "failed", "teardown": "passed"}}
                  for nodeid in expected]
        launcher.validate_suite_node_receipt(failed, "fence-diagnostic", sorted(expected))
        workflow = (Path(__file__).parents[2] / ".github/workflows/phase-d-validation.yml").read_text(
            encoding="utf-8")
        self.assertIn('"ci:validate-fences": "fence-diagnostic"', workflow)
        self.assertIn('if [[ "$VALIDATION_STAGE" == fence-diagnostic ]]; then suite=fence-diagnostic; fi', workflow)

    def test_selection_fence_observer_preserves_results_bounds_and_restores_trace(self):
        worker_path = Path(__file__).parents[1] / "application_acceptance" / "worker.py"
        parsed = ast.parse(worker_path.read_text(encoding="utf-8"))
        observer = next(node for node in parsed.body
                        if isinstance(node, ast.FunctionDef)
                        and node.name == "_install_acceptance_selection_fence_observer")
        namespace = {
            "__builtins__": __builtins__, "os": __import__("os"), "sys": sys,
            "json": json, "threading": __import__("threading"),
            "Any": __import__("typing").Any,
        }
        exec(compile(ast.Module(body=[observer], type_ignores=[]), str(worker_path), "exec"), namespace)
        self.assertIsNone(sys.gettrace(), "observer contract must start without an active trace")
        failure = LookupError("private exception text")

        def fake_capture(mode):
            if mode == "escape":
                raise failure
            try:
                if mode == "caught":
                    raise RuntimeError("password=private-value")
                return {"identity": "private-id"}
            except Exception:
                return None

        commit_results = {"none": None, "success": object()}

        def fake_commit(_connection, **kwargs):
            return commit_results[kwargs["result_key"]]

        reconciliation = {"ok": False, "error_code": "VPN_AUTO_SELECTION_RECONCILIATION_STALE",
                          "server_id": "private-id"}
        selector = SimpleNamespace(
            commit_active_selection=fake_commit,
            _reconcile_observed_selection_after_cas_miss=lambda *_args, **_kwargs: reconciliation,
        )
        provider_recovery = SimpleNamespace(_capture_recovery_context=fake_capture)
        services_module = ModuleType("fwrouter_api.services")
        services_module.selector = selector
        services_module.provider_recovery = provider_recovery
        observed = io.StringIO()
        with mock.patch.dict(sys.modules, {"fwrouter_api.services": services_module}), \
             mock.patch.dict(__import__("os").environ, {
                 "FWROUTER_ENVIRONMENT": "test",
                 "FWROUTER_APPLICATION_ACCEPTANCE_ROOT": "/tmp/fwrouter-application-acceptance",
             }), mock.patch("sys.stderr", observed):
            namespace["_install_acceptance_selection_fence_observer"]()
            previous_trace = sys.gettrace()
            context = provider_recovery._capture_recovery_context("ok")
            self.assertEqual({"identity": "private-id"}, context)
            self.assertIs(previous_trace, sys.gettrace())
            self.assertIsNone(provider_recovery._capture_recovery_context("caught"))
            self.assertIs(previous_trace, sys.gettrace())
            with self.assertRaises(LookupError) as raised:
                provider_recovery._capture_recovery_context("escape")
            self.assertIs(failure, raised.exception)
            self.assertIs(previous_trace, sys.gettrace())

            prior_trace = lambda *_args: None
            sys.settrace(prior_trace)
            try:
                context = provider_recovery._capture_recovery_context("ok")
                self.assertEqual({"identity": "private-id"}, context)
                self.assertIs(prior_trace, sys.gettrace())
            finally:
                sys.settrace(previous_trace)

            no_commit = selector.commit_active_selection(None, expected_active_server_id="other",
                                                         server_id="selected", result_key="none")
            committed = selector.commit_active_selection(None, expected_active_server_id="selected",
                                                          server_id="selected", result_key="success")
            self.assertIsNone(no_commit)
            self.assertIs(commit_results["success"], committed)
            self.assertIs(reconciliation, selector._reconcile_observed_selection_after_cas_miss())
            for _ in range(3):
                provider_recovery._capture_recovery_context("ok")

        self.assertIs(previous_trace, sys.gettrace())
        lines = observed.getvalue().splitlines()
        records = [json.loads(line.split(" ", 1)[1]) for line in lines]
        capture_records = [row for row in records if row["event"] == "capture"]
        self.assertEqual(4, len(capture_records))
        self.assertTrue(all(row.get("trace_status") in {"captured", "preexisting_trace"}
                            for row in capture_records))
        self.assertTrue(all(type(row.get("return_line")) is int or row.get("return_line") is None
                            for row in capture_records))
        self.assertEqual("RuntimeError", capture_records[1]["exception_type"])
        self.assertEqual("LookupError", capture_records[2]["exception_type"])
        self.assertEqual("preexisting_trace", capture_records[3]["trace_status"])
        commit_records = [row for row in records if row["event"] == "commit"]
        self.assertEqual(2, len(commit_records))
        self.assertEqual((False, False), (commit_records[0]["active_matches_selected"],
                                          commit_records[0]["cas_committed"]))
        self.assertEqual((True, True), (commit_records[1]["active_matches_selected"],
                                        commit_records[1]["cas_committed"]))
        reconcile_record = next(row for row in records if row["event"] == "reconcile")
        self.assertEqual("VPN_AUTO_SELECTION_RECONCILIATION_STALE", reconcile_record["error_code"])
        diagnostic = observed.getvalue()
        self.assertNotIn("private-value", diagnostic)
        self.assertNotIn("private-id", diagnostic)
        self.assertNotIn("expected_active_server_id", diagnostic)

    def test_public_artifact_redaction_masks_uuid_email_and_preserves_junit_ids(self):
        secret_uuid = "123e4567-e89b-42d3-a456-426614174000"
        secret_email = "collector-user@example.org"
        redacted = launcher._redact_public(
            f"failure id={secret_uuid} email={secret_email}", limit=128)
        self.assertNotIn(secret_uuid, redacted)
        self.assertNotIn(secret_email, redacted)
        self.assertIn("[UUID]", redacted)
        self.assertIn("[EMAIL]", redacted)
        bounded = launcher._redact_public((f"{secret_uuid} {secret_email} " * 100), limit=256)
        self.assertLessEqual(len(bounded.encode("utf-8")), 256)

        with tempfile.TemporaryDirectory(prefix="fwrouter-junit-redaction-") as temp:
            junit = Path(temp) / "junit.xml"
            root = ET.Element("testsuite")
            case = ET.SubElement(root, "testcase", {
                "classname": f"tests.test_case_{secret_uuid}",
                "name": f"test_client_{secret_email}",
            })
            failure = ET.SubElement(case, "failure", {"message": f"failed for {secret_uuid} {secret_email}"})
            failure.text = f"observed {secret_uuid} {secret_email}"
            ET.ElementTree(root).write(junit, encoding="utf-8", xml_declaration=True)
            launcher._redact_junit_file(junit)
            observed = ET.parse(junit).getroot().find(".//testcase")
            self.assertEqual(f"tests.test_case_{secret_uuid}", observed.get("classname"))
            self.assertEqual(f"test_client_{secret_email}", observed.get("name"))
            self.assertNotIn(secret_uuid, str(observed.find("failure").attrib))
            self.assertNotIn(secret_email, str(observed.find("failure").attrib))
            self.assertNotIn(secret_uuid, observed.find("failure").text)
            self.assertNotIn(secret_email, observed.find("failure").text)

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

    def test_kernel_dataplane_profile_has_exact_root_capability_namespace_and_receipt_scope(self):
        facts = {"uid": 0, "gid": 0, "cap_eff": 1 << 12, "netns_matches_pid1": True,
                 "netns_differs_from_host": True, "hostname": "acceptance-container",
                 "docker_marker_regular": True, "production_api_absent": True,
                 "readonly_root": True, "readonly_profile_mount": True, "fixed_workspace": True}
        self.assertIsNone(acceptance_profile._kernel_qualification_error(facts))
        for key, value in (("uid", 10001), ("gid", 10001), ("cap_eff", (1 << 12) | 1),
                           ("netns_matches_pid1", False), ("netns_differs_from_host", False),
                           ("readonly_root", False), ("readonly_profile_mount", False)):
            with self.subTest(key=key):
                self.assertIsNotNone(acceptance_profile._kernel_qualification_error(dict(facts, **{key: value})))

        schema = json.loads((LAUNCHER_PATH.parent / "kernel_dataplane_profile_schema.json").read_text())
        self.assertEqual("hosted-kernel-dataplane", schema["properties"]["profile"]["const"])
        self.assertFalse(schema["additionalProperties"])
        script_schema = schema["properties"]["kernel_dataplane"]["properties"]["dataplane_scripts"]
        self.assertFalse(script_schema["additionalProperties"])
        self.assertEqual(set(launcher.KERNEL_SCRIPT_SOURCES), set(script_schema["required"]))

        expected = {
            "tests/application_acceptance/test_core_provider_mihomo.py::test_confirmed_provider_failure_applies_emergency_direct_and_failed_reentry_stays_direct",
            "tests/application_acceptance/test_core_provider_mihomo.py::test_provider_handoff_rejects_stale_selection_after_real_selector_wins_probe_race",
        }
        self.assertEqual(expected, launcher.expected_acceptance_nodeids("kernel-recovery-diagnostic"))
        self.assertEqual(set(), launcher.expected_acceptance_nodeids("kernel-preflight"))
        with self.assertRaises(launcher.NotRun):
            launcher.expected_acceptance_nodeids("kernel-recovery-diagnostic-extra")
        launcher.validate_application_receipt_scope(
            {"scope": "hosted-kernel-dataplane"}, {"profile": "hosted-kernel-dataplane"})
        with self.assertRaises(launcher.NotRun):
            launcher.validate_application_receipt_scope(
                {"scope": "hosted-native-process"}, {"profile": "hosted-kernel-dataplane"})
        with self.assertRaises(launcher.NotRun):
            launcher.validate_application_receipt_scope(
                {"scope": "hosted-kernel-preflight"}, {"profile": "hosted-kernel-preflight"})
        conftest_path = LAUNCHER_PATH.parents[1] / "application_acceptance/conftest.py"
        conftest = conftest_path.read_text(encoding="utf-8")
        conftest_module = ast.parse(conftest, filename=str(conftest_path))
        prepare_db = next(node for node in conftest_module.body
                          if isinstance(node, ast.FunctionDef) and node.name == "_prepare_db")
        acceptance_stack = next(node for node in conftest_module.body
                                if isinstance(node, ast.FunctionDef) and node.name == "acceptance_stack")
        top_level_imports = []
        pending = list(conftest_module.body)
        while pending:
            node = pending.pop()
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                top_level_imports.append(node)
            else:
                pending.extend(ast.iter_child_nodes(node))
        application_imports = lambda node: (
            [alias.name for alias in node.names if alias.name.startswith("fwrouter_api")]
            if isinstance(node, ast.Import) else
            [node.module or ""] if (node.module or "").startswith("fwrouter_api") else []
        )
        self.assertFalse(any(application_imports(node) for node in top_level_imports),
                         "production application modules must not load while conftest is imported")
        all_application_imports = [node for node in ast.walk(conftest_module)
                                   if isinstance(node, (ast.Import, ast.ImportFrom))
                                   and application_imports(node)]
        scoped_imports = [node for node in ast.walk(prepare_db)
                          if isinstance(node, ast.ImportFrom)
                          and (node.module or "").startswith("fwrouter_api.")]
        self.assertEqual({(node.lineno, node.module) for node in scoped_imports},
                         {(node.lineno, node.module or "") for node in all_application_imports},
                         "application imports must stay scoped to the isolated database setup")
        self.assertEqual({"fwrouter_api.core.config", "fwrouter_api.db.connection",
                          "fwrouter_api.services.bootstrap"},
                         {node.module for node in scoped_imports})
        prepare_calls = [node for node in ast.walk(conftest_module)
                         if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                         and node.func.id == "_prepare_db"]
        self.assertEqual(1, len(prepare_calls), "application imports must have one isolated fixture entrypoint")
        self.assertIn(prepare_calls[0], list(ast.walk(acceptance_stack)))
        isolation_lines = {"_owned_dir": [], "tempfile.mkdtemp": []}
        for node in ast.walk(acceptance_stack):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name) and node.func.id == "_owned_dir":
                isolation_lines["_owned_dir"].append(node.lineno)
            elif (isinstance(node.func, ast.Attribute) and node.func.attr == "mkdtemp"
                  and isinstance(node.func.value, ast.Name) and node.func.value.id == "tempfile"):
                isolation_lines["tempfile.mkdtemp"].append(node.lineno)
        self.assertTrue(all(isolation_lines.values()), "owned-dir and private-suite setup calls are required")
        self.assertLess(max(line for lines in isolation_lines.values() for line in lines),
                        prepare_calls[0].lineno,
                        "application imports must follow ownership and private-suite setup")
        scope_values = []
        for node in ast.walk(conftest_module):
            if not isinstance(node, ast.Assign) or not any(
                    isinstance(target, ast.Name) and target.id == "receipt" for target in node.targets):
                continue
            if not isinstance(node.value, ast.Dict):
                continue
            scope_values.extend(value for key, value in zip(node.value.keys, node.value.values)
                                if isinstance(key, ast.Constant) and key.value == "scope")
        self.assertEqual(1, len(scope_values), "conftest must emit one application receipt scope")
        scope_code = compile(ast.Expression(scope_values[0]), str(conftest_path), "eval")
        for names, expected_scope in (
            ({"packet_profile": False, "kernel_dataplane": False}, "hosted-native-process"),
            ({"packet_profile": False, "kernel_dataplane": True}, "hosted-kernel-dataplane"),
            ({"packet_profile": True, "kernel_dataplane": True}, "hosted-kernel-packet"),
        ):
            with self.subTest(receipt_scope=expected_scope):
                self.assertEqual(expected_scope, eval(scope_code, {"__builtins__": {}}, names))
        self.assertIn("limitations", conftest)
        workflow = (LAUNCHER_PATH.parents[2] / ".github/workflows/phase-d-validation.yml").read_text()
        self.assertIn('"ci:validate-dataplane": "kernel-recovery-diagnostic"', workflow)
        self.assertIn('if [[ "$VALIDATION_STAGE" == kernel-recovery-diagnostic ]]; then suite=kernel-recovery-diagnostic; fi', workflow)

    def test_export_hashes_copied_source_and_writes_provenance_sidecar(self):
        with tempfile.TemporaryDirectory(prefix="fwrouter-acceptance-export-") as temp:
            root = Path(temp) / "root"
            dest = Path(temp) / "context"
            (root / "ui").mkdir(parents=True)
            (root / "tests/acceptance").mkdir(parents=True)
            (root / "host/libexec/fwrouter").mkdir(parents=True)
            (root / "ui/index.html").write_bytes(b"synthetic ui")
            (root / "tests/acceptance/Dockerfile").write_text("FROM example\n", encoding="utf-8")
            (root / "tests/acceptance/compose.yaml").write_text("services: {}\n", encoding="utf-8")
            collector = root / "host/libexec/fwrouter/traffic-collect.sh"
            collector.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            collector.chmod(0o755)
            kernel_scripts = {}
            for name in ("dataplane-common.sh", "dataplane-check.sh", "dataplane-apply.sh", "dataplane-rollback.sh"):
                script = root / "host/libexec/fwrouter" / name
                script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                script.chmod(0o755)
                kernel_scripts[f"host/libexec/fwrouter/{name}"] = script
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
                tracked=["tests/acceptance/Dockerfile", "tests/acceptance/compose.yaml", "ui/index.html",
                         "host/libexec/fwrouter/traffic-collect.sh", *kernel_scripts],
                binaries={"xray": xray, "mihomo": mihomo}, browser_bundle=bundle,
                source_revision="a" * 40,
            )
            sidecar = json.loads((dest / ".fwrouter-acceptance-revision").read_text())
            self.assertEqual(result["source_manifest_sha256"], sidecar["source_manifest_sha256"])
            self.assertEqual(result["ui_tree_sha256"], sidecar["ui_tree_sha256"])
            self.assertEqual("a" * 40, sidecar["source_revision"])
            self.assertEqual(0o444, (dest / ".fwrouter-acceptance-revision").stat().st_mode & 0o777)
            self.assertEqual("fakechrome", (dest / "native/chromium/chrome-linux64/chrome").read_text())
            self.assertEqual(0o755, (dest / "host/libexec/fwrouter/traffic-collect.sh").stat().st_mode & 0o777)
            for relative in kernel_scripts:
                self.assertEqual(0o755, (dest / relative).stat().st_mode & 0o777)
            self.assertEqual(0o644, (dest / "tests/acceptance/compose.yaml").stat().st_mode & 0o777)
            digest = hashlib.sha256()
            tracked = ("tests/acceptance/Dockerfile", "tests/acceptance/compose.yaml", "ui/index.html",
                       "host/libexec/fwrouter/traffic-collect.sh", *kernel_scripts)
            for relative in sorted(tracked):
                digest.update(relative.encode("utf-8") + b"\0")
                digest.update((dest / relative).read_bytes())
            self.assertEqual(digest.hexdigest(), result["source_manifest_sha256"])
            self.assertEqual(result["source_manifest_sha256"],
                             acceptance_profile._source_manifest_digest(dest))

            collector.unlink()
            collector.symlink_to(root / "ui/index.html")
            with self.assertRaises(launcher.NotRun):
                launcher.export_build_context(
                    root, Path(temp) / "symlink-context",
                    tracked=["host/libexec/fwrouter/traffic-collect.sh"],
                    binaries={"xray": xray, "mihomo": mihomo}, browser_bundle=bundle,
                    source_revision="b" * 40,
                )

    def test_dockerfile_copies_the_full_acceptance_tree_used_by_preflight_digest(self):
        dockerfile = (LAUNCHER_PATH.parent / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("apt-get install --no-install-recommends -y", dockerfile)
        self.assertRegex(dockerfile, r"(?s)apt-get install --no-install-recommends -y.*?\bjq\b")
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
            kernel = json.loads(json.dumps(base))
            kernel["Config"]["User"] = "0:0"
            kernel["HostConfig"]["CapAdd"] = ["NET_ADMIN"]
            launcher.validate_container_inspect(kernel, project="project", run_id="run", profile_path=profile,
                                                image_id=kernel["Image"], kernel_preflight=True)
            kernel_prefix = json.loads(json.dumps(kernel))
            kernel_prefix["HostConfig"]["CapAdd"] = ["CAP_NET_ADMIN"]
            launcher.validate_container_inspect(kernel_prefix, project="project", run_id="run",
                                                profile_path=profile, image_id=kernel["Image"],
                                                kernel_preflight=True)
            confinement = launcher.stopped_container_confinement_summary(kernel_prefix)
            self.assertEqual(["CAP_NET_ADMIN"], confinement["cap_add"])
            self.assertEqual(["ALL"], confinement["cap_drop"])
            self.assertEqual("0:0", confinement["user"])
            self.assertIn("network_mode", confinement)
            self.assertIn("pid_mode", confinement)
            self.assertIn("ipc_mode", confinement)
            self.assertEqual(0, confinement["devices_count"])
            self.assertNotIn("Mounts", confinement)
            self.assertNotIn("Env", confinement)
            for rejected_caps in (["CAP_SYS_ADMIN"], ["NET_ADMIN", "CAP_NET_ADMIN"],
                                  ["NET_ADMIN", "NET_ADMIN"]):
                with self.subTest(rejected_caps=rejected_caps):
                    invalid_caps = json.loads(json.dumps(kernel))
                    invalid_caps["HostConfig"]["CapAdd"] = rejected_caps
                    with self.assertRaises(launcher.NotRun):
                        launcher.validate_container_inspect(invalid_caps, project="project", run_id="run",
                                                            profile_path=profile, image_id=kernel["Image"],
                                                            kernel_preflight=True)
            ordinary_prefix = json.loads(json.dumps(base))
            ordinary_prefix["HostConfig"]["CapAdd"] = ["CAP_NET_ADMIN"]
            with self.assertRaises(launcher.NotRun):
                launcher.validate_container_inspect(ordinary_prefix, project="project", run_id="run",
                                                    profile_path=profile, image_id=base["Image"])
            extra_cap = json.loads(json.dumps(kernel))
            extra_cap["HostConfig"]["CapAdd"] = ["NET_ADMIN", "SYS_ADMIN"]
            with self.assertRaises(launcher.NotRun):
                launcher.validate_container_inspect(extra_cap, project="project", run_id="run",
                                                    profile_path=profile, image_id=kernel["Image"],
                                                    kernel_preflight=True)
            host_namespace = json.loads(json.dumps(kernel))
            host_namespace["HostConfig"]["NetworkMode"] = "host"
            with self.assertRaises(launcher.NotRun):
                launcher.validate_container_inspect(host_namespace, project="project", run_id="run",
                                                    profile_path=profile, image_id=kernel["Image"],
                                                    kernel_preflight=True)
            named_ipc = json.loads(json.dumps(kernel))
            named_ipc["HostConfig"]["IpcMode"] = "container:other"
            with self.assertRaises(launcher.NotRun):
                launcher.validate_container_inspect(named_ipc, project="project", run_id="run",
                                                    profile_path=profile, image_id=kernel["Image"],
                                                    kernel_preflight=True)
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
            kernel = json.loads(json.dumps(config))
            kernel["services"]["application"]["user"] = "0:0"
            kernel["services"]["application"]["cap_add"] = ["NET_ADMIN"]
            kernel["services"]["application"]["build"] = {"args": {
                "FWROUTER_KERNEL_PREFLIGHT": "1", "DEBIAN_SNAPSHOT": launcher.KERNEL_DEBIAN_SNAPSHOT}}
            launcher.validate_compose_config(kernel, run_id=run_id, profile_path=profile, kernel_preflight=True)
            extra_capability = json.loads(json.dumps(kernel))
            extra_capability["services"]["application"]["cap_add"] = ["NET_ADMIN", "SYS_ADMIN"]
            with self.assertRaises(launcher.NotRun):
                launcher.validate_compose_config(extra_capability, run_id=run_id,
                                                 profile_path=profile, kernel_preflight=True)
            user_downgrade = json.loads(json.dumps(kernel))
            user_downgrade["services"]["application"]["user"] = "10001:10001"
            with self.assertRaises(launcher.NotRun):
                launcher.validate_compose_config(user_downgrade, run_id=run_id,
                                                 profile_path=profile, kernel_preflight=True)

    def test_profile_schema_has_no_unbound_authorization_or_port_fields(self):
        schema = json.loads((LAUNCHER_PATH.parent / "profile_schema.json").read_text())
        required = set(schema["required"])
        self.assertEqual({"schema", "profile", "source_revision", "plan_digest", "xray", "mihomo", "chromium",
                          "playwright_python", "baseline_xray_config_sha256", "ui_tree_sha256", "suite_nonce"}, required)
        self.assertNotIn("loopback_ports", schema["properties"])
        self.assertNotIn("authorization", schema["properties"])

    def test_kernel_profile_and_source_allowlist_are_exact_and_fixed_snapshot_matches_lock(self):
        schema = json.loads((LAUNCHER_PATH.parent / "kernel_profile_schema.json").read_text(encoding="utf-8"))
        required = set(schema["required"])
        self.assertIn("kernel_preflight", required)
        kernel = schema["properties"]["kernel_preflight"]
        self.assertFalse(kernel["additionalProperties"])
        scripts = kernel["properties"]["dataplane_scripts"]["required"]
        self.assertEqual(set(launcher.KERNEL_SCRIPT_SOURCES), set(scripts))
        for path in launcher.KERNEL_SCRIPT_SOURCES:
            self.assertTrue(launcher._safe_source(path))
        self.assertFalse(launcher._safe_source("host/libexec/fwrouter/other-script.sh"))
        lock = json.loads((LAUNCHER_PATH.parent / "dependency-image.lock.json").read_text(encoding="utf-8"))
        self.assertEqual(lock["debian_snapshot"]["url"], launcher.KERNEL_DEBIAN_SNAPSHOT)
        workflow = Path(__file__).parents[2] / ".github/workflows/phase-d-validation.yml"
        source = workflow.read_text(encoding="utf-8")
        self.assertIn('"ci:validate-kernel": "kernel-preflight"', source)
        self.assertIn('if [[ "$VALIDATION_STAGE" == kernel-preflight ]]; then suite=kernel-preflight; fi', source)
        self.assertEqual(set(), launcher.expected_acceptance_nodeids("kernel-preflight"))

    def test_application_receipt_scope_expression_selects_all_kernel_profiles_without_app_imports(self):
        source = LAUNCHER_PATH.read_text(encoding="utf-8")
        module = ast.parse(source, filename=str(LAUNCHER_PATH))
        hosted_acceptance = next(node for node in module.body
                                 if isinstance(node, ast.FunctionDef) and node.name == "run_hosted_acceptance")
        role_assignments = [node for node in ast.walk(hosted_acceptance)
                            if isinstance(node, ast.Assign)
                            and any(isinstance(target, ast.Name) and target.id == "roles"
                                    for target in node.targets)
                            and isinstance(node.value, ast.IfExp)]
        self.assertEqual(1, len(role_assignments), "packet mode must choose one explicit Compose role set")
        self.assertEqual(("application", "lanclient", "endpoint"),
                         ast.literal_eval(role_assignments[0].value.body))
        self.assertEqual(("application",), ast.literal_eval(role_assignments[0].value.orelse))
        candidates = []
        for node in ast.walk(hosted_acceptance):
            if not isinstance(node, ast.AnnAssign) or not isinstance(node.target, ast.Name):
                continue
            if node.target.id != "receipt" or not isinstance(node.value, ast.Dict):
                continue
            for key, value in zip(node.value.keys, node.value.values):
                if isinstance(key, ast.Constant) and key.value == "scope":
                    candidates.append(value)
        self.assertEqual(1, len(candidates), "source must define one receipt scope expression")
        scope_code = compile(ast.Expression(candidates[0]), str(LAUNCHER_PATH), "eval")
        shared = {"__builtins__": {}}
        cases = (
            ({"kernel_preflight": True, "packet_mode": False, "kernel_dataplane": False,
              "suite": "kernel-preflight"}, "hosted-kernel-preflight"),
            ({"kernel_preflight": False, "packet_mode": False, "kernel_dataplane": True,
              "suite": "kernel-recovery-diagnostic"}, "hosted-kernel-dataplane"),
            ({"kernel_preflight": False, "packet_mode": True, "kernel_dataplane": True,
              "suite": "packet-diagnostic"}, "hosted-kernel-packet"),
        )
        for names, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(expected, eval(scope_code, shared, names | {"_DIAGNOSTIC_SUITES": set()}))
        imported_names = {
            alias.name for node in ast.walk(module) if isinstance(node, ast.Import) for alias in node.names
        } | {
            (node.module or "") for node in ast.walk(module) if isinstance(node, ast.ImportFrom)
        }
        self.assertFalse(any(name == "fastapi" or name.startswith("fastapi.")
                             or name == "fwrouter_api" or name.startswith("fwrouter_api.")
                             for name in imported_names))
        self.assertNotIn("fwrouter_api", launcher.sys.modules)
        self.assertNotIn("fastapi", launcher.sys.modules)

if __name__ == "__main__":
    unittest.main(verbosity=2)
