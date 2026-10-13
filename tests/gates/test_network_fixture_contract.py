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
import urllib.error
import urllib.request
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
    def test_mihomo_path_receipt_projection_is_fixed_bounded_and_redacted(self):
        tree = ast.parse(PACKET_TEST.read_text(encoding="utf-8"))
        proof = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                     and node.name == "_mihomo_packet_path_proof")
        packet_lines = PACKET_TEST.read_text(encoding="utf-8").splitlines()
        source = "\n".join(packet_lines[proof.lineno - 1:proof.end_lineno])
        state_assign = next(node for node in ast.walk(proof) if isinstance(node, ast.Assign)
                            and any(isinstance(target, ast.Name) and target.id == "reconcile_state"
                                    for target in node.targets)
                            and isinstance(node.value, ast.Dict))
        schema = {key.value for key in state_assign.value.keys if isinstance(key, ast.Constant)}
        self.assertEqual(schema, {
            "receipt_present", "shape_valid", "active_hash_matches", "runtime_incarnation_matches",
            "version_matches", "native_top_level_rules_match", "native_selector_membership_match",
            "subrule_config_guard_match", "error_code",
        })
        error_values = {node.value for node in ast.walk(proof)
                        if isinstance(node, ast.Constant) and isinstance(node.value, str)
                        and node.value in {"missing", "bounds", "shape", "io_error", "invalid_json"}}
        self.assertEqual(error_values, {"missing", "bounds", "shape", "io_error", "invalid_json"})
        path_assign = next(node for node in ast.walk(proof) if isinstance(node, ast.Assign)
                           and any(isinstance(target, ast.Name) and target.id == "receipt_path"
                                   for target in node.targets))
        self.assertIsInstance(path_assign.value, ast.BinOp)
        self.assertIsInstance(path_assign.value.left, ast.Attribute)
        self.assertEqual(path_assign.value.left.attr, "parent")
        self.assertEqual(path_assign.value.right.value, "reconcile-state.json")
        self.assertIn("receipt_path.lstat()", source)
        self.assertIn("receipt_path.is_symlink()", source)
        self.assertIn("stat.S_ISREG(receipt_info.st_mode)", source)
        self.assertIn("receipt_info.st_size > 262144", source)
        self.assertIn('receipt_path.open("rb")', source)
        self.assertIn('handle.read(262145)', source)
        self.assertIn("len(receipt_bytes) > 262144", source)
        self.assertIn('"version_matches": (version_shape and fingerprint_version == 2', source)
        self.assertIn('"reconcile_state_receipt": reconcile_state', source)
        for raw_field in ('"active_config_hash":', '"runtime_incarnation":', '"fingerprint_hash":',
                          '"candidate_config_hash":'):
            self.assertNotIn(raw_field, source)
        bounded_receipt = {
            "receipt_present": True, "shape_valid": True, "active_hash_matches": True,
            "runtime_incarnation_matches": True, "version_matches": True,
            "native_top_level_rules_match": True, "native_selector_membership_match": True,
            "subrule_config_guard_match": True, "error_code": None,
        }
        self.assertLessEqual(len(json.dumps(bounded_receipt, separators=(",", ":")).encode("ascii")), 512)
        projector = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                         and node.name == "_record_phase_diagnostic")
        projector_source = ast.unparse(projector)
        self.assertIn("MAX_PHASE_DIAGNOSTIC_BYTES", projector_source)
        self.assertIn("packet phase diagnostic exceeded its fixed byte bound", projector_source)
        diagnostic_bound = next(node for node in tree.body if isinstance(node, ast.Assign)
                                and any(isinstance(target, ast.Name)
                                        and target.id == "MAX_PHASE_DIAGNOSTIC_BYTES"
                                        for target in node.targets))
        self.assertEqual(ast.literal_eval(diagnostic_bound.value), 4096)

    def test_dns53_failure_receipt_is_bounded_and_recorded_before_502_assertion(self):
        packet_source = PACKET_TEST.read_text(encoding="utf-8")
        packet_tree = ast.parse(packet_source)
        functions = {node.name: node for node in packet_tree.body if isinstance(node, ast.FunctionDef)
                     and node.name in {"_record_dns53_probe_failure", "_role_json"}}
        self.assertEqual(set(functions), {"_record_dns53_probe_failure", "_role_json"})
        namespace = {"Any": object, "json": json, "urllib": urllib}
        exec(compile(ast.Module(body=[functions["_record_dns53_probe_failure"], functions["_role_json"]],
                                type_ignores=[]), str(PACKET_TEST), "exec"), namespace)
        receipt = {"schema": "fwrouter-dns53-probe-failure/v1", "query_label": "vpn",
                   "transport": "udp", "error_code": "timeout"}
        recorded = []
        self.assertTrue(namespace["_record_dns53_probe_failure"](
            lambda name, value: recorded.append((name, value)), receipt))
        self.assertEqual(json.loads(recorded[0][1]), receipt)
        for invalid in (
            {**receipt, "query_label": "private.example"},
            {**receipt, "transport": "icmp"},
            {**receipt, "error_code": "SECRET_SENTINEL"},
            {**receipt, "raw_query": "SECRET_SENTINEL"},
        ):
            self.assertFalse(namespace["_record_dns53_probe_failure"](
                lambda *_args: self.fail("invalid DNS receipt must not be emitted"), invalid))

        body = json.dumps({"ok": False, "error": "TimeoutError", "dns53_failure": receipt}).encode("ascii")

        def fail_http_request(_request, *, timeout):
            raise urllib.error.HTTPError("http://fixed-role.invalid/", 502, "bounded", {}, io.BytesIO(body))

        events = []
        with mock.patch.object(urllib.request, "urlopen", side_effect=fail_http_request):
            with self.assertRaises(AssertionError) as caught:
                namespace["_role_json"](
                    "http://fixed-role.invalid/probe/dns53", method="POST",
                    failure_record_property=lambda name, value: events.append((name, value)))
        self.assertEqual(events[0][0], "dns53_probe_failure")
        self.assertEqual(json.loads(events[0][1]), receipt)
        self.assertEqual(str(caught.exception), "{'status': 502, 'shape': 'dict'}")
        self.assertNotIn("fixed-role.invalid", events[0][1])

        malformed = b"[]"
        with mock.patch.object(urllib.request, "urlopen", side_effect=urllib.error.HTTPError(
                "http://fixed-role.invalid/", 502, "bounded", {}, io.BytesIO(malformed))):
            with self.assertRaises(AssertionError) as malformed_error:
                namespace["_role_json"](
                    "http://fixed-role.invalid/probe/dns53", method="POST",
                    failure_record_property=lambda *_args: self.fail("non-dict error must not be projected"))
        self.assertEqual(str(malformed_error.exception), "{'status': 502, 'shape': 'list'}")

    def test_packet_capture_summary_is_compact_and_retains_all_required_packet_evidence(self):
        tree = ast.parse(PACKET_TEST.read_text(encoding="utf-8"))
        writer = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                      and node.name == "_write_capture_summary")
        with tempfile.TemporaryDirectory(prefix="packet-summary-contract-") as raw_root:
            output = Path(raw_root) / "summary.json"
            namespace = {"Any": object, "json": json, "os": os, "CAPTURE_SUMMARY_PATH": output}
            exec(compile(ast.Module(body=[writer], type_ignores=[]), str(PACKET_TEST), "exec"), namespace)
            direct_udp = {"client_source_to_service": 0, "mihomo_direct_to_service": 0}
            core = {"vpn_tcp_classified": 3, "vpn_udp_classified": 4, "vpn_udp_handoff_packets": 4}
            phases = {
                "direct_before_vpn": {
                    "flows": {"tcp_9080": 2, "udp_9081": 2, "dns_5353": 2},
                    "core_counters": {"direct_lan_forward": 9},
                    "observations": {"counts": {"http": 1, "udp": 1, "dns": 1}},
                },
                "vpn": {"vless_endpoint_packets": 7,
                        "direct_client_leaks": {"tcp_9080": 0, "udp_9081": 0, "dns_5353": 0},
                        "core_counters": core, "observations": {"counts": {"http": 1, "udp": 1, "dns": 1}}},
                "protected_direct": {
                    "native_vpn_active": True, "native_target_stable": True, "selection_stable": True,
                    "native_protected_rule": {"classifier_bypass_installed": True,
                                               "protected_vip_in_active_set": True},
                    "native_protected_rule_stable": True,
                    "core_vpn_delta": {"vpn_tcp_classified": 0, "vpn_udp_classified": 0},
                    "endpoint_counts_delta": {"tcp": 1, "udp": 1},
                    "capture_flow_delta": {"tcp_9080": 2, "udp_9081": 1},
                    "router_guard_delta": {
                        "router_protected_tcp_forward": {"packets": 3},
                        "router_protected_udp_forward": {"packets": 1},
                        "router_protected_tcp_output": {"packets": 0},
                        "router_protected_udp_output": {"packets": 0},
                    },
                },
                "udp_capability_contract": {
                    "supported": True,
                    "supported_packet": {"endpoint_udp_observation": 1,
                                         "vless_tunnel_tcp_capture_packets": 7, "direct_udp_packets": 0},
                    "unknown": True,
                    "unknown_packet": {"endpoint_udp_delta": 1, "core": core,
                                       "direct_udp_capture_delta": direct_udp, "tcp": 3},
                    "unsupported": True,
                    "unsupported_packet": {"endpoint_udp_delta": 0,
                                           "core": {"vpn_udp_classified": 2},
                                           "direct_udp_capture_delta": direct_udp, "tcp": 0},
                    "endpoint_unavailable": {"endpoint_udp_delta": 0, "core": core,
                                              "direct_udp_capture_delta": direct_udp},
                    "empty_pool": {
                        "native": {"vpn_auto_selected_reject": True, "vpn_auto_target_count": 1,
                                   "vpn_global_has_direct_target": False, "selection_fence_revision": 22,
                                   "runtime_enforcement": {"level": "global_vpn_fail_closed",
                                                           "vpn_egress_ready": False}},
                        "endpoint_udp_delta": 0, "core": core,
                        "tcp_capture": {"client_direct": 0, "router_direct": 0, "vpn_proxy": 0},
                        "direct_udp_capture_delta": direct_udp, "selection_stable": True,
                    },
                },
                "emergency_direct": {"flows": {"tcp_9080": 1, "udp_9081": 1, "dns_5353": 1},
                                      "core_counters": {"direct_lan_forward": 5},
                                      "observations": {"counts": {"http": 1, "udp": 1, "dns": 1}}},
                "vpn_after_reentry": {"vless_endpoint_packets": 5,
                                       "direct_client_leaks": {"tcp_9080": 0, "udp_9081": 0, "dns_5353": 0},
                                       "core_counters": core,
                                       "observations": {"counts": {"http": 1, "udp": 1, "dns": 1}}},
                "native_dns53_policy": {
                    "api_status": 200,
                    "client_queries": {"udp": 1, "tcp": 1},
                    "external_resolver_queries": {"udp": 1, "tcp": 1},
                    "native_dnsmasq": {"daemon_running": True, "selective_status_ok": True,
                                       "dns_capture_ok": True, "nftset_probe_ok": True,
                                       "nftset_probe_status": "ok",
                                       "nft_probe_targets": [
                                           {"domain": "vpn.test", "ok": True, "resolved": True,
                                            "materialized_count": 1},
                                           {"domain": "direct.test", "ok": True, "resolved": True,
                                            "materialized_count": 1},
                                       ],
                                       "native_process_arguments_match": True,
                                       "unexpected_upstream_rule_count": 0},
                    "capture_router_to_owned_upstream": {"udp_upstream": 1, "tcp_upstream": 1},
                    "endpoint_observations_delta": {"udp": 2, "tcp": 2, "vpn_name": 1,
                                                    "direct_name": 1, "other_name": 0},
                    "endpoint_reply_delta": {"udp": 2, "tcp": 0, "vpn_name": 2},
                    "dns_selected_endpoint_flow_delta": {"vpn_domain_tcp": 1, "vpn_domain_udp": 1,
                                                          "direct_domain_tcp": 1, "direct_domain_udp": 1},
                    "dns_selected_endpoint_peer_proof": {"direct_domain_tcp_local": True,
                        "direct_domain_udp_local": True, "vpn_domain_tcp_local": True,
                        "vpn_domain_udp_local": True},
                    "resolved_flow_core_delta": {"vpn_tcp_classified": 1,
                        "vpn_udp_classified": 1, "vpn_udp_handoff_packets": 1},
                    "resolved_flow_direct_capture_delta": {key: 0 for key in (
                        "service_tcp_router_direct", "service_udp_router_direct",
                        "vpn_alias_tcp_router_direct", "vpn_alias_udp_router_direct",
                        "service_tcp_client_direct", "service_udp_client_direct",
                        "vpn_alias_tcp_client_direct", "vpn_alias_udp_client_direct")},
                    "router_guard_delta": {key: {"packets": count} for key, count in (
                        ("router_dns_udp_input", 1), ("router_dns_tcp_input", 1),
                        ("router_dns_udp_output", 1), ("router_dns_tcp_output", 1),
                        ("router_dns_vpn_direct_output", 0), ("router_dns_vpn_direct_forward", 0))},
                    "selection_stable": True,
                    "cleanup": {"owned_paths_cleaned": True},
                    "selective_direct_priority": {
                        "selective_mode_with_vpn_default": True,
                        "resolved_flows_fixed": True,
                        "last_peer_matches": {key: True for key in (
                            "vpn_domain_tcp", "vpn_domain_udp", "direct_domain_tcp", "direct_domain_udp")},
                        "last_peer_classes": {
                            "vpn_domain_tcp": "service_vip", "vpn_domain_udp": "service_vip",
                            "direct_domain_tcp": "client", "direct_domain_udp": "client",
                        },
                        "direct_core_path_delta": 17,
                        "selective_vpn_mark_delta": {"tcp": 3, "udp": 4},
                        "packet_delta": {
                            "direct_client_tcp": 2, "direct_client_udp": 3,
                            "direct_client_to_vpn_tcp": 0, "direct_client_to_vpn_udp": 0,
                            "direct_router_to_vpn_tcp": 0, "direct_router_to_vpn_udp": 0,
                            "provider_vless_tcp": 1,
                        },
                        "native_selector_stable": True,
                        "provider_selection_stable": True,
                        "provider_api_calls_unchanged": True,
                        "state_restored": True,
                    },
                },
                "ignored_large_diagnostic": "x" * 100_000,
            }
            namespace["_write_capture_summary"](phases)
            raw = output.read_bytes()
            self.assertLessEqual(len(raw), 4096)
            encoded = json.loads(raw)
            self.assertEqual(encoded["schema"], "fwrouter-packet-test-flows/v2")
            summary = encoded["phases"]
            self.assertEqual(set(summary), {"direct_before_vpn", "vpn", "protected_direct", "udp_capability",
                                            "emergency_direct", "vpn_after_reentry", "native_dns53_policy",
                                            "native_dns53_selective_direct_priority"})
            self.assertEqual(summary["protected_direct"]["endpoint"], {"tcp": 1, "udp": 1})
            self.assertEqual(summary["protected_direct"]["capture"], {"tcp_9080": 2, "udp_9081": 1})
            self.assertEqual(summary["protected_direct"]["core_vpn_classified"],
                             {"vpn_tcp_classified": 0, "vpn_udp_classified": 0})
            self.assertEqual(summary["protected_direct"]["forward_guard"],
                             {"router_protected_tcp_forward": 3, "router_protected_udp_forward": 1})
            self.assertEqual(summary["protected_direct"]["output_guard"],
                             {"router_protected_tcp_output": 0, "router_protected_udp_output": 0})
            self.assertTrue(summary["protected_direct"]["protected_rule_installed"])
            self.assertTrue(summary["protected_direct"]["protected_rule_stable"])
            self.assertTrue(summary["protected_direct"]["native_target_stable"])
            capability = summary["udp_capability"]
            self.assertEqual(set(capability), {"supported", "unknown", "unsupported",
                                               "endpoint_unavailable", "empty_pool"})
            for name in ("unknown", "unsupported", "endpoint_unavailable", "empty_pool"):
                self.assertEqual(capability[name]["direct_udp"], direct_udp)
            self.assertIsNone(capability["unsupported"]["handoff"],
                              "missing native counter must remain unknown, not become zero")
            self.assertEqual(capability["empty_pool"]["native"]["reject_selected"], True)
            self.assertEqual(capability["empty_pool"]["native"]["fence_revision"], 22)
            self.assertTrue(capability["empty_pool"]["selection_stable"])
            self.assertEqual(summary["direct_before_vpn"]["endpoint"], {"http": 1, "udp": 1, "dns": 1})
            dns53 = summary["native_dns53_policy"]
            self.assertTrue(dns53["dnsmasq_native"])
            self.assertTrue(dns53["nftset_materialized"])
            self.assertEqual(dns53["endpoint_replies"], {"udp": 2, "tcp": 0, "vpn_name": 2})
            self.assertEqual(dns53["unexpected_upstream_rules"], 0)
            self.assertEqual(dns53["router_upstream_capture"], {"udp_upstream": 1, "tcp_upstream": 1})
            self.assertEqual(dns53["resolved_user_flows"], {"vpn_domain_tcp": 1, "vpn_domain_udp": 1,
                                                             "direct_domain_tcp": 1, "direct_domain_udp": 1})
            self.assertTrue(all(dns53["endpoint_peer_proof"].values()))
            self.assertTrue(dns53["selection_stable"] and dns53["cleanup"])
            selective = summary["native_dns53_selective_direct_priority"]
            self.assertEqual(selective, {
                "selective_mode_with_vpn_default": True,
                "resolved_flows_fixed": True,
                "endpoint_last_peer_matches": {
                    "vpn_domain_tcp": True, "vpn_domain_udp": True,
                    "direct_domain_tcp": True, "direct_domain_udp": True,
                },
                "endpoint_peer_classes": {
                    "vpn_domain_tcp": "service_vip", "vpn_domain_udp": "service_vip",
                    "direct_domain_tcp": "client", "direct_domain_udp": "client",
                },
                "direct_core_path": 17,
                "selective_vpn_marks": {"tcp": 3, "udp": 4},
                "packet_delta": {
                    "direct_client_tcp": 2, "direct_client_udp": 3,
                    "direct_client_to_vpn_tcp": 0, "direct_client_to_vpn_udp": 0,
                    "direct_router_to_vpn_tcp": 0, "direct_router_to_vpn_udp": 0,
                    "provider_vless_tcp": 1,
                },
                "native_selector_stable": True,
                "provider_selection_stable": True,
                "provider_api_calls_unchanged": True,
                "state_restored": True,
            })

    def test_packet_phase_diagnostics_are_bounded_junit_json_emitted_before_probes_assert(self):
        tree = ast.parse(PACKET_TEST.read_text(encoding="utf-8"))
        functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
        namespace = {"Any": object, "json": json, "MAX_PHASE_DIAGNOSTIC_BYTES": 4096}
        helper = functions["_record_phase_diagnostic"]
        exec(compile(ast.Module(body=[helper], type_ignores=[]), str(PACKET_TEST), "exec"), namespace)
        properties = []
        diagnostic = {"phase": "vpn", "core_counter_delta": {"vpn_udp_classified": 3},
                      "endpoint_observation_delta": {"udp": {"count": 1, "last_peer": "203.0.113.53"}},
                      "mihomo_path": {"available": True, "transparent_udp_sessions_count": 1}}
        namespace["_record_phase_diagnostic"](lambda name, value: properties.append((name, value)), diagnostic)
        self.assertEqual(len(properties), 1)
        self.assertEqual(properties[0][0], "packet_phase_vpn")
        encoded = properties[0][1]
        self.assertLessEqual(len(encoded.encode("ascii")), 4096)
        self.assertEqual(json.loads(encoded), diagnostic)
        with self.assertRaises(AssertionError):
            namespace["_record_phase_diagnostic"](lambda *args: self.fail("oversize diagnostic was emitted"),
                                                   {"phase": "vpn", "detail": "x" * 4096})

        probe = functions["_probe_phase"]
        first_assert_line = min(node.lineno for node in ast.walk(probe) if isinstance(node, ast.Assert))
        record_calls = [node for node in ast.walk(probe) if isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Name) and node.func.id == "_record_phase_diagnostic"]
        self.assertEqual(len(record_calls), 1)
        self.assertLess(record_calls[0].lineno, first_assert_line)
        scenario = functions["test_core_vpn_emergency_direct_reentry_forwards_owned_tcp_udp_dns_without_leaks"]
        self.assertIn("record_property", {arg.arg for arg in scenario.args.args})
        phase_calls = [node for node in ast.walk(scenario) if isinstance(node, ast.Call)
                       and isinstance(node.func, ast.Name) and node.func.id == "_probe_phase"]
        self.assertEqual(len(phase_calls), 5)
        source = PACKET_TEST.read_text(encoding="utf-8")
        for label in ("vpn-supported-udp", "vpn-unknown-udp", "vpn-unsupported-udp",
                      "vpn-endpoint-unavailable", "vpn-empty-pool", "vpn-tcp-after-unknown",
                      "vpn-tcp-after-unsupported", "vpn-empty-pool-tcp",
                      "provider-recovery-confirmations", "provider-recovery-confirmation-1",
                      "provider-reentry", "vpn-after-selector-switch"):
            self.assertIn(label, source)

        self.assertIn('provider_calls_after_first == [("GET", "/configs", {}, None)]', source)
        self.assertIn('provider_calls_after_recovery == provider_calls_after_first', source)
        self.assertIn('"mihomo_path": _packet_refresh_native_proof(stack)', source)
        refresh_provider = functions["_refresh_packet_provider"]
        refresh_source = ast.get_source_segment(source, refresh_provider)
        for receipt_fact in (
            'native_proof.get("vpn_udp_policy_installed") is True',
            'native_proof.get("vpn_udp_policy_receipt_matches") is True',
            'receipt.get("active_hash_matches") is True',
            'receipt.get("runtime_incarnation_matches") is True',
            'receipt.get("version_supported") is True',
        ):
            self.assertIn(receipt_fact, refresh_source)
        self.assertLess(refresh_source.index('assert accepted_ok'),
                        refresh_source.index('assert isinstance(job, dict)'))
        self.assertLess(refresh_source.index('assert isinstance(job, dict)'),
                        refresh_source.index('assert ('))
        confirmation_1_receipt = source.index('"phase": "provider-recovery-confirmation-1"')
        confirmation_2_call = source.index('"decision_id": "packet-health-failure-2"')
        self.assertLess(confirmation_1_receipt, confirmation_2_call)
        self.assertIn('"mihomo_path": _packet_refresh_native_proof(stack)',
                      source[confirmation_1_receipt:confirmation_2_call])
        reentry_diagnostic = source.index('"phase": "provider-reentry"')
        reentry_assertion = source.index('assert code == 200 and reentered.get("ok") is True')
        self.assertLess(reentry_diagnostic, reentry_assertion)
        self.assertIn("CAPTURE_LIMIT = 512", source)
        self.assertIn("expect_active_server=False", source)
        capability_state = functions["_packet_capability_state"]
        capability_source = ast.get_source_segment(source, capability_state)
        self.assertIn('effective.get("active_server_id")', capability_source)
        self.assertIn('get_logical_runtime_name(logical_id)', capability_source)
        self.assertIn('active.get("server_id") is None', capability_source)
        assertions = [node.test for node in ast.walk(functions["_packet_capability_state"])
                      if isinstance(node, ast.Assert)]
        runtime_name_equality = ast.parse("effective_active_id == runtime_name", mode="eval").body
        empty_target_equality = ast.parse("effective_active_id is None", mode="eval").body
        self.assertTrue(any(ast.dump(runtime_name_equality, include_attributes=False)
                            in ast.dump(test, include_attributes=False) for test in assertions))
        self.assertTrue(any(ast.dump(empty_target_equality, include_attributes=False)
                            in ast.dump(test, include_attributes=False) for test in assertions))
        self.assertTrue(any(
            any(isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                and call.func.id == "isinstance"
                and len(call.args) == 2
                and isinstance(call.args[0], ast.Name) and call.args[0].id == "runtime_name"
                for call in ast.walk(test))
            and any(isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                    and call.func.id == "bool" and len(call.args) == 1
                    and isinstance(call.args[0], ast.Name) and call.args[0].id == "runtime_name"
                    for call in ast.walk(test))
            for test in assertions
        ))
        auto_eligibility = ast.get_source_segment(source, functions["_set_server_auto_eligibility"])
        self.assertIn('payload["vpn_auto_priority"] = priority', auto_eligibility)
        health_control = functions["_set_health_available"]
        self.assertEqual(ast.get_source_segment(source, health_control.returns), "dict[str, Any]")
        self.assertTrue(any(isinstance(node, ast.Return) and isinstance(node.value, ast.Name)
                            and node.value.id == "result" for node in ast.walk(health_control)))
        scenario_source = ast.get_source_segment(source, scenario)
        self.assertLess(
            scenario_source.index("preference_baseline = _set_server_auto_eligibility(api, logical_id, True, priority=1)"),
            scenario_source.index('_apply_global_mode(api, "direct"'),
        )
        self.assertIn('baseline_preferences.get("vpn_auto_priority_origin") == "manual"', scenario_source)
        self.assertTrue(all(any(isinstance(keyword.value, ast.Name) and keyword.value.id == "record_property"
                                for keyword in call.keywords)
                            or any(isinstance(arg, ast.Name) and arg.id == "record_property" for arg in call.args)
                            for call in phase_calls))

    def test_packet_scenario_proves_public_selector_change_with_second_owned_inbound(self):
        packet_source = PACKET_TEST.read_text(encoding="utf-8")
        packet_tree = ast.parse(packet_source)
        packet_functions = [node for node in packet_tree.body if isinstance(node, ast.FunctionDef)
                            and node.name ==
                            "test_core_vpn_emergency_direct_reentry_forwards_owned_tcp_udp_dns_without_leaks"]
        self.assertEqual(len(packet_functions), 1)
        packet_lines = packet_source.splitlines()
        scenario = "\n".join(packet_lines[packet_functions[0].lineno - 1:packet_functions[0].end_lineno])
        baseline = scenario.index("provider_binding_before_selector = binding_for(source_ref)")
        membership = scenario.index("_upsert_subscription_servers(")
        pool = scenario.index("set(candidate_ids) == {logical_id, ordinary_server.server_id}")
        public_switch = scenario.index('f"{api}/selector/vpn-auto/switch"')
        post_switch_probe = scenario.index(
            '_probe_phase(stack, DNS_VPN_SERVICE_VIP, "vpn-after-selector-switch"')
        self.assertLess(baseline, membership)
        self.assertLess(membership, pool)
        self.assertLess(pool, public_switch)
        self.assertLess(public_switch, post_switch_probe)
        for required in (
            '"selected_member_id") is None', 'ENDPOINT_SELECTOR_PORT',
            '"active_auto_server_id") == ordinary_server.server_id',
            '"native_incarnation_stable"', '"endpoint_peer_matches_vip_b"',
            '"client_dns_vpn_tcp_9080"', '"client_dns_vpn_udp_9081"',
            '"router_dns_vpn_tcp_9080"', '"router_dns_vpn_udp_9081"',
            '"router_tcp_9080"', '"router_udp_9081"', '"router_dns_5353"',
            'ENDPOINT_SELECTOR_PORT)) > 0',
            '"provider_binding_unchanged"', 'bridge.snapshot_calls() == provider_calls_before_selector',
            '"provider_candidate_only"',
        ):
            self.assertIn(required, scenario)
        self.assertIn('"vpn_auto_priority": 1', scenario)
        self.assertIn('get("vpn_auto_priority") == 1', scenario)

    def test_bounded_negative_udp_probe_is_fixed_and_reports_timeout_without_network(self):
        fixture = _fixture_module()

        class FakeSocket:
            def __init__(self, response):
                self.response = response
                self.timeout = None
                self.sent = None

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def settimeout(self, value):
                self.timeout = value

            def sendto(self, payload, target):
                self.sent = (payload, target)

            def recvfrom(self, _size):
                if isinstance(self.response, BaseException):
                    raise self.response
                return self.response

        timeout_socket = FakeSocket(TimeoutError("bounded"))
        with mock.patch.object(fixture.socket, "socket", return_value=timeout_socket):
            with self.assertRaises(TimeoutError):
                fixture._probe_udp_negative()
        self.assertEqual(timeout_socket.timeout, fixture.UDP_NEGATIVE_TIMEOUT)
        self.assertEqual(timeout_socket.sent, (fixture.PROBE_BODY,
                                               (fixture.SERVICE_VIP, fixture.UDP_ECHO_PORT)))

        success_socket = FakeSocket((fixture.PROBE_BODY,
                                     (fixture.SERVICE_VIP, fixture.UDP_ECHO_PORT)))
        with mock.patch.object(fixture.socket, "socket", return_value=success_socket):
            self.assertEqual(fixture._probe_udp_negative(),
                             {"service": "udp", "peer": fixture.SERVICE_VIP})

    def test_bounded_negative_tcp_probe_uses_only_the_fixed_service_vip(self):
        fixture = _fixture_module()

        class FakeConnection:
            def __init__(self, data):
                self.data = data
                self.timeout = None
                self.sent = None

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def settimeout(self, value):
                self.timeout = value

            def sendall(self, data):
                self.sent = data

            def recv(self, _size):
                return self.data

        connection = FakeConnection(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
        with mock.patch.object(fixture.socket, "create_connection", return_value=connection) as connect:
            result = fixture._probe_tcp_negative()
        connect.assert_called_once_with((fixture.SERVICE_VIP, fixture.HTTP_PORT),
                                        timeout=fixture.UDP_NEGATIVE_TIMEOUT)
        self.assertEqual(connection.timeout, fixture.UDP_NEGATIVE_TIMEOUT)
        self.assertIn(b"GET /probe HTTP/1.1", connection.sent)
        self.assertEqual(result["peer"], fixture.SERVICE_VIP)

        with mock.patch.object(fixture.socket, "create_connection", side_effect=TimeoutError("bounded")):
            with self.assertRaises(TimeoutError):
                fixture._probe_tcp_negative()

    def test_xray_availability_control_stops_and_restarts_only_owned_runtime(self):
        fixture = _fixture_module()

        class FakeRuntime:
            def __init__(self, running):
                self.running = running
                self.expected_exit = False

            def terminate(self):
                self.expected_exit = True
                self.running = False

        class FakeHandler:
            def __init__(self, path, available, runtime=None, profile=None):
                self.server = type("Server", (), {
                    "server_address": (fixture.ENDPOINT_WAN_IP, fixture.ENDPOINT_CONTROL_PORT),
                    "xray_runtime": runtime,
                    "xray_profile": profile,
                    "xray_lock": __import__("threading").Lock(),
                })()
                self.path = path
                body = json.dumps({"available": available}, separators=(",", ":")).encode("ascii")
                self.headers = {"Content-Length": str(len(body))}
                self.rfile = io.BytesIO(body)
                self.response = None

            def _send_json(self, status, payload):
                self.response = (status, payload)

        stopped_runtime = FakeRuntime(True)
        stop = FakeHandler("/xray/availability", available=False, runtime=stopped_runtime)
        fixture._EndpointHandler.do_POST(stop)
        self.assertTrue(stopped_runtime.expected_exit)
        self.assertIsNone(stop.server.xray_runtime)
        self.assertEqual(stop.response, (200, {"available": False}))

        restarted_runtime = FakeRuntime(True)
        start = FakeHandler("/xray/availability", available=True, profile={"fixed": True})
        fake_thread = mock.Mock()
        with (mock.patch.object(fixture, "_start_xray", return_value=restarted_runtime) as start_xray,
              mock.patch.object(fixture, "_wait_tcp") as wait_tcp,
              mock.patch.object(fixture.threading, "Thread", return_value=fake_thread)):
            fixture._EndpointHandler.do_POST(start)
        start_xray.assert_called_once_with({"fixed": True})
        wait_tcp.assert_called_once()
        fake_thread.start.assert_called_once_with()
        self.assertIs(start.server.xray_runtime, restarted_runtime)
        self.assertEqual(start.response, (200, {"available": True}))

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
        self.assertEqual(seen_limits, [2048, 2048])

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
                                  "run": staticmethod(run), "PIPE": -1, "TimeoutExpired": TimeoutError}),
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
            FakeServer((fixture.PROTECTED_VIP, fixture.HTTP_PORT), tcp=True),
            FakeServer((fixture.PROTECTED_VIP, fixture.PROTECTED_UDP_PORT), tcp=False),
            FakeServer((fixture.DNS_UPSTREAM_IP, 53), tcp=False),
            FakeServer((fixture.DNS_UPSTREAM_IP, 53), tcp=True),
            FakeServer((fixture.DNS_VPN_SERVICE_VIP, fixture.HTTP_PORT), tcp=True),
            FakeServer((fixture.DNS_VPN_SERVICE_VIP, fixture.UDP_ECHO_PORT), tcp=False),
            FakeServer(("127.0.0.1", fixture.ENDPOINT_TLS_DECOY_PORT), tcp=True),
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
            self.assertTrue(ready["sockets"]["xray_tcp_5302"])

            del control.role_servers
            missing = fixture._endpoint_status(control, FakeRuntime())
            self.assertFalse(missing["ready"])
            self.assertFalse(missing["sockets"]["http_tcp_9080"])
            self.assertFalse(missing["sockets"]["udp_echo_9081"])
            self.assertFalse(missing["sockets"]["dns_udp_5353"])
            self.assertFalse(missing["sockets"]["protected_tcp_9080"])
            self.assertFalse(missing["sockets"]["protected_udp_9081"])
            self.assertFalse(missing["sockets"]["dns_upstream_udp_53"])
            self.assertFalse(missing["sockets"]["dns_upstream_tcp_53"])

            control.role_servers = (servers[0], servers[1],
                                    FakeServer((fixture.SERVICE_VIP, fixture.DNS_PORT), tcp=False), servers[3])
            wrong_udp = fixture._endpoint_status(control, FakeRuntime())
            self.assertFalse(wrong_udp["ready"])
            self.assertFalse(wrong_udp["sockets"]["udp_echo_9081"])

            control.role_servers = (servers[0], servers[1], servers[2],
                                    FakeServer((fixture.SERVICE_VIP, fixture.UDP_ECHO_PORT), tcp=False),
                                    servers[4], servers[5])
            wrong_dns = fixture._endpoint_status(control, FakeRuntime())
            self.assertFalse(wrong_dns["ready"])
            self.assertFalse(wrong_dns["sockets"]["dns_udp_5353"])

            control.role_servers = (servers[0], servers[1], servers[2], servers[3],
                                    FakeServer((fixture.PROTECTED_VIP, fixture.HTTP_PORT), tcp=False),
                                    servers[5], servers[6], servers[7])
            wrong_protected = fixture._endpoint_status(control, FakeRuntime())
            self.assertFalse(wrong_protected["ready"])
            self.assertFalse(wrong_protected["sockets"]["protected_tcp_9080"])

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
        flows_node = next(node for node in worker_tree.body if isinstance(node, ast.Assign)
                          and any(isinstance(target, ast.Name) and target.id == "FLOWS"
                                  for target in node.targets))
        self.assertEqual(ast.literal_eval(flows_node.value), {
            "tcp": {"snaplen": 54, "filter": "(tcp and dst host 203.0.113.53 and dst port 9080) or "
                                                  "(tcp and dst host 198.51.100.77 and dst port 9080) or "
                                                  "(tcp and dst host 10.250.240.2 and dst port 9080) or "
                                                  "(tcp and dst host 198.18.240.2 and (dst port 5301 or dst port 5302 or dst port 5303)) or "
                                                  "(tcp and dst host 1.1.1.1 and dst port 53)"},
            "udp": {"snaplen": 42, "filter": "(udp and dst host 203.0.113.53 and "
                                                  "(dst port 9081 or dst port 5353)) or "
                                                  "(udp and dst host 198.51.100.77 and dst port 9081) or "
                                                  "(udp and dst host 10.250.240.2 and dst port 9081) or "
                                                  "(udp and dst host 1.1.1.1 and dst port 53)"},
        })
        for flow in ("10.250.240.2 and dst port 9080", "10.250.240.2 and dst port 9081"):
            self.assertIn(flow, start)
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
        packet_lines = source.splitlines()

        def packet_function_source(name: str) -> str:
            matches = [node for node in tree.body
                       if isinstance(node, ast.FunctionDef) and node.name == name]
            self.assertEqual(1, len(matches), f"expected one packet source function named {name}")
            node = matches[0]
            return "\n".join(packet_lines[node.lineno - 1:node.end_lineno])

        udp_contract = packet_function_source("_probe_vpn_udp_contract_phase")
        empty_pool_tcp = packet_function_source("_probe_vpn_tcp_empty_pool")
        self.assertIn('"core_counter_delta": core_delta', udp_contract)
        self.assertIn('"direct_udp_capture_delta": direct_flow_deltas', udp_contract)
        self.assertIn('direct_flow_deltas == {"client_source_to_service": 0, "mihomo_direct_to_service": 0}',
                      udp_contract)
        self.assertIn('(CLIENT_IP, SERVICE_VIP, "udp", 9081)', udp_contract)
        self.assertIn('(ROUTER_WAN_IP, SERVICE_VIP, "udp", 9081)', udp_contract)
        self.assertIn('attempt.get("peer") == SERVICE_VIP', udp_contract)
        self.assertNotIn('core_delta["direct_lan_forward"] == 0', udp_contract)
        self.assertIn('"core_counter_delta": core_delta', empty_pool_tcp)
        self.assertIn('deltas == {"client_direct": 0, "router_direct": 0, "vpn_proxy": 0}', empty_pool_tcp)
        self.assertIn('(CLIENT_IP, SERVICE_VIP, "tcp", 9080)', empty_pool_tcp)
        self.assertIn('(ROUTER_WAN_IP, SERVICE_VIP, "tcp", 9080)', empty_pool_tcp)
        self.assertIn('endpoint_http_delta == 0', empty_pool_tcp)
        self.assertNotIn('core_delta["direct_lan_forward"] == 0', empty_pool_tcp)
        selector_path = ROOT / "backend" / "fwrouter_api" / "services" / "selector.py"
        selector_source = selector_path.read_text(encoding="utf-8")
        selector_tree = ast.parse(selector_source)
        selector_functions = [node for node in selector_tree.body
                              if isinstance(node, ast.FunctionDef) and node.name == "get_vpn_auto_state"]
        self.assertEqual(1, len(selector_functions), "expected one canonical read-only selector-state projection")
        selector_lines = selector_source.splitlines()
        selector_state_source = "\n".join(
            selector_lines[selector_functions[0].lineno - 1:selector_functions[0].end_lineno]
        )
        self.assertLess(selector_state_source.index("elif not enabled_candidate_ids:"),
                        selector_state_source.index("elif not auto_selectable_candidate_ids:"))
        self.assertIn('problem_code = "vpn_auto_no_candidates"', selector_state_source)
        self.assertIn('problem_code = "vpn_auto_no_auto_selectable_candidates"', selector_state_source)
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
        core_delta_pairs = [
            (key, value)
            for node in ast.walk(probe_phase) if isinstance(node, ast.Dict)
            for key, value in zip(node.keys, node.values)
            if isinstance(key, ast.Constant) and key.value == "core_counter_delta"
        ]
        self.assertTrue(any(isinstance(value, ast.Name) and value.id == "core_counters"
                            for _, value in core_delta_pairs),
                        "phase summary must store the observed Core counter delta")
        endpoint_delta_keys = [
            key.value
            for node in ast.walk(probe_phase) if isinstance(node, ast.Dict)
            for key in node.keys
            if isinstance(key, ast.Constant) and key.value == "endpoint_observation_delta"
        ]
        self.assertTrue(endpoint_delta_keys,
                        "phase summary must retain endpoint observations separately from counter proof")
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
                         "unscoped inet fwrouter_v2 fwrouter_direct / global direct path counter; also counts host-output and host-local direct branches, diagnostic only (not flow-specific leak proof)")
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
            "DNS_UPSTREAM_IP": "1.1.1.1",
            "DNS_VPN_SERVICE_VIP": "198.51.100.77",
            "DNS_BYPASS_NEGATIVE_IP": "8.8.8.8",
            "SERVICE_VIP": "203.0.113.53", "PROTECTED_VIP": "10.250.240.2",
            "ENDPOINT_XRAY_PORT": 5301, "ENDPOINT_XRAY_SELECTOR_PORT": 5302, "ENDPOINT_XRAY_REALITY_PORT": 5303,
            "HTTP_PORT": 9080, "UDP_ECHO_PORT": 9081, "DNS_PORT": 5353,
            "CLIENT_CONTROL_PORT": 8081, "ENDPOINT_CONTROL_PORT": 8082,
            "DNS_NAME": "probe.fwrouter.test", "DNS_NEGATIVE_NAME": "missing.fwrouter.test",
            "LEAK_DNS_TARGET": "127.0.0.11", "LEAK_RESERVED_TARGET": "192.0.2.1",
            "LEAK_RESERVED_PORT": 9,
        }
        for key, value in expected.items():
            self.assertEqual(value, values[key], key)
        self.assertRegex(values["XRAY_ID"], r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
        self.assertRegex(values["XRAY_SELECTOR_ID"], r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
        vip = ipaddress.ip_address(values["SERVICE_VIP"])
        for network in ("10.0.0.0/8", "100.64.0.0/10", "172.16.0.0/12", "192.168.0.0/16",
                        "127.0.0.0/8", "169.254.0.0/16", "224.0.0.0/4", "240.0.0.0/4"):
            self.assertNotIn(vip, ipaddress.ip_network(network))
        self.assertNotIn(vip, ipaddress.ip_network(values["CLIENT_NET"]))
        self.assertNotIn(vip, ipaddress.ip_network(values["WAN_NET"]))
        dns_vip = ipaddress.ip_address(values["DNS_VPN_SERVICE_VIP"])
        self.assertIn(dns_vip, ipaddress.ip_network("198.51.100.0/24"))
        self.assertNotIn(dns_vip, ipaddress.ip_network(values["CLIENT_NET"]))
        self.assertNotIn(dns_vip, ipaddress.ip_network(values["WAN_NET"]))

    def test_protected_direct_probe_uses_real_fixed_tcp_udp_sockets_and_counterproof(self):
        values = _literal_assignments()
        self.assertEqual("10.250.240.2", values["PROTECTED_VIP"])
        self.assertEqual(9081, values["PROTECTED_UDP_PORT"])
        role_network = _function_source("_validate_role_network")
        endpoint_guard = _function_source("_install_guard")
        tcp_probe = _function_source("_probe_protected_tcp")
        udp_probe = _function_source("_probe_protected_udp")
        endpoint_status = _function_source("_endpoint_status")
        serve = _function_source("_serve")
        self.assertIn("PROTECTED_VIP", role_network)
        self.assertIn('vip_interfaces != {"lo"}', role_network)
        self.assertIn("PROTECTED_VIP", endpoint_guard)
        self.assertIn("PROTECTED_UDP_PORT", endpoint_guard)
        self.assertIn("socket.create_connection((PROTECTED_VIP, HTTP_PORT)", tcp_probe)
        self.assertIn("sock.sendto(PROBE_BODY, (PROTECTED_VIP, PROTECTED_UDP_PORT))", udp_probe)
        self.assertIn("_PROTECTED_OBS.record(\"tcp\"", _method_source("_EndpointHandler", "do_GET"))
        self.assertIn("_PROTECTED_OBS.record(\"udp\"", _method_source("_ProtectedUDPEchoHandler", "handle"))
        self.assertIn('"protected_tcp_9080"', endpoint_status)
        self.assertIn('"protected_udp_9081"', endpoint_status)
        self.assertIn('"dns_upstream_udp_53"', endpoint_status)
        self.assertIn('"dns_upstream_tcp_53"', endpoint_status)
        self.assertIn("_BoundedHTTPServer((PROTECTED_VIP, HTTP_PORT)", serve)
        self.assertIn("_BoundedUDPServer((PROTECTED_VIP, PROTECTED_UDP_PORT)", serve)

        packet_source = (ROOT / "tests/application_acceptance/test_packet_dataplane.py").read_text(encoding="utf-8")
        packet_tree = ast.parse(packet_source)
        probe = next(node for node in packet_tree.body if isinstance(node, ast.FunctionDef)
                     and node.name == "_probe_protected_direct_phase")
        probe_source = "\n".join(packet_source.splitlines()[probe.lineno - 1:probe.end_lineno])
        self.assertIn("_assert_packet_runtime(stack, mode=\"vpn\", policy_required=True)", probe_source)
        self.assertIn("vpn_tcp_classified\"] == 0", probe_source)
        self.assertIn("vpn_udp_classified\"] == 0", probe_source)
        self.assertIn('native_before == native_after', probe_source)
        self.assertIn('selection_before == selection_after', probe_source)
        self.assertIn('guard_delta["router_protected_tcp_forward"]["packets"] > 0', probe_source)
        self.assertIn('guard_delta["router_protected_udp_forward"]["packets"] > 0', probe_source)
        self.assertIn('guard_delta["router_protected_tcp_output"]["packets"] == 0', probe_source)
        self.assertIn('guard_delta["router_protected_udp_output"]["packets"] == 0', probe_source)

    def test_dns53_packet_acceptance_uses_real_native_service_and_fixed_private_upstream(self):
        source = (ROOT / "tests/application_acceptance/test_packet_dataplane.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        def packet_function_source(name):
            selected = next(item for item in tree.body if isinstance(item, ast.FunctionDef)
                            and item.name == name)
            return "\n".join(source.splitlines()[selected.lineno - 1:selected.end_lineno])

        node = next(item for item in tree.body if isinstance(item, ast.FunctionDef)
                    and item.name == "_probe_dns53_policy_phase")
        phase = "\n".join(source.splitlines()[node.lineno - 1:node.end_lineno])
        self.assertIn("/rules/manual/apply", phase)
        self.assertIn("_router_guard_counter_delta", phase)
        self.assertIn("_packet_counts()", phase)
        self.assertIn("dns53-observations", phase)
        self.assertIn('"endpoint_status_rejection_delta"', phase)
        self.assertIn('"flags_unsupported"', phase)
        self.assertIn('"last_flags"', phase)
        rejection_helper = next(item for item in ast.walk(node)
                                if isinstance(item, ast.FunctionDef)
                                and item.name == "endpoint_rejection_delta")
        namespace = {"Any": object}
        exec(compile(ast.Module(body=[rejection_helper], type_ignores=[]),
                     str(ROOT / "tests/application_acceptance/test_packet_dataplane.py"), "exec"), namespace)
        rejection_delta = namespace["endpoint_rejection_delta"](
            {"rejection_counts": {"flags_unsupported": 1}},
            {"rejection_counts": {"flags_unsupported": 2},
             "last_rejection": {"reason": "flags_unsupported", "transport": "udp", "flags": 0x0300}},
        )
        self.assertEqual(rejection_delta, {
            "counts": {"query_size_invalid": None, "flags_unsupported": 1,
                       "question_boundary_invalid": None, "name_invalid": None},
            "last_reason": "flags_unsupported", "last_transport": "udp", "last_flags": 0x0300,
        })
        self.assertEqual(set(rejection_delta), {"counts", "last_reason", "last_transport", "last_flags"})
        redacted_delta = namespace["endpoint_rejection_delta"](
            {"rejection_counts": {"flags_unsupported": 1}},
            {"rejection_counts": {"flags_unsupported": 2},
             "last_rejection": {"reason": "SECRET_SENTINEL", "transport": "private", "flags": 65_536}},
        )
        self.assertEqual(redacted_delta["counts"]["flags_unsupported"], 1)
        self.assertEqual(redacted_delta["last_reason"], None)
        self.assertEqual(redacted_delta["last_transport"], None)
        self.assertEqual(redacted_delta["last_flags"], None)
        self.assertNotIn("SECRET_SENTINEL", json.dumps(redacted_delta))
        self.assertIn("nftset_probe_ok", phase)
        self.assertIn("router_nxdomain", phase)
        self.assertIn("/probe/dns53-flows", phase)
        self.assertIn("dns53-flow-observations", phase)
        self.assertIn("vpn_udp_classified", phase)
        self.assertIn("vpn_alias_udp_router_direct", phase)
        self.assertIn('selected_flow_delta == {"vpn_domain_tcp": 1, "vpn_domain_udp": 1,', phase)
        selective_default_set = phase.index('_apply_selective_default(api, "vpn"')
        provider_calls_before = phase.index('selective_provider_calls_before = stack["provider_bridge"].snapshot_calls()')
        selective_mode_set = phase.index('_apply_global_mode(api, "selective"')
        selective_dns_probe = phase.index('f"{CLIENT_CONTROL}/probe/dns53-flows"', selective_mode_set)
        mode_restore = phase.index('_apply_global_mode(api, original_mode')
        default_restore = phase.index('restored_default_data = _apply_selective_default(')
        selective_helper = packet_function_source("_apply_selective_default")
        self.assertLess(selective_default_set, selective_mode_set)
        self.assertLess(provider_calls_before, selective_default_set)
        self.assertLess(selective_mode_set, selective_dns_probe)
        self.assertLess(selective_dns_probe, mode_restore)
        self.assertLess(mode_restore, default_restore)
        self.assertIn('selective_restore_errors["mode"] = exc', phase)
        self.assertIn('selective_restore_errors["default"] = exc', phase)
        self.assertIn('"mode_error_class": fixed_exception_class(selective_restore_errors["mode"])', phase)
        self.assertIn('"default_error_class": fixed_exception_class(selective_restore_errors["default"])', phase)
        self.assertIn('"api_error": _bounded_reconcile_summary(accepted)', selective_helper)
        cleanup_start = phase.index('cleanup_code, cleanup = http_json(')
        cleanup_receipt = phase.index('selective_priority_proof["cleanup_receipt"] = {')
        diagnostic_record = phase.index('_record_phase_diagnostic(record_property, selective_priority_proof)')
        primary_reraise = phase.index('if selective_primary_error is not None:')
        self.assertLess(cleanup_start, cleanup_receipt)
        self.assertLess(cleanup_receipt, diagnostic_record)
        self.assertLess(diagnostic_record, primary_reraise)
        self.assertIn('raise selective_primary_error.with_traceback(selective_primary_traceback)', phase)
        self.assertIn('from next(\n                iter(selective_restore_errors.values()))', phase)
        self.assertIn('from cleanup_error', phase)
        self.assertIn('except Exception as exc:\n        cleanup_error = exc', phase)
        self.assertIn('fixed_exception_class(cleanup_error)', phase)
        proof_initialization = phase.index('selective_priority_proof: dict[str, Any] = {')
        try_start = phase.index('    try:\n        _apply_selective_default(api, "vpn"')
        self.assertLess(proof_initialization, try_start)
        self.assertIn('"phase": "native-dns53-selective-direct-priority"', phase)
        self.assertIn('selective_priority_proof.get("last_peer_matches") == {', phase)
        classifier = next(item for item in ast.walk(node)
                          if isinstance(item, ast.FunctionDef) and item.name == "fixed_peer_class")
        classifier_namespace = {
            "Any": object,
            "SERVICE_VIP": "203.0.113.53",
            "DNS_VPN_SERVICE_VIP": "198.51.100.77",
            "CLIENT_IP": "10.240.0.2",
        }
        exec(compile(ast.Module(body=[classifier], type_ignores=[]),
                     str(ROOT / "tests/application_acceptance/test_packet_dataplane.py"), "exec"),
             classifier_namespace)
        classify_peer = classifier_namespace["fixed_peer_class"]
        self.assertEqual({
            "service": classify_peer("203.0.113.53"),
            "vpn_alias": classify_peer("198.51.100.77"),
            "client": classify_peer("10.240.0.2"),
            "loopback": classify_peer("127.0.0.1"),
            "other": classify_peer("SECRET_PEER_SENTINEL"),
            "missing": classify_peer(None),
        }, {
            "service": "service_vip", "vpn_alias": "dns_vpn_vip", "client": "client",
            "loopback": "localhost", "other": "other", "missing": "missing",
        })
        self.assertNotIn("SECRET_PEER_SENTINEL", json.dumps({
            key: classify_peer(value) for key, value in (
                ("service", "203.0.113.53"), ("vpn_alias", "198.51.100.77"),
                ("client", "10.240.0.2"), ("loopback", "127.0.0.1"),
                ("other", "SECRET_PEER_SENTINEL"), ("missing", None),
            )
        }))
        peer_class_comprehensions = [item for item in ast.walk(node)
                                     if isinstance(item, ast.DictComp)
                                     and isinstance(item.value, ast.Call)
                                     and isinstance(item.value.func, ast.Name)
                                     and item.value.func.id == "fixed_peer_class"]
        self.assertEqual(1, len(peer_class_comprehensions))
        peer_class_generator = peer_class_comprehensions[0].generators[0]
        self.assertIsInstance(peer_class_generator.iter, ast.Tuple)
        self.assertEqual(
            ["vpn_domain_tcp", "vpn_domain_udp", "direct_domain_tcp", "direct_domain_udp"],
            [item.value for item in peer_class_generator.iter.elts],
        )
        self.assertNotIn('str(selective_primary_error)', phase)
        self.assertNotIn('str(cleanup_error)', phase)
        for required in (
            '"native-dns53-selective-direct-priority"',
            'selective_flows.get("results") == {',
            '"direct_domain_tcp": last_peers.get("direct_domain_tcp") == CLIENT_IP',
            '"direct_domain_udp": last_peers.get("direct_domain_udp") == CLIENT_IP',
            '"vpn_domain_tcp": last_peers.get("vpn_domain_tcp") == SERVICE_VIP',
            '"vpn_domain_udp": last_peers.get("vpn_domain_udp") == SERVICE_VIP',
            'selective_core["direct_lan_forward"]',
            '_core_selective_vpn_counter_snapshot()',
            '"direct_client_to_vpn_tcp"', '"direct_router_to_vpn_udp"',
            'selective_priority_proof.get("state_restored") is True',
            'selective_priority_proof.get("provider_api_calls_unchanged") is True',
            'stack["provider_bridge"].snapshot_calls() == selective_provider_calls_before',
        ):
            self.assertIn(required, phase)
        for required in (
            'f"{api}/routing/global"', '"selective_default": selective_default',
            '"selective-default-vpn", "selective-default-restore"',
            'routing.get("selective_default") == selective_default',
        ):
            self.assertIn(required, selective_helper)
        selective_counter = packet_function_source("_core_selective_vpn_counter_snapshot")
        self.assertIn('"fwrouter vpn mark tcp:5202"', selective_counter)
        self.assertIn('"fwrouter vpn mark udp:5203"', selective_counter)
        self.assertIn('rule.get("chain") != "fwrouter_vpn"', selective_counter)
        self.assertIn('"vpn_domain_tcp_local"', phase)
        self.assertIn('"unexpected_upstream_rule_count") == 0', phase)
        self.assertIn('upstream_domains") == ["vpn.test"]', phase)
        self.assertIn('unexpected_upstream_rule_count") == 0', phase)
        fixture_source = (ROOT / "tests/acceptance/network_fixture.py").read_text(encoding="utf-8")
        self.assertIn("_BoundedDnsTCPServer((DNS_UPSTREAM_IP, 53)", fixture_source)
        self.assertIn("_BoundedUDPServer((DNS_UPSTREAM_IP, 53)", fixture_source)
        self.assertIn('(DNS_UPSTREAM_IP, 32)', fixture_source)
        self.assertIn("target=DNS_BYPASS_NEGATIVE_IP", fixture_source)
        self.assertNotIn("--network=host", fixture_source)
        worker = (ROOT / "tests/application_acceptance/worker.py").read_text(encoding="utf-8")
        self.assertIn("dnsmasq.subprocess = SimpleNamespace", worker)
        self.assertIn("/usr/sbin/dnsmasq", worker)
        self.assertIn("/api/v2/__acceptance/dns53/configure", worker)
        self.assertIn('"owned_paths_cleaned": cleaned', worker)
        self.assertIn('import re', worker.split('def _clean_environment', 1)[0])
        self.assertIn('except subprocess.TimeoutExpired:', worker)
        self.assertIn('"--user=root", "--group=root"', worker)
        self.assertIn('"native_process_arguments_match": argv_matches', worker)
        fixture_source = (ROOT / "tests/acceptance/network_fixture.py").read_text(encoding="utf-8")
        self.assertIn('DNS_SELECTED_PROBE_BODY = b"fwrouter-dns-selected-flow-v1"', fixture_source)
        self.assertIn('GET /probe/dns-selected HTTP/1.1', fixture_source)
        self.assertIn('if data == DNS_SELECTED_PROBE_BODY:', fixture_source)
        self.assertIn('_DNS_SELECTED_FLOW_OBS.record(f"{suffix}_udp"', fixture_source)
        selected_observations = fixture_source.split("class _DnsSelectedFlowObservations:", 1)[1].split(
            "_DNS_SELECTED_FLOW_OBS =", 1)[0]
        self.assertIn('self._last_peers[flow] = peer', selected_observations)
        self.assertIn('"last_peers": dict(self._last_peers)', selected_observations)
        self.assertIn('last_peers.get("direct_domain_tcp") == CLIENT_IP', phase)
        self.assertIn('last_peers.get("vpn_domain_tcp") == SERVICE_VIP', phase)
        self.assertIn('"native_dns53_selective_direct_priority"', packet_function_source("_write_capture_summary"))

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
        self.assertIn('output ip daddr {{ {ROUTER_LAN_IP}, {CLIENT_IP}, {ENDPOINT_WAN_IP}, {SERVICE_VIP}, {DNS_VPN_SERVICE_VIP}, {PROTECTED_VIP}, {DNS_BYPASS_NEGATIVE_IP} }}', guard)
        self.assertIn('output ip daddr {ENDPOINT_WAN_IP} tcp dport {ENDPOINT_XRAY_PORT}', guard)
        self.assertIn('output ip daddr {ENDPOINT_WAN_IP} tcp dport 22 accept', guard)
        self.assertIn('input ip saddr {{ {ROUTER_LAN_IP}, {ENDPOINT_WAN_IP}, {SERVICE_VIP}, {DNS_VPN_SERVICE_VIP}, {PROTECTED_VIP}, {DNS_BYPASS_NEGATIVE_IP} }}', guard)
        endpoint_input = guard.split('else:', 1)[1]
        self.assertNotIn('tcp dport 22 accept', endpoint_input)
        self.assertIn('hook input priority 0; policy drop;', guard)
        self.assertIn('hook output priority 0; policy drop;', guard)
        self.assertNotIn("fwrouter_v2", guard)
        self.assertIn('"nft", "delete", "table", "inet", TABLE_NAME', _function_source("_remove_guard"))

        tree = _tree()
        install = next(node for node in tree.body
                       if isinstance(node, ast.FunctionDef) and node.name == "_install_guard")
        captured: dict[str, bytes] = {}
        namespace = {
            "FixtureError": RuntimeError,
            "TABLE_NAME": "fwrouter_packet_fixture",
            "ROUTER_LAN_IP": "10.240.0.1", "CLIENT_IP": "10.240.0.2",
            "ROUTER_WAN_IP": "198.18.240.1",
            "ENDPOINT_WAN_IP": "198.18.240.2", "SERVICE_VIP": "203.0.113.53",
            "DNS_VPN_SERVICE_VIP": "198.51.100.77", "PROTECTED_VIP": "10.250.240.2",
            "DNS_UPSTREAM_IP": "1.1.1.1", "ENDPOINT_CONTROL_PORT": 8082,
            "DNS_BYPASS_NEGATIVE_IP": "8.8.8.8", "LEAK_DNS_TARGET": "127.0.0.11",
            "LEAK_RESERVED_TARGET": "192.0.2.1", "LEAK_RESERVED_PORT": 9,
            "CLIENT_CONTROL_PORT": 8081, "ENDPOINT_XRAY_PORT": 5301,
            "ENDPOINT_XRAY_SELECTOR_PORT": 5302, "ENDPOINT_XRAY_REALITY_PORT": 5303,
            "HTTP_PORT": 9080, "UDP_ECHO_PORT": 9081, "DNS_PORT": 5353,
            "PROTECTED_UDP_PORT": 9081,
            "_redact": lambda value: str(value),
            "_run_fixed": lambda _args, *, input_bytes: (captured.update(script=input_bytes)
                                                           or type("Result", (), {"returncode": 0})()),
        }
        exec(compile(ast.Module(body=[install], type_ignores=[]), str(FIXTURE), "exec"), namespace)
        namespace["_install_guard"]("client")
        rules = captured["script"].decode("ascii").splitlines()
        router_output = [rule for rule in rules if "output ip daddr 10.240.0.1" in rule]
        self.assertEqual(set(router_output), {
            "add rule inet fwrouter_packet_fixture output ip daddr 10.240.0.1 udp dport 53 accept",
            "add rule inet fwrouter_packet_fixture output ip daddr 10.240.0.1 tcp dport 53 accept",
        })
        self.assertIn("add rule inet fwrouter_packet_fixture output ip daddr { 10.240.0.1, 10.240.0.2, "
                      "198.18.240.2, 203.0.113.53, 198.51.100.77, 10.250.240.2, 8.8.8.8 } "
                      "ct state established,related accept", rules)
        self.assertLess(rules.index("add rule inet fwrouter_packet_fixture output ip daddr 127.0.0.11 drop"),
                        rules.index('add rule inet fwrouter_packet_fixture output oifname "lo" accept'))
        self.assertIn("add rule inet fwrouter_packet_fixture output meta nfproto ipv6 oifname != \"lo\" drop",
                      rules)
        namespace["_install_guard"]("endpoint")
        endpoint_rules = captured["script"].decode("ascii").splitlines()
        self.assertIn(
            "add rule inet fwrouter_packet_fixture input ip saddr { 10.240.0.2, 198.18.240.1, "
            "198.18.240.2, 203.0.113.53, 198.51.100.77, 10.250.240.2 } ip daddr 198.18.240.2 "
            "tcp dport { 5301, 5302, 5303, 8082 } accept", endpoint_rules)
        self.assertIn(
            "add rule inet fwrouter_packet_fixture output ip daddr 198.18.240.2 "
            "tcp dport { 5301, 5302, 5303, 8082 } accept", endpoint_rules)

    def test_dns53_flow_failure_receipt_is_fixed_and_projects_before_502(self):
        fixture_tree = _tree()
        selected = [node for node in fixture_tree.body if isinstance(node, ast.ClassDef)
                    and node.name in {"DNS53FlowProbeFailure", "DNS53ProbeFailure", "_ClientHandler"}]
        flow_probe = next(node for node in fixture_tree.body if isinstance(node, ast.FunctionDef)
                          and node.name == "_probe_dns53_resolved_flows")
        selected.append(flow_probe)
        fixture_ns = {
            "Any": object, "_QuietHandler": object,
            "FixtureError": type("FixtureError", (RuntimeError,), {}),
            "DNS53ProbeFailure": type("DNS53ProbeFailure", (RuntimeError,), {}),
            "DNS53FlowProbeFailure": type("DNS53FlowProbeFailure", (RuntimeError,), {}),
            "CLIENT_IP": "10.240.0.2", "ENDPOINT_WAN_IP": "198.18.240.2",
            "ROUTER_WAN_IP": "198.18.240.1",
        }
        exec(compile(ast.Module(body=selected, type_ignores=[]), str(FIXTURE), "exec"), fixture_ns)
        self.assertTrue(all(stage in _function_source("_probe_dns53_resolved_flows") for stage in (
            '"dns_resolution"', '"tcp_connect"', '"tcp_receive"', '"tcp_shape"', '"tcp_peer"',
            '"udp_receive"', '"udp_shape"')))
        self.assertIn('peer_class = ("missing"', _function_source("_probe_dns53_resolved_flows"))

        class FakeTcp:
            def __init__(self):
                self.chunks = [b'HTTP/1.1 200 OK\r\nContent-Length: 29\r\n\r\n'
                               b'{"ok":true,"peer":"127.0.0.1"}', b""]

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def settimeout(self, _timeout):
                pass

            def sendall(self, _payload):
                pass

            def recv(self, _size):
                return self.chunks.pop(0)

        class FakeUdp:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def settimeout(self, _timeout):
                pass

            def sendto(self, _payload, _address):
                pass

            def recvfrom(self, _size):
                raise TimeoutError("private timeout detail")

        flow_calls = []
        fixture_ns.update({
            "DNS_VPN_SERVICE_VIP": "198.51.100.77", "SERVICE_VIP": "203.0.113.53",
            "HTTP_PORT": 9080, "UDP_ECHO_PORT": 9081, "CONTROL_TIMEOUT": 3.0,
            "DNS_SELECTED_PROBE_BODY": b"fixed-probe", "json": json,
            "_dns53_request": lambda name, *, tcp: (flow_calls.append((name, tcp)) or
                                                      ("answer", "198.51.100.77")),
            "socket": mock.Mock(AF_INET=2, SOCK_DGRAM=2,
                                create_connection=lambda _address, timeout: FakeTcp(),
                                socket=lambda *_args: FakeUdp()),
        })
        with self.assertRaises(fixture_ns["DNS53FlowProbeFailure"]) as flow_error:
            fixture_ns["_probe_dns53_resolved_flows"]()
        self.assertEqual(flow_calls, [("probe.vpn.test", False)])
        self.assertEqual(flow_error.exception.receipt(), {
            "schema": "fwrouter-dns53-flow-failure/v1", "query_label": "vpn",
            "stage": "udp_receive", "error_code": "timeout",
        })

        packet_tree = ast.parse(PACKET_TEST.read_text(encoding="utf-8"))
        packet_funcs = {node.name: node for node in packet_tree.body if isinstance(node, ast.FunctionDef)
                        and node.name in {"_record_dns53_flow_failure", "_record_dns53_probe_failure", "_role_json"}}
        self.assertEqual(set(packet_funcs), {
            "_record_dns53_flow_failure", "_record_dns53_probe_failure", "_role_json",
        })
        packet_ns = {"Any": object, "json": json, "urllib": urllib}
        exec(compile(ast.Module(body=list(packet_funcs.values()), type_ignores=[]), str(PACKET_TEST), "exec"),
             packet_ns)
        receipt = {"schema": "fwrouter-dns53-flow-failure/v1", "query_label": "vpn",
                   "stage": "tcp_peer", "error_code": "response_mismatch", "peer_class": "other"}
        recorded = []
        self.assertTrue(packet_ns["_record_dns53_flow_failure"](
            lambda name, value: recorded.append((name, value)), receipt))
        self.assertEqual(recorded[0][0], "dns53_flow_failure")
        self.assertEqual(json.loads(recorded[0][1]), receipt)
        for invalid in (
            {**receipt, "raw_peer": "198.18.240.2"},
            {**receipt, "query_label": "probe.secret.test"},
            {**receipt, "stage": "tcp_peer", "peer_class": "198.18.240.2"},
            {**receipt, "stage": "tcp_connect", "peer_class": "other"},
        ):
            self.assertFalse(packet_ns["_record_dns53_flow_failure"](
                lambda *_args: self.fail("invalid flow receipt must not be emitted"), invalid))
        error_body = json.dumps({"ok": False, "error": "FixtureError",
                                 "dns53_flow_failure": receipt}).encode("ascii")

        def fail_http_request(_request, *, timeout):
            raise urllib.error.HTTPError("http://fixed-role.invalid/", 502, "bounded", {},
                                         io.BytesIO(error_body))

        events = []
        with mock.patch.object(urllib.request, "urlopen", side_effect=fail_http_request):
            with self.assertRaises(AssertionError) as caught:
                packet_ns["_role_json"]("http://fixed-role.invalid/probe/dns53-flows", method="POST",
                                         failure_record_property=lambda name, value: events.append((name, value)))
        self.assertEqual(events[0][0], "dns53_flow_failure")
        self.assertEqual(json.loads(events[0][1]), receipt)
        self.assertEqual(str(caught.exception), "{'status': 502, 'shape': 'dict'}")
        self.assertNotIn("198.18.240.2", events[0][1])

    def test_dns53_resolved_flow_accepts_direct_client_peer_but_rejects_unknown_peer(self):
        fixture_tree = _tree()
        selected = [node for node in fixture_tree.body if isinstance(node, ast.ClassDef)
                    and node.name == "DNS53FlowProbeFailure"]
        flow_probe = next(node for node in fixture_tree.body if isinstance(node, ast.FunctionDef)
                          and node.name == "_probe_dns53_resolved_flows")
        selected.append(flow_probe)
        fixture_ns = {
            "FixtureError": type("FixtureError", (RuntimeError,), {}),
            "DNS53FlowProbeFailure": type("DNS53FlowProbeFailure", (RuntimeError,), {}),
            "DNS_VPN_SERVICE_VIP": "198.51.100.77", "SERVICE_VIP": "203.0.113.53",
            "CLIENT_IP": "10.240.0.2", "ENDPOINT_WAN_IP": "198.18.240.2",
            "ROUTER_WAN_IP": "198.18.240.1", "HTTP_PORT": 9080, "UDP_ECHO_PORT": 9081,
            "CONTROL_TIMEOUT": 3.0, "DNS_SELECTED_PROBE_BODY": b"fixed-probe", "json": json,
        }

        def run_with_direct_peer(direct_peer):
            calls = []

            class FakeTcp:
                def __init__(self, peer):
                    body = json.dumps({"ok": True, "peer": peer}, separators=(",", ":")).encode("ascii")
                    self.chunks = [b"HTTP/1.1 200 OK\r\nContent-Length: "
                                   + str(len(body)).encode("ascii") + b"\r\n\r\n" + body, b""]

                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    return False

                def settimeout(self, _timeout):
                    pass

                def sendall(self, _payload):
                    pass

                def recv(self, _size):
                    return self.chunks.pop(0)

            class FakeUdp:
                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    return False

                def settimeout(self, _timeout):
                    pass

                def sendto(self, _payload, address):
                    self.address = address

                def recvfrom(self, _size):
                    return b"fixed-probe", self.address

            def create_connection(address, timeout):
                del timeout
                peer = "198.51.100.77" if address[0] == "198.51.100.77" else direct_peer
                return FakeTcp(peer)

            fixture_ns.update({
                "_dns53_request": lambda name, *, tcp: (
                    calls.append((name, tcp)) or
                    ("answer", "198.51.100.77" if name == "probe.vpn.test" else "203.0.113.53")
                ),
                "socket": mock.Mock(AF_INET=2, SOCK_DGRAM=2, create_connection=create_connection,
                                    socket=lambda *_args: FakeUdp()),
            })
            return calls, fixture_ns["_probe_dns53_resolved_flows"]

        exec(compile(ast.Module(body=selected, type_ignores=[]), str(FIXTURE), "exec"), fixture_ns)
        calls, probe = run_with_direct_peer("10.240.0.2")
        result = probe()
        self.assertEqual(result["results"], {
            "vpn": {"answer": "198.51.100.77", "tcp": True, "udp": True},
            "direct": {"answer": "203.0.113.53", "tcp": True, "udp": True},
        })
        self.assertEqual(calls, [("probe.vpn.test", False), ("probe.direct.test", False)])

        calls, probe = run_with_direct_peer("192.0.2.99")
        with self.assertRaises(fixture_ns["DNS53FlowProbeFailure"]) as failure:
            probe()
        self.assertEqual(calls, [("probe.vpn.test", False), ("probe.direct.test", False)])
        self.assertEqual(failure.exception.receipt(), {
            "schema": "fwrouter-dns53-flow-failure/v1", "query_label": "direct",
            "stage": "tcp_peer", "error_code": "response_mismatch", "peer_class": "other",
        })

    def test_dns53_failure_receipt_identifies_first_fixed_probe_and_projects_before_502(self):
        tree = _tree()
        selected = [node for node in tree.body if isinstance(node, ast.ClassDef)
                    and node.name in {"DNS53ProbeFailure", "DNS53FlowProbeFailure", "_ClientHandler"}]
        selected.extend(node for node in tree.body if isinstance(node, ast.FunctionDef)
                        and node.name == "_probe_dns53")
        failure_type = type("FixtureError", (RuntimeError,), {})
        namespace: dict[str, object] = {
            "Any": object, "_QuietHandler": object, "FixtureError": failure_type,
        }
        exec(compile(ast.Module(body=selected, type_ignores=[]), str(FIXTURE), "exec"), namespace)
        failure_class = namespace["DNS53ProbeFailure"]
        probe = namespace["_probe_dns53"]
        scenarios = (
            ("timeout", 0, TimeoutError("private diagnostic"), None, "vpn", "udp", "TimeoutError"),
            ("os_error", 1, ConnectionResetError("private diagnostic"), None,
             "vpn", "tcp", "OSError"),
            ("fixture_error", 2, failure_type("private diagnostic"), None,
             "direct", "udp", "FixtureError"),
            ("response_mismatch", 3, None, ("answer", "192.0.2.9"),
             "direct", "tcp", "FixtureError"),
        )
        expected_attempts = [("probe.vpn.test", False), ("probe.vpn.test", True),
                             ("probe.direct.test", False), ("probe.direct.test", True)]
        for error_code, fail_index, raised, returned, label, transport, error_type in scenarios:
            calls = []

            def dns_request(name, *, tcp):
                calls.append((name, tcp))
                if len(calls) - 1 == fail_index:
                    if raised is not None:
                        raise raised
                    return returned
                answer = "198.51.100.77" if name == "probe.vpn.test" else "203.0.113.53"
                return "answer", answer

            namespace["_dns53_request"] = dns_request
            with self.subTest(error_code=error_code):
                with self.assertRaises(failure_class) as caught:
                    probe()
                self.assertEqual(calls, expected_attempts[:fail_index + 1])
                self.assertEqual(caught.exception.original_error_type, error_type)
                self.assertEqual(caught.exception.receipt(), {
                    "schema": "fwrouter-dns53-probe-failure/v1",
                    "query_label": label, "transport": transport, "error_code": error_code,
                })
                self.assertNotIn("private diagnostic", json.dumps(caught.exception.receipt()))

        sent = []

        class Headers:
            @staticmethod
            def get(key, default=None):
                return "0" if key == "Content-Length" else default

        class Handler:
            path = "/probe/dns53"
            headers = Headers()

            @staticmethod
            def _send_json(status, body):
                sent.append((status, body))

        def timeout_request(_name, *, tcp):
            if tcp:
                raise TimeoutError("secret endpoint name and message")
            return "answer", "198.51.100.77"

        namespace["_dns53_request"] = timeout_request
        namespace["_ClientHandler"].do_POST(Handler())
        self.assertEqual(sent, [(502, {
            "ok": False, "error": "TimeoutError",
            "dns53_failure": {
                "schema": "fwrouter-dns53-probe-failure/v1", "query_label": "vpn",
                "transport": "tcp", "error_code": "timeout",
            },
        })])
        self.assertNotIn("probe.vpn.test", json.dumps(sent))
        self.assertNotIn("secret endpoint", json.dumps(sent))

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
        self.assertIn("XRAY_SELECTOR_ID", config)
        self.assertIn("ENDPOINT_XRAY_SELECTOR_PORT", config)
        self.assertIn('"protocol": "vless"', config)
        self.assertIn('"protocol": "freedom"', config)
        self.assertIn('"sendThrough": SERVICE_VIP', config)
        self.assertIn('"sendThrough": DNS_VPN_SERVICE_VIP', config)
        self.assertIn('"fixture-vless-a"', config)
        self.assertIn('"fixture-vless-b"', config)
        tree = _tree()
        config_node = next(node for node in tree.body
                           if isinstance(node, ast.FunctionDef) and node.name == "_expected_xray_config")
        namespace = {"Any": object, "ENDPOINT_WAN_IP": "198.18.240.2",
                     "ENDPOINT_XRAY_PORT": 5301, "ENDPOINT_XRAY_SELECTOR_PORT": 5302, "ENDPOINT_XRAY_REALITY_PORT": 5303,
                     "XRAY_ID": "88c7ce2a-465e-4e72-9c56-2a9e2fc84a51",
                     "XRAY_SELECTOR_ID": "e27bc911-23fb-4d12-8d51-bb1b9cb2cd08",
                     "SERVICE_VIP": "203.0.113.53", "DNS_VPN_SERVICE_VIP": "198.51.100.77"}
        fixture = _fixture_module()
        namespace.update({key: getattr(fixture, key) for key in ("ENDPOINT_TLS_DECOY_PORT", "XRAY_REALITY_ID", "XRAY_REALITY_PRIVATE_KEY", "XRAY_REALITY_SHORT_ID", "XRAY_REALITY_SERVER_NAME")})
        exec(compile(ast.Module(body=[config_node], type_ignores=[]), str(FIXTURE), "exec"), namespace)
        observed = namespace["_expected_xray_config"]()
        self.assertEqual([row["port"] for row in observed["inbounds"]], [5301, 5302, 5303])
        self.assertEqual([row["tag"] for row in observed["inbounds"]],
                         ["fixture-vless-a", "fixture-vless-b", "fixture-vless-reality"])
        self.assertEqual([row["settings"]["clients"][0]["id"] for row in observed["inbounds"]],
                         [namespace["XRAY_ID"], namespace["XRAY_SELECTOR_ID"], namespace["XRAY_REALITY_ID"]])
        self.assertEqual([row["tag"] for row in observed["outbounds"]],
                         ["fixture-egress-a", "fixture-egress-b"])
        self.assertEqual([row["sendThrough"] for row in observed["outbounds"]],
                         ["203.0.113.53", "198.51.100.77"])
        self.assertEqual(observed["routing"]["rules"], [
            {"type": "field", "inboundTag": ["fixture-vless-a"], "outboundTag": "fixture-egress-a"},
            {"type": "field", "inboundTag": ["fixture-vless-b"], "outboundTag": "fixture-egress-b"},
        ])
        reality = observed["inbounds"][2]["streamSettings"]
        self.assertEqual(reality["security"], "reality")
        self.assertEqual(reality["realitySettings"]["dest"], "127.0.0.1:9443")
        self.assertEqual(reality["realitySettings"]["serverNames"], ["reality.test"])
        self.assertEqual(reality["realitySettings"]["privateKey"], fixture.XRAY_REALITY_PRIVATE_KEY)
        self.assertEqual(reality["realitySettings"]["shortIds"], [fixture.XRAY_REALITY_SHORT_ID])
        tls_source = _function_source("_create_tls_decoy_material")
        self.assertIn('"/usr/bin/openssl"', tls_source)
        self.assertIn('timeout=10.0', tls_source)
        self.assertIn('os.chmod(key_path, 0o600', tls_source)
        self.assertIn('os.getuid()', tls_source)
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

    def test_fixed_http_peer_contract_accepts_both_owned_endpoint_vips_and_rejects_other_peers(self):
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
                               side_effect=lambda _target, timeout: FakeConnection(fixture.DNS_VPN_SERVICE_VIP)):
            result = fixture._probe_http()
        self.assertEqual(result["peer"], fixture.DNS_VPN_SERVICE_VIP)

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

    def test_dns_upstream_reply_receipt_counts_only_successful_socket_writes(self):
        fixture = _fixture_module()
        udp_handler_source = _method_source("_DnsUpstreamUDPHandler", "handle")
        tcp_handler_source = _method_source("_DnsUpstreamTCPHandler", "handle")
        self.assertLess(udp_handler_source.index("sock.sendto(response"),
                        udp_handler_source.index("record_reply("))
        self.assertLess(tcp_handler_source.index("self.request.sendall("),
                        tcp_handler_source.index("record_reply("))
        observations = fixture._DnsUpstreamObservations()
        original_observations = fixture._DNS_UPSTREAM_OBS
        fixture._DNS_UPSTREAM_OBS = observations

        class DatagramSocket:
            def __init__(self, fail=False):
                self.fail = fail
                self.sent = []

            def sendto(self, payload, address):
                if self.fail:
                    raise OSError("owned fixture send failed")
                self.sent.append((payload, address))

        try:
            query = fixture._dns_query("vpn.test")
            sock = DatagramSocket()
            handler = object.__new__(fixture._DnsUpstreamUDPHandler)
            handler.request = (query, sock)
            handler.client_address = (fixture.ROUTER_WAN_IP, 54321)
            handler.handle()
            after_success = observations.snapshot()
            self.assertEqual(after_success["counts"]["udp"], 1)
            self.assertEqual(after_success["counts"]["vpn_name"], 1)
            self.assertEqual(after_success["replies"]["udp"], 1)
            self.assertEqual(after_success["replies"]["vpn_name"], 1)
            self.assertEqual(sock.sent[0][1], (fixture.ROUTER_WAN_IP, 54321))

            failed = DatagramSocket(fail=True)
            failed_handler = object.__new__(fixture._DnsUpstreamUDPHandler)
            failed_handler.request = (query, failed)
            failed_handler.client_address = (fixture.ROUTER_WAN_IP, 54322)
            with self.assertRaisesRegex(OSError, "owned fixture send failed"):
                failed_handler.handle()
            after_failure = observations.snapshot()
            self.assertEqual(after_failure["counts"]["udp"], 2)
            self.assertEqual(after_failure["counts"]["vpn_name"], 2)
            self.assertEqual(after_failure["replies"]["udp"], 1)
            self.assertEqual(after_failure["replies"]["vpn_name"], 1)
            self.assertEqual(set(after_failure["replies"]),
                             {"udp", "tcp", "vpn_name", "direct_name", "other_name"})

            unsupported_flags = bytearray(query)
            unsupported_flags[2] = 0x02
            flags_socket = DatagramSocket()
            flags_handler = object.__new__(fixture._DnsUpstreamUDPHandler)
            flags_handler.request = (bytes(unsupported_flags), flags_socket)
            flags_handler.client_address = (fixture.ROUTER_WAN_IP, 54323)
            flags_handler.handle()
            after_rejected = observations.snapshot()
            self.assertEqual(after_rejected["counts"]["udp"], 3)
            self.assertEqual(after_rejected["counts"]["vpn_name"], 3)
            self.assertEqual(after_rejected["replies"]["udp"], 1)
            self.assertEqual(flags_socket.sent, [])
            self.assertEqual(after_rejected["rejection_counts"]["flags_unsupported"], 1)
            self.assertEqual(after_rejected["last_rejection"], {
                "reason": "flags_unsupported", "transport": "udp", "flags": 0x0200,
            })

            recursive_ad_query = bytearray(query)
            recursive_ad_query[3] = 0x20
            recursive_ad_query = bytes(recursive_ad_query)
            self.assertEqual(int.from_bytes(recursive_ad_query[2:4], "big"), 0x0120)

            def expected_vpn_answer(request):
                question_end = request.find(b"\0", 12)
                question = request[12:question_end + 5]
                answer = (b"\xc0\x0c\x00\x01\x00\x01\x00\x00\x00\x1e\x00\x04"
                          + fixture.socket.inet_aton("198.51.100.77"))
                return request[:2] + b"\x81\x80\x00\x01\x00\x01\x00\x00\x00\x00" + question + answer

            ad_udp_socket = DatagramSocket()
            ad_udp_handler = object.__new__(fixture._DnsUpstreamUDPHandler)
            ad_udp_handler.request = (recursive_ad_query, ad_udp_socket)
            ad_udp_handler.client_address = (fixture.ROUTER_WAN_IP, 54324)
            ad_udp_handler.handle()
            udp_response = ad_udp_socket.sent[0][0]
            self.assertEqual(udp_response, expected_vpn_answer(recursive_ad_query))
            udp_response_flags = int.from_bytes(udp_response[2:4], "big")
            self.assertEqual(udp_response_flags, 0x8180)
            self.assertTrue(udp_response_flags & 0x8000)  # QR
            self.assertTrue(udp_response_flags & 0x0100)  # RD
            self.assertTrue(udp_response_flags & 0x0080)  # RA
            self.assertFalse(udp_response_flags & 0x0020)  # AD remains clear

            class StreamSocket:
                def __init__(self, payload):
                    self.payload = payload
                    self.sent = []

                def recv(self, size):
                    part, self.payload = self.payload[:size], self.payload[size:]
                    return part

                def sendall(self, payload):
                    self.sent.append(payload)

            framed_query = len(recursive_ad_query).to_bytes(2, "big") + recursive_ad_query
            ad_tcp_socket = StreamSocket(framed_query)
            ad_tcp_handler = object.__new__(fixture._DnsUpstreamTCPHandler)
            ad_tcp_handler.request = ad_tcp_socket
            ad_tcp_handler.client_address = (fixture.ROUTER_WAN_IP, 54325)
            ad_tcp_handler.handle()
            tcp_frame = ad_tcp_socket.sent[0]
            tcp_length = int.from_bytes(tcp_frame[:2], "big")
            self.assertEqual(tcp_length, len(tcp_frame) - 2)
            self.assertEqual(tcp_frame[2:], expected_vpn_answer(recursive_ad_query))
            tcp_response_flags = int.from_bytes(tcp_frame[4:6], "big")
            self.assertEqual(tcp_response_flags, 0x8180)
            self.assertTrue(tcp_response_flags & 0x8000)  # QR
            self.assertTrue(tcp_response_flags & 0x0100)  # RD
            self.assertTrue(tcp_response_flags & 0x0080)  # RA
            self.assertFalse(tcp_response_flags & 0x0020)  # AD remains clear
            after_ad_success = observations.snapshot()
            self.assertEqual(after_ad_success["counts"]["udp"], 4)
            self.assertEqual(after_ad_success["counts"]["tcp"], 1)
            self.assertEqual(after_ad_success["counts"]["vpn_name"], 5)
            self.assertEqual(after_ad_success["replies"]["udp"], 2)
            self.assertEqual(after_ad_success["replies"]["tcp"], 1)
            self.assertEqual(after_ad_success["replies"]["vpn_name"], 3)

            malformed = (
                b"\x12\x34\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00"
                + b"\x40" + b"a" * 64 + b"\x00\x00\x01\x00\x01"
            )
            self.assertLess(len(malformed), 512)
            self.assertEqual(malformed[12], 64)
            self.assertIsNone(fixture._dns_upstream_query_name(malformed))
            self.assertEqual(fixture._dns_upstream_response(malformed, "udp", fixture.ROUTER_WAN_IP), b"")
            after_malformed = observations.snapshot()
            self.assertEqual(after_malformed["counts"], after_ad_success["counts"])
            self.assertEqual(after_malformed["replies"], after_ad_success["replies"])
            self.assertEqual(after_malformed["rejection_counts"]["name_invalid"], 1)
            self.assertEqual(after_malformed["last_rejection"], {
                "reason": "name_invalid", "transport": "udp", "flags": 0x0100,
            })
            fixture._dns_upstream_response(b"\x12\x34", "udp", fixture.ROUTER_WAN_IP)
            boundary_invalid = b"\x12\x34\x01\x00" + b"\x00" * 8 + b"a" * 5
            fixture._dns_upstream_response(boundary_invalid, "tcp", fixture.ROUTER_WAN_IP)
            after_parser_rejections = observations.snapshot()
            self.assertEqual(after_parser_rejections["rejection_counts"], {
                "query_size_invalid": 1, "flags_unsupported": 1,
                "question_boundary_invalid": 1, "name_invalid": 1,
            })
            self.assertEqual(after_parser_rejections["last_rejection"], {
                "reason": "question_boundary_invalid", "transport": "tcp", "flags": 0x0100,
            })
        finally:
            fixture._DNS_UPSTREAM_OBS = original_observations

    def test_packet_dns53_status_uses_uncached_native_receipt_with_bounded_fields(self):
        worker_source = (ROOT / "tests" / "application_acceptance" / "worker.py").read_text(encoding="utf-8")
        worker_tree = ast.parse(worker_source)
        status_nodes = [node for node in ast.walk(worker_tree)
                        if isinstance(node, ast.FunctionDef) and node.name == "packet_dns53_status"]
        self.assertEqual(len(status_nodes), 1)
        status_source = ast.get_source_segment(worker_source, status_nodes[0]) or ""
        self.assertEqual(status_source.count("_inspect_dnsmasq_selective_status_uncached()"), 1)
        self.assertNotIn("inspect_dnsmasq_selective_status()", status_source)
        for required in ('"dns_capture_missing_codes"', '"dns_capture_rule_count_capped"',
                         '"dns_capture_rules_present"', '"nftset_probe_status"',
                         '"nft_probe_targets"', 'capture_rules[:1]', 'nft_probe[:4]'):
            self.assertIn(required, status_source)
        self.assertIn('"not_applicable"', status_source)
        self.assertIn('"unknown"', status_source)

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
                class FakeConnection:
                    def __init__(self):
                        self.shutdown_called = False
                        self.closed = False

                    def shutdown(self, _how):
                        self.shutdown_called = True

                    def close(self):
                        self.closed = True

                self.server = type("Server", (), {"server_address": address})()
                self.path = path
                self.headers = {"Content-Length": str(len(payload or b""))}
                self.rfile = io.BytesIO(payload or b"")
                self.response = None
                self.headers_out = []
                self.connection = FakeConnection()
                self.close_connection = False

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
            for available in (False, True):
                payload = json.dumps({"available": available}, separators=(",", ":")).encode("ascii")
                handler = FakeHealthHandler((fixture.ENDPOINT_WAN_IP, fixture.ENDPOINT_CONTROL_PORT),
                                           "/health", payload)
                fixture._EndpointHandler.do_POST(handler)
                self.assertEqual(handler.response, (200, {"available": available}))
                for method in (fixture._EndpointHandler.do_GET, fixture._EndpointHandler.do_HEAD):
                    probe = FakeHealthHandler((fixture.SERVICE_VIP, fixture.HTTP_PORT), "/generate_204")
                    method(probe)
                    if available:
                        self.assertEqual(probe.response, (204, None))
                        self.assertIn(("Content-Length", "0"), probe.headers_out)
                        self.assertIn(("Connection", "close"), probe.headers_out)
                        self.assertFalse(probe.connection.shutdown_called)
                    else:
                        self.assertIsNone(probe.response)
                        self.assertEqual([], probe.headers_out)
                        self.assertTrue(probe.close_connection)
                        self.assertTrue(probe.connection.shutdown_called)
                        self.assertTrue(probe.connection.closed)
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
