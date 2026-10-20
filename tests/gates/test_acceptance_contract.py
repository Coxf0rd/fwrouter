from __future__ import annotations

import importlib.util
import ast
import builtins
import copy
import hashlib
import json
import math
import os
import re
import sqlite3
import tarfile
import tempfile
import threading
import unittest
import zipfile
import stat
import sys
import socket
import subprocess
import time
from datetime import datetime, timedelta
from urllib.parse import urlparse, urlsplit
from unittest import mock
import io
from pathlib import Path
from typing import Any
import xml.etree.ElementTree as ET
from types import ModuleType, SimpleNamespace

sys.path.insert(0, str(Path(__file__).parents[1]))
from application_acceptance.xray_support import parse_inbound_users_reply
from application_acceptance.joined_support import ProviderHttpTestBridge
from application_acceptance.diagnostics import encode_safe_reentry_native_record
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
STARTUP_COMPARISON_PATH = Path(__file__).parents[1] / "acceptance" / "startup_comparison.py"
STARTUP_COMPARISON_SPEC = importlib.util.spec_from_file_location(
    "fwrouter_startup_comparison_contract", STARTUP_COMPARISON_PATH)
assert STARTUP_COMPARISON_SPEC and STARTUP_COMPARISON_SPEC.loader
startup_comparison = importlib.util.module_from_spec(STARTUP_COMPARISON_SPEC)
STARTUP_COMPARISON_SPEC.loader.exec_module(startup_comparison)
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
APPLICATION_ACCEPTANCE_CONFTEST = Path(__file__).parents[1] / "application_acceptance" / "conftest.py"
APPLICATION_ACCEPTANCE_WORKER = Path(__file__).parents[1] / "application_acceptance" / "worker.py"
BROWSER_ACCEPTANCE_SOURCE = Path(__file__).parents[1] / "application_acceptance" / "test_browser_locale.py"
PACKET_ACCEPTANCE_SOURCE = Path(__file__).parents[1] / "application_acceptance" / "test_packet_dataplane.py"
STARTUP_CAUSALITY_PATH = Path(__file__).parents[1] / "application_acceptance" / "startup_causality.py"


def _extract_browser_resource_helpers():
    tree = ast.parse(BROWSER_ACCEPTANCE_SOURCE.read_text(encoding="utf-8"))
    wanted_functions = {
        "_read_cgroup_integer", "_read_cgroup_events", "_filesystem_usage",
        "_browser_resource_snapshot", "_safe_browser_resource_snapshot",
        "_launch_chromium_with_diagnostics",
    }
    body = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in wanted_functions:
            body.append(node)
        elif isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id in {
                "_BROWSER_DIAGNOSTIC_SCENARIOS", "_CGROUP_V2_ROOT", "_CGROUP_FILE_MAX_BYTES",
            }
            for target in node.targets
        ):
            body.append(node)
    namespace: dict[str, Any] = {"Path": Path, "os": os, "json": json}
    module = ast.Module(body=body, type_ignores=[])
    exec(compile(module, str(BROWSER_ACCEPTANCE_SOURCE), "exec"), namespace)
    return namespace


def _extract_health_readiness_predicate():
    """Load the pure acceptance predicate without importing the app or pytest fixtures."""
    tree = ast.parse(APPLICATION_ACCEPTANCE_CONFTEST.read_text(encoding="utf-8"))
    function = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_health_response_is_ready"
    )
    module = ast.Module(body=[function], type_ignores=[])
    namespace: dict[str, Any] = {}
    exec(compile(module, str(APPLICATION_ACCEPTANCE_CONFTEST), "exec"), namespace)
    return namespace["_health_response_is_ready"]


def _extract_startup_phase_parser():
    """Load the small parser without importing pytest fixtures or FWRouter modules."""
    tree = ast.parse(APPLICATION_ACCEPTANCE_CONFTEST.read_text(encoding="utf-8"))
    function = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_startup_phase_diagnostics"
    )
    module = ast.Module(body=[function], type_ignores=[])
    namespace = {"Path": Path, "os": os, "json": json, "math": math}
    exec(compile(module, str(APPLICATION_ACCEPTANCE_CONFTEST), "exec"), namespace)
    return namespace["_startup_phase_diagnostics"]


def _extract_current_xray_subject_identity_pairs():
    """Load the synthetic-DB identity oracle without importing hosted fixtures."""
    source_path = Path(__file__).parents[1] / "application_acceptance" / "test_xray_generation.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    function = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "current_xray_subject_identity_pairs"
    )
    namespace = {"Path": Path, "sqlite3": sqlite3}
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(source_path), "exec"), namespace)
    return namespace["current_xray_subject_identity_pairs"]


def _extract_bootstrap_phase_summary():
    """Load the nested redacted projection without importing the application worker."""
    tree = ast.parse(APPLICATION_ACCEPTANCE_WORKER.read_text(encoding="utf-8"))
    build_app = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "build_app"
    )
    function = next(
        node for node in ast.walk(build_app)
        if isinstance(node, ast.FunctionDef) and node.name == "phase_summary"
    )
    module = ast.Module(body=[function], type_ignores=[])
    namespace = {"Any": Any, "re": re}
    exec(compile(module, str(APPLICATION_ACCEPTANCE_WORKER), "exec"), namespace)
    return namespace["phase_summary"]


def _extract_worker_candidate_fingerprint(state_root: Path):
    """Load the worker's bounded file fingerprint without importing FWRouter."""
    tree = ast.parse(APPLICATION_ACCEPTANCE_WORKER.read_text(encoding="utf-8"))
    build_app = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "build_app"
    )
    function = next(
        node for node in ast.walk(build_app)
        if isinstance(node, ast.FunctionDef) and node.name == "candidate_fingerprint"
    )
    module = ast.Module(body=[function], type_ignores=[])
    namespace = {
        "Any": Any, "Path": Path, "stat": stat, "os": os,
        "hashlib": hashlib, "state": state_root,
    }
    exec(compile(module, str(APPLICATION_ACCEPTANCE_WORKER), "exec"), namespace)
    return namespace["candidate_fingerprint"]


def _extract_bounded_inventory_generation_recovery():
    """Load the fixed-field projection without importing the application worker."""
    tree = ast.parse(APPLICATION_ACCEPTANCE_WORKER.read_text(encoding="utf-8"))
    function = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_bounded_inventory_generation_recovery"
    )
    constants = [node for node in tree.body if isinstance(node, ast.Assign) and any(
        isinstance(target, ast.Name) and target.id in {
            "_XRAY_GENERATION_RECOVERY_REASONS", "_XRAY_GENERATION_SELECTION_ERRORS",
            "_XRAY_GENERATION_CHECKPOINT_PHASES",
        } for target in node.targets
    )]
    module = ast.Module(body=[*constants, function], type_ignores=[])
    namespace = {"Any": Any, "frozenset": frozenset}
    exec(compile(module, str(APPLICATION_ACCEPTANCE_WORKER), "exec"), namespace)
    return namespace["_bounded_inventory_generation_recovery"]


def _extract_bounded_generation_checkpoint_facts():
    tree = ast.parse(APPLICATION_ACCEPTANCE_WORKER.read_text(encoding="utf-8"))
    function = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_bounded_generation_checkpoint_facts"
    )
    constants = [node for node in tree.body if isinstance(node, ast.Assign) and any(
        isinstance(target, ast.Name) and target.id == "_XRAY_GENERATION_CHECKPOINT_PHASES"
        for target in node.targets
    )]
    namespace = {"Any": Any, "frozenset": frozenset}
    exec(compile(ast.Module(body=[*constants, function], type_ignores=[]), str(APPLICATION_ACCEPTANCE_WORKER), "exec"), namespace)
    return namespace["_bounded_generation_checkpoint_facts"]


def _extract_xray_bindings_startup_comparison():
    """Load the real pure binding-payload comparator without importing packet fixtures."""
    tree = ast.parse(PACKET_ACCEPTANCE_SOURCE.read_text(encoding="utf-8"))
    wanted = {
        "_xray_bindings_startup_projection", "_assert_xray_bindings_startup_equivalent",
        "_subscription_profile_snapshot_projection", "_assert_subscription_profile_startup_equivalent",
        "_assert_subject_overrides_startup_equivalent",
        "_assert_subject_inventory_startup_equivalent",
    }
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in wanted]
    assert {function.name for function in functions} == wanted
    namespace = {"Any": Any, "datetime": datetime, "timedelta": timedelta,
                 "timezone": __import__("datetime").timezone, "re": re, "json": json}
    module = ast.Module(body=functions, type_ignores=[])
    exec(compile(module, str(PACKET_ACCEPTANCE_SOURCE), "exec"), namespace)
    return namespace


class AcceptanceContractTests(unittest.TestCase):
    def test_startup_subject_inventory_allows_only_monotonic_xray_observation_timestamps(self):
        compare = _extract_xray_bindings_startup_comparison()[
            "_assert_subject_inventory_startup_equivalent"
        ]
        before = [{
            "subject_id": "xray:client-a",
            "subject_type": "explicit_external_client",
            "subject_role": "vless_client",
            "implementation_kind": "xray",
            "stable_key": "xray:client-a",
            "display_name": "Client A",
            "alias": "retained alias",
            "desired_mode": "enabled",
            "applied_mode": "enabled",
            "apply_state": "clean",
            "runtime_state": "active",
            "is_active": 1,
            "is_deleted": 0,
            "last_seen_at": "2026-10-19 09:00:00",
            "metadata_json": json.dumps({
                "source": "xray_adapter", "provider": "xray",
                "collected_at": "2026-10-19T10:00:00+00:00",
                "future_metadata": {"owner": "preserved"},
            }, sort_keys=True),
            "created_at": "2026-10-19 08:00:00",
            "updated_at": "2026-10-19 10:00:00",
        }]
        after = copy.deepcopy(before)
        after[0]["metadata_json"] = json.dumps({
            "source": "xray_adapter", "provider": "xray",
            "collected_at": "2026-10-19T10:00:01+00:00",
            "future_metadata": {"owner": "preserved"},
        }, sort_keys=True)
        after[0]["updated_at"] = "2026-10-19 10:00:01"
        builtin_rows = [
            {
                "subject_id": "fwrouter:global",
                "subject_type": "fwrouter",
                "implementation_kind": "fwrouter",
                "desired_mode": "direct",
                "metadata_json": json.dumps({"source": "system_subjects"}),
                "updated_at": "2026-10-19 10:00:00",
            },
            {
                "subject_id": "host:ssh",
                "subject_type": "host",
                "implementation_kind": "host",
                "desired_mode": "direct",
                "metadata_json": json.dumps({"source": "system_subjects"}),
                "updated_at": "2026-10-19 10:00:00",
            },
        ]
        first_after = [*after, *builtin_rows]
        compare(before, first_after, allowed_added_ids={"fwrouter:global", "host:ssh"})
        compare(first_after, first_after)
        with self.assertRaises(AssertionError):
            compare(before, first_after)
        with self.assertRaises(AssertionError):
            compare(before, first_after, allowed_added_ids={"arbitrary:subject"})
        with self.assertRaises(AssertionError):
            compare(first_after, after)

        invalid_updates = {
            "identity": lambda row: row.update(subject_id="xray:other"),
            "intent": lambda row: row.update(desired_mode="direct"),
            "runtime": lambda row: row.update(runtime_state="inactive"),
            "alias": lambda row: row.update(alias="changed alias"),
            "provider_metadata": lambda row: row["metadata_json"],
            "unknown_metadata": lambda row: row.update(metadata_json=json.dumps({
                "source": "xray_adapter", "provider": "xray",
                "collected_at": "2026-10-19T10:00:01+00:00",
                "future_metadata": {"owner": "changed"},
            }, sort_keys=True)),
            "nested_timestamp_like_data": lambda row: row.update(metadata_json=json.dumps({
                "source": "xray_adapter", "provider": "xray",
                "collected_at": "2026-10-19T10:00:01+00:00",
                "future_metadata": {"collected_at": "semantic-data-changed"},
            }, sort_keys=True)),
        }
        for name, update in invalid_updates.items():
            with self.subTest(name=name):
                changed = copy.deepcopy(after)
                if name == "provider_metadata":
                    changed[0]["metadata_json"] = json.dumps({
                        "source": "xray_adapter", "provider": "other",
                        "collected_at": "2026-10-19T10:00:01+00:00",
                        "future_metadata": {"owner": "preserved"},
                    }, sort_keys=True)
                else:
                    update(changed[0])
                with self.assertRaises(AssertionError):
                    compare(before, changed)

        invalid_timestamps = (
            {**after[0], "updated_at": "2026-10-19 09:59:59"},
            {**after[0], "updated_at": "2026-10-19T10:00:01+00:00"},
            {**after[0], "metadata_json": json.dumps({
                "source": "xray_adapter", "provider": "xray",
                "collected_at": "2026-10-19T11:00:01+01:00",
                "future_metadata": {"owner": "preserved"},
            })},
            {**after[0], "metadata_json": json.dumps({
                "source": "xray_adapter", "provider": "xray",
                "collected_at": "2026-10-19T09:59:59+00:00",
                "future_metadata": {"owner": "preserved"},
            })},
        )
        for changed_row in invalid_timestamps:
            with self.subTest(timestamp=changed_row):
                with self.assertRaises(AssertionError):
                    compare(before, [changed_row])

        changed_builtin = copy.deepcopy(first_after)
        changed_builtin[1]["desired_mode"] = "vpn"
        with self.assertRaises(AssertionError):
            compare(first_after, changed_builtin)

    def test_startup_xray_binding_comparison_allows_only_monotonic_utc_metadata(self):
        compare = _extract_xray_bindings_startup_comparison()["_assert_xray_bindings_startup_equivalent"]
        before = {
            "bindings_version": 1,
            "generated_at": "2026-10-19T10:00:01.000000+00:00",
            "bindings_count": 1,
            "applied_count": 1,
            "bindings": [{
                "subject_id": "subject-a",
                "client_id": "client-a",
                "selected_server_id": "vpn-global",
                "status": "applied",
                "applied_at": "2026-10-19T10:00:00.999000+00:00",
                "future_binding_field": {"generated_at": "kept-as-semantic-data"},
            }],
            "handoff_count": 1,
            "handoff_listeners": [{"listener_name": "owned-listener", "port": 5301}],
            "client_modes_count": 1,
            "client_modes": [{"client_id": "client-a", "effective_mode": "vpn"}],
            "future_root_field": {"generation": 1},
        }
        after = copy.deepcopy(before)
        after["generated_at"] = "2026-10-19T10:00:02.000000+00:00"
        after["bindings"][0]["applied_at"] = "2026-10-19T10:00:01.999000+00:00"
        compare(before, after)

        invalid_updates = {
            "binding_identity": lambda value: value["bindings"][0].update(subject_id="subject-b"),
            "binding_status": lambda value: (
                value["bindings"][0].update(status="pending"), value.update(applied_count=0),
            ),
            "handoff": lambda value: value["handoff_listeners"][0].update(port=5302),
            "counter": lambda value: value.update(handoff_count=2),
            "unknown_root_field": lambda value: value["future_root_field"].update(generation=2),
            "nested_timestamp_named_field": lambda value: value["bindings"][0]["future_binding_field"].update(
                generated_at="different-semantic-value",
            ),
        }
        for name, update in invalid_updates.items():
            with self.subTest(name=name):
                changed = copy.deepcopy(after)
                update(changed)
                with self.assertRaises(AssertionError):
                    compare(before, changed)

        invalid_timestamps = (
            {**after, "generated_at": "2026-10-19T10:00:02"},
            {**after, "generated_at": "2026-10-19T11:00:02+01:00"},
            {**after, "generated_at": "not-a-timestamp"},
            {**after, "generated_at": "2026-10-19T09:59:59+00:00"},
            {
                **after,
                "bindings": [{key: value for key, value in after["bindings"][0].items()
                              if key != "applied_at"}],
            },
        )
        for index, changed in enumerate(invalid_timestamps):
            with self.subTest(timestamp_case=index):
                with self.assertRaises(AssertionError):
                    compare(before, changed)

    def test_startup_subscription_profile_comparison_preserves_rows_and_only_refreshes_timestamps(self):
        compare = _extract_xray_bindings_startup_comparison()[
            "_assert_subscription_profile_startup_equivalent"
        ]
        before = [{
            "token": "fixture-token",
            "nodes_json": json.dumps([{"server_id": "server-a", "client_uuid": "uuid-a"}], sort_keys=True),
            "runtime_verified_at": "2026-10-19 10:00:00",
            "updated_at": "2026-10-19 10:00:00",
            "future_projection_field": {"owner": "startup"},
        }]
        after = copy.deepcopy(before)
        after[0]["runtime_verified_at"] = "2026-10-19 10:00:01"
        after[0]["updated_at"] = "2026-10-19 10:00:01"
        compare(before, after)

        invalid_updates = {
            "token": lambda row: row.update(token="different-token"),
            "nodes_json": lambda row: row.update(nodes_json=json.dumps([{"server_id": "other"}])),
            "unknown_column": lambda row: row["future_projection_field"].update(owner="other"),
        }
        for name, update in invalid_updates.items():
            with self.subTest(name=name):
                changed = copy.deepcopy(after)
                update(changed[0])
                with self.assertRaises(AssertionError):
                    compare(before, changed)

        invalid_timestamps = (
            {**after[0], "runtime_verified_at": "not-a-timestamp"},
            {**after[0], "updated_at": "2026-10-19 09:59:59"},
            {key: value for key, value in after[0].items() if key != "updated_at"},
        )
        for index, changed_row in enumerate(invalid_timestamps):
            with self.subTest(timestamp_case=index):
                with self.assertRaises(AssertionError):
                    compare(before, [changed_row])

    def test_startup_override_comparison_limits_first_owner_update_and_requires_second_startup_stability(self):
        compare = _extract_xray_bindings_startup_comparison()["_assert_subject_overrides_startup_equivalent"]
        before_server = [{
            "subject_id": "subject-a", "selected_server_id": "server-a",
            "selected_until": "2099-12-31 23:59:59", "apply_state": "clean",
            "error_code": None, "error_message": None,
            "requested_by": "subscription-import", "created_by": "subscription-import",
            "updated_by": "subscription-import", "updated_at": "2026-10-19 10:00:00",
            "future_override_field": {"owner": "retained"},
        }]
        after_server = copy.deepcopy(before_server)
        after_server[0]["requested_by"] = "startup-xray-vpn-auto-reconcile"
        after_server[0]["updated_by"] = "startup-xray-vpn-auto-reconcile"
        after_server[0]["updated_at"] = "2026-10-19 10:00:01"
        user_rows = [{"subject_id": "subject-a", "override_mode": "direct", "updated_at": "stable"}]
        compare(before_server, after_server, user_rows, copy.deepcopy(user_rows), allow_first_startup_owner=True)
        compare(after_server, copy.deepcopy(after_server), user_rows, copy.deepcopy(user_rows),
                allow_first_startup_owner=False)

        invalid_first_updates = (
            {**after_server[0], "selected_server_id": "server-b"},
            {**after_server[0], "created_by": "startup-xray-vpn-auto-reconcile"},
            {**after_server[0], "requested_by": "different-startup"},
            {**after_server[0], "future_override_field": {"owner": "changed"}},
            {**after_server[0], "updated_at": "2026-10-19 09:59:59"},
            {key: value for key, value in after_server[0].items() if key != "updated_by"},
        )
        for index, changed_row in enumerate(invalid_first_updates):
            with self.subTest(first_startup_case=index):
                with self.assertRaises(AssertionError):
                    compare(before_server, [changed_row], user_rows, copy.deepcopy(user_rows),
                            allow_first_startup_owner=True)
        changed_user_rows = [{**user_rows[0], "override_mode": "vpn"}]
        with self.assertRaises(AssertionError):
            compare(after_server, copy.deepcopy(after_server), user_rows, changed_user_rows,
                    allow_first_startup_owner=False)
        changed_second_startup = copy.deepcopy(after_server)
        changed_second_startup[0]["updated_at"] = "2026-10-19 10:00:02"
        with self.assertRaises(AssertionError):
            compare(after_server, changed_second_startup, user_rows, copy.deepcopy(user_rows),
                    allow_first_startup_owner=False)

    def test_startup_comparison_has_three_distinct_immutable_arms_and_nine_runs(self):
        candidate = "c" * 40
        schedule = startup_comparison.comparison_schedule(candidate)
        self.assertEqual(9, len(schedule))
        self.assertEqual({"old-qualified", "main-baseline", "candidate"},
                         {row["arm"] for row in schedule})
        self.assertEqual("4f0c43cecb3b7515be111fe169038ecdbdad6df0",
                         startup_comparison.OLD_QUALIFIED_SHA)
        self.assertEqual({"old-qualified": 3, "main-baseline": 3, "candidate": 3}, {
            arm: sum(row["arm"] == arm for row in schedule)
            for arm in ("old-qualified", "main-baseline", "candidate")
        })
        self.assertEqual([1, 2, 3, 3, 1, 2, 2, 3, 1], [
            {"old-qualified": 1, "main-baseline": 2, "candidate": 3}[row["arm"]]
            for row in schedule
        ])
        with self.assertRaises(startup_comparison.ComparisonError):
            startup_comparison.comparison_schedule(startup_comparison.MAIN_BASELINE_SHA)
        with self.assertRaises(startup_comparison.ComparisonError):
            startup_comparison.comparison_schedule("not-a-full-sha")

    def test_startup_comparison_command_and_report_bind_source_plan_and_scope(self):
        checkout = Path("/tmp/owned-startup-arm")
        self.assertEqual([
            sys.executable, str(checkout / "tests/acceptance/launcher.py"),
            "--run", "--suite", "runtime-scale-diagnostic",
        ], startup_comparison._launcher_command(sys.executable, checkout))
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "report.json"
            report = {"suite": "runtime-scale-diagnostic", "source_revision": "a" * 40,
                      "plan_digest": "b" * 64, "suite_nonce": "nonce-1",
                      "scope": "hosted-kernel-packet", "status": "passed"}
            path.write_text(json.dumps(report), encoding="utf-8")
            self.assertEqual(report, startup_comparison._parse_report(
                path, expected_sha="a" * 40, plan_digest="b" * 64))
            with self.assertRaises(startup_comparison.ComparisonError):
                startup_comparison._parse_report(path, expected_sha="c" * 40, plan_digest="b" * 64)

    def test_startup_comparison_candidate_must_descend_from_the_fixed_main_arm(self):
        repo = Path("/tmp/startup-comparison-repo")
        candidate = "c" * 40
        with mock.patch.object(startup_comparison, "_run", return_value="") as run:
            startup_comparison._validate_candidate_lineage(repo, candidate)
        self.assertEqual([
            mock.call(["git", "cat-file", "-e", f"{candidate}^{{commit}}"], cwd=repo),
            mock.call(["git", "merge-base", "--is-ancestor",
                       startup_comparison.MAIN_BASELINE_SHA, candidate], cwd=repo),
        ], run.call_args_list)
        with mock.patch.object(startup_comparison, "_run",
                               side_effect=startup_comparison.ComparisonError("not ancestor")):
            with self.assertRaisesRegex(startup_comparison.ComparisonError, "descend"):
                startup_comparison._validate_candidate_lineage(repo, candidate)

    def test_startup_comparison_old_arm_is_pinned_to_the_original_measured_source_tree(self):
        repo = Path("/tmp/startup-comparison-repo")
        with mock.patch.object(startup_comparison, "_run", side_effect=[
                startup_comparison.QUALIFIED_OLD_TREE_OID,
                startup_comparison.QUALIFIED_OLD_TREE_OID]) as run:
            provenance = startup_comparison._validate_old_arm_tree(repo)
        self.assertEqual(startup_comparison.OLD_QUALIFIED_SHA,
                         provenance["comparison_arm_commit"])
        self.assertEqual(startup_comparison.ORIGINAL_QUALIFIED_SHA,
                         provenance["original_measured_commit"])
        self.assertIn("source-tree equivalent", provenance["timing_attribution"])
        self.assertEqual(2, run.call_count)
        with mock.patch.object(startup_comparison, "_run", side_effect=[
                "wrong-tree", startup_comparison.QUALIFIED_OLD_TREE_OID]):
            with self.assertRaisesRegex(startup_comparison.ComparisonError, "reviewed source tree"):
                startup_comparison._validate_old_arm_tree(repo)

    def test_startup_comparison_per_arm_workspace_satisfies_the_real_path_predicate(self):
        with tempfile.TemporaryDirectory(prefix="fwrouter-startup-worktrees-") as temporary:
            root = Path(temporary)
            original_workspace = root / "original-checkout"
            arm_workspace = root / "_temp/phase-d-startup-comparison/source-arms/old-qualified"
            original_workspace.mkdir(parents=True)
            arm_workspace.mkdir(parents=True)
            base_environment = {"GITHUB_WORKSPACE": str(original_workspace), "RUNNER_TEMP": "/tmp/temp",
                                "GITHUB_ACTIONS": "true", "EXTRA_ENV": "preserved"}
            with mock.patch.object(launcher, "ROOT", arm_workspace):
                self.assertFalse(launcher._under(launcher.ROOT,
                                                 Path(base_environment["GITHUB_WORKSPACE"])))
                child_environment = startup_comparison._launcher_environment(
                    base_environment, arm_workspace)
                self.assertTrue(launcher._under(
                    launcher.ROOT, Path(child_environment["GITHUB_WORKSPACE"])))
            self.assertEqual(str(arm_workspace.resolve()), child_environment["GITHUB_WORKSPACE"])
            self.assertEqual("/tmp/temp", child_environment["RUNNER_TEMP"])
            self.assertEqual("true", child_environment["GITHUB_ACTIONS"])
            self.assertEqual("preserved", child_environment["EXTRA_ENV"])
            expected_environment = dict(base_environment)
            expected_environment["GITHUB_WORKSPACE"] = str(arm_workspace.resolve())
            self.assertEqual(expected_environment, child_environment)
            self.assertEqual(arm_workspace.stat().st_dev, Path(child_environment["GITHUB_WORKSPACE"]).stat().st_dev)
            self.assertEqual(arm_workspace.stat().st_uid, Path(child_environment["GITHUB_WORKSPACE"]).stat().st_uid)
            self.assertFalse(arm_workspace.stat().st_mode & 0o022)

    def test_startup_comparison_failure_continues_only_after_confirmed_cleanup(self):
        safe_failure = {"cleanup": "owned_resources_removed", "temporary_artifacts_removed": True}
        unsafe = {"cleanup": "unknown", "temporary_artifacts_removed": False,
                  "cleanup_unconfirmed": ["container:router"]}
        self.assertTrue(startup_comparison._cleanup_confirmed(safe_failure))
        self.assertFalse(startup_comparison._cleanup_confirmed(unsafe))
        self.assertEqual(9, startup_comparison.MAX_ATTEMPTS)
        self.assertEqual(3, startup_comparison.ATTEMPT_COUNT_PER_ARM)

    def test_startup_comparison_timeout_interrupts_launcher_for_cleanup_then_stops_schedule(self):
        events = []

        class GracefulProcess:
            pid = 1234
            returncode = None

            def communicate(self, timeout):
                events.append(("communicate", timeout))
                if len([event for event in events if event[0] == "communicate"]) == 1:
                    raise subprocess.TimeoutExpired(["launcher"], timeout,
                                                    output=b"partial stdout", stderr=b"partial stderr")
                self.returncode = -startup_comparison.signal.SIGINT
                return b"complete stdout", b"complete stderr"

            def send_signal(self, value):
                events.append(("launcher-signal", value))

        def factory(argv, **kwargs):
            self.assertTrue(kwargs["start_new_session"])
            self.assertIs(subprocess.PIPE, kwargs["stdout"])
            self.assertIs(subprocess.PIPE, kwargs["stderr"])
            return GracefulProcess()

        completed, timeout = startup_comparison._run_launcher(
            ["launcher"], cwd=Path("/tmp"), env={}, timeout=10, cleanup_grace=30,
            popen_factory=factory)
        self.assertTrue(timeout["deadline_exceeded"])
        self.assertTrue(timeout["interrupt_sent"])
        self.assertTrue(timeout["launcher_exited"])
        self.assertFalse(timeout["terminate_group_sent"])
        self.assertEqual(b"complete stdout", completed.stdout)
        self.assertEqual([("communicate", 10),
                          ("launcher-signal", startup_comparison.signal.SIGINT),
                          ("communicate", 30)], events)
        attempts = []
        for item in startup_comparison.comparison_schedule("c" * 40):
            attempts.append(item)
            if not startup_comparison._may_continue_attempts(
                    orchestration_deadline_exceeded=True, cleanup_confirmed=True):
                break
        self.assertEqual(1, len(attempts))

    def test_startup_comparison_timeout_escalates_only_owned_process_group(self):
        events = []

        class StuckProcess:
            pid = 9876
            returncode = None

            def communicate(self, timeout):
                events.append(("communicate", timeout))
                if len([event for event in events if event[0] == "communicate"]) <= 3:
                    raise subprocess.TimeoutExpired(["launcher"], timeout, output=b"partial")
                self.returncode = -startup_comparison.signal.SIGKILL
                return b"after kill", b""

            def send_signal(self, value):
                events.append(("launcher-signal", value))

        def factory(argv, **kwargs):
            self.assertTrue(kwargs["start_new_session"])
            return StuckProcess()

        def killpg(pid, value):
            events.append(("group-signal", pid, value))

        with mock.patch.object(startup_comparison.os, "killpg", side_effect=killpg):
            completed, timeout = startup_comparison._run_launcher(
                ["launcher"], cwd=Path("/tmp"), env={}, timeout=10, cleanup_grace=30,
                terminate_grace=5, kill_wait=5, popen_factory=factory)
        self.assertTrue(timeout["terminate_group_sent"])
        self.assertTrue(timeout["kill_group_sent"])
        self.assertEqual([("group-signal", 9876, startup_comparison.signal.SIGTERM),
                          ("group-signal", 9876, startup_comparison.signal.SIGKILL)],
                         [event for event in events if event[0] == "group-signal"])
        self.assertEqual(b"after kill", completed.stdout)
        self.assertFalse(startup_comparison._may_continue_attempts(
            orchestration_deadline_exceeded=timeout["deadline_exceeded"], cleanup_confirmed=True))

    def test_startup_comparison_requires_observed_memory_cap_and_oom_stability(self):
        wave = {"before": {"cgroup": {"max": 2560 * 1024 ** 2, "events": {
                    "oom": 0, "oom_kill": 0, "oom_group_kill": 0}}},
                "after": {"cgroup": {"max": 2560 * 1024 ** 2, "events": {
                    "oom": 0, "oom_kill": 0, "oom_group_kill": 0}}}}
        metrics = {"schema": "fwrouter-runtime-scale-metrics/v1", "startup_ready": True,
                   "startup_limits_seconds": {"hard_deadline": 45.0,
                                               "engineering_target": 40.0},
                   "config": {"config_bytes": 20_722_965,
                              "config_sha256": startup_comparison.WORKLOAD_CONFIG_SHA256,
                              "top_level_rule_count": 199_257, "transparent_subrule_bytes": 10_560_545,
                              "transparent_subrule_count": 199_257,
                              "full_vpn_fallback": ["MATCH,vpn-global", "NETWORK,udp,REJECT"],
                              "selector_targets": {"vpn-auto": ["REJECT"],
                                                   "vpn-global": ["vpn-auto", "REJECT"]}},
                   "subscription_source": {"native_clients": 82, "profile_accounts": 7,
                                           "profile_nodes": 70, "fixed_external_clients": 2,
                                           "internal_vpn_auto_clients": 10},
                   "subscription_profile_accounts": 7, "subscription_profile_nodes": 70,
                   "runtime_verified_profile_snapshots": 7, "fixed_external_clients": 2,
                   "internal_vpn_auto_clients": 10, "applied_profile_bindings": 82,
                   "synthetic_xray_loaded_clients": 82,
                   "synthetic_xray_identities_preserved": 82,
                   "wave_resource_observations": [wave]}
        projection = startup_comparison._workload_projection(metrics)
        self.assertIsNotNone(projection)
        self.assertEqual(2560 * 1024 ** 2, projection["wave_resource_ceilings"][0]["max_bytes"])
        changed_cap = json.loads(json.dumps(metrics))
        changed_cap["wave_resource_observations"][0]["after"]["cgroup"]["max"] -= 1
        self.assertIsNone(startup_comparison._workload_projection(changed_cap))
        changed_oom = json.loads(json.dumps(metrics))
        changed_oom["wave_resource_observations"][0]["after"]["cgroup"]["events"]["oom_kill"] += 1
        self.assertIsNone(startup_comparison._workload_projection(changed_oom))

    def test_startup_comparison_workload_projection_captures_config_profiles_and_bindings(self):
        wave = {side: {"cgroup": {"max": startup_comparison.CONTAINER_MEMORY_LIMIT_BYTES,
                                 "events": {"oom": 0, "oom_kill": 0,
                                            "oom_group_kill": 0}}}
                for side in ("before", "after")}
        metrics = {"schema": "fwrouter-runtime-scale-metrics/v1", "startup_ready": True,
                   "startup_limits_seconds": {"hard_deadline": 45.0,
                                               "engineering_target": 40.0},
                   "config": {"config_sha256": startup_comparison.WORKLOAD_CONFIG_SHA256,
                              "config_bytes": 20_722_965,
                              "top_level_rule_count": 199_257,
                              "transparent_subrule_bytes": 10_560_545,
                              "transparent_subrule_count": 199_257,
                              "full_vpn_fallback": ["MATCH,vpn-global", "NETWORK,udp,REJECT"],
                              "selector_targets": {"vpn-auto": ["REJECT"],
                                                   "vpn-global": ["vpn-auto", "REJECT"]}},
                   "subscription_source": {"native_clients": 82, "profile_accounts": 7,
                                           "profile_nodes": 70, "fixed_external_clients": 2,
                                           "internal_vpn_auto_clients": 10},
                   "subscription_profile_accounts": 7, "subscription_profile_nodes": 70,
                   "runtime_verified_profile_snapshots": 7, "fixed_external_clients": 2,
                   "internal_vpn_auto_clients": 10, "applied_profile_bindings": 82,
                   "synthetic_xray_loaded_clients": 82,
                   "synthetic_xray_identities_preserved": 82,
                   "wave_resource_observations": [wave]}
        projection = startup_comparison._workload_projection(metrics)
        self.assertEqual(82, projection["applied_profile_bindings"])
        self.assertEqual(70, projection["subscription_profile_nodes"])
        changed = json.loads(json.dumps(metrics))
        changed["config"]["config_sha256"] = "b" * 64
        self.assertNotEqual(projection, startup_comparison._workload_projection(changed))
        incomplete_success = json.loads(json.dumps(metrics))
        incomplete_success.pop("applied_profile_bindings")
        self.assertIsNone(startup_comparison._workload_projection(incomplete_success))

    def test_startup_comparison_summarizes_samples_and_low_overhead_runner_counters(self):
        before = {"cgroup_v2": {
            "cpu.stat": "usage_usec 10\nnr_throttled 2\nthrottled_usec 5\n",
            "memory.events": "oom 0\noom_kill 0\n",
            "io.stat": "8:0 rbytes=10 wbytes=4\n",
        }}
        after = {"cgroup_v2": {
            "cpu.stat": "usage_usec 24\nnr_throttled 5\nthrottled_usec 11\n",
            "memory.events": "oom 0\noom_kill 1\n",
            "io.stat": "8:0 rbytes=40 wbytes=14\n",
        }}
        self.assertEqual({"cpu.stat": {"usage_usec": 14, "nr_throttled": 3,
                                       "throttled_usec": 6},
                          "memory.events": {"oom_kill": 1, "oom": 0},
                          "io.stat": {"8:0.rbytes": 30, "8:0.wbytes": 10}},
                         startup_comparison._runner_counter_deltas(before, after))
        self.assertEqual({"n": 3, "min": 26.0, "median": 34.0, "max": 44.0},
                         startup_comparison._numeric_distribution([26, 34, 44]))
        self.assertEqual({"n": 0, "min": None, "median": None, "max": None},
                         startup_comparison._numeric_distribution([None, True]))

    def test_startup_comparison_native_input_schema_binds_chromium_bundle_and_executable(self):
        manifest = {"native": {name: {"binary_sha256": digit * 64}
                               for name, digit in (("xray", "a"), ("mihomo", "b"))},
                    "chromium": {"bundle_sha256": "c" * 64, "executable_sha256": "d" * 64}}
        report = {"binary_sha256": {"xray": "a" * 64, "mihomo": "b" * 64,
                                     "chromium": "c" * 64}}
        profile = {"chromium": {"bundle_sha256": "c" * 64, "sha256": "d" * 64},
                   "xray": {"sha256": "a" * 64}, "mihomo": {"sha256": "b" * 64}}
        self.assertTrue(startup_comparison._native_input_bindings_valid(manifest, report, profile))
        for target, key, value in ((report["binary_sha256"], "chromium", "d" * 64),
                                   (profile["chromium"], "bundle_sha256", "e" * 64),
                                   (profile["chromium"], "sha256", None)):
            changed = copy.deepcopy(profile)
            target_profile = changed["chromium"]
            if target is report["binary_sha256"]:
                changed_report = {"binary_sha256": {**report["binary_sha256"], key: value}}
            else:
                target_profile[key] = value
                changed_report = report
            self.assertFalse(startup_comparison._native_input_bindings_valid(
                manifest, changed_report, changed))
        self.assertFalse(startup_comparison._native_input_bindings_valid(
            manifest, {"binary_sha256": {}}, profile))

    def test_startup_comparison_deadline_censored_attempts_are_not_completed_samples(self):
        outcomes = [
            {"arm": "old-qualified", "runtime_metrics": {
                "startup_ready": True, "startup_wall_seconds": 44.8,
                "api_process_cpu_seconds_at_readiness": 35.0}},
            {"arm": "old-qualified", "runtime_metrics": {
                "startup_ready": False, "startup_wall_seconds": 45.01,
                "api_process_cpu_seconds_at_readiness_deadline": 34.9}},
            {"arm": "old-qualified", "runtime_metrics": {
                "startup_ready": False, "startup_wall_seconds": 45.0,
                "api_process_cpu_seconds_at_readiness_deadline": 35.1}},
        ]
        result = startup_comparison._arm_summary(outcomes)["old-qualified"]
        self.assertEqual(1, result["completed_startup_count"])
        self.assertEqual(2, result["readiness_failed_count"])
        self.assertEqual({"n": 1, "min": 44.8, "median": 44.8, "max": 44.8},
                         result["startup_wall_seconds"])
        self.assertEqual(2, result["deadline_elapsed_seconds"]["n"])
        self.assertEqual(45.0, result["deadline_elapsed_seconds"]["min"])
        self.assertAlmostEqual(45.005, result["deadline_elapsed_seconds"]["median"])
        self.assertEqual(45.01, result["deadline_elapsed_seconds"]["max"])
        self.assertEqual(0, result["engineering_target_met_count"])

    def test_startup_comparison_failed_readiness_keeps_workload_identity_without_resource_proof(self):
        metrics = {"schema": "fwrouter-runtime-scale-metrics/v1", "startup_ready": False,
                   "startup_limits_seconds": {"hard_deadline": 45.0, "engineering_target": 40.0},
                   "config": {"config_bytes": 20_722_965,
                              "config_sha256": startup_comparison.WORKLOAD_CONFIG_SHA256,
                              "top_level_rule_count": 199_257, "transparent_subrule_bytes": 10_560_545,
                              "transparent_subrule_count": 199_257,
                              "full_vpn_fallback": ["MATCH,vpn-global", "NETWORK,udp,REJECT"],
                              "selector_targets": {"vpn-auto": ["REJECT"],
                                                   "vpn-global": ["vpn-auto", "REJECT"]}},
                   "subscription_source": {"native_clients": 82, "profile_accounts": 7,
                                           "profile_nodes": 70, "fixed_external_clients": 2,
                                           "internal_vpn_auto_clients": 10},
                   "subscription_profile_accounts": 7, "subscription_profile_nodes": 70,
                   "wave_resource_observations": None}
        projection = startup_comparison._workload_projection(metrics)
        self.assertIsNotNone(projection)
        self.assertEqual("unavailable_readiness_failed", projection["wave_resource_observation_status"])
        self.assertEqual([], projection["wave_resource_ceilings"])
        for key in ("startup_limits_seconds", "config", "subscription_source"):
            malformed = copy.deepcopy(metrics)
            malformed[key] = {}
            self.assertIsNone(startup_comparison._workload_projection(malformed), key)
        malformed = copy.deepcopy(metrics)
        malformed["config"]["config_sha256"] = "invalid"
        self.assertIsNone(startup_comparison._workload_projection(malformed))
        malformed = copy.deepcopy(metrics)
        malformed["config"]["selector_targets"] = {"vpn-auto": "REJECT"}
        self.assertIsNone(startup_comparison._workload_projection(malformed))
        malformed = copy.deepcopy(metrics)
        malformed["subscription_source"]["native_clients"] = "82"
        self.assertIsNone(startup_comparison._workload_projection(malformed))
        metrics["startup_ready"] = True
        self.assertIsNone(startup_comparison._workload_projection(metrics))

    def test_startup_comparison_workflow_is_exact_branch_marker_and_reuses_qualified_inputs(self):
        workflow = (Path(__file__).parents[2] / ".github/workflows/phase-d-validation.yml").read_text(
            encoding="utf-8")
        self.assertIn("cleanup/ruff-completion-startup-audit", workflow.split("permissions:", 1)[0])
        self.assertIn('"ci:validate-startup-comparison": "startup-comparison"', workflow)
        self.assertIn('GITHUB_REF: ${{ github.ref }}', workflow)
        self.assertIn('os.environ.get("GITHUB_REF") != "refs/heads/cleanup/ruff-completion-startup-audit"',
                      workflow)
        self.assertIn("needs.select-stage.outputs.stage == 'startup-comparison'", workflow)
        self.assertIn("tests/acceptance/startup_comparison.py", workflow)
        self.assertIn("${{ runner.temp }}/phase-d-startup-comparison/**", workflow)
        self.assertIn("Provision exact hosted Xray/Mihomo/browser inputs", workflow)
        self.assertIn("FWROUTER_ACCEPTANCE_PLAN_DIGEST", workflow)
        self.assertEqual(2, workflow.count(
            "needs.select-stage.outputs.stage == 'startup-comparison' && "
            "'47cfd63fc220f2890e140da27ff54c91a8a2c7a1'"))
        self.assertEqual(2, workflow.count(
            "((needs.select-stage.outputs.stage == 'identity-diagnostic' || "
            "needs.select-stage.outputs.stage == 'final-regressions-diagnostic' || "
            "needs.select-stage.outputs.stage == 'packet-diagnostic') && "
            "'1b302e91b3cd652cd4aab4f45bbdd88c088e5786' || "
            "'6bdf37e9c73b5728b8d50e971f34d848610eb345')"))
        self.assertIn('if: needs.select-stage.outputs.stage != \'identity-diagnostic\' && needs.select-stage.outputs.stage != \'startup-comparison\'',
                      workflow)
        self.assertEqual(45, startup_comparison.HARD_STARTUP_SECONDS)
        self.assertEqual(40, startup_comparison.ENGINEERING_TARGET_SECONDS)
        self.assertEqual(2560 * 1024 ** 2, startup_comparison.CONTAINER_MEMORY_LIMIT_BYTES)
        self.assertEqual("runtime-scale-diagnostic", startup_comparison.SUITE)
        self.assertIn("timeout-minutes: ${{ needs.select-stage.outputs.stage == 'startup-comparison' && 120 || 30 }}",
                      workflow)
        self.assertIn('"source_manifest_sha256": source_manifest_hashes[item["arm"]]',
                      STARTUP_COMPARISON_PATH.read_text(encoding="utf-8"))

    def test_browser_resource_snapshot_is_bounded_allowlisted_and_uses_unknown_for_missing_data(self):
        helpers = _extract_browser_resource_helpers()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "memory.current").write_text("12345\n", encoding="ascii")
            (root / "memory.peak").write_text("max\n", encoding="ascii")
            (root / "memory.max").write_text("not-a-counter SECRET_SENTINEL\n", encoding="ascii")
            (root / "memory.events").write_text(
                "low 1\nhigh 2\nmax 3\noom 4\noom_kill 5\noom_group_kill 6\nprivate_secret 99\n",
                encoding="ascii",
            )
            (root / "pids.current").write_text("7\n", encoding="ascii")
            (root / "pids.events").write_text("max 8\nsecret 9\n", encoding="ascii")

            def fake_statvfs(path):
                self.assertIn(path, ("/tmp", "/dev/shm"))
                return SimpleNamespace(f_frsize=4096, f_bsize=4096, f_blocks=100, f_bfree=25)

            snapshot = helpers["_browser_resource_snapshot"](root, fake_statvfs)
            self.assertEqual(12345, snapshot["memory_current_bytes"])
            self.assertIsNone(snapshot["memory_peak_bytes"])
            self.assertIsNone(snapshot["memory_max_bytes"])
            self.assertIsNone(snapshot["pids_max"])
            self.assertEqual(7, snapshot["pids_current"])
            self.assertEqual({"low": 1, "high": 2, "max": 3, "oom": 4,
                              "oom_kill": 5, "oom_group_kill": 6}, snapshot["memory_events"])
            self.assertEqual({"max": 8}, snapshot["pids_events"])
            self.assertEqual({"used_bytes": 307200, "capacity_bytes": 409600}, snapshot["tmp"])
            self.assertEqual(snapshot["tmp"], snapshot["dev_shm"])
            encoded = json.dumps(snapshot)
            self.assertNotIn("SECRET_SENTINEL", encoded)
            self.assertNotIn("private_secret", encoded)
            self.assertNotIn(temporary, encoded)

            (root / "memory.current").write_bytes(b"1" * 4097)
            malformed = helpers["_browser_resource_snapshot"](root, fake_statvfs)
            self.assertIsNone(malformed["memory_current_bytes"])
            self.assertIsNone(malformed["memory_peak_bytes"])

    def test_browser_launch_diagnostic_records_one_unchanged_attempt_and_preserves_failure(self):
        helpers = _extract_browser_resource_helpers()
        launch = helpers["_launch_chromium_with_diagnostics"]
        fixed_snapshot = {"memory_current_bytes": 100, "pids_current": 2}

        class Request:
            def __init__(self):
                self.finalizers = []

            def addfinalizer(self, callback):
                self.finalizers.append(callback)

        class Chromium:
            def __init__(self, result=None, error=None):
                self.calls = []
                self.result = result
                self.error = error

            def launch(self, **kwargs):
                self.calls.append(kwargs)
                if self.error is not None:
                    raise self.error
                return self.result

        expected_args = ["--disable-dev-shm-usage", "--no-proxy-server"]
        properties = []
        request = Request()
        browser_object = object()
        chromium = Chromium(result=browser_object)
        snapshots = iter((fixed_snapshot, {"memory_current_bytes": 110}, {"memory_current_bytes": 120}))
        result = launch(
            chromium, scenario="navigation", executable_path="/fixed/chromium", args=expected_args,
            request=request, record_property=lambda *args: properties.append(args),
            snapshot_fn=lambda: next(snapshots),
        )
        self.assertIs(result, browser_object)
        self.assertEqual(1, len(chromium.calls))
        self.assertEqual({"headless": True, "executable_path": "/fixed/chromium", "args": expected_args}, chromium.calls[0])
        self.assertEqual(1, len(request.finalizers))
        request.finalizers[0]()
        self.assertEqual(1, len(properties))
        property_name, encoded = properties[0]
        self.assertEqual("browser_resource_diagnostic_navigation", property_name)
        diagnostic = json.loads(encoded)
        self.assertEqual("succeeded", diagnostic["launch_outcome"])
        self.assertEqual(["before_launch", "after_launch", "posttest"],
                         [item["phase"] for item in diagnostic["samples"]])
        self.assertEqual(fixed_snapshot, diagnostic["samples"][0]["resources"])

        secret = "SECRET_SENTINEL_DO_NOT_EXPORT"
        original_error = RuntimeError(secret)
        failing_request = Request()
        failing_properties = []
        failing_chromium = Chromium(error=original_error)
        failure_snapshots = iter((fixed_snapshot, {"memory_current_bytes": None}, {"memory_current_bytes": 130}))
        with self.assertRaises(RuntimeError) as caught:
            launch(
                failing_chromium, scenario="xray_client_editor", executable_path="/fixed/chromium",
                args=expected_args, request=failing_request,
                record_property=lambda *args: failing_properties.append(args),
                snapshot_fn=lambda: next(failure_snapshots),
            )
        self.assertIs(caught.exception, original_error)
        self.assertEqual(1, len(failing_chromium.calls))
        self.assertEqual(chromium.calls[0], failing_chromium.calls[0])
        failing_request.finalizers[0]()
        self.assertEqual(1, len(failing_properties))
        failed_json = failing_properties[0][1]
        self.assertEqual("failed", json.loads(failed_json)["launch_outcome"])
        self.assertNotIn(secret, failed_json)
        self.assertEqual(["before_launch", "after_launch", "posttest"],
                         [item["phase"] for item in json.loads(failed_json)["samples"]])

        diagnostic_error = RuntimeError("diagnostic-only-secret")
        protected_request = Request()
        protected_properties = []
        protected_chromium = Chromium(error=original_error)
        with self.assertRaises(RuntimeError) as protected_caught:
            launch(
                protected_chromium, scenario="provider_error", executable_path="/fixed/chromium",
                args=expected_args, request=protected_request,
                record_property=lambda *args: protected_properties.append(args),
                snapshot_fn=lambda: (_ for _ in ()).throw(diagnostic_error),
            )
        self.assertIs(protected_caught.exception, original_error)
        self.assertEqual(1, len(protected_chromium.calls))
        protected_request.finalizers[0]()
        protected_diagnostic = json.loads(protected_properties[0][1])
        self.assertTrue(all(
            sample["resources"]["memory_current_bytes"] is None
            for sample in protected_diagnostic["samples"]
        ))
        self.assertNotIn("diagnostic-only-secret", protected_properties[0][1])

    def test_browser_diagnostic_rejects_unregistered_scenario_before_launch(self):
        helpers = _extract_browser_resource_helpers()
        calls = []

        class Chromium:
            def launch(self, **kwargs):
                calls.append(kwargs)

        with self.assertRaisesRegex(ValueError, "unsupported browser diagnostic scenario"):
            helpers["_launch_chromium_with_diagnostics"](
                Chromium(), scenario="arbitrary-user-value", executable_path="ignored", args=[],
                request=SimpleNamespace(addfinalizer=lambda _callback: None),
                record_property=lambda *_args: None, snapshot_fn=lambda: {},
            )
        self.assertEqual([], calls)

    def test_inventory_recovery_projection_is_fixed_redacted_and_preserves_false_proof(self):
        project = _extract_bounded_inventory_generation_recovery()
        source = {
            "ok": True,
            "recovered": "restored_last_good",
            "operation_id": "private-operation-id",
            "mihomo_preimage_proof": {
                "ok": True, "policy_verified": False, "native_config_validated": True,
                "config_path": "/private/state/config.yaml",
            },
            "xray_reload": True,
            "mihomo_restart": True,
            "derived_rows_restored": True,
            "xray_runtime_readback": {"ok": True, "client_uuid": "UUID_SENTINEL"},
            "mihomo_runtime_readback": {"ok": True, "config": "RAW_CONFIG_SENTINEL"},
            "private_path": "/private/state/checkpoint.json",
            "private_message": "token=SECRET_SENTINEL",
        }
        result = project(source)
        self.assertEqual({
            "ok": True,
            "recovered": "restored_last_good",
            "mihomo_preimage_proof": {
                "ok": True, "policy_verified": False, "native_config_validated": True,
            },
            "xray_runtime_readback": {"ok": True},
            "mihomo_runtime_readback": {"ok": True},
            "xray_reload": True,
            "mihomo_restart": True,
            "derived_rows_restored": True,
        }, result)
        encoded = json.dumps(result)
        for secret in ("private-operation-id", "/private/state", "UUID_SENTINEL",
                       "RAW_CONFIG_SENTINEL", "SECRET_SENTINEL"):
            self.assertNotIn(secret, encoded)
        malformed = project({
            "ok": False,
            "recovered": "unexpected-outcome",
            "mihomo_preimage_proof": {
                "ok": "true", "policy_verified": 1, "native_config_validated": None,
            },
            "xray_runtime_readback": {"ok": False, "raw": "private"},
            "mihomo_restart": 1,
        })
        self.assertEqual({"ok": False, "xray_runtime_readback": {"ok": False}}, malformed)

    def test_failed_inventory_publication_projects_bounded_recovery_reason(self):
        project = _extract_bounded_inventory_generation_recovery()
        for reason in (
            "ambiguous_prior_prepare", "ambiguous_prior_reload", "inventory_mihomo_checkpoint_mismatch",
            "inventory_native_receipt_unconfirmed", "inventory_generation_promotion_unconfirmed",
        ):
            with self.subTest(reason=reason):
                source = {
                    "ok": False,
                    "stage": "generation_recovery",
                    "error_code": "XRAY_GENERATION_RECOVERY_FAILED",
                    "details": {
                        "ok": False,
                        "recovered": "stale_checkpoint_declined",
                        "reason": reason,
                        "checkpoint_path": "/private/state/generation-checkpoint.json",
                        "selection_revision": 9931,
                        "candidate_digest": "DIGEST_SENTINEL",
                        "client_uuid": "UUID_SENTINEL",
                        "exception": "token=SECRET_SENTINEL",
                    },
                }
                expected = {
                    "ok": False,
                    "recovered": "stale_checkpoint_declined",
                    "reason": reason,
                }
                projected = project(source)
                self.assertEqual(expected, projected)
                encoded = json.dumps(projected)
                for secret in ("/private/state", "9931", "DIGEST_SENTINEL", "UUID_SENTINEL", "SECRET_SENTINEL"):
                    self.assertNotIn(secret, encoded)
        unknown = project({
            "ok": False, "stage": "generation_recovery",
            "details": {"ok": False, "reason": "token=DO_NOT_EXPOSE"},
        })
        self.assertEqual({"ok": False}, unknown)

    def test_checkpoint_recovery_facts_expose_only_closed_state_and_boolean_fences(self):
        worker_source = APPLICATION_ACCEPTANCE_WORKER.read_text(encoding="utf-8")
        worker_tree = ast.parse(worker_source, filename=str(APPLICATION_ACCEPTANCE_WORKER))
        observer = next(
            node for node in worker_tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "_install_acceptance_xray_generation_rollback_observer"
        )
        observer_source = ast.get_source_segment(worker_source, observer) or ""
        self.assertIn("_recover_checkpoint_from_current_projection", observer_source)
        self.assertIn("selection_error_code", observer_source)
        for forbidden in ("checkpoint_path\":", "candidate_digest\":", "selection_revision\":",
                          "client_uuid\":", "str(exc)"):
            self.assertNotIn(forbidden, observer_source)
        route = next(
            node for node in ast.walk(worker_tree)
            if isinstance(node, ast.FunctionDef) and node.name == "publish_pending_subscription_inventory"
        )
        route_source = ast.get_source_segment(worker_source, route) or ""
        for required in ("_read_bounded_generation_checkpoint_facts", "checkpoint_facts"):
            self.assertIn(required, route_source)

    def test_xray_checkpoint_lifecycle_observer_records_failures_close_and_rejects_symlink_attribution(self):
        worker_source = APPLICATION_ACCEPTANCE_WORKER.read_text(encoding="utf-8")
        worker_tree = ast.parse(worker_source, filename=str(APPLICATION_ACCEPTANCE_WORKER))
        observer = next(
            node for node in worker_tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "_install_acceptance_xray_checkpoint_lifecycle_observer"
        )
        namespace = {
            "__builtins__": __builtins__, "__package__": "application_acceptance",
            "os": __import__("os"), "sys": sys, "json": json,
            "threading": threading, "Path": Path, "stat": __import__("stat"),
            "Any": __import__("typing").Any,
            "_XRAY_GENERATION_CHECKPOINT_PHASES": {"prepared", "xray_applied", "restore_failed"},
            "_bounded_inventory_generation_recovery": lambda _result: {},
        }
        services = ModuleType("fwrouter_api.services")
        observed = io.StringIO()
        with tempfile.TemporaryDirectory(prefix="fwrouter-checkpoint-observer-") as temp:
            (Path(temp) / "run").mkdir(mode=0o700)
            checkpoint = Path(temp) / "generation-checkpoint.json"
            checkpoint_target = Path(temp) / "checkpoint-target.json"
            checkpoint_location = {"path": checkpoint}
            calls = []
            finalized_pending = []

            def checkpoint_path(_adapter):
                return checkpoint_location["path"]

            def original_write(*args, **kwargs):
                calls.append(("write", args, kwargs))
                checkpoint.write_text(json.dumps({"phase": kwargs["phase"]}), encoding="utf-8")
                return checkpoint

            def original_finalize(pending, verification):
                calls.append(("finalize", pending, verification))
                Path(pending["checkpoint_path"]).unlink(missing_ok=True)
                return {"ok": True, "status": "success"}

            reconcile_calls = {"count": 0}

            def original_reconcile(*, requested_by, **kwargs):
                reconcile_calls["count"] += 1
                if reconcile_calls["count"] == 1:
                    service._write_xray_generation_checkpoint(
                        phase="xray_applied", requested_by=requested_by, **kwargs,
                    )
                    return {
                        "ok": False, "status": "pending", "stage": "xray_generation_apply",
                        "error_code": "XRAY_GENERATION_APPLY_FAILED",
                    }
                if reconcile_calls["count"] == 3:
                    return {"ok": True, "status": "success"}
                pending = {"checkpoint_path": str(checkpoint), "private_token": "DO_NOT_LOG"}
                finalized_pending.append(pending)
                return service._finalize_xray_profile_publication(
                    pending, {"ok": True},
                )

            service = SimpleNamespace(
                _xray_adapter=lambda: object(),
                _xray_generation_checkpoint_path=checkpoint_path,
                _write_xray_generation_checkpoint=original_write,
                _finalize_xray_profile_publication=original_finalize,
                _restore_xray_generation_checkpoint=lambda *_args: {},
                reconcile_xray_subscription_profile_nodes=original_reconcile,
            )
            services.xray_subscription_service = service
            with mock.patch.dict(sys.modules, {"fwrouter_api.services": services}), \
                    mock.patch.dict(__import__("os").environ, {
                        "FWROUTER_ENVIRONMENT": "test",
                        "FWROUTER_APPLICATION_ACCEPTANCE_ROOT": "/tmp/fwrouter-application-acceptance",
                        "FWROUTER_STATE_DIR": temp,
                    }), mock.patch("sys.stderr", observed):
                exec(compile(ast.Module(body=[observer], type_ignores=[]),
                             str(APPLICATION_ACCEPTANCE_WORKER), "exec"), namespace)
                namespace[observer.name]()
                failed = service.reconcile_xray_subscription_profile_nodes(
                    requested_by="subscription-refresh", private_token="DO_NOT_LOG",
                )
                result = service.reconcile_xray_subscription_profile_nodes(
                    requested_by="subscription-refresh", private_token="DO_NOT_LOG",
                )
                checkpoint_target.write_text(json.dumps({"phase": "xray_applied"}), encoding="utf-8")
                checkpoint.unlink(missing_ok=True)
                symlink_checkpoint = Path(temp) / "symlink-checkpoint.json"
                symlink_checkpoint.symlink_to(checkpoint_target)
                checkpoint_location["path"] = symlink_checkpoint
                symlink_result = service.reconcile_xray_subscription_profile_nodes(
                    requested_by="subscription-refresh", private_token="DO_NOT_LOG",
                )
            self.assertFalse(failed["ok"])
            self.assertEqual("XRAY_GENERATION_APPLY_FAILED", failed["error_code"])
            self.assertEqual({"ok": True, "status": "success"}, result)
            self.assertEqual({"ok": True, "status": "success"}, symlink_result)
            self.assertFalse(checkpoint.exists())
            self.assertTrue(symlink_checkpoint.is_symlink())
            self.assertEqual(1, len(finalized_pending))
            self.assertEqual(["write", "finalize"], [call[0] for call in calls])
            self.assertIs(finalized_pending[0], calls[1][1])

        text = observed.getvalue()
        self.assertNotIn("DO_NOT_LOG", text)
        self.assertNotIn(str(checkpoint), text)
        records = [json.loads(line.split(" ", 1)[1]) for line in text.splitlines()]
        self.assertEqual(5, len(records))
        self.assertEqual("checkpoint_created", records[0]["event"])
        self.assertEqual("subscription_refresh", records[0]["owner"])
        self.assertEqual("xray_applied", records[0]["phase"])
        self.assertTrue(records[0]["checkpoint_exists"])
        self.assertEqual("reconcile_result", records[1]["event"])
        self.assertEqual("xray_generation_apply", records[1]["stage"])
        self.assertEqual("XRAY_GENERATION_APPLY_FAILED", records[1]["error_code"])
        self.assertTrue(records[1]["checkpoint_after"]["exists"])
        self.assertEqual("xray_applied", records[1]["checkpoint_after"]["phase"])
        self.assertEqual("publication_finalized", records[2]["event"])
        self.assertTrue(records[2]["ok"])
        self.assertEqual("success", records[2]["status"])
        self.assertTrue(records[2]["checkpoint_before"]["exists"])
        self.assertFalse(records[2]["checkpoint_after"]["exists"])
        self.assertEqual("reconcile_result", records[3]["event"])
        self.assertTrue(records[3]["checkpoint_before"]["exists"])
        self.assertFalse(records[3]["checkpoint_after"]["exists"])
        self.assertEqual("reconcile_result", records[4]["event"])
        self.assertEqual("unknown", records[4]["checkpoint_before"]["phase"])
        self.assertEqual("unknown", records[4]["checkpoint_after"]["phase"])
        self.assertNotIn("xray_applied", json.dumps(records[4]))

    def test_xray_checkpoint_lifecycle_capture_survives_noisy_log_and_caps_records(self):
        worker_source = APPLICATION_ACCEPTANCE_WORKER.read_text(encoding="utf-8")
        worker_tree = ast.parse(worker_source, filename=str(APPLICATION_ACCEPTANCE_WORKER))
        observer = next(node for node in worker_tree.body if isinstance(node, ast.FunctionDef)
                        and node.name == "_install_acceptance_xray_checkpoint_lifecycle_observer")
        namespace = {
            "__builtins__": __builtins__, "__package__": "application_acceptance",
            "os": os, "sys": sys, "json": json, "threading": threading,
            "Path": Path, "stat": stat, "Any": Any,
            "_XRAY_GENERATION_CHECKPOINT_PHASES": {"prepared", "xray_applied", "restore_failed"},
            "_bounded_inventory_generation_recovery": lambda _result: {},
        }
        services = ModuleType("fwrouter_api.services")
        noisy_stderr = io.StringIO()
        with tempfile.TemporaryDirectory(prefix="fwrouter-checkpoint-capture-") as temp:
            state = Path(temp)
            (state / "run").mkdir(mode=0o700)
            checkpoint = state / "generation-checkpoint.json"
            checkpoint_location = {"path": checkpoint}

            def original_reconcile(**_kwargs):
                return {"ok": True, "status": "success"}

            service = SimpleNamespace(
                _xray_adapter=lambda: object(),
                _xray_generation_checkpoint_path=lambda _adapter: checkpoint_location["path"],
                reconcile_xray_subscription_profile_nodes=original_reconcile,
                _write_xray_generation_checkpoint=lambda **_kwargs: checkpoint,
                _finalize_xray_profile_publication=lambda pending, _verification: pending,
                _restore_xray_generation_checkpoint=lambda *_args: {},
            )
            services.xray_subscription_service = service
            with mock.patch.dict(sys.modules, {"fwrouter_api.services": services}), \
                    mock.patch.dict(os.environ, {
                        "FWROUTER_ENVIRONMENT": "test",
                        "FWROUTER_APPLICATION_ACCEPTANCE_ROOT": "/tmp/fwrouter-application-acceptance",
                        "FWROUTER_STATE_DIR": temp,
                    }), mock.patch("sys.stderr", noisy_stderr):
                exec(compile(ast.Module(body=[observer], type_ignores=[]),
                             str(APPLICATION_ACCEPTANCE_WORKER), "exec"), namespace)
                namespace[observer.name]()
                noisy_stderr.write("UNRELATED_LOG_NOISE " + ("x" * 20000) + "\n")
                for _ in range(20):
                    service.reconcile_xray_subscription_profile_nodes(requested_by="api")
                # A worker process restart installs a fresh observer over the
                # original service callable; it must continue the durable cap.
                service.reconcile_xray_subscription_profile_nodes = original_reconcile
                namespace[observer.name]()
                for _ in range(40):
                    service.reconcile_xray_subscription_profile_nodes(requested_by="api")
            event_file = state / "run" / "xray-checkpoint-lifecycle.jsonl"
            events = [json.loads(line) for line in event_file.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(48, len(events))
            self.assertEqual(list(range(1, 49)), [event["sequence"] for event in events])
            self.assertTrue(all(event["schema"] == "fwrouter-acceptance-xray-checkpoint/v1" for event in events))
            self.assertTrue(all(len(line.encode("utf-8")) <= 1024
                                for line in event_file.read_text(encoding="utf-8").splitlines()))
            self.assertNotIn("UNRELATED_LOG_NOISE", event_file.read_text(encoding="utf-8"))
            self.assertNotIn("UNRELATED_LOG_NOISE", noisy_stderr.getvalue()[-16 * 1024:])
            self.assertLessEqual(max(map(len, event_file.read_text(encoding="utf-8").splitlines())), 1024)

    def test_xray_checkpoint_lifecycle_artifact_reader_rejects_symlink_and_unknown_fields(self):
        conftest_path = Path(__file__).parents[1] / "application_acceptance" / "conftest.py"
        source = conftest_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(conftest_path))
        reader = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                      and node.name == "_read_acceptance_xray_checkpoint_lifecycle")
        namespace = {"Path": Path, "os": os, "stat": stat, "json": json}
        exec(compile(ast.Module(body=[reader], type_ignores=[]), str(conftest_path), "exec"), namespace)
        event = {
            "schema": "fwrouter-acceptance-xray-checkpoint/v1", "sequence": 1,
            "event": "checkpoint_created", "owner": "subscription_refresh",
            "phase": "xray_applied", "checkpoint_exists": True,
            "checkpoint_phase": "xray_applied", "inventory_admission_present": True,
        }
        with tempfile.TemporaryDirectory(prefix="fwrouter-checkpoint-artifact-") as temp:
            state = Path(temp)
            (state / "run").mkdir(mode=0o700)
            capture = state / "run" / "xray-checkpoint-lifecycle.jsonl"
            capture.write_text(json.dumps(event) + "\n", encoding="utf-8")
            os.chmod(capture, 0o600)
            valid = namespace[reader.name](state)
            self.assertEqual("captured", valid["status"])
            self.assertEqual([event], valid["events"])
            capture.unlink()
            private_target = state / "run" / "private.jsonl"
            private_target.write_text(json.dumps({**event, "private_token": "DO_NOT_EXPORT"}) + "\n",
                                      encoding="utf-8")
            os.chmod(private_target, 0o600)
            capture.symlink_to(private_target)
            symlink_result = namespace[reader.name](state)
            self.assertEqual("rejected", symlink_result["status"])
            self.assertEqual([], symlink_result["events"])
            self.assertNotIn("DO_NOT_EXPORT", json.dumps(symlink_result))
            capture.unlink()
            capture.write_text("not-json\n", encoding="utf-8")
            os.chmod(capture, 0o600)
            invalid = namespace[reader.name](state)
            self.assertEqual("invalid", invalid["status"])
            self.assertEqual([], invalid["events"])
            capture.write_text(json.dumps({**event, "unrecognized": "DO_NOT_EXPORT"}) + "\n", encoding="utf-8")
            os.chmod(capture, 0o600)
            unknown = namespace[reader.name](state)
            self.assertEqual("invalid", unknown["status"])
            self.assertEqual([], unknown["events"])
            self.assertNotIn("DO_NOT_EXPORT", json.dumps(unknown))

    def test_checkpoint_facts_compare_fences_without_exposing_values(self):
        project = _extract_bounded_generation_checkpoint_facts()
        checkpoint = {
            "phase": "xray_applied", "selection_revision": 18,
            "subscription_inventory_generation": {"digest": "DIGEST_SENTINEL", "revision": 7},
            "client_uuid": "UUID_SENTINEL", "artifact_path": "/private/state/config.json",
        }
        inventory = {"status": "pending", "candidate_digest": "DIGEST_SENTINEL", "candidate_revision": 7}
        fence = {"revision": 19, "decision_id": "PRIVATE_DECISION"}
        result = project(checkpoint, inventory, fence)
        self.assertEqual({
            "checkpoint_phase": "xray_applied",
            "checkpoint_selection_fence_matches": False,
            "checkpoint_inventory_binding_matches": True,
        }, result)
        self.assertNotIn("DIGEST_SENTINEL", json.dumps(result))
        self.assertNotIn("UUID_SENTINEL", json.dumps(result))
        self.assertNotIn("PRIVATE_DECISION", json.dumps(result))
        self.assertNotIn("/private/state", json.dumps(result))

    def test_reentry_native_diagnostic_is_redacted_and_size_bounded(self):
        secret_url = "http://probe-user:probe-pass@vpn-secret.test/private"
        secret_uuid = "11111111-2222-4333-8444-555555555555"
        secret_config = "proxies:\n  - uuid: " + secret_uuid
        payload = {
            "schema": "fwrouter-acceptance-reentry-native-proof/v1",
            "operation": 3,
            "sequence": 99,
            "phase": "core_apply",
            "runtime_running": True,
            "proof": {"wiring_proven": False, "unexpected_raw_value": secret_url},
            "proof_fields_present": ["wiring_proven", "unexpected_raw_value"],
            "selector_readback": {
                "vpn_auto_exists": True,
                "vpn_auto_selected_sha256": "a" * 64,
                "raw_selector": secret_uuid,
            },
            "native_selected_probe_history": {"present": True, "alive": False, "history": secret_url},
            "active_config_sha256": "b" * 64,
            "runtime_incarnation_sha256": "c" * 64,
            "receipt": {"present": True, "active_config_hash": secret_config},
            "raw_url": secret_url,
            "raw_uuid": secret_uuid,
            "raw_config": secret_config,
        }

        encoded = encode_safe_reentry_native_record(payload, max_bytes=1024)
        self.assertIsNotNone(encoded)
        assert encoded is not None
        self.assertLessEqual(len(encoded.encode("utf-8")), 1024)
        self.assertNotIn(secret_url, encoded)
        self.assertNotIn(secret_uuid, encoded)
        self.assertNotIn(secret_config, encoded)
        decoded = json.loads(encoded)
        self.assertEqual({"wiring_proven": False}, decoded["proof"])
        self.assertEqual(["wiring_proven"], decoded["proof_fields_present"])
        self.assertNotIn("raw_selector", decoded["selector_readback"])
        self.assertNotIn("raw_url", decoded)
        self.assertIsNone(encode_safe_reentry_native_record(payload, max_bytes=1))

    def test_startup_readiness_uses_strict_health_and_xray_is_a_separate_bounded_readback(self):
        ready = _extract_health_readiness_predicate()
        healthy = {
            "ok": True,
            "data": {
                "service": "fwrouter-api", "status": "healthy",
                "database": {"status": "healthy", "schema": {"ok": True}},
            },
        }
        self.assertTrue(ready(200, healthy))
        self.assertFalse(ready(503, healthy))
        for invalid in (
            {},
            {**healthy, "ok": False},
            {"ok": True, "data": {**healthy["data"], "service": "other"}},
            {"ok": True, "data": {**healthy["data"], "status": "starting"}},
            {"ok": True, "data": {**healthy["data"], "database": {"status": "failed", "schema": {"ok": True}}}},
            {"ok": True, "data": {**healthy["data"], "database": {"status": "healthy", "schema": {"ok": False}}}},
        ):
            self.assertFalse(ready(200, invalid), invalid)
        self.assertFalse(ready(200, None))

        fixture_source = APPLICATION_ACCEPTANCE_CONFTEST.read_text(encoding="utf-8")
        fixture_tree = ast.parse(fixture_source)
        acceptance_stack = next(
            node for node in fixture_tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "acceptance_stack"
        )
        start_worker = next(
            node for node in ast.walk(acceptance_stack)
            if isinstance(node, ast.FunctionDef) and node.name == "start_worker"
        )
        start_source = ast.get_source_segment(fixture_source, start_worker) or ""
        self.assertIn('base + "/health"', start_source)
        self.assertIn("_health_response_is_ready(code, payload)", start_source)
        self.assertIn("timeout=min(0.5, remaining)", start_source)
        self.assertIn('"path": "/health"', start_source)
        self.assertNotIn('base + "/xray"', start_source)

        scale_source = (Path(__file__).parents[1] / "application_acceptance" / "test_runtime_scale.py").read_text(
            encoding="utf-8")
        scale_tree = ast.parse(scale_source)
        scale_case = next(
            node for node in scale_tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "test_large_mihomo_health_readback_stays_bounded_under_concurrent_api_load"
        )
        scale_case_source = ast.get_source_segment(scale_source, scale_case) or ""
        self.assertIn('http_json(api + "/xray", timeout=20.0)', scale_case_source)
        self.assertIn('metrics["startup_readiness_probe"]', scale_case_source)
        self.assertIn('"native_loaded_client_count": len(loaded_after_readback)', scale_case_source)
        self.assertIn("len(loaded_after_readback) == SCALE_NATIVE_CLIENT_COUNT", scale_case_source)

    def test_worker_candidate_fingerprint_is_bounded_owned_and_path_redacted(self):
        with tempfile.TemporaryDirectory(prefix="fwrouter-candidate-fingerprint-") as temp:
            root = Path(temp) / "state"
            root.mkdir()
            candidate = root / "mihomo-candidate.yaml"
            candidate.write_bytes(b"synthetic-candidate-content")
            fingerprint = _extract_worker_candidate_fingerprint(root)
            result = fingerprint(candidate)
            self.assertEqual(candidate.name, result["candidate"])
            self.assertEqual(candidate.stat().st_size, result["bytes"])
            self.assertEqual(hashlib.sha256(candidate.read_bytes()).hexdigest(), result["sha256"])
            self.assertNotIn(str(root), json.dumps(result))
            self.assertIsNone(fingerprint(Path(temp) / "outside.yaml"))
            linked = root / "linked-candidate.yaml"
            linked.symlink_to(candidate)
            self.assertIsNone(fingerprint(linked))

    def test_bootstrap_projection_exposes_only_the_staged_native_proof_needed_by_scale_gate(self):
        project = _extract_bootstrap_phase_summary()
        source = {
            "ok": True,
            "status": "success",
            "inventory_authority": "persisted_success",
            "staged_publication_receipt": {
                "schema": 1, "staged_native_publication_complete": True,
                "client_uuid": "UUID_SENTINEL",
            },
            "generation_apply": {
                "final_mihomo": {"ok": True, "config": "RAW_CONFIG_SENTINEL"},
                "public_snapshots_changed": True,
                "xray": {"client_uuid": "UUID_SENTINEL"},
            },
            "public_profile_promote": {"profiles_count": 7, "nodes_count": 70,
                                        "client_uuid": "UUID_SENTINEL"},
            "final_mihomo_reconcile": {
                "ok": True, "skipped": True,
                "reason": "staged_publication_native_proof_complete",
                "details": "RAW_DETAILS_SENTINEL",
            },
            "raw_subscription": "RAW_SUBSCRIPTION_SENTINEL",
        }
        result = project(source)
        self.assertTrue(result["ok"])
        self.assertEqual("success", result["status"])
        self.assertEqual("persisted_success", result["inventory_authority"])
        self.assertEqual({"schema": 1, "staged_native_publication_complete": True},
                         result["staged_publication_receipt"])
        self.assertEqual({"final_mihomo": {"ok": True}, "public_snapshots_changed": True},
                         result["generation_apply"])
        self.assertEqual({"profiles_count": 7, "nodes_count": 70}, result["public_profile_promote"])
        self.assertEqual({"ok": True, "skipped": True,
                          "reason": "staged_publication_native_proof_complete"},
                         result["final_mihomo_reconcile"])
        encoded = json.dumps(result)
        for sentinel in ("UUID_SENTINEL", "RAW_CONFIG_SENTINEL", "RAW_DETAILS_SENTINEL",
                         "RAW_SUBSCRIPTION_SENTINEL"):
            self.assertNotIn(sentinel, encoded)

    def test_bootstrap_projection_does_not_promote_malformed_proof_or_counts_to_success(self):
        project = _extract_bootstrap_phase_summary()
        malformed = {
            "ok": True,
            "status": "success",
            "staged_publication_receipt": {"schema": True, "staged_native_publication_complete": "true"},
            "generation_apply": {
                "final_mihomo": {"ok": "true"}, "public_snapshots_changed": 1,
            },
            "public_profile_promote": {"profiles_count": True, "nodes_count": -1},
            "final_mihomo_reconcile": {"ok": True, "skipped": True, "reason": "untrusted reason"},
        }
        result = project(malformed)
        self.assertTrue(result["ok"])
        self.assertNotIn("staged_publication_receipt", result)
        self.assertNotIn("generation_apply", result)
        self.assertNotIn("public_profile_promote", result)
        self.assertEqual({"ok": True, "skipped": True}, result["final_mihomo_reconcile"])

    def test_startup_phase_diagnostic_parser_reads_only_a_bounded_tail(self):
        parser = _extract_startup_phase_parser()
        prefix = b"FWROUTER_ACCEPTANCE_BOOTSTRAP_PHASE "
        old_event = prefix + b'{"phase":"old_phase","event":"exit","seconds":9.0}\n'
        recent_event = prefix + b'{"phase":"recover_xray","event":"exit","seconds":1.25,"cpu_seconds":0.5}\n'
        with tempfile.TemporaryDirectory(prefix="fwrouter-startup-phase-parser-") as temp:
            log = Path(temp) / "worker.log"
            log.write_bytes(old_event + b"x" * (16 * 1024 - 1) + b"\n" + recent_event)
            result = parser(log)
        self.assertEqual("parsed", result["status"])
        self.assertLessEqual(result["tail_bytes"], 16 * 1024)
        self.assertEqual(["recover_xray"], [item["phase"] for item in result["events"]])
        self.assertNotIn("old_phase", result["totals"])
        self.assertEqual({"calls": 1, "wall_seconds": 1.25, "cpu_seconds": 0.5},
                         result["totals"]["recover_xray"])

    def test_startup_phase_diagnostic_parser_rejects_malformed_timings_and_drops_untrusted_fields(self):
        parser = _extract_startup_phase_parser()
        prefix = b"FWROUTER_ACCEPTANCE_BOOTSTRAP_PHASE "
        lines = [
            prefix + b"[]\n",
            prefix + b'{"phase":"unsafe phase","event":"exit","seconds":1}\n',
            prefix + b'{"phase":"bad_float","event":"exit","seconds":NaN,"cpu_seconds":Infinity,"token":"TOKEN_SENTINEL"}\n',
            prefix + b'{"phase":"negative","event":"exit","seconds":-1,"cpu_seconds":-2}\n',
            prefix + b'{"phase":"safe_phase","event":"exit","seconds":1.5,"cpu_seconds":0.25,"authorization":"TOKEN_SENTINEL"}\n',
        ]
        with tempfile.TemporaryDirectory(prefix="fwrouter-startup-phase-parser-invalid-") as temp:
            log = Path(temp) / "worker.log"
            log.write_bytes(b"".join(lines))
            result = parser(log)
        self.assertEqual(["bad_float", "negative", "safe_phase"],
                         [item["phase"] for item in result["events"]])
        self.assertEqual({"calls": 0, "wall_seconds": 0.0, "cpu_seconds": 0.0},
                         result["totals"]["bad_float"])
        self.assertEqual({"calls": 0, "wall_seconds": 0.0, "cpu_seconds": 0.0},
                         result["totals"]["negative"])
        self.assertEqual({"calls": 1, "wall_seconds": 1.5, "cpu_seconds": 0.25},
                         result["totals"]["safe_phase"])
        self.assertNotIn("TOKEN_SENTINEL", json.dumps(result))

    def test_startup_resource_spans_are_fixed_bootstrap_only_and_bounded(self):
        tree = ast.parse(APPLICATION_ACCEPTANCE_WORKER.read_text(encoding="utf-8"))
        build_app = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "build_app"
        )
        registrations = {
            (element.elts[0].id if isinstance(element.elts[0], ast.Name) else None,
             element.elts[1].value, element.elts[2].value)
            for node in ast.walk(build_app)
            if isinstance(node, ast.For) and isinstance(node.iter, ast.Tuple)
            for element in node.iter.elts
            if isinstance(element, ast.Tuple) and len(element.elts) == 3
            and isinstance(element.elts[1], ast.Constant) and isinstance(element.elts[1].value, str)
            and isinstance(element.elts[2], ast.Constant) and isinstance(element.elts[2].value, str)
        }
        required = {
            ("dataplane_global", "_read_effective_rules_artifact_uncached",
             "startup.dataplane.read_effective_rules_artifact"),
            ("dataplane_nft_render", "build_nft_rule_sets", "startup.dataplane.build_nft_rule_sets"),
            ("dataplane_status", "build_applied_runtime_enforcement",
             "startup.dataplane_status.build_applied_runtime_enforcement"),
            ("apply_service", "build_global_preflight", "startup.apply.build_global_preflight"),
            ("apply_service", "build_applied_runtime_enforcement",
             "startup.apply.build_applied_runtime_enforcement"),
        }
        self.assertTrue(required <= registrations, required - registrations)
        worker_source = APPLICATION_ACCEPTANCE_WORKER.read_text(encoding="utf-8")
        for attribute, phase in (
            ("_config_runtime_details", "startup.mihomo.config_runtime_details"),
            ("_config_text", "startup.mihomo.config_text"),
        ):
            self.assertIn(f'("{attribute}", "{phase}")', worker_source)
        self.assertIn("setattr(mihomo_adapter, attribute, observe_phase(phase, getattr(mihomo_adapter, attribute)))",
                      worker_source)

        observe_phase = next(
            node for node in ast.walk(build_app)
            if isinstance(node, ast.FunctionDef) and node.name == "observe_phase"
        )
        source = ast.get_source_segment(
            APPLICATION_ACCEPTANCE_WORKER.read_text(encoding="utf-8"), observe_phase,
        ) or ""
        self.assertIn("if not bootstrap_diagnostic_active", source)
        self.assertIn("record_phase = count <= bootstrap_diagnostic_limit", source)
        self.assertIn("if not record_phase", source)

    def test_canonical_xray_identity_oracle_uses_active_subjects_and_client_id_fallback(self):
        identity_pairs = _extract_current_xray_subject_identity_pairs()
        with tempfile.TemporaryDirectory(prefix="fwrouter-xray-identity-oracle-") as temp:
            database = Path(temp) / "synthetic.db"
            connection = sqlite3.connect(database)
            connection.execute(
                "CREATE TABLE subjects (subject_id TEXT, implementation_kind TEXT, is_active INTEGER, "
                "is_deleted INTEGER, metadata_json TEXT)"
            )
            rows = [
                ("active-uuid", 1, 0, {"detail": {"client_uuid": "synthetic-uuid", "email": "u1@test.invalid"}}),
                ("active-fallback", 1, 0, {"detail": {"client_id": "synthetic-client-id", "email": "u2@test.invalid"}}),
                ("disabled-detail", 1, 0, {"detail": {"client_uuid": "disabled-id", "email": "disabled@test.invalid", "enabled": False}}),
                ("inactive", 0, 0, {"detail": {"client_uuid": "inactive-id", "email": "inactive@test.invalid"}}),
                ("deleted", 1, 1, {"detail": {"client_uuid": "deleted-id", "email": "deleted@test.invalid"}}),
            ]
            connection.executemany(
                "INSERT INTO subjects VALUES (?, 'xray', ?, ?, ?)",
                [(subject_id, active, deleted, json.dumps(metadata))
                 for subject_id, active, deleted, metadata in rows],
            )
            connection.execute(
                "INSERT INTO subjects VALUES (?, 'mihomo', 1, 0, ?)",
                ("other-module", json.dumps({"detail": {"client_uuid": "other-id", "email": "other@test.invalid"}})),
            )
            connection.commit()
            connection.close()

            self.assertEqual(
                {("synthetic-uuid", "u1@test.invalid"), ("synthetic-client-id", "u2@test.invalid")},
                identity_pairs(database),
            )

            connection = sqlite3.connect(database)
            connection.execute(
                "INSERT INTO subjects VALUES (?, 'xray', 1, 0, ?)",
                ("duplicate", json.dumps({"detail": {"client_uuid": "synthetic-uuid", "email": "u1@test.invalid"}})),
            )
            connection.commit()
            connection.close()
            with self.assertRaisesRegex(AssertionError, "duplicated"):
                identity_pairs(database)

    def test_routine_unit_failure_report_is_bound_bounded_and_never_eligible(self):
        path = "backend/tests/test_apply_pipeline.py"
        manifest = aggregate_hosted.gate.load_manifest()
        source, base = "a" * 40, "b" * 40
        with mock.patch.object(aggregate_hosted.gate, "git",
                               side_effect=lambda _cmd, revision: source if revision == "HEAD" else base), \
             mock.patch.object(aggregate_hosted.gate, "changed_paths", return_value=[path]):
            plan = aggregate_hosted.gate.make_plan(base, manifest, [path])
            unit = {
                "schema_version": 1, "source_commit": source, "plan_digest": plan["plan_digest"],
                "manifest_version": manifest["version"],
                "manifest_digest": aggregate_hosted.gate.canonical_digest(manifest),
                "blocked_reasons": ["installer failed token=SECRET_SENTINEL"] * 40,
            }
            unit["report_digest"] = aggregate_hosted.gate.canonical_digest(unit)
            with tempfile.TemporaryDirectory(prefix="fwrouter-unit-failure-report-contract-") as temp:
                root = Path(temp)
                plan_path, unit_path = root / "plan.json", root / "unit.json"
                plan_path.write_text(json.dumps(plan), encoding="utf-8")
                unit_path.write_text(json.dumps(unit), encoding="utf-8")
                result = aggregate_hosted.unit_failure_report(plan_path, unit_path, 1)
        self.assertEqual(("routine-unit-failure", "blocked", False, 1),
                         (result["report_kind"], result["aggregate_status"],
                          result["eligible"], result["unit_exit_code"]))
        self.assertEqual(32, len(result["unit_blocked_reasons"]))
        self.assertLessEqual(max(map(len, result["unit_blocked_reasons"])), 1024)
        self.assertNotIn("SECRET_SENTINEL", json.dumps(result))
        digest = result.pop("report_digest")
        self.assertEqual(aggregate_hosted.gate.canonical_digest(result), digest)
        self.assertIn("hosted-functional-profile-bundle", result["not_run"])

    def test_aggregate_exception_persists_distinct_ineligible_error_report(self):
        with tempfile.TemporaryDirectory(prefix="fwrouter-aggregate-error-contract-") as temp:
            output_path = Path(temp) / "aggregate.json"
            with mock.patch.object(sys, "argv", ["aggregate_hosted.py", "--plan", "unused-plan",
                                                   "--unit-report", "unused-unit", "--output", str(output_path)]), \
                 mock.patch.object(aggregate_hosted, "aggregate",
                                   side_effect=aggregate_hosted.gate.GateError(
                                       "invalid api_token=SECRET_SENTINEL evidence")), \
                 mock.patch("builtins.print"), mock.patch("sys.stderr", io.StringIO()):
                self.assertEqual(2, aggregate_hosted.main())
            result = json.loads(output_path.read_text(encoding="utf-8"))
        self.assertEqual(("aggregate-error", "invalid-or-incomplete-evidence", False),
                         (result["report_kind"], result["aggregate_status"], result["eligible"]))
        self.assertNotIn("SECRET_SENTINEL", json.dumps(result))
        digest = result.pop("report_digest")
        self.assertEqual(aggregate_hosted.gate.canonical_digest(result), digest)

    def test_aggregate_missing_required_main_smoke_is_ineligible(self):
        manifest = aggregate_hosted.gate.load_manifest()
        path = "backend/tests/test_configuration_contract.py"
        source = "a" * 40
        plan = {
            "schema_version": 1, "source_commit": source, "plan_digest": "b" * 64,
            "selected_files": [path], "required_levels": ["L0", "L2"], "full_suite_required": False,
        }
        unit = {
            "schema_version": 1, "source_commit": source, "plan_digest": plan["plan_digest"],
            "manifest_version": manifest["version"],
            "manifest_digest": aggregate_hosted.gate.canonical_digest(manifest),
            "deferred_remote_suites": [], "executions": [{"path": path, "status": "passed"}],
            "blocked_reasons": [], "completed_levels": ["L0", "L2"],
            "l0": {"status": "passed"}, "node_status": {},
        }
        unit["report_digest"] = aggregate_hosted.gate.canonical_digest(unit)
        with tempfile.TemporaryDirectory(prefix="fwrouter-main-smoke-required-contract-") as temp:
            root = Path(temp)
            plan_path, unit_path = root / "plan.json", root / "unit.json"
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            unit_path.write_text(json.dumps(unit), encoding="utf-8")
            with mock.patch.object(aggregate_hosted.gate, "ensure_plan"), \
                 mock.patch.object(aggregate_hosted.gate, "classify", return_value={"eligible": True}), \
                 mock.patch.object(aggregate_hosted.gate, "load_baseline", return_value={}):
                result = aggregate_hosted.aggregate(
                    plan_path, unit_path, [], [], [], minimum_repeats=1,
                    require_main_smoke=True)
        self.assertFalse(result["eligible"])
        self.assertEqual("not-run", result["main_smoke_status"])
        self.assertTrue(any("required main native application smoke" in item
                            for item in result["blocked_reasons"]))

    def test_aggregate_reports_qcp_selected_files_separately_from_unrun_files(self):
        manifest = aggregate_hosted.gate.load_manifest()
        selected = sorted(
            row["path"] for row in manifest["test_files"]
            if row.get("execution_profile") == "qualified-child-process"
            and row["path"] != "backend/tests/test_xray_native_readback.py"
        )
        scenario_path = launcher.ROOT / "tests/acceptance/qualified-child-scenarios.json"
        scenario = json.loads(scenario_path.read_text(encoding="utf-8"))
        expected_ids = scenario["nodeids"]
        registered_files = {nodeid.split("::", 1)[0] for nodeid in expected_ids}
        self.assertEqual(set(selected), registered_files)

        source, plan_digest = "a" * 40, "b" * 64
        levels = sorted({"L0", *(row["primary_level"] for row in manifest["test_files"]
                                  if row["path"] in selected)})
        plan = {
            "schema_version": 1, "source_commit": source, "plan_digest": plan_digest,
            "selected_files": selected, "required_levels": levels, "full_suite_required": False,
        }
        unit = {
            "schema_version": 1, "source_commit": source, "plan_digest": plan_digest,
            "manifest_version": manifest["version"],
            "manifest_digest": aggregate_hosted.gate.canonical_digest(manifest),
            "deferred_remote_suites": selected,
            "executions": [{"path": path, "status": "deferred_remote_hosted"} for path in selected],
            "blocked_reasons": [], "completed_levels": ["L0"],
            "l0": {"status": "passed"}, "node_status": {},
        }
        unit["report_digest"] = aggregate_hosted.gate.canonical_digest(unit)

        runtime = {
            "schema": "fwrouter-hosted-inputs/v1",
            "base_image": provision.BASE_IMAGE,
            "playwright": provision.PLAYWRIGHT_VERSION,
            "native": {
                name: {
                    "version": provision.INPUTS[name]["version"],
                    "source_archive_sha256": provision.INPUTS[name]["sha256"],
                    "binary_sha256": provision.BINARY_SHA256[name],
                }
                for name in ("xray", "mihomo")
            },
            "chromium": {
                "version": provision.CHROMIUM_VERSION,
                "source_archive_sha256": provision.INPUTS["chromium"]["sha256"],
                "bundle_sha256": provision.CHROMIUM_BUNDLE_SHA256,
                "executable_sha256": provision.CHROMIUM_BINARY_SHA256,
            },
        }
        nonce = "c" * 32
        profile = {
            "schema": "fwrouter-qualified-child-profile/v1",
            "profile": "qualified-child-process",
            "source_revision": source,
            "plan_digest": plan_digest,
            "suite_nonce": nonce,
            "mihomo": {
                "version": provision.INPUTS["mihomo"]["version"],
                "sha256": provision.BINARY_SHA256["mihomo"],
            },
        }
        profile_sha = hashlib.sha256(
            (json.dumps(profile, sort_keys=True, indent=2) + "\n").encode("utf-8")
        ).hexdigest()
        qcp_receipt = {
            "schema": "fwrouter-qualified-child-receipt/v1",
            "scope": "qualified-child-process", "status": "passed",
            "source_revision": source, "plan_digest": plan_digest, "suite_nonce": nonce,
            "cleanup": "owned_resources_removed", "expected_ids": expected_ids,
            "profile_sha256": profile_sha,
            "tests": {
                "node_status": {nodeid: "passed" for nodeid in expected_ids},
                "tests": len(expected_ids), "failed": 0, "skipped": 0, "contract_valid": True,
            },
        }

        with tempfile.TemporaryDirectory(prefix="fwrouter-qcp-report-contract-") as temp:
            root = Path(temp)
            plan_path, unit_path = root / "plan.json", root / "unit.json"
            runtime_path = root / "runtime.json"
            qcp_path = root / "qcp-receipt.json"
            (root / "profile.json").write_text(json.dumps(profile), encoding="utf-8")
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            unit_path.write_text(json.dumps(unit), encoding="utf-8")
            runtime_path.write_text(json.dumps(runtime), encoding="utf-8")
            qcp_path.write_text(json.dumps(qcp_receipt), encoding="utf-8")
            with mock.patch.object(aggregate_hosted.gate, "ensure_plan"), \
                 mock.patch.object(aggregate_hosted.gate, "classify", return_value={"eligible": True}), \
                 mock.patch.object(aggregate_hosted.gate, "load_baseline", return_value={}):
                completed = aggregate_hosted.aggregate(
                    plan_path, unit_path, [], [qcp_path], [], minimum_repeats=1,
                    runtime_inputs_path=runtime_path)
                missing = aggregate_hosted.aggregate(
                    plan_path, unit_path, [], [], [], minimum_repeats=1,
                    runtime_inputs_path=runtime_path)

        self.assertEqual(selected, completed["qualified_child_process_selected"])
        self.assertEqual([], completed["qualified_child_process_not_run"])
        self.assertTrue(completed["eligible"])
        self.assertEqual(selected, missing["qualified_child_process_selected"])
        self.assertEqual(selected, missing["qualified_child_process_not_run"])
        self.assertFalse(missing["eligible"])

    def test_main_smoke_registry_rejects_non_object_root_and_non_object_rows(self):
        with tempfile.TemporaryDirectory(prefix="fwrouter-main-smoke-registry-contract-") as temp:
            root = Path(temp)
            acceptance = root / "tests" / "acceptance"
            acceptance.mkdir(parents=True)
            registry = acceptance / "scenarios.json"
            with mock.patch.object(aggregate_hosted, "ROOT", root):
                registry.write_text("[]", encoding="utf-8")
                with self.assertRaises(aggregate_hosted.gate.GateError):
                    aggregate_hosted.main_smoke_nodeid()
                registry.write_text(json.dumps({
                    "schema": "fwrouter-acceptance-scenarios/v1",
                    "scenarios": [None],
                }), encoding="utf-8")
                with self.assertRaises(aggregate_hosted.gate.GateError):
                    aggregate_hosted.main_smoke_nodeid()

    def test_functional_provider_and_packet_node_sets_are_exact_and_disjoint(self):
        ordinary = launcher.expected_acceptance_nodeids("functional-ordinary")
        provider = launcher.expected_acceptance_nodeids("provider-cohort")
        packet = launcher.expected_acceptance_nodeids("packet-diagnostic")
        functional = aggregate_hosted.functional_nodeids()
        self.assertEqual((27, 12, 4), (len(ordinary), len(provider), len(packet)))
        self.assertFalse(ordinary & provider)
        self.assertFalse(packet & (ordinary | provider))
        self.assertEqual(functional, ordinary | provider)
        with self.assertRaisesRegex(launcher.NotRun, "functional.*ambiguous"):
            launcher.expected_acceptance_nodeids("functional")
        self.assertEqual(43, len(ordinary | provider | packet))
        with self.assertRaises(launcher.NotRun):
            launcher.validate_suite_node_receipt(
                [{"nodeid": nodeid, "status": "passed",
                  "phases": {"setup": "passed", "call": "passed", "teardown": "passed"}}
                 for nodeid in functional],
                "functional-ordinary", sorted(functional))

    def test_aggregate_requires_only_the_selected_exact_hosted_profile_partitions(self):
        manifests = json.loads((launcher.ROOT / "tests/gates/manifest.json").read_text(encoding="utf-8"))
        rows = manifests["test_files"]
        app_rows = [row for row in rows if row.get("execution_profile") == "hosted-isolated-compose"]
        packet_rows = [row for row in rows if row.get("execution_profile") == "hosted-kernel-packet"]
        self.assertEqual(["functional-ordinary", "provider-cohort"],
                         aggregate_hosted.required_hosted_partitions(app_rows))
        self.assertEqual(["packet-diagnostic"], aggregate_hosted.required_hosted_partitions(packet_rows))
        self.assertEqual([], aggregate_hosted.required_hosted_partitions([]))

    def test_aggregate_rejects_missing_duplicate_and_unexpected_hosted_partitions(self):
        with tempfile.TemporaryDirectory(prefix="fwrouter-profile-bundle-contract-") as temp:
            root = Path(temp)
            paths = []
            for index, suite in enumerate(("functional-ordinary", "functional-ordinary",
                                           "provider-cohort", "packet-diagnostic", "functional")):
                path = root / f"receipt-{index}.json"
                path.write_text(json.dumps({"suite": suite}), encoding="utf-8")
                paths.append(path)
            grouped, problems = aggregate_hosted.group_hosted_receipts(
                paths, ["functional-ordinary", "provider-cohort", "packet-diagnostic"], 1)
            self.assertEqual(2, len(grouped["functional-ordinary"]))
            self.assertEqual(1, len(grouped["provider-cohort"]))
            self.assertEqual(1, len(grouped["packet-diagnostic"]))
            self.assertEqual(2, len(problems))
            self.assertTrue(any("unexpected profile partition" in item for item in problems))
            self.assertTrue(any("functional-ordinary receipts: 2; required: 1" in item for item in problems))
            _, missing = aggregate_hosted.group_hosted_receipts(
                [paths[0]], ["functional-ordinary", "provider-cohort", "packet-diagnostic"], 1)
            self.assertEqual(2, len(missing))
            self.assertTrue(all("required: 1" in item for item in missing))

    def test_packet_base_image_inspect_failure_is_staged_bounded_and_redacted(self):
        stderr = (b"x" * 5000 + b" /var/lib/docker/image/metadata token=TOKEN_SENTINEL "
                  b"Bearer BEARER_SENTINEL")
        completed = SimpleNamespace(returncode=1, stdout=b"STDOUT_SENTINEL",
                                    stderr=stderr)
        with mock.patch.object(launcher.subprocess, "run", return_value=completed):
            with self.assertRaises(launcher.NotRun) as raised:
                launcher._docker_json(
                    ["/usr/bin/docker", "image", "inspect", "python:3.11-bookworm@sha256:" + "a" * 64],
                    cwd=Path("/tmp"), env={}, stage="packet pinned base image inspect")
        message = str(raised.exception)
        self.assertIn("packet pinned base image inspect", message)
        self.assertIn("exit_code=1", message)
        self.assertIn("[REDACTED]", message)
        self.assertNotIn("TOKEN_SENTINEL", message)
        self.assertNotIn("BEARER_SENTINEL", message)
        self.assertNotIn("STDOUT_SENTINEL", message)
        self.assertIn("/var/lib/docker/image/metadata", message)
        self.assertLess(len(message), 2300)

    def test_packet_base_image_missing_cache_pulls_once_and_requires_pinned_readback(self):
        digest = "a" * 64
        image_ref = "python:3.11-bookworm@sha256:" + digest
        inspected = [{"Id": "sha256:" + "c" * 64,
                      "RepoDigests": ["python@sha256:" + digest],
                      "Os": "linux", "Architecture": "amd64",
                      "Config": {"Env": [f"PATH={launcher.PYTHON_BASE_FIXED_ENV['PATH']}",
                                             f"LANG={launcher.PYTHON_BASE_FIXED_ENV['LANG']}",
                                             "PYTHON_VERSION=3.11.13"]}}]

        def readback(_argv, **kwargs):
            self.assertEqual("packet pinned base image inspect", kwargs.get("stage"))
            return inspected

        missing = SimpleNamespace(returncode=1, stdout=b"",
                                  stderr=f"Error response from daemon: No such image: {image_ref}".encode())
        with mock.patch.object(launcher.subprocess, "run", return_value=missing) as cache_inspect, \
             mock.patch.object(launcher, "_docker_exec_capture", return_value={
                 "exit_code": 0, "stdout": "unrecorded pull output", "stderr": ""}) as pull, \
             mock.patch.object(launcher, "_docker_json", side_effect=readback) as inspect:
            receipt = {"image_measurements": {}}
            metadata = launcher.prepare_packet_base_image(
                "/usr/bin/docker", image_ref, cwd=Path("/tmp"), env={}, receipt=receipt)
        self.assertEqual({"PYTHON_VERSION": "3.11.13"}, metadata)
        pull.assert_called_once_with(
            ["/usr/bin/docker", "pull", "--platform", "linux/amd64", image_ref],
            cwd=Path("/tmp"), env={}, timeout=300)
        cache_inspect.assert_called_once_with(
            ["/usr/bin/docker", "image", "inspect", image_ref], cwd=Path("/tmp"), env={},
            capture_output=True, timeout=10, check=False)
        inspect.assert_called_once()
        self.assertEqual(1, receipt["image_measurements"]["base_image_pull_count"])
        self.assertEqual("passed", receipt["packet_base_image_pull"]["status"])
        self.assertNotIn("unrecorded pull output", json.dumps(receipt))

        with mock.patch.object(launcher, "_base_image_cache_presence", return_value=(True, 0.1)), \
             mock.patch.object(launcher, "_docker_exec_capture") as cached_pull, \
             mock.patch.object(launcher, "_docker_json", side_effect=readback):
            cached_receipt = {"image_measurements": {}}
            launcher.prepare_packet_base_image(
                "/usr/bin/docker", image_ref, cwd=Path("/tmp"), env={}, receipt=cached_receipt)
        cached_pull.assert_not_called()
        self.assertEqual(0, cached_receipt["image_measurements"]["base_image_pull_count"])
        self.assertEqual("cached", cached_receipt["packet_base_image_pull"]["status"])

        wrong_missing = SimpleNamespace(returncode=1, stdout=b"",
                                        stderr=b"Error response from daemon: No such image: another:image")
        with mock.patch.object(launcher.subprocess, "run", return_value=wrong_missing) as unknown_cache, \
             mock.patch.object(launcher, "_docker_exec_capture") as unknown_pull, \
             mock.patch.object(launcher, "_docker_json") as unknown_inspect:
            with self.assertRaises(launcher.NotRun):
                launcher.prepare_packet_base_image(
                    "/usr/bin/docker", image_ref, cwd=Path("/tmp"), env={}, receipt={})
        unknown_pull.assert_not_called()
        unknown_inspect.assert_not_called()
        unknown_cache.assert_called_once()

        with mock.patch.object(launcher, "_base_image_cache_presence", return_value=(False, 0.2)), \
             mock.patch.object(launcher, "_docker_exec_capture", return_value={
                 "exit_code": 1, "stdout": "", "stderr": "denied token=TOKEN_SENTINEL"}) as failed_pull, \
             mock.patch.object(launcher, "_docker_json") as failed_inspect:
            failed_receipt = {"image_measurements": {}}
            with self.assertRaises(launcher.NotRun):
                launcher.prepare_packet_base_image(
                    "/usr/bin/docker", image_ref, cwd=Path("/tmp"), env={}, receipt=failed_receipt)
        failed_pull.assert_called_once()
        failed_inspect.assert_not_called()
        self.assertEqual(1, failed_receipt["image_measurements"]["base_image_pull_count"])
        self.assertEqual("failed", failed_receipt["packet_base_image_pull"]["status"])
        self.assertNotIn("TOKEN_SENTINEL", json.dumps(failed_receipt))

        with mock.patch.object(launcher.subprocess, "run") as invalid_ref_docker, \
             mock.patch.object(launcher, "_docker_exec_capture") as invalid_ref_pull:
            with self.assertRaises(launcher.NotRun):
                launcher.prepare_packet_base_image(
                    "/usr/bin/docker", image_ref + ".other", cwd=Path("/tmp"), env={}, receipt={})
        invalid_ref_docker.assert_not_called()
        invalid_ref_pull.assert_not_called()

        suffixed_missing = SimpleNamespace(
            returncode=1, stdout=b"",
            stderr=f"Error response from daemon: No such image: {image_ref} additional text".encode())
        with mock.patch.object(launcher.subprocess, "run", return_value=suffixed_missing), \
             mock.patch.object(launcher, "_docker_exec_capture") as suffix_pull, \
             mock.patch.object(launcher, "_docker_json") as suffix_readback:
            with self.assertRaises(launcher.NotRun):
                launcher.prepare_packet_base_image(
                    "/usr/bin/docker", image_ref, cwd=Path("/tmp"), env={}, receipt={})
        suffix_pull.assert_not_called()
        suffix_readback.assert_not_called()

    def test_packet_inherited_environment_is_bound_to_pinned_amd64_image_metadata(self):
        digest = "a" * 64
        image_ref = "python:3.11-bookworm@sha256:" + digest
        base_env = [
            f"PATH={launcher.PYTHON_BASE_FIXED_ENV['PATH']}",
            f"LANG={launcher.PYTHON_BASE_FIXED_ENV['LANG']}",
            "PYTHON_VERSION=3.11.13", "PYTHON_SHA256=" + "b" * 64,
            "GPG_KEY=official-public-key-id",
        ]
        inspected = [{"Id": "sha256:" + "c" * 64,
                      "RepoDigests": ["python@sha256:" + digest],
                      "Os": "linux", "Architecture": "amd64", "Config": {"Env": base_env}}]
        metadata = launcher.validate_pinned_python_base_environment(inspected, image_ref)
        self.assertEqual({"PYTHON_VERSION": "3.11.13", "PYTHON_SHA256": "b" * 64,
                          "GPG_KEY": "official-public-key-id"}, metadata)

        unknown = json.loads(json.dumps(inspected))
        unknown[0]["Config"]["Env"].append("UNREVIEWED_BASE_FIELD=VALUE_SENTINEL")
        with self.assertRaises(launcher.NotRun) as raised:
            launcher.validate_pinned_python_base_environment(unknown, image_ref)
        self.assertIn("UNREVIEWED_BASE_FIELD", str(raised.exception))
        self.assertNotIn("VALUE_SENTINEL", str(raised.exception))

        wrong_digest = json.loads(json.dumps(inspected))
        wrong_digest[0]["RepoDigests"] = ["python@sha256:" + "d" * 64]
        with self.assertRaises(launcher.NotRun):
            launcher.validate_pinned_python_base_environment(wrong_digest, image_ref)
        wrong_arch = json.loads(json.dumps(inspected))
        wrong_arch[0]["Architecture"] = "arm64"
        with self.assertRaises(launcher.NotRun):
            launcher.validate_pinned_python_base_environment(wrong_arch, image_ref)

    def test_packet_dependency_base_qualification_passes_full_inspect_rows(self):
        image_ref = "python:3.11-bookworm@sha256:" + "a" * 64
        inspected = [{"Id": "sha256:" + "c" * 64,
                      "RepoDigests": ["python@sha256:" + "a" * 64],
                      "Os": "linux", "Architecture": "amd64",
                      "Config": {"Env": [f"PATH={launcher.PYTHON_BASE_FIXED_ENV['PATH']}",
                                             f"LANG={launcher.PYTHON_BASE_FIXED_ENV['LANG']}",
                                             "PYTHON_VERSION=3.11.13"]}}]
        metadata = {"image_id": inspected[0]["Id"], "digest": "a" * 64,
                    "lock_sha256": "b" * 64, "size_bytes": 2_600_000_000}
        receipt = {}
        with mock.patch.object(launcher, "prepare_dependency_base_image", return_value=metadata) as prepare, \
             mock.patch.object(launcher, "_docker_json", return_value=inspected) as inspect:
            inherited = launcher.prepare_packet_dependency_base_image(
                "/usr/bin/docker", image_ref, lock_digest="b" * 64,
                cwd=Path("/tmp"), env={}, receipt=receipt)
        self.assertEqual({"PYTHON_VERSION": "3.11.13"}, inherited)
        self.assertEqual(inspected[0]["Id"], receipt["packet_base_image_environment"]["image_id"])
        prepare.assert_called_once()
        self.assertEqual("immutable packet dependency base metadata", inspect.call_args.kwargs["stage"])

        with mock.patch.object(launcher, "prepare_dependency_base_image", return_value=metadata), \
             mock.patch.object(launcher, "_docker_json", return_value=inspected[0]):
            with self.assertRaises(launcher.NotRun):
                launcher.prepare_packet_dependency_base_image(
                    "/usr/bin/docker", image_ref, lock_digest="b" * 64,
                    cwd=Path("/tmp"), env={}, receipt={})

    def test_runtime_cleanup_plan_skips_compose_discovery_before_creation(self):
        self.assertEqual((False, set()), launcher.runtime_cleanup_plan(
            creation_attempted=False, packet_mode=True, network_id=None))
        discover_roles, networks = launcher.runtime_cleanup_plan(
            creation_attempted=True, packet_mode=True, network_id=None)
        self.assertTrue(discover_roles)
        self.assertEqual(set(launcher.PACKET_NETWORKS), networks)

    def test_packet_runtime_environment_matches_base_values_and_rejects_unknown_or_changed_values(self):
        metadata = {"PYTHON_VERSION": "3.11.13", "PYTHON_SHA256": "b" * 64}
        expected = {
            **launcher.PYTHON_BASE_FIXED_ENV,
            "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": "/workspace/backend",
            "FWROUTER_XRAY_BINARY": "/opt/fwrouter-test/bin/xray",
            "FWROUTER_MIHOMO_BINARY": "/opt/fwrouter-test/bin/mihomo",
            "FWROUTER_BROWSER_EXECUTABLE": "/opt/fwrouter-test/chromium/chrome-linux64/chrome",
            "FWROUTER_CHROMIUM_BINARY": "/opt/fwrouter-test/chromium/chrome-linux64/chrome",
            "PLAYWRIGHT_BROWSERS_PATH": "/opt/fwrouter-test/playwright-browsers",
            "FWROUTER_ACCEPTANCE_PROFILE": "/run/fwrouter-acceptance/profile.json",
            "HOME": "/tmp", **metadata,
        }
        raw_env = [f"{key}={value}" for key, value in expected.items()]
        launcher.validate_packet_runtime_environment(raw_env, "client", metadata)

        with self.assertRaises(launcher.NotRun) as raised:
            launcher.validate_packet_runtime_environment(raw_env + ["UNREVIEWED_RUNTIME_FIELD=VALUE_SENTINEL"],
                                                         "client", metadata)
        self.assertIn("UNREVIEWED_RUNTIME_FIELD", str(raised.exception))
        self.assertNotIn("VALUE_SENTINEL", str(raised.exception))
        changed = ["PYTHON_VERSION=3.12.0" if item.startswith("PYTHON_VERSION=") else item for item in raw_env]
        with self.assertRaises(launcher.NotRun) as changed_raised:
            launcher.validate_packet_runtime_environment(changed, "client", metadata)
        self.assertIn("mismatched_names=['PYTHON_VERSION']", str(changed_raised.exception))
        self.assertNotIn("3.12.0", str(changed_raised.exception))

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
                      '"mode=1777"', '"noexec"', '"nosuid"', '"nodev"',
                      'pathlib.Path("/tmp").lstat()', 'stat.S_IMODE(tmp_stat.st_mode)',
                      'tmp_stat.st_uid!=0', 'tmp_stat.st_gid!=0'):
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

        tmp_stat = SimpleNamespace(st_mode=stat.S_IFDIR | 0o1777, st_uid=0, st_gid=0)
        expected = {"router": (512 * 1024 ** 2, "524288k"),
                    "client": (32 * 1024 ** 2, "32768k"),
                    "endpoint": (64 * 1024 ** 2, "65536k")}
        for role, (size_bytes, rendered_size) in expected.items():
            mode_option = ",mode=1777" if role != "router" else ""
            mountinfo = ("36 25 0:32 / /tmp rw,nosuid,nodev,noexec,relatime "
                         f"- tmpfs tmpfs rw,size={rendered_size}{mode_option}\n")
            with self.subTest(role=role), mock.patch.object(sys, "argv", ["probe", role]), \
                    mock.patch.object(Path, "read_text", return_value=mountinfo), \
                    mock.patch.object(Path, "lstat", return_value=tmp_stat), \
                    mock.patch("builtins.print") as print_result:
                exec(code, {"__name__": "__main__"})
                observed = json.loads(print_result.call_args.args[0])
                self.assertEqual({"status": "passed", "role": role, "size_bytes": size_bytes,
                                  "flags": sorted({"mode=1777", "noexec", "nodev", "nosuid", "rw"}),
                                  "tmp_directory_uid": 0, "tmp_directory_gid": 0,
                                  "tmp_directory_mode": 0o1777}, observed)

        failing_mountinfos = {
            "missing-flag": "36 25 0:32 / /tmp rw,nodev,noexec,relatime - tmpfs tmpfs rw,size=524288k,mode=1777\n",
            "wrong-size": "36 25 0:32 / /tmp rw,nosuid,nodev,noexec,relatime - tmpfs tmpfs rw,size=1m,mode=1777\n",
            "duplicate-mount": (
                "36 25 0:32 / /tmp rw,nosuid,nodev,noexec,relatime - tmpfs tmpfs rw,size=524288k,mode=1777\n"
                "37 25 0:33 / /tmp rw,nosuid,nodev,noexec,relatime - tmpfs tmpfs rw,size=524288k,mode=1777\n"),
            "conflicting-mode": "36 25 0:32 / /tmp rw,nosuid,nodev,noexec,relatime - tmpfs tmpfs rw,size=524288k,mode=1770\n",
            "duplicate-mode": "36 25 0:32 / /tmp rw,nosuid,nodev,noexec,relatime - tmpfs tmpfs rw,size=524288k,mode=1777,mode=1777\n",
        }
        expected_failure = {
            "missing-flag": ("mount_flags", "required_tmpfs_flags"),
            "wrong-size": ("mount_size", "exact_role_size"),
            "duplicate-mount": ("mount_shape", "exactly_one_tmp_mount"),
            "conflicting-mode": ("mount_flags", "compatible_explicit_mode"),
            "duplicate-mode": ("mount_flags", "compatible_explicit_mode"),
        }
        for failure, mountinfo in failing_mountinfos.items():
            with self.subTest(failure=failure), mock.patch.object(sys, "argv", ["probe", "router"]), \
                    mock.patch.object(Path, "read_text", return_value=mountinfo), \
                    mock.patch.object(Path, "lstat", return_value=tmp_stat), \
                    mock.patch("builtins.print") as print_result:
                with self.assertRaises(SystemExit) as raised:
                    exec(code, {"__name__": "__main__"})
                self.assertEqual(2, raised.exception.code)
                observed = json.loads(print_result.call_args.args[0])
                self.assertEqual(("failed", "router", *expected_failure[failure]),
                                 (observed["status"], observed["role"], observed["stage"], observed["predicate"]))
                self.assertNotIn("36 25 0:32", json.dumps(observed))

        bad_tmp_directories = {
            "wrong_mode": SimpleNamespace(st_mode=stat.S_IFDIR | 0o1770, st_uid=0, st_gid=0),
            "wrong_owner": SimpleNamespace(st_mode=stat.S_IFDIR | 0o1777, st_uid=1, st_gid=0),
            "wrong_group": SimpleNamespace(st_mode=stat.S_IFDIR | 0o1777, st_uid=0, st_gid=1),
            "symlink": SimpleNamespace(st_mode=stat.S_IFLNK | 0o777, st_uid=0, st_gid=0),
        }
        expected_tmp_failure = {
            "wrong_mode": ("tmp_directory_mode", "sticky_world_writable_1777"),
            "wrong_owner": ("tmp_directory_owner", "root_owned_directory"),
            "wrong_group": ("tmp_directory_owner", "root_owned_directory"),
            "symlink": ("tmp_directory_shape", "real_directory"),
        }
        for failure, observed_stat in bad_tmp_directories.items():
            with self.subTest(tmp_directory=failure), mock.patch.object(sys, "argv", ["probe", "router"]), \
                    mock.patch.object(Path, "lstat", return_value=observed_stat), \
                    mock.patch("builtins.print") as print_result:
                with self.assertRaises(SystemExit) as raised:
                    exec(code, {"__name__": "__main__"})
                self.assertEqual(2, raised.exception.code)
                observed = json.loads(print_result.call_args.args[0])
                self.assertEqual(("failed", "router", *expected_tmp_failure[failure]),
                                 (observed["status"], observed["role"], observed["stage"], observed["predicate"]))

    def test_packet_tmpfs_failed_exec_reports_only_sanitized_predicate_summary(self):
        safe = {"status": "failed", "role": "router", "stage": "mount_flags",
                "predicate": "required_tmpfs_flags", "tmp_mount_count": 1,
                "filesystem_types": ["tmpfs"], "observed_flags": ["nodev", "noexec", "rw"],
                "size_option_count": 1, "size_bytes": 536870912}
        completed = SimpleNamespace(returncode=2, stdout=json.dumps(safe).encode("utf-8"),
                                    stderr=b"/proc/self/mountinfo PATH_SENTINEL token=TOKEN_SENTINEL")
        with mock.patch.object(launcher.subprocess, "run", return_value=completed):
            with self.assertRaises(launcher.NotRun) as raised:
                launcher._docker_exec_small(
                    ["/usr/bin/docker", "exec", "<owned-id>"], cwd=Path("/tmp"), env={},
                    stage="packet_tmpfs_mount_readback", role="router")
        message = str(raised.exception)
        self.assertIn("stage=packet_tmpfs_mount_readback", message)
        self.assertIn("role=router", message)
        self.assertIn('"predicate":"required_tmpfs_flags"', message)
        self.assertIn('"size_bytes":536870912', message)
        for sentinel in ("PATH_SENTINEL", "TOKEN_SENTINEL", "/proc/self/mountinfo"):
            self.assertNotIn(sentinel, message)

        unsafe = dict(safe, raw_mountinfo="/proc/self/mountinfo PATH_SENTINEL")
        completed = SimpleNamespace(returncode=2, stdout=json.dumps(unsafe).encode("utf-8"), stderr=b"")
        with mock.patch.object(launcher.subprocess, "run", return_value=completed):
            with self.assertRaises(launcher.NotRun) as raised_unsafe:
                launcher._docker_exec_small(
                    ["/usr/bin/docker", "exec", "<owned-id>"], cwd=Path("/tmp"), env={},
                    stage="packet_tmpfs_mount_readback", role="router")
        self.assertIn("diagnostic=unavailable", str(raised_unsafe.exception))
        self.assertNotIn("PATH_SENTINEL", str(raised_unsafe.exception))

    def test_runtime_preflight_failed_exec_reports_fixed_statement_without_raw_stderr(self):
        code = launcher.runtime_preflight_code()
        expected_line = next(index for index, row in enumerate(code.splitlines(), 1)
                             if row == 'assert p["schema"] == "fwrouter-acceptance-profile/v2"')
        stderr = (f'Traceback (most recent call last):\n  File "<string>", line {expected_line}, in <module>\n'
                  'AssertionError: TOKEN_SENTINEL /secret/path\n').encode("utf-8")
        completed = SimpleNamespace(returncode=1, stdout=b"", stderr=stderr)
        with mock.patch.object(launcher.subprocess, "run", return_value=completed):
            with self.assertRaises(launcher.NotRun) as raised:
                launcher._docker_exec_small(
                    ["/usr/bin/docker", "exec", "<owned-id>"], cwd=Path("/tmp"), env={},
                    stage="container_runtime_profile_preflight", role="router")
        message = str(raised.exception)
        self.assertIn('"exception_type":"AssertionError"', message)
        self.assertIn('"statement":"assert p[\\"schema\\"] == \\"fwrouter-acceptance-profile/v2\\""', message)
        self.assertIn(f'"line":{expected_line}', message)
        self.assertNotIn("TOKEN_SENTINEL", message)
        self.assertNotIn("/secret/path", message)

        crafted_stderr = (f'Traceback (most recent call last):\n  File "<string>", line {expected_line}, in <module>\n'
                          'TOKEN_Error: TOKEN_SENTINEL\n').encode("utf-8")
        crafted = SimpleNamespace(returncode=1, stdout=b"", stderr=crafted_stderr)
        with mock.patch.object(launcher.subprocess, "run", return_value=crafted):
            with self.assertRaises(launcher.NotRun) as raised_crafted:
                launcher._docker_exec_small(
                    ["/usr/bin/docker", "exec", "<owned-id>"], cwd=Path("/tmp"), env={},
                    stage="container_runtime_profile_preflight", role="router")
        self.assertIn('"exception_type":"Other"', str(raised_crafted.exception))
        self.assertNotIn("TOKEN_Error", str(raised_crafted.exception))
        self.assertNotIn("TOKEN_SENTINEL", str(raised_crafted.exception))

        oversized = SimpleNamespace(returncode=1, stdout=b"", stderr=b"x" * (16 * 1024 + 1))
        with mock.patch.object(launcher.subprocess, "run", return_value=oversized):
            with self.assertRaises(launcher.NotRun) as raised_oversized:
                launcher._docker_exec_small(
                    ["/usr/bin/docker", "exec", "<owned-id>"], cwd=Path("/tmp"), env={},
                    stage="container_runtime_profile_preflight", role="router")
        self.assertIn("diagnostic=unavailable", str(raised_oversized.exception))

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

    def test_packet_route_readback_normalizes_only_exact_numeric_ipv4_routes_and_keeps_bounded_evidence(self):
        interfaces = {"lan": "eth0", "wan": "eth1"}
        expected = [
            ("10.240.0.0/29", None, "eth0"),
            ("198.18.240.0/29", None, "eth1"),
            ("203.0.113.53/32", "198.18.240.2", "eth1"),
        ]
        actual = [
            {"dst": "10.240.0.0/29", "dev": "eth0"},
            {"dst": "198.18.240.0/29", "dev": "eth1"},
            {"dst": "203.0.113.53", "gateway": "198.18.240.2", "dev": "eth1"},
        ]
        observed, evidence = launcher.validate_packet_route_inventory(
            actual, role="router", expected=expected, interfaces=interfaces)
        route_key = lambda row: (row[0], row[1] or "", row[2])
        self.assertEqual(sorted(expected, key=route_key), sorted(observed, key=route_key))
        vip_row = next(row for row in evidence["routes"] if row["destination"] == "203.0.113.53/32")
        self.assertEqual("203.0.113.53", vip_row["destination_token"])
        self.assertEqual("eth1", vip_row["dev"])
        self.assertFalse(evidence["routes_truncated"])
        explicit_host, _ = launcher.validate_packet_route_inventory(
            [*actual[:2], dict(actual[2], dst="203.0.113.53/32")],
            role="router", expected=expected, interfaces=interfaces)
        self.assertEqual(sorted(expected, key=route_key), sorted(explicit_host, key=route_key))

        invalid_cases = {
            "wrong_interface": [dict(actual[0]), dict(actual[1]), dict(actual[2], dev="eth0")],
            "wrong_gateway": [dict(actual[0]), dict(actual[1]), dict(actual[2], gateway="198.18.240.6")],
            "extra_default": [*actual, {"gateway": "10.240.0.6", "dev": "eth0"}],
            "foreign_route": [*actual, {"dst": "192.0.2.0/24", "dev": "eth0"}],
            "duplicate": [*actual, dict(actual[2])],
            "missing_dev": [*actual[:2], {"dst": "203.0.113.53", "gateway": "198.18.240.2"}],
            "bad_type": [*actual[:2], {"dst": "203.0.113.53", "gateway": "198.18.240.2",
                                        "dev": "eth1", "type": "blackhole"}],
            "noncanonical_network": [*actual[:2], {"dst": "203.0.113.53/31", "gateway": "198.18.240.2",
                                                    "dev": "eth1"}],
            "invalid_token": [*actual[:2], {"dst": "secret=DO_NOT_PUBLISH", "gateway": "198.18.240.2",
                                            "dev": "eth1"}],
        }
        for name, rows in invalid_cases.items():
            with self.subTest(name=name), self.assertRaises(launcher.NotRun) as raised:
                launcher.validate_packet_route_inventory(rows, role="router", expected=expected,
                                                         interfaces=interfaces)
            message = str(raised.exception)
            self.assertIn("observed=", message)
            self.assertNotIn("DO_NOT_PUBLISH", message)
            self.assertNotIn("secret=", message)

    def test_packet_compose_normalized_null_entrypoint_is_allowed_but_override_is_not(self):
        role_keys = {
            "application": {"build", "cap_add", "cap_drop", "command", "cpus", "environment", "image", "init",
                            "labels", "mem_limit", "memswap_limit", "networks", "pids_limit", "read_only",
                            "restart", "security_opt", "sysctls", "tmpfs", "user", "volumes"},
            "lanclient": {"cap_add", "cap_drop", "command", "cpus", "environment", "image", "init", "labels",
                          "mem_limit", "memswap_limit", "networks", "pids_limit", "read_only", "restart",
                          "security_opt", "sysctls", "tmpfs", "user", "volumes"},
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

    def test_packet_application_memory_cap_is_2560m_and_rejects_unbounded_or_swap(self):
        import yaml

        compose_source = yaml.safe_load((Path(__file__).parents[1] / "acceptance" / "compose.yaml")
                                        .read_text(encoding="utf-8"))
        application_source = compose_source["services"]["application"]
        self.assertEqual(2560 * 1024**2, launcher.APPLICATION_MEMORY_LIMIT_BYTES)
        self.assertEqual("2560m", application_source["mem_limit"])
        self.assertEqual(application_source["mem_limit"], application_source["memswap_limit"])
        run_id = "packet-memory-policy-run"
        base_image_ref = "ghcr.io/coxf0rd/fwrouter-acceptance-dependencies@sha256:" + "a" * 64
        profile_paths = {}
        with tempfile.TemporaryDirectory(prefix="fwrouter-packet-memory-policy-") as temp:
            root = Path(temp)
            for role in ("router", "client", "endpoint"):
                profile_paths[role] = root / f"{role}.json"
                profile_paths[role].write_text("{}")
            labels = {launcher.OWNER_LABEL: launcher.OWNER_VALUE, launcher.RUN_LABEL: run_id}
            services = {
                "application": {
                    "build": {"dockerfile": launcher.DEPENDENCY_APPLICATION_DOCKERFILE,
                              "args": {"BASE_IMAGE": base_image_ref}},
                    "cap_add": ["NET_ADMIN", "NET_RAW"], "cap_drop": ["ALL"],
                    "command": ["-c", "import signal; signal.pause()"], "cpus": 2.0,
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
                    "image": f"fwrouter-acceptance:{run_id}", "init": True, "labels": labels,
                    "mem_limit": "2560m", "memswap_limit": "2560m", "networks": {
                        "lan": {"ipv4_address": "10.240.0.1"},
                        "wan": {"ipv4_address": "198.18.240.1"},
                    }, "pids_limit": 256, "read_only": True, "restart": "no",
                    "security_opt": ["no-new-privileges:true"],
                    "sysctls": {"net.ipv4.ip_forward": "1", "net.ipv4.ip_unprivileged_port_start": "53"},
                    "tmpfs": ["/tmp:rw,noexec,nosuid,nodev,size=512m,mode=1777"], "user": "0:0",
                    "volumes": [{"source": str(profile_paths["router"]),
                                 "target": "/run/fwrouter-acceptance/profile.json", "read_only": True}],
                },
                "lanclient": {
                    "cap_add": ["NET_ADMIN"], "cap_drop": ["ALL"],
                    "command": ["-c", "import signal; signal.pause()"], "cpus": 1.0,
                    "environment": {"FWROUTER_ACCEPTANCE_PROFILE": "/run/fwrouter-acceptance/profile.json",
                                    "FWROUTER_XRAY_BINARY": "/opt/fwrouter-test/bin/xray",
                                    "PYTHONDONTWRITEBYTECODE": "1", "HOME": "/tmp"},
                    "image": f"fwrouter-acceptance:{run_id}", "init": True, "labels": labels,
                    "mem_limit": "256m", "memswap_limit": "256m", "networks": {
                        "lan": {"ipv4_address": "10.240.0.2"}}, "pids_limit": 64, "read_only": True,
                    "restart": "no", "security_opt": ["no-new-privileges:true"],
                    "tmpfs": ["/tmp:rw,noexec,nosuid,nodev,size=32m,mode=1777"], "user": "0:0",
                    "volumes": [{"source": str(profile_paths["client"]),
                                 "target": "/run/fwrouter-acceptance/profile.json", "read_only": True}],
                },
                "endpoint": {
                    "cap_add": ["NET_ADMIN"], "cap_drop": ["ALL"],
                    "command": ["-c", "import signal; signal.pause()"], "cpus": 1.0,
                    "environment": {"FWROUTER_ACCEPTANCE_PROFILE": "/run/fwrouter-acceptance/profile.json",
                                    "FWROUTER_XRAY_BINARY": "/opt/fwrouter-test/bin/xray",
                                    "PYTHONDONTWRITEBYTECODE": "1", "HOME": "/tmp"},
                    "image": f"fwrouter-acceptance:{run_id}", "init": True, "labels": labels,
                    "mem_limit": "384m", "memswap_limit": "384m", "networks": {
                        "wan": {"ipv4_address": "198.18.240.2"}}, "pids_limit": 96, "read_only": True,
                    "restart": "no", "security_opt": ["no-new-privileges:true"],
                    "sysctls": {"net.ipv4.ip_unprivileged_port_start": "53"},
                    "tmpfs": ["/tmp:rw,noexec,nosuid,nodev,size=64m,mode=1777"], "user": "0:0",
                    "volumes": [{"source": str(profile_paths["endpoint"]),
                                 "target": "/run/fwrouter-acceptance/profile.json", "read_only": True}],
                },
            }
            networks = {name: {
                "internal": True, "enable_ipv6": False,
                "ipam": {"config": [{"subnet": spec["subnet"], "gateway": spec["gateway"]}]},
                "labels": labels,
            } for name, spec in launcher.PACKET_NETWORKS.items()}
            config = {"services": services, "networks": networks}
            launcher.validate_packet_compose_config(
                config, run_id=run_id, profile_paths=profile_paths, base_image_ref=base_image_ref,
            )
            self.assertEqual(application_source["mem_limit"], services["application"]["mem_limit"])
            self.assertEqual(application_source["memswap_limit"], services["application"]["memswap_limit"])
            for memory, swap in (("2561m", "2561m"), ("0", "0"), ("2560m", "2561m"), ("2560m", "")):
                invalid = json.loads(json.dumps(config))
                invalid["services"]["application"]["mem_limit"] = memory
                invalid["services"]["application"]["memswap_limit"] = swap
                with self.subTest(memory=memory, swap=swap), self.assertRaises(launcher.NotRun):
                    launcher.validate_packet_compose_config(
                        invalid, run_id=run_id, profile_paths=profile_paths, base_image_ref=base_image_ref,
                    )
            missing_limit = json.loads(json.dumps(config))
            missing_limit["services"]["application"].pop("mem_limit")
            with self.assertRaises(launcher.NotRun):
                launcher.validate_packet_compose_config(
                    missing_limit, run_id=run_id, profile_paths=profile_paths, base_image_ref=base_image_ref,
                )
            missing_swap = json.loads(json.dumps(config))
            missing_swap["services"]["application"].pop("memswap_limit")
            with self.assertRaises(launcher.NotRun):
                launcher.validate_packet_compose_config(
                    missing_swap, run_id=run_id, profile_paths=profile_paths, base_image_ref=base_image_ref,
                )

            runtime = {
                "Config": {
                    "Labels": {**labels, "com.docker.compose.project": "packet-project"},
                    "User": "0:0", "Entrypoint": ["python"],
                    "Cmd": ["-c", "import signal; signal.pause()"],
                    "Env": ["PATH=/usr/local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                            "LANG=C.UTF-8", "PYTHONPATH=/workspace/backend",
                            "FWROUTER_ENVIRONMENT=test", "FWROUTER_STATE_DIR=/tmp/fwrouter-acceptance-state",
                            "FWROUTER_STARTUP_TASKS_ENABLED=0", "FWROUTER_ACCEPTANCE_PROFILE=/run/fwrouter-acceptance/profile.json",
                            "FWROUTER_XRAY_BINARY=/opt/fwrouter-test/bin/xray",
                            "FWROUTER_MIHOMO_BINARY=/opt/fwrouter-test/bin/mihomo",
                            "FWROUTER_BROWSER_EXECUTABLE=/opt/fwrouter-test/chromium/chrome-linux64/chrome",
                            "FWROUTER_CHROMIUM_BINARY=/opt/fwrouter-test/chromium/chrome-linux64/chrome",
                            "FWROUTER_APPLICATION_ACCEPTANCE_ROOT=/tmp/fwrouter-application-acceptance",
                            "FWROUTER_ACCEPTANCE_RECEIPT_PATH=/tmp/fwrouter-receipts/application-acceptance.json",
                            "PLAYWRIGHT_BROWSERS_PATH=/opt/fwrouter-test/playwright-browsers",
                            "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1", "PYTHONDONTWRITEBYTECODE=1", "HOME=/tmp/fwrouter-home"],
                },
                "HostConfig": {
                    "ReadonlyRootfs": True, "Privileged": False, "CapDrop": ["ALL"],
                    "CapAdd": ["NET_ADMIN", "NET_RAW"], "Devices": [], "PortBindings": {},
                    "PidMode": "", "IpcMode": "private", "SecurityOpt": ["no-new-privileges:true"],
                    "NetworkMode": "packet-project_lan", "Sysctls": services["application"]["sysctls"],
                    "Memory": 2560 * 1024**2, "MemorySwap": 2560 * 1024**2,
                    "NanoCpus": 2_000_000_000, "PidsLimit": 256,
                    "Tmpfs": {"/tmp": "rw,noexec,nosuid,nodev,size=512m,mode=1777"},
                },
                "State": {"Status": "created"}, "Image": "sha256:" + "b" * 64,
                "Mounts": [{"Type": "bind", "Destination": "/run/fwrouter-acceptance/profile.json",
                            "Source": str(profile_paths["router"]), "RW": False}],
                "NetworkSettings": {"Networks": {
                    "packet-project_lan": {"IPAddress": "10.240.0.1"},
                    "packet-project_wan": {"IPAddress": "198.18.240.1"},
                }},
            }
            self.assertEqual(int(application_source["mem_limit"][:-1]) * 1024**2,
                             runtime["HostConfig"]["Memory"])
            self.assertEqual(runtime["HostConfig"]["Memory"], runtime["HostConfig"]["MemorySwap"])
            launcher.validate_packet_container_inspect(
                runtime, project="packet-project", run_id=run_id, image_id=runtime["Image"],
                role="router", profile_path=profile_paths["router"], inherited_base_env={},
            )
            for memory, swap in ((2560 * 1024**2 + 1, 2560 * 1024**2 + 1),
                                 (0, 0), (2560 * 1024**2, 2561 * 1024**2)):
                invalid_runtime = json.loads(json.dumps(runtime))
                invalid_runtime["HostConfig"]["Memory"] = memory
                invalid_runtime["HostConfig"]["MemorySwap"] = swap
                with self.subTest(runtime_memory=memory, runtime_swap=swap), self.assertRaises(launcher.NotRun):
                    launcher.validate_packet_container_inspect(
                        invalid_runtime, project="packet-project", run_id=run_id,
                        image_id=runtime["Image"], role="router", profile_path=profile_paths["router"],
                        inherited_base_env={},
                    )
            missing_limit = json.loads(json.dumps(runtime))
            missing_limit["HostConfig"].pop("Memory")
            with self.assertRaises(launcher.NotRun):
                launcher.validate_packet_container_inspect(
                    missing_limit, project="packet-project", run_id=run_id,
                    image_id=runtime["Image"], role="router", profile_path=profile_paths["router"],
                    inherited_base_env={},
                )
            missing_swap = json.loads(json.dumps(runtime))
            missing_swap["HostConfig"].pop("MemorySwap")
            with self.assertRaises(launcher.NotRun):
                launcher.validate_packet_container_inspect(
                    missing_swap, project="packet-project", run_id=run_id,
                    image_id=runtime["Image"], role="router", profile_path=profile_paths["router"],
                    inherited_base_env={},
                )

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
            launcher.validate_packet_compose_config(
                config, run_id=run_id, profile_paths=profile_paths,
                base_image_ref="python:3.11-bookworm@sha256:" + "a" * 64)
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
                allowed_destinations={("10.240.0.2", "203.0.113.53", 6): {9080}},
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
                allowed_destinations={("10.240.0.2", "203.0.113.53", 17): {9081}},
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
                allowed_destinations={("10.240.0.2", "203.0.113.53", 6): {9080}},
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
        self.assertEqual(sum('output ip daddr 198.18.240.2 tcp dport 5302 accept comment "fwrouter packet guard selector vless allow v1"' in rule
                             for rule in rules), 1)
        self.assertFalse(any("tcp dport 5302" in rule and "198.18.240.2" not in rule for rule in rules))
        self.assertTrue(any("udp dport { 5203, 5205 } accept" in rule for rule in rules))
        self.assertTrue(any("udp dport { 9081, 5353 } accept" in rule and "forward" in rule for rule in rules))
        exact_rules = (
            'output ip daddr 10.250.240.2 tcp dport 9080 counter name router_protected_tcp_output accept',
            'output ip daddr 10.250.240.2 udp dport 9081 counter name router_protected_udp_output accept',
            'forward iifname "eth0" oifname "eth1" ip saddr 10.240.0.2 ip daddr 10.250.240.2 tcp dport 9080 counter name router_protected_tcp_forward accept',
            'forward iifname "eth0" oifname "eth1" ip saddr 10.240.0.2 ip daddr 10.250.240.2 udp dport 9081 counter name router_protected_udp_forward accept',
        )
        for expected in exact_rules:
            self.assertTrue(any(expected in rule for rule in rules), expected)
        dns_rules = (
            'output ip daddr 1.1.1.1 tcp dport 53 counter name router_dns_tcp_output accept',
            'output ip daddr 1.1.1.1 udp dport 53 counter name router_dns_udp_output accept',
            'input iifname "eth0" ip saddr 10.240.0.2 ip daddr 10.240.0.1 tcp dport 53 counter name router_dns_tcp_input accept',
            'input iifname "eth0" ip saddr 10.240.0.2 ip daddr 10.240.0.1 udp dport 53 counter name router_dns_udp_input accept',
            'input iifname "eth0" ip saddr 10.240.0.2 ip daddr 198.51.100.77 udp dport 9081 accept',
            'output ip daddr 198.51.100.77 tcp dport 9080 counter name router_dns_vpn_direct_output accept',
            'output ip daddr 198.51.100.77 udp dport 9081 counter name router_dns_vpn_direct_output accept',
            'forward iifname "eth0" oifname "eth1" ip saddr 10.240.0.2 ip daddr 198.51.100.77 tcp dport 9080 counter name router_dns_vpn_direct_forward accept',
            'forward iifname "eth0" oifname "eth1" ip saddr 10.240.0.2 ip daddr 198.51.100.77 udp dport 9081 counter name router_dns_vpn_direct_forward accept',
        )
        for expected in dns_rules:
            self.assertTrue(any(expected in rule for rule in rules), expected)
        self.assertFalse(any("(9081, 5353)" in rule or "(5202, 5204)" in rule for rule in rules))

    def test_kernel_acceptance_worker_path_exposes_admin_tools_only_in_kernel_profiles(self):
        conftest = (LAUNCHER_PATH.parents[1] / "application_acceptance/conftest.py").read_text(encoding="utf-8")
        self.assertIn('profile.get("profile") in {"hosted-kernel-dataplane", "hosted-kernel-packet"}', conftest)
        self.assertIn('"/opt/fwrouter-test/bin:/usr/sbin:/sbin:/usr/bin:/bin" if kernel_profile', conftest)
        self.assertIn('else "/opt/fwrouter-test/bin:/usr/bin:/bin"', conftest)

    def test_owned_worker_rewrites_only_copied_logical_health_check_groups(self):
        original = [
            {"name": "fallback-group", "type": "fallback", "url": "https://www.gstatic.com/generate_204",
             "proxies": ["member-a"]},
            {"name": "url-test-group", "type": "url-test", "url": "https://www.gstatic.com/generate_204",
             "proxies": ["member-b"]},
            {"name": "selector", "type": "select", "proxies": ["member-a", "member-b"]},
        ]
        probe_url = "http://127.0.0.1:52044/generate_204"
        worker_tree = ast.parse(WORKER_PATH.read_text(encoding="utf-8"), filename=str(WORKER_PATH))
        helper_node = next(
            node for node in worker_tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "_owned_logical_health_check_groups"
        )
        helper_namespace = {"copy": copy, "urlsplit": urlsplit, "Any": Any}
        exec(compile(ast.Module(body=[helper_node], type_ignores=[]), str(WORKER_PATH), "exec"), helper_namespace)
        owned_groups = helper_namespace["_owned_logical_health_check_groups"]

        observed = owned_groups(original, probe_url)
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
                owned_groups(original, unsafe)
        packet_profile = {"profile": "hosted-kernel-packet"}
        packet_url = "http://203.0.113.53:9080/generate_204"
        packet_groups = owned_groups(
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
                owned_groups(original, unsafe, profile=packet_profile)
        with self.assertRaises(RuntimeError):
            owned_groups(original, packet_url)
        dataplane_profile = {
            "profile": "hosted-kernel-dataplane",
            "mihomo_handoff": {"address": "172.18.0.1", "subnet": "172.18.0.0/24",
                               "gateway": "172.18.0.254"},
        }
        internal_url = "http://172.18.0.1:49152/generate_204"
        internal_groups = owned_groups(
            original, internal_url, profile=dataplane_profile, expected_probe_port=49152)
        self.assertEqual(internal_url, internal_groups[0]["url"])
        for unsafe in (
            "http://172.18.0.2:49152/generate_204",
            "http://172.18.0.1:9080/generate_204",
            "http://172.18.0.1:49152/other",
            "http://172.18.0.1:49152/generate_204?target=127.0.0.1",
        ):
            with self.subTest(dataplane_probe_url=unsafe), self.assertRaises(RuntimeError):
                owned_groups(
                    original, unsafe, profile=dataplane_profile, expected_probe_port=49152)
        with self.assertRaises(RuntimeError):
            owned_groups(
                original, internal_url, profile=dataplane_profile, expected_probe_port=49153)
        normal_groups = owned_groups(original, probe_url)
        self.assertEqual(probe_url, normal_groups[0]["url"])
        source = WORKER_PATH.read_text(encoding="utf-8")
        worker_tree = ast.parse(source, filename=str(WORKER_PATH))
        bind_function = next(
            node for node in worker_tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "_bind_acceptance_mihomo"
        )
        wrapper_node = next(
            node for node in bind_function.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "generated_logical_profile_group_on_owned_bridge"
        )
        def generated_factory(runtime_name, members, runtime_policy="fallback"):
            return {
                "name": runtime_name,
                "type": runtime_policy,
                "proxies": [member["name"] for member in members],
                "url": "https://www.gstatic.com/generate_204",
                "interval": 300,
            }

        factory_namespace = {
            "original_generated_logical_profile_group": generated_factory,
            "_owned_logical_health_check_groups": owned_groups,
            "owned_probe_url": internal_url,
            "acceptance_profile": dataplane_profile,
            "owned_probe_port": 49152,
            "Any": Any,
        }
        exec(compile(ast.Module(body=[wrapper_node], type_ignores=[]), str(WORKER_PATH), "exec"), factory_namespace)
        wrapped_factory = factory_namespace["generated_logical_profile_group_on_owned_bridge"]
        active_group = wrapped_factory("active-profile", [{"name": "active-member"}])
        inactive_group = wrapped_factory("inactive-profile", [{"name": "inactive-member"}])
        self.assertEqual(internal_url, active_group["url"])
        self.assertEqual(internal_url, inactive_group["url"])
        self.assertEqual(["active-member"], active_group["proxies"])
        self.assertEqual(["inactive-member"], inactive_group["proxies"])
        with self.assertRaises(RuntimeError):
            owned_groups([generated_factory("wrong-endpoint", [{"name": "member"}])],
                         "http://172.18.0.2:49152/generate_204",
                         profile=dataplane_profile, expected_probe_port=49152)

        service_path = WORKER_PATH.parents[2] / "backend/fwrouter_api/services/mihomo_config_proxies.py"
        service_tree = ast.parse(service_path.read_text(encoding="utf-8"), filename=str(service_path))
        for function_name in ("_logical_profile_groups", "_known_inactive_logical_profile_groups"):
            function_node = next(
                node for node in service_tree.body
                if isinstance(node, ast.FunctionDef) and node.name == function_name
            )
            factory_calls = [
                node for node in ast.walk(function_node)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "_generated_logical_profile_group"
            ]
            self.assertTrue(factory_calls, function_name)
        self.assertIn(
            "mihomo_config_proxies._generated_logical_profile_group = generated_logical_profile_group_on_owned_bridge",
            source,
        )
        self.assertNotIn(
            "mihomo_config_proxies._logical_profile_groups = logical_profile_groups_on_owned_bridge",
            source,
        )

    def test_owned_core_candidate_rpc_is_profile_gated_and_delegates_to_core_selector(self):
        from application_acceptance import worker

        calls = []

        def fake_selector(**kwargs):
            calls.append(kwargs)
            return {
                "ok": True, "selected_server_id": "provider-1", "active_after": "provider-1",
                "selection_revision": 17, "applied": True, "selection_outcome": "applied",
                "provider_operation": {"ok": True, "runtime_verified": True,
                                        "actual_member_id": "902", "secret": "SECRET_SENTINEL"},
                "secret": "SECRET_SENTINEL",
            }

        valid_profile = {"profile": "hosted-kernel-dataplane"}
        invalid_requests = (
            {"environment": "production", "acceptance_root": "/tmp/fwrouter-application-acceptance",
             "profile": valid_profile, "payload": {"candidate_server_id": "provider-1"}},
            {"environment": "test", "acceptance_root": "/wrong-root",
             "profile": valid_profile, "payload": {"candidate_server_id": "provider-1"}},
            {"environment": "test", "acceptance_root": "/tmp/fwrouter-application-acceptance",
             "profile": {"profile": "hosted-kernel-packet"},
             "payload": {"candidate_server_id": "provider-1"}},
            {"environment": "test", "acceptance_root": "/tmp/fwrouter-application-acceptance",
             "profile": valid_profile, "payload": {"candidate_server_id": "provider-1", "apply": False}},
        )
        for request in invalid_requests:
            result = worker._select_owned_core_candidate(
                request["payload"], environment=request["environment"],
                acceptance_root=request["acceptance_root"], profile=request["profile"],
                selector=fake_selector)
            self.assertFalse(result["ok"])
            self.assertEqual("ACCEPTANCE_CORE_SELECTOR_REQUEST_INVALID", result["error_code"])
        invalid_candidate = worker._select_owned_core_candidate(
            {"candidate_server_id": "bad candidate"}, environment="test",
            acceptance_root="/tmp/fwrouter-application-acceptance", profile=valid_profile,
            selector=fake_selector,
        )
        self.assertEqual("ACCEPTANCE_CORE_SELECTOR_CANDIDATE_INVALID", invalid_candidate["error_code"])
        self.assertEqual([], calls)

        result = worker._select_owned_core_candidate(
            {"candidate_server_id": "provider-1"}, environment="test",
            acceptance_root="/tmp/fwrouter-application-acceptance", profile=valid_profile,
            selector=fake_selector,
        )
        self.assertEqual({
            "apply": True, "reason": "acceptance_provider_baseline", "requested_by": "api",
            "check_on_demand": True, "update_ping_state": False, "on_demand_limit": 20,
            "timeout_ms": 1000, "candidate_server_id": "provider-1", "origin": "api",
        }, calls[0])
        self.assertEqual("provider-1", result["selected_server_id"])
        self.assertEqual({"ok": True, "runtime_verified": True, "actual_member_id": "902"},
                         result["provider_operation"])
        self.assertNotIn("SECRET_SENTINEL", json.dumps(result))
        helper = next(node for node in ast.parse(WORKER_PATH.read_text(encoding="utf-8")).body
                      if isinstance(node, ast.FunctionDef)
                      and node.name == "_select_owned_core_candidate")
        helper_names = {node.id for node in ast.walk(helper) if isinstance(node, ast.Name)}
        self.assertFalse(helper_names & {"db_session", "mihomo_adapter", "write_selection_fence"})

    def test_browser_api_response_summary_bounds_and_redacts_error_details(self):
        from application_acceptance.test_browser_locale import (
            _bounded_browser_api_response, _matches_preferences_patch,
        )

        class FakeResponse:
            def __init__(self, status, headers, body):
                self.status = status
                self.status_text = "Failure " + ("X" * 200)
                self.headers = headers
                self._body = body

            def body(self):
                return self._body

        secret_message = "provider detail SECRET_SENTINEL"
        body = json.dumps({"ok": False, "error": {"code": "PROVIDER_FAILED", "message": secret_message}}).encode()
        summary = _bounded_browser_api_response(FakeResponse(
            409, {"content-length": str(len(body))}, body))
        self.assertEqual(409, summary["status"])
        self.assertEqual("PROVIDER_FAILED", summary["error_code"])
        self.assertTrue(summary["error_message_present"])
        self.assertLessEqual(len(summary["status_text"]), 160)
        self.assertNotIn(secret_message, json.dumps(summary))
        self.assertNotIn("SECRET_SENTINEL", json.dumps(summary))

        oversized = _bounded_browser_api_response(FakeResponse(
            502, {"content-length": "8193"}, b"BODY_SENTINEL"))
        self.assertEqual("omitted_unbounded_length", oversized["body_observation"])
        self.assertNotIn("BODY_SENTINEL", json.dumps(oversized))
        malformed_length = _bounded_browser_api_response(FakeResponse(
            500, {"content-length": "unknown"}, b"BODY_SENTINEL"))
        self.assertEqual("omitted_unbounded_length", malformed_length["body_observation"])
        self.assertNotIn("BODY_SENTINEL", json.dumps(malformed_length))

        class FakeRequest:
            def __init__(self, method):
                self.method = method

        class FakeBrowserResponse:
            def __init__(self, url, method="PATCH"):
                self.url = url
                self.request = FakeRequest(method)

        browser_api_base = "http://127.0.0.1:41000/api/v2"
        encoded_id = "node:west/one?two"
        encoded_url = (
            "http://127.0.0.1:41000/api/v2/servers/node%3Awest%2Fone%3Ftwo/preferences?source=browser"
        )
        self.assertTrue(_matches_preferences_patch(
            FakeBrowserResponse(encoded_url), browser_api_base, encoded_id))
        self.assertFalse(_matches_preferences_patch(
            FakeBrowserResponse(encoded_url, method="GET"), browser_api_base, encoded_id))
        self.assertFalse(_matches_preferences_patch(
            FakeBrowserResponse(encoded_url.replace("127.0.0.1:41000", "127.0.0.1:41001")),
            browser_api_base, encoded_id))
        self.assertFalse(_matches_preferences_patch(
            FakeBrowserResponse(encoded_url), browser_api_base, "node:west/other"))
        self.assertFalse(_matches_preferences_patch(
            FakeBrowserResponse(encoded_url.replace("/preferences?", "/preferences-extra?")),
            browser_api_base, encoded_id))

    def test_provider_probe_junit_evidence_keeps_exact_bounded_statuses_without_urls(self):
        from application_acceptance.test_core_provider_mihomo import _record_owned_probe_boundary

        recorded = {}
        summary = {
            "request_count": 9, "response_count": 6, "event_count": 8,
            "transport_closed_count": 2, "response_status_counts": {"204": 4, "504": 2},
            "header_write_completion_count": 3,
            "header_write_error_counts": {"broken_pipe": 1, "secret_error": 99},
            "recent_events": [
                {"kind": "http_response", "status": 204, "url": "http://secret.invalid/path"},
                {"kind": "transport_closed", "status": None, "token": "SECRET_SENTINEL"},
                {"kind": "untrusted-kind-with-sensitive-text", "status": 500},
            ],
            "header_write_recent_events": [
                {"kind": "header_write_complete", "method": "HEAD", "status": 204,
                 "monotonic_ms": 123456},
                {"kind": "header_write_error", "method": "GET", "status": 204,
                 "error_code": "broken_pipe", "monotonic_ms": 123457, "message": "SECRET_SENTINEL"},
                {"kind": "header_write_error", "method": "POST", "status": 204,
                 "error_code": "os_error", "monotonic_ms": "SECRET_SENTINEL"},
            ],
        }
        _record_owned_probe_boundary(lambda key, value: recorded.setdefault(key, value), "failure", summary)
        property_name, encoded = next(iter(recorded.items()))
        self.assertEqual("owned_probe_failure", property_name)
        self.assertLessEqual(len(encoded.encode("utf-8")), 2048)
        result = json.loads(encoded)
        self.assertEqual(9, result["request_count"])
        self.assertEqual({"204": 4, "504": 2}, result["response_status_counts"])
        self.assertEqual([{"kind": "http_response", "status": 204},
                          {"kind": "transport_closed", "status": None}], result["recent_events"])
        self.assertEqual(3, result["header_write_completion_count"])
        self.assertEqual({"broken_pipe": 1}, result["header_write_error_counts"])
        self.assertEqual([
            {"kind": "header_write_complete", "method": "HEAD", "status": 204,
             "monotonic_ms": 123456},
            {"kind": "header_write_error", "method": "GET", "status": 204,
             "error_code": "broken_pipe", "monotonic_ms": 123457},
        ], result["header_write_recent_events"])
        self.assertNotIn("secret.invalid", encoded)
        self.assertNotIn("SECRET_SENTINEL", encoded)

        worst_case_summary = {
            "request_count": 1_000_000, "response_count": 1_000_000,
            "event_count": 1_000_000, "transport_closed_count": 1_000_000,
            "response_status_counts": {"204": 1_000_000, "504": 1_000_000, "secret": 1},
            "header_write_completion_count": 1_000_000,
            "header_write_error_counts": {name: 1_000_000 for name in
                                           ("broken_pipe", "connection_reset", "timeout", "os_error")},
            "recent_events": [{"kind": "http_response", "status": 204, "url": "SECRET_SENTINEL"} for _ in range(8)],
            "header_write_recent_events": ([
                {"kind": "header_write_complete", "method": method, "status": 204,
                 "monotonic_ms": 9_007_199_254_740_991}
                for method in ("GET", "HEAD") for _ in range(4)
            ] + [{"kind": "header_write_error", "method": "GET", "status": 204,
                  "error_code": "os_error", "monotonic_ms": -1} for _ in range(8)]),
        }
        worst_case_recorded = {}
        _record_owned_probe_boundary(
            lambda key, value: worst_case_recorded.setdefault(key, value), "worst_case", worst_case_summary)
        self.assertLessEqual(len(next(iter(worst_case_recorded.values())).encode("utf-8")), 2048)

    def test_native_delay_request_junit_evidence_is_bounded_and_redacted(self):
        from application_acceptance.test_core_provider_mihomo import _record_native_delay_request

        self.assertTrue(native_runner._valid_mihomo_probe_timing({
            "request_started_monotonic_ms": 9_000,
            "request_finished_monotonic_ms": 9_050,
            "request_elapsed_ms": 50,
        }))
        self.assertTrue(native_runner._valid_mihomo_probe_timing({
            "request_started_monotonic_ms": None,
            "request_finished_monotonic_ms": None,
            "request_elapsed_ms": None,
        }))
        self.assertFalse(native_runner._valid_mihomo_probe_timing({
            "request_started_monotonic_ms": 9_000,
            "request_finished_monotonic_ms": 9_050,
            "request_elapsed_ms": 49,
        }))
        self.assertFalse(native_runner._valid_mihomo_probe_timing({
            "request_started_monotonic_ms": 9_000,
            "request_finished_monotonic_ms": 100_000,
            "request_elapsed_ms": 91_000,
        }))

        recorded = {}
        evidence = {
            "probe_ok": False,
            "delay_request_evidence": {
                "request_observed": True, "request_kind": "member", "response_received": True,
                "status": 503, "url_matches_known_owned": True, "payload_map_empty": False,
                "integer_delay_result_found": False, "integer_delay_zero": None,
                "error_code": "mihomo_generic_delay_failure", "adapter_probe_ok": False,
                "request_started_monotonic_ms": 12_345_600,
                "request_finished_monotonic_ms": 12_345_650,
                "request_elapsed_ms": 50,
                "url": "http://secret.invalid/proxies/SECRET/delay", "body": "SECRET_SENTINEL",
            },
        }
        _record_native_delay_request(lambda key, value: recorded.setdefault(key, value), "case", evidence)
        property_name, encoded = next(iter(recorded.items()))
        self.assertEqual("native_delay_request_case", property_name)
        self.assertLessEqual(len(encoded.encode("utf-8")), 1024)
        result = json.loads(encoded)
        self.assertEqual("member", result["request_kind"])
        self.assertEqual(503, result["status"])
        self.assertEqual("mihomo_generic_delay_failure", result["error_code"])
        self.assertEqual(12_345_600, result["request_started_monotonic_ms"])
        self.assertEqual(12_345_650, result["request_finished_monotonic_ms"])
        self.assertEqual(50, result["request_elapsed_ms"])
        self.assertNotIn("secret.invalid", encoded)
        self.assertNotIn("SECRET", encoded)

        bounded = {}
        _record_native_delay_request(
            lambda key, value: bounded.setdefault(key, value), "empty",
            {"delay_request_evidence": {"request_observed": False, "request_kind": "group",
                                        "status": None, "error_code": "secret", "url": "SECRET"}},
        )
        self.assertEqual({
            "adapter_probe_ok": None,
            "integer_delay_result_found": None,
            "integer_delay_zero": None,
            "payload_map_empty": None,
            "request_kind": "group",
            "request_observed": False,
            "response_received": None,
            "status": None,
            "url_matches_known_owned": None,
            "request_started_monotonic_ms": None,
            "request_finished_monotonic_ms": None,
            "request_elapsed_ms": None,
        }, json.loads(next(iter(bounded.values()))))

        runner_tree = ast.parse(NATIVE_RUNNER_PATH.read_text(encoding="utf-8"))
        runner_class = next(node for node in runner_tree.body
                            if isinstance(node, ast.ClassDef) and node.name == "NativeXrayProcess")
        action = next(node for node in runner_class.body
                      if isinstance(node, ast.FunctionDef) and node.name == "_dispatch")
        self.assertIn("_valid_mihomo_probe_timing", ast.unparse(action))

    def test_native_probe_barrier_publishes_valid_current_snapshot_before_hold(self):
        native = native_runner.NativeXrayProcess(
            Path("/tmp/xray"), Path("/tmp/mihomo"), Path("/tmp/root"),
            Path("/tmp/config"), Path("/tmp/mihomo-config"), Path("/tmp/socket"),
        )

        def probe_payload(*, target: str, ok: bool, status: int, elapsed_ms: int) -> dict:
            return {
                "logical_runtime_target": target,
                "probe_ok": ok,
                "delay_request_evidence": {
                    "request_observed": True, "request_kind": "member", "response_received": True,
                    "status": status, "url_matches_known_owned": True, "payload_map_empty": False,
                    "integer_delay_result_found": ok, "integer_delay_zero": False if ok else None,
                    "error_code": None if ok else "mihomo_timeout", "adapter_probe_ok": ok,
                    "request_started_monotonic_ms": 20_000,
                    "request_finished_monotonic_ms": 20_000 + elapsed_ms,
                    "request_elapsed_ms": elapsed_ms,
                },
            }

        previous = probe_payload(target="previous-failed-1000ms", ok=False, status=504, elapsed_ms=1000)
        self.assertTrue(native._dispatch({"action": "mihomo_probe_result", "payload": previous})["ok"])
        self.assertEqual(previous, native.last_mihomo_probe_result)

        current = probe_payload(target="current-success-1500ms", ok=True, status=200, elapsed_ms=1500)
        native.hold_action("mihomo_probe_result")
        replies: list[dict] = []
        errors: list[BaseException] = []

        def dispatch_current() -> None:
            try:
                replies.append(native._dispatch({"action": "mihomo_probe_result", "payload": current}))
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=dispatch_current)
        thread.start()
        try:
            self.assertTrue(native.action_entered.wait(timeout=10))
            self.assertEqual(current, native.last_mihomo_probe_result)
            self.assertEqual(1500, native.last_mihomo_probe_result["delay_request_evidence"]["request_elapsed_ms"])
            current["delay_request_evidence"]["status"] = 599
            self.assertEqual(200, native.last_mihomo_probe_result["delay_request_evidence"]["status"])
            returned_copy = native.last_mihomo_probe_result
            returned_copy["delay_request_evidence"]["status"] = 598
            self.assertEqual(200, native.last_mihomo_probe_result["delay_request_evidence"]["status"])
            self.assertEqual([], replies, "the RPC reply must remain held until release")
        finally:
            native.release_action()
            thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        self.assertEqual([], errors)
        self.assertEqual(1, len(replies))
        self.assertTrue(replies[0]["ok"])

        native.hold_action("mihomo_probe_result")
        invalid = probe_payload(target="invalid-must-not-publish", ok=False, status=504, elapsed_ms=1000)
        invalid["delay_request_evidence"]["request_elapsed_ms"] = 999
        with self.assertRaises(ValueError):
            native._dispatch({"action": "mihomo_probe_result", "payload": invalid})
        self.assertFalse(native.action_entered.is_set())
        self.assertEqual("current-success-1500ms", native.last_mihomo_probe_result["logical_runtime_target"])

    def test_native_delay_observer_scopes_group_and_member_calls_without_changing_http_result(self):
        tree = ast.parse(WORKER_PATH.read_text(encoding="utf-8"))
        installer = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                         and node.name == "_install_acceptance_mihomo_delay_error_observer")
        observer = next(node for node in ast.walk(installer) if isinstance(node, ast.FunctionDef)
                        and node.name == "observed_group_client_get")
        calls = [node for node in ast.walk(observer) if isinstance(node, ast.Call)]
        original_get_calls = [node for node in calls if isinstance(node.func, ast.Name)
                              and node.func.id == "original_client_get"]
        self.assertEqual(2, len(original_get_calls), "one pass-through and one observed request path")
        self.assertTrue(any(isinstance(node, ast.Return) and isinstance(node.value, ast.Name)
                            and node.value.id == "response" for node in ast.walk(observer)))
        self.assertTrue(any(isinstance(node, ast.Raise) and node.exc is None
                            for node in ast.walk(observer)), "transport exceptions must be re-raised unchanged")
        observer_source = ast.unparse(observer)
        self.assertIn("time.monotonic_ns", observer_source)
        self.assertIn("request_started_monotonic_ms", observer_source)
        self.assertIn("request_finished_monotonic_ms", observer_source)
        self.assertEqual(observer_source.count("bounded_request_timing("), 2)
        timing_helper = next(node for node in ast.walk(installer) if isinstance(node, ast.FunctionDef)
                             and node.name == "bounded_request_timing")
        self.assertIn("request_elapsed_ms", ast.unparse(timing_helper))
        bridge_tree = ast.parse((Path(__file__).parents[1] / "application_acceptance" /
                                 "joined_support.py").read_text(encoding="utf-8"))
        bridge_class = next(node for node in bridge_tree.body
                            if isinstance(node, ast.ClassDef) and node.name == "ProviderHttpTestBridge")
        header_recorder = next(node for node in bridge_class.body
                               if isinstance(node, ast.FunctionDef)
                               and node.name == "record_probe_header_write")
        self.assertIn("time.monotonic_ns", ast.unparse(header_recorder))
        self.assertIn("monotonic_ms", ast.unparse(header_recorder))
        path_matcher = next(node for node in ast.walk(installer) if isinstance(node, ast.FunctionDef)
                            and node.name == "request_probe_path_matches")
        self.assertIn("raw_path", ast.unparse(path_matcher))
        self.assertIn("quote", ast.unparse(path_matcher))
        bound = next(node for node in ast.walk(installer) if isinstance(node, ast.FunctionDef)
                     and node.name == "finish_group_probe")
        self.assertIn("events[:-8]", ast.unparse(bound))
        adapter = ast.parse(WORKER_PATH.read_text(encoding="utf-8"))
        class_node = next(node for node in ast.walk(adapter) if isinstance(node, ast.ClassDef)
                          and node.name == "LoopbackProbeMihomoHttpAdapter")
        for method_name in ("probe_logical_group", "probe_logical_member"):
            method = next(node for node in class_node.body if isinstance(node, ast.FunctionDef)
                          and node.name == method_name)
            source = ast.unparse(method)
            self.assertIn('begin_probe', source)
            self.assertIn('finish_group_probe', source)
            self.assertIn('delay_request_evidence', ast.unparse(
                next(node for node in class_node.body if isinstance(node, ast.FunctionDef)
                     and node.name == "_probe_result_barrier")))

    def test_native_delay_observer_matches_encoded_group_and_member_paths_and_preserves_result(self):
        worker_path = WORKER_PATH
        tree = ast.parse(worker_path.read_text(encoding="utf-8"))
        installer = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                         and node.name == "_install_acceptance_mihomo_delay_error_observer")

        class FakeURL:
            def __init__(self, value):
                from urllib.parse import urlsplit
                parsed = urlsplit(str(value))
                self.scheme, self.host, self.port = parsed.scheme, parsed.hostname, parsed.port
                self.raw_path = parsed.path.encode("ascii") + (b"?" + parsed.query.encode("ascii") if parsed.query else b"")

        class FakeResponse:
            def __init__(self, status, payload):
                self.status_code = status
                self.content = json.dumps(payload).encode("utf-8")

            def json(self):
                return json.loads(self.content)

            def raise_for_status(self):
                return None

        class FakeHTTPError(Exception):
            pass

        class FakeConnectTimeout(FakeHTTPError):
            pass

        class FakeReadTimeout(FakeHTTPError):
            pass

        class FakeWriteTimeout(FakeHTTPError):
            pass

        class FakePoolTimeout(FakeHTTPError):
            pass

        class FakeClient:
            def __init__(self):
                self.calls = []
                self.next_result = None

            def get(self, url, *args, **kwargs):
                self.calls.append((url, args, kwargs))
                if isinstance(self.next_result, BaseException):
                    raise self.next_result
                return self.next_result

        class QueryParams(dict):
            pass

        httpx_stub = SimpleNamespace(
            URL=FakeURL, Response=FakeResponse, Client=FakeClient, QueryParams=QueryParams,
            ConnectTimeout=FakeConnectTimeout, ReadTimeout=FakeReadTimeout,
            WriteTimeout=FakeWriteTimeout, PoolTimeout=FakePoolTimeout,
            ConnectError=FakeHTTPError, ReadError=FakeHTTPError,
            WriteError=FakeHTTPError, RemoteProtocolError=FakeHTTPError,
            LocalProtocolError=FakeHTTPError, NetworkError=FakeHTTPError, HTTPError=FakeHTTPError,
        )
        adapter_module = ModuleType("fwrouter_api.adapters.mihomo")
        adapter_module.MihomoHttpAdapter = SimpleNamespace(
            _has_fresh_zero_delay_for_url=staticmethod(lambda *_args, **_kwargs: False),
        )
        native_module = ModuleType("application_acceptance.native_runner")
        native_module._redact_diagnostic = native_runner._redact_diagnostic
        monotonic_clock = {"ns": 1_000_000_000}

        def monotonic_ns():
            value = monotonic_clock["ns"]
            monotonic_clock["ns"] += 1_000_000
            return value

        namespace = {
            "__builtins__": __builtins__, "__package__": "application_acceptance",
            "os": __import__("os"), "sys": sys, "json": json,
            "time": SimpleNamespace(monotonic_ns=monotonic_ns),
            "threading": __import__("threading"), "Any": __import__("typing").Any,
            "quote": __import__("urllib.parse", fromlist=["quote"]).quote,
            "urlsplit": __import__("urllib.parse", fromlist=["urlsplit"]).urlsplit,
        }
        exec(compile(ast.Module(body=[installer], type_ignores=[]), str(worker_path), "exec"), namespace)
        original_client_get = FakeClient.get
        original_raise = FakeResponse.raise_for_status
        try:
            with mock.patch.dict(sys.modules, {
                "application_acceptance.native_runner": native_module,
                "fwrouter_api.adapters.mihomo": adapter_module,
                "httpx": httpx_stub,
            }), mock.patch.dict(__import__("os").environ, {
                "FWROUTER_ENVIRONMENT": "test",
                "FWROUTER_APPLICATION_ACCEPTANCE_ROOT": "/tmp/fwrouter-application-acceptance",
            }), mock.patch("sys.stderr", io.StringIO()):
                observer = namespace["_install_acceptance_mihomo_delay_error_observer"]()
                client = FakeClient()
                owned_url = "http://127.0.0.1:52044/generate_204"
                group_path = "http://127.0.0.1:5200/group/logical%2Ftarget%20one/delay"
                group_response = FakeResponse(200, {"member": 0})
                client.next_result = group_response
                group_token = observer["begin_probe"]("group", "logical/target one", owned_url)
                result = FakeClient.get(client, group_path, params=QueryParams(url=owned_url))
                group_evidence = observer["finish_group_probe"](group_token, {"ok": False})
                self.assertIs(group_response, result)
                self.assertEqual(1, len(client.calls))
                self.assertEqual({
                    "request_observed": True, "request_kind": "group", "response_received": True,
                    "status": 200, "url_matches_known_owned": True, "payload_map_empty": False,
                    "integer_delay_result_found": True, "integer_delay_zero": True,
                    "error_code": None, "adapter_probe_ok": False,
                    "request_started_monotonic_ms": 1000,
                    "request_finished_monotonic_ms": 1001,
                    "request_elapsed_ms": 1,
                }, group_evidence)

                member_response = FakeResponse(503, {"message": "An error occurred in the delay test"})
                client.next_result = member_response
                member_path = "http://127.0.0.1:5200/proxies/member%2Fidentity/delay"
                member_token = observer["begin_probe"]("member", "member/identity", owned_url)
                result = FakeClient.get(client, member_path, params=QueryParams({"url": owned_url}))
                member_evidence = observer["finish_group_probe"](member_token, {"ok": False})
                self.assertIs(member_response, result)
                self.assertEqual(2, len(client.calls))
                self.assertEqual("mihomo_generic_delay_failure", member_evidence["error_code"])
                self.assertEqual("member", member_evidence["request_kind"])
                self.assertEqual(1002, member_evidence["request_started_monotonic_ms"])
                self.assertEqual(1003, member_evidence["request_finished_monotonic_ms"])
                self.assertEqual(1, member_evidence["request_elapsed_ms"])

                failure = FakeReadTimeout("private exception text")
                client.next_result = failure
                member_token = observer["begin_probe"]("member", "member/identity", owned_url)
                with self.assertRaises(FakeReadTimeout) as raised:
                    FakeClient.get(client, member_path, params={"url": owned_url})
                member_error = observer["finish_group_probe"](member_token, {"ok": False})
                self.assertIs(failure, raised.exception)
                self.assertEqual("read_timeout", member_error["error_code"])
                self.assertEqual(1004, member_error["request_started_monotonic_ms"])
                self.assertEqual(1005, member_error["request_finished_monotonic_ms"])
                self.assertEqual(1, member_error["request_elapsed_ms"])
                self.assertEqual(3, len(client.calls))
        finally:
            FakeClient.get = original_client_get
            FakeResponse.raise_for_status = original_raise

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

    def test_owned_vpn_auto_reject_fault_route_is_membership_and_readback_bound(self):
        tree = ast.parse(WORKER_PATH.read_text(encoding="utf-8"))
        build_app = next(node for node in tree.body
                         if isinstance(node, ast.FunctionDef) and node.name == "build_app")
        route = next(node for node in ast.walk(build_app)
                     if isinstance(node, ast.FunctionDef)
                     and node.name == "set_owned_vpn_auto_selector_reject")
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
        self.assertEqual(["REJECT"], [value.value for value in payload.values])
        self.assertFalse(any(isinstance(node, ast.Constant) and node.value == "DIRECT"
                             for node in ast.walk(route)))
        self.assertTrue(any(isinstance(node, ast.Compare)
                            and any(isinstance(comparator, ast.Constant)
                                    and comparator.value == "REJECT" for comparator in node.comparators)
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
                            and call.args[0].value == "/api/v2/__acceptance/selection/runtime-reject")
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

    def test_nonloopback_mihomo_probe_is_separate_private_owned_listener(self):
        bridge = ProviderHttpTestBridge(mihomo_probe_address="172.18.0.1")
        self.assertEqual("172.18.0.1", bridge.mihomo_probe_address)
        self.assertIsNone(bridge.mihomo_probe_url)
        for address in ("127.0.0.1", "0.0.0.0", "224.0.0.1", "203.0.113.53", "not-an-address"):
            with self.subTest(address=address), self.assertRaises(ValueError):
                ProviderHttpTestBridge(mihomo_probe_address=address)
        with self.assertRaises(ValueError):
            ProviderHttpTestBridge(packet_endpoint=True, mihomo_probe_address="172.18.0.1")

        bridge_source = Path(__file__).parents[1] / "application_acceptance" / "joined_support.py"
        bridge_tree = ast.parse(bridge_source.read_text(encoding="utf-8"))
        owner = next(node for node in bridge_tree.body
                     if isinstance(node, ast.ClassDef) and node.name == "ProviderHttpTestBridge")
        probe_factory = next(node for node in owner.body
                             if isinstance(node, ast.FunctionDef) and node.name == "_probe_handler")
        probe_handler = next(node for node in ast.walk(probe_factory)
                             if isinstance(node, ast.ClassDef) and node.name == "ProbeHandler")
        handler_source = ast.get_source_segment(bridge_source.read_text(encoding="utf-8"), probe_handler) or ""
        self.assertIn('parsed.path != "/generate_204"', handler_source)
        self.assertIn('self.command not in {"GET", "HEAD"}', handler_source)
        self.assertIn("parsed.query", handler_source)
        self.assertNotIn('parsed.path == "/configs"', handler_source)
        handler_method = next(node for node in probe_handler.body
                              if isinstance(node, ast.FunctionDef) and node.name == "_handle")
        namespace = {"urlparse": urlparse}
        exec(compile(ast.Module(body=[handler_method], type_ignores=[]), str(bridge_source), "exec"), namespace)
        handle = namespace["_handle"]

        class Request:
            def __init__(self, path, method="GET"):
                self.path = path
                self.command = method
                self.errors = []

            def send_error(self, code):
                self.errors.append(code)

        for path, method in (("/configs", "GET"), ("/generate_204?external=1", "GET"),
                             ("/generate_204", "POST")):
            request = Request(path, method)
            handle(request)
            self.assertEqual([404], request.errors)

        conftest_source = (Path(__file__).parents[1] / "application_acceptance" / "conftest.py").read_text(
            encoding="utf-8")
        self.assertIn('profile["mihomo_handoff"]["address"]', conftest_source)
        self.assertIn('"FWROUTER_ACCEPTANCE_MIHOMO_PROBE_URL"', conftest_source)
        self.assertIn('"FWROUTER_ACCEPTANCE_MIHOMO_PROBE_PORT"', conftest_source)
        self.assertIn('"FWROUTER_ACCEPTANCE_PROVIDER_BASE_URL": provider_bridge.base_url', conftest_source)
        worker_source = (Path(__file__).parents[1] / "application_acceptance" / "worker.py").read_text(
            encoding="utf-8")
        self.assertIn('"FWROUTER_ACCEPTANCE_MIHOMO_PROBE_URL", "FWROUTER_ACCEPTANCE_MIHOMO_PROBE_PORT"',
                      worker_source)

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
                self.probe_header_write_completion_count = 0
                self.probe_header_write_errors = {}
                self.probe_header_write_events = []
                self.header_write_order = []
                self.last_header_write_error = None
                self.mode = "normal"
                self.calls = []
                self.request_entered = threading.Event()
                self._release_response = threading.Event()

            def record_probe_header_write(self, method, status, error=None):
                event = {"kind": "header_write_error" if error else "header_write_complete",
                         "method": method, "status": status,
                         "monotonic_ms": time.monotonic_ns() // 1_000_000}
                self.header_write_order.append("record")
                if error:
                    self.last_header_write_error = error
                    error_code = "broken_pipe" if isinstance(error, BrokenPipeError) else "os_error"
                    event["error_code"] = error_code
                    self.probe_header_write_errors[error_code] = 1
                else:
                    self.probe_header_write_completion_count += 1
                self.probe_header_write_events.append(event)

        class Connection:
            def __init__(self):
                self.shutdown_how = []

            def shutdown(self, how):
                self.shutdown_how.append(how)

        class Request:
            def __init__(self, path, header_write_error=None):
                self.headers = {"Content-Length": "0"}
                self.rfile = io.BytesIO()
                self.path = path
                self.command = "GET"
                self.connection = Connection()
                self.close_connection = False
                self.protocol_calls = []
                self.header_write_error = header_write_error

            def _respond(self, status, payload, headers=None):
                self.protocol_calls.append(("response", status, payload, headers))

            def send_response(self, status):
                self.protocol_calls.append(("send_response", status))

            def send_header(self, name, value):
                self.protocol_calls.append(("send_header", name, value))

            def end_headers(self):
                self.protocol_calls.append(("end_headers",))
                if bridge is not None:
                    bridge.header_write_order.append("end_headers")
                if self.header_write_error is not None:
                    raise self.header_write_error

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
        self.assertEqual(0, bridge.probe_header_write_completion_count)
        self.assertEqual([{"kind": "transport_closed", "status": None}], bridge.probe_events)
        self.assertEqual(1, bridge.probe_requests)

        bridge.probe_available = True
        bridge.header_write_order.clear()
        completed = Request("/generate_204")
        namespace["_handle"](completed)
        self.assertEqual(["end_headers", "record"], bridge.header_write_order)
        self.assertEqual(1, bridge.probe_header_write_completion_count)
        self.assertIs(type(bridge.probe_header_write_events[-1]["monotonic_ms"]), int)

        write_failure = BrokenPipeError("SECRET_SENTINEL")
        failed_write = Request("/generate_204", header_write_error=write_failure)
        with self.assertRaises(BrokenPipeError) as raised:
            namespace["_handle"](failed_write)
        self.assertIs(write_failure, raised.exception)
        self.assertIs(write_failure, bridge.last_header_write_error)
        self.assertEqual("header_write_error", bridge.probe_header_write_events[-1]["kind"])
        self.assertEqual("broken_pipe", bridge.probe_header_write_events[-1]["error_code"])

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
        start = workflow.index("      - name: Run exact hosted functional profile bundle")
        end = workflow.index("      - name: Aggregate exact full-phase", start)
        step = workflow[start:end]
        for suite in ("functional-ordinary", "provider-cohort", "packet-diagnostic"):
            self.assertIn(suite, workflow)
        self.assertIn("run_bundle 1 || first=$?", step)
        self.assertIn('if [ "$first" -ne 0 ]; then', step)
        self.assertIn("NOT_RUN_first_bundle_failed", step)
        self.assertIn("run_bundle 2 || second=$?", step)
        self.assertIn('test "$second" -eq 0', step)

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

    def test_qualified_child_junit_requires_exact_selected_nodes_and_pass_status(self):
        scenarios = json.loads(qualified_child.ROOT.joinpath(
            "tests/acceptance/qualified-child-scenarios.json").read_text(encoding="utf-8"))
        expected = set(scenarios["nodeids"])
        required_recovery = {
            "backend/tests/test_xray.py::test_failed_new_generation_restores_last_good_runtime_and_public_snapshot[success]",
            "backend/tests/test_xray.py::test_failed_new_generation_restores_last_good_runtime_and_public_snapshot[receipt_failure]",
            "backend/tests/test_xray.py::test_failed_new_generation_restores_last_good_runtime_and_public_snapshot[readback_failure]",
            "backend/tests/test_xray.py::test_failed_generation_after_inventory_sync_restores_scoped_derived_rows",
        }
        self.assertTrue(required_recovery <= expected)
        with tempfile.TemporaryDirectory(prefix="fwrouter-qualified-child-junit-") as temp:
            path = Path(temp) / "report.xml"

            def write_report(nodeids, *, skipped=()):
                root = ET.Element("testsuite")
                for nodeid in nodeids:
                    identity, name = nodeid.split("::", 1)
                    classname = Path(identity).stem
                    case = ET.SubElement(root, "testcase", {
                        "classname": f"tests.{classname}", "name": name,
                    })
                    if nodeid in skipped:
                        ET.SubElement(case, "skipped", {"message": "unexpected skip"})
                ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)
                return qualified_child._read_junit(path)

            complete = write_report(scenarios["nodeids"])
            self.assertTrue(complete["contract_valid"])
            self.assertEqual(expected, set(complete["node_status"]))
            self.assertTrue(required_recovery <= set(complete["node_status"]))

            omitted_node = (
                "backend/tests/test_xray.py::test_failed_new_generation_restores_last_good_runtime_and_public_snapshot[success]"
            )
            missing = write_report([node for node in scenarios["nodeids"] if node != omitted_node])
            self.assertFalse(missing["contract_valid"])
            skipped = write_report(scenarios["nodeids"], skipped={omitted_node})
            self.assertFalse(skipped["contract_valid"])
            with self.assertRaisesRegex(qualified_child.launcher.NotRun, "unrecognized test identity"):
                write_report([*scenarios["nodeids"],
                              "backend/tests/test_xray.py::test_unreviewed_generation_case"])

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
        scenarios = json.loads((qualified_child.ROOT / "tests/acceptance/qualified-child-scenarios.json").read_text(
            encoding="utf-8"))
        expected = [nodeid.removeprefix("backend/") for nodeid in scenarios["nodeids"]]
        self.assertEqual(expected, qualified_child._qualified_child_pytest_selectors())
        self.assertEqual(41, len(expected))
        self.assertTrue(all("::" in selector for selector in expected))
        required_xray_nodes = {
            "tests/test_xray.py::test_profile_generation_preserves_specific_nested_native_validation_failure",
            "tests/test_xray.py::test_generation_failure_operation_rejects_untrusted_nested_codes[lowercase]",
            "tests/test_xray.py::test_generation_failure_operation_rejects_untrusted_nested_codes[bad-punctuation]",
            "tests/test_xray.py::test_generation_failure_operation_rejects_untrusted_nested_codes[overlong]",
            "tests/test_xray.py::test_generation_failure_operation_rejects_untrusted_nested_codes[newline]",
            "tests/test_xray.py::test_generation_failure_operation_prefers_failed_local_code_only",
            "tests/test_xray.py::test_identity_compensation_allows_unchanged_preexisting_subject_and_snapshot_references",
            "tests/test_xray.py::test_identity_reference_preimage_rejects_malformed_projection_before_cleanup",
            "tests/test_xray.py::test_vpn_auto_stage_exception_compensates_new_identity_intent",
            "tests/test_xray.py::test_vpn_auto_source_fence_change_preserves_unverified_identity_intent",
            "tests/test_xray.py::test_vpn_auto_external_reference_blocks_identity_compensation",
            "tests/test_xray.py::test_failed_new_generation_restores_last_good_runtime_and_public_snapshot[success]",
            "tests/test_xray.py::test_failed_new_generation_restores_last_good_runtime_and_public_snapshot[receipt_failure]",
            "tests/test_xray.py::test_failed_new_generation_restores_last_good_runtime_and_public_snapshot[readback_failure]",
            "tests/test_xray.py::test_failed_generation_after_inventory_sync_restores_scoped_derived_rows",
        }
        self.assertTrue(required_xray_nodes <= set(expected))

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

    def test_mihomo_reconcile_failure_observer_is_bounded_and_passthrough(self):
        worker_path = Path(__file__).parents[1] / "application_acceptance" / "worker.py"
        parsed = ast.parse(worker_path.read_text(encoding="utf-8"))
        observer = next(node for node in parsed.body
                        if isinstance(node, ast.FunctionDef)
                        and node.name == "_install_acceptance_mihomo_reconcile_failure_observer")
        namespace = {
            "__builtins__": __builtins__, "__package__": "application_acceptance",
            "os": __import__("os"), "sys": sys, "json": json,
            "threading": threading, "Any": __import__("typing").Any,
        }
        exec(compile(ast.Module(body=[observer], type_ignores=[]), str(worker_path), "exec"), namespace)
        calls = []
        returned = {"unchanged": True}
        satisfies_calls = []
        satisfies_returned = {
            "ok": False, "reason": "fwrouter_metadata_mismatch",
            "metadata": {"resolved_selective_default": "secret-runtime-value"},
            "expected": {"resolved_selective_default": "secret-expected-value"},
            "selector_ok": False, "transparent_ok": True,
        }
        satisfies_failure = RuntimeError("secret exception")
        raise_satisfies = {"enabled": False}

        def original(*args, **kwargs):
            calls.append((args, kwargs))
            return returned

        def original_satisfies(*args, **kwargs):
            satisfies_calls.append((args, kwargs))
            if raise_satisfies["enabled"]:
                raise satisfies_failure
            return satisfies_returned

        apply_orchestrator = SimpleNamespace(
            _build_failure_result=original, mihomo_runtime_satisfies_routing=original_satisfies,
        )
        services = ModuleType("fwrouter_api.services")
        services.apply_orchestrator = apply_orchestrator
        reconcile = {
            "ok": False, "stage": "native_udp_policy_readback", "error_code": "PROOF_FAILED",
            "reconcile_action": "restart", "reconcile_reason": "native_policy_or_fence_unconfirmed",
            "config_validation": {"ok": True, "returncode": 0, "stderr_tail": "secret-config-content"},
            "candidate": {"rules_count": 23, "handoff_assignments_count": 4,
                          "candidate_path": "/private/path/config.yaml"},
            "promoted": {"ok": True, "promoted": True},
            "container": {"ok": True, "restarted": True},
            "native_udp_policy_proof": {"ok": False, "wiring_proven": False,
                                         "native_top_level_rules_match": False},
            "generation_recovery": {"ok": False, "recovered": False,
                                    "reason": "restore_restart_failed", "error_message": "private"},
            "last_good_retained": False, "error_message": "private message",
        }
        details = {"mihomo_reconcile": reconcile, "extra": "private"}
        observed = io.StringIO()
        with mock.patch.dict(sys.modules, {"fwrouter_api.services": services}), \
                mock.patch.dict(__import__("os").environ, {
                    "FWROUTER_ENVIRONMENT": "test",
                    "FWROUTER_APPLICATION_ACCEPTANCE_ROOT": "/tmp/fwrouter-application-acceptance",
                }), mock.patch("sys.stderr", observed):
            namespace["_install_acceptance_mihomo_reconcile_failure_observer"]()
            routing = {"active_auto_server_id": "secret-server-id"}
            self.assertIs(satisfies_returned, apply_orchestrator.mihomo_runtime_satisfies_routing(routing))
            self.assertEqual([((routing,), {})], satisfies_calls)
            raise_satisfies["enabled"] = True
            with self.assertRaises(RuntimeError) as raised:
                apply_orchestrator.mihomo_runtime_satisfies_routing(routing)
            self.assertIs(satisfies_failure, raised.exception)
            self.assertEqual([((routing,), {}), ((routing,), {})], satisfies_calls)
            result = None
            for _ in range(17):
                result = apply_orchestrator._build_failure_result("sentinel", details=details)
            self.assertIs(returned, result)
            self.assertEqual(17, len(calls))
            self.assertTrue(all(args == ("sentinel",) and kwargs == {"details": details}
                                for args, kwargs in calls))

        text = observed.getvalue()
        prefix = "FWROUTER_ACCEPTANCE_MIHOMO_RECONCILE_FAILURE "
        self.assertLessEqual(len(text.encode("ascii")), 16 * (4096 + len(prefix) + 1))
        for line in text.splitlines():
            self.assertTrue(line.startswith(prefix))
            self.assertLessEqual(len(line[len(prefix):].encode("ascii")), 4096)
        self.assertNotIn("secret-config-content", text)
        self.assertNotIn("/private/path", text)
        self.assertNotIn("private message", text)
        self.assertNotIn("private\"", text)
        self.assertNotIn("secret-runtime-value", text)
        self.assertNotIn("secret-expected-value", text)
        self.assertNotIn("secret-server-id", text)
        self.assertNotIn("secret exception", text)
        records = [json.loads(line.split(" ", 1)[1]) for line in text.splitlines()]
        self.assertEqual(16, len(records))
        satisfaction = next(record for record in records if record["event"] == "runtime_satisfaction_failure")
        self.assertEqual({"ok": False, "reason": "fwrouter_metadata_mismatch",
                          "metadata_comparisons": {"resolved_selective_default": False},
                          "policy_comparisons": {}, "selector_group_counts": {
                              "expected": None, "actual": None},
                          "runtime_state": None,
                          "predicates": {"selector_ok": False, "transparent_ok": True}},
                         satisfaction["summary"])
        summary = next(record["summary"] for record in records if record["event"] == "failure_result_input")
        self.assertEqual("native_udp_policy_readback", summary["stage"])
        self.assertEqual("PROOF_FAILED", summary["error_code"])
        self.assertEqual(0, summary["validation"]["returncode"])
        self.assertEqual({"ok": False, "native_top_level_rules_match": False,
                          "wiring_proven": False}, summary["native_udp_policy_proof"])
        self.assertEqual({"ok": False, "recovered": False, "error_code": None,
                          "reason": "restore_restart_failed"}, summary["recovery"])

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
            "os": __import__("os"), "sys": sys, "json": json, "re": re,
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

        class FakeClient:
            def get(self, url, *args, **kwargs):
                raise AssertionError("unexpected HTTP request in response-error observer contract")

        httpx_stub = SimpleNamespace(Response=FakeResponse, Client=FakeClient)
        adapter_module = ModuleType("fwrouter_api.adapters.mihomo")
        adapter_module.MihomoHttpAdapter = SimpleNamespace(
            _has_fresh_zero_delay_for_url=staticmethod(lambda *_args, **_kwargs: False),
        )
        native_module = ModuleType("application_acceptance.native_runner")
        native_module._redact_diagnostic = native_runner._redact_diagnostic
        namespace = {
            "__builtins__": __builtins__, "__package__": "application_acceptance",
            "os": __import__("os"), "sys": sys, "json": json, "re": re,
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
        original_client_get = FakeClient.get
        try:
            with mock.patch.dict(sys.modules, {
                "application_acceptance.native_runner": native_module,
                "fwrouter_api.adapters.mihomo": adapter_module,
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
            FakeClient.get = original_client_get

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

            def get(self, url, *args, **kwargs):
                if args or set(kwargs) != {"headers"}:
                    raise AssertionError("unexpected native delay observer GET in state-snapshot test")
                headers = kwargs["headers"]
                diagnostic_gets.append((url, headers, self.timeout, self.trust_env))
                return snapshot_response

        httpx_stub = SimpleNamespace(Response=FakeResponse, Client=FakeClient)
        native_module = ModuleType("application_acceptance.native_runner")
        native_module._redact_diagnostic = native_runner._redact_diagnostic
        class FakeMihomoHttpAdapter:
            @staticmethod
            def _has_fresh_zero_delay_for_url(*_args, **_kwargs):
                return False

        mihomo_module = ModuleType("fwrouter_api.adapters.mihomo")
        mihomo_module.MihomoHttpAdapter = FakeMihomoHttpAdapter
        namespace = {
            "__builtins__": __builtins__, "__package__": "application_acceptance",
            "os": __import__("os"), "sys": sys, "json": json, "re": re,
            "threading": __import__("threading"), "Any": __import__("typing").Any,
        }
        exec(compile(ast.Module(body=[observer], type_ignores=[]), str(worker_path), "exec"), namespace)
        original_method = FakeResponse.raise_for_status
        original_client_get = FakeClient.get
        request_headers = {"authorization": "synthetic-secret"}
        failed = RuntimeError("original 503")
        observed = io.StringIO()
        try:
            with mock.patch.dict(sys.modules, {
                "application_acceptance.native_runner": native_module,
                "fwrouter_api.adapters.mihomo": mihomo_module,
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
            FakeClient.get = original_client_get

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

    def test_mihomo_zero_readback_observer_reports_exact_sample_predicates_without_changing_result(self):
        worker_path = Path(__file__).parents[1] / "application_acceptance" / "worker.py"
        parsed = ast.parse(worker_path.read_text(encoding="utf-8"))
        observer = next(node for node in parsed.body
                        if isinstance(node, ast.FunctionDef)
                        and node.name == "_install_acceptance_mihomo_delay_error_observer")

        class FakeResponse:
            def raise_for_status(self):
                return None

        class FakeAdapter:
            helper_results = iter((False, True, False))

            @staticmethod
            def _has_fresh_zero_delay_for_url(*_args, **_kwargs):
                return next(FakeAdapter.helper_results)

        class FakeClient:
            def get(self, url, *args, **kwargs):
                raise AssertionError("unexpected HTTP request in zero-readback predicate contract")

        adapter_module = ModuleType("fwrouter_api.adapters.mihomo")
        adapter_module.MihomoHttpAdapter = FakeAdapter
        native_module = ModuleType("application_acceptance.native_runner")
        native_module._redact_diagnostic = native_runner._redact_diagnostic
        namespace = {
            "__builtins__": __builtins__, "__package__": "application_acceptance",
            "os": __import__("os"), "sys": sys, "json": json, "re": re,
            "threading": __import__("threading"), "Any": __import__("typing").Any,
        }
        exec(compile(ast.Module(body=[observer], type_ignores=[]), str(worker_path), "exec"), namespace)
        now = __import__("datetime").datetime(2026, 10, 11, 12, 0, 1,
                                               tzinfo=__import__("datetime").timezone.utc)
        started = now - __import__("datetime").timedelta(seconds=1)
        probe_url = "http://owned-private-endpoint.invalid/generate_204"
        inputs = [
            {"extra": {probe_url: {"alive": False, "history": [
                {"time": "2026-10-11T12:00:00.500000Z", "delay": 0},
            ]}}},
            {"extra": {probe_url: {"alive": True, "history": [
                {"time": "2026-10-11T12:00:00.500000Z", "delay": 0},
            ]}}},
            {"extra": {"different-private-url": {"alive": True, "history": [
                {"time": "2026-10-11T12:00:00.500000Z", "delay": 0},
            ]}}},
        ]
        output = io.StringIO()
        original_client_get = FakeClient.get
        with mock.patch.dict(sys.modules, {
            "application_acceptance.native_runner": native_module,
            "fwrouter_api.adapters.mihomo": adapter_module,
            "httpx": SimpleNamespace(Response=FakeResponse, Client=FakeClient),
        }), mock.patch.dict(__import__("os").environ, {
            "FWROUTER_ENVIRONMENT": "test",
            "FWROUTER_APPLICATION_ACCEPTANCE_ROOT": "/tmp/fwrouter-application-acceptance",
        }), mock.patch("sys.stderr", output):
            try:
                namespace["_install_acceptance_mihomo_delay_error_observer"]()
                observed = [FakeAdapter._has_fresh_zero_delay_for_url(
                    proxy, test_url=probe_url, probe_started_at=started, observed_end=now,
                ) for proxy in inputs]
            finally:
                FakeClient.get = original_client_get

        rows = [json.loads(line.split(" ", 1)[1]) for line in output.getvalue().splitlines()
                if line.startswith("FWROUTER_ACCEPTANCE_MIHOMO_ZERO_READBACK ")]
        self.assertEqual([False, True, False], observed)
        self.assertEqual([
            {"extra_url_sample_present": True, "sample_alive": False, "latest_delay_zero": True,
             "history_time_in_probe_window": True, "native_helper_accepted": False},
            {"extra_url_sample_present": True, "sample_alive": True, "latest_delay_zero": True,
             "history_time_in_probe_window": True, "native_helper_accepted": True},
            {"extra_url_sample_present": False, "sample_alive": False, "latest_delay_zero": False,
             "history_time_in_probe_window": False, "native_helper_accepted": False},
        ], [{key: value for key, value in row.items() if key != "schema"} for row in rows])
        self.assertTrue(all(row["schema"] == "fwrouter-acceptance-mihomo-zero-readback/v1" for row in rows))
        self.assertNotIn(probe_url, output.getvalue())
        self.assertNotIn("different-private-url", output.getvalue())

    def test_hosted_receipt_aggregation_rejects_source_plan_and_skipped_node_mismatch(self):
        expected = aggregate_hosted.hosted_partition_nodeids()["functional-ordinary"]
        source = "a" * 40
        plan_digest = "b" * 64
        nonce = "c" * 32
        profile = {
            "schema": "fwrouter-acceptance-profile/v2", "profile": "hosted-native-process",
            "source_revision": source,
            "plan_digest": plan_digest, "suite_nonce": nonce,
            "xray": {"version": provision.INPUTS["xray"]["version"],
                     "sha256": provision.BINARY_SHA256["xray"]},
            "mihomo": {"version": provision.INPUTS["mihomo"]["version"],
                       "sha256": provision.BINARY_SHA256["mihomo"]},
            "chromium": {"version": provision.CHROMIUM_VERSION,
                         "bundle_sha256": provision.CHROMIUM_BUNDLE_SHA256,
                         "sha256": provision.CHROMIUM_BINARY_SHA256},
            "playwright_python": provision.PLAYWRIGHT_VERSION,
        }
        self.assertEqual(nonce, profile["suite_nonce"])
        profile_sha = hashlib.sha256((json.dumps(profile, sort_keys=True, indent=2) + "\n").encode()).hexdigest()
        nodeids = sorted(expected)
        rows = [{"nodeid": nodeid, "status": "passed",
                 "phases": {phase: "passed" for phase in ("setup", "call", "teardown")}}
                for nodeid in nodeids]
        receipt = {
            "schema_version": 1, "scope": "hosted-native-process", "status": "passed",
            "suite": "functional-ordinary", "source_revision": source, "plan_digest": plan_digest,
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
            self.assertEqual([], aggregate_hosted.validate_functional_receipt(
                path, plan, expected, runtime_inputs, suite="functional-ordinary"))
            self.assertEqual([], aggregate_hosted.validate_main_smoke_receipt(
                path, plan, runtime_inputs, hosted_paths=[path],
                required_partitions=["functional-ordinary"]))
            self.assertIn(aggregate_hosted.main_smoke_nodeid(), expected)
            self.assertTrue(aggregate_hosted.validate_main_smoke_receipt(
                path, plan, runtime_inputs, hosted_paths=[],
                required_partitions=["functional-ordinary"]))
            stale = json.loads(json.dumps(receipt))
            stale["source_revision"] = "e" * 40
            path.write_text(json.dumps(stale), encoding="utf-8")
            self.assertTrue(aggregate_hosted.validate_main_smoke_receipt(
                path, plan, runtime_inputs, hosted_paths=[path],
                required_partitions=["functional-ordinary"]))
            path.write_text(json.dumps(receipt), encoding="utf-8")
            missing = Path(temp) / "missing-main-smoke.json"
            self.assertTrue(aggregate_hosted.validate_main_smoke_receipt(
                missing, plan, runtime_inputs, hosted_paths=[], required_partitions=[]))
            diagnostic = json.loads(json.dumps(receipt))
            diagnostic_node = aggregate_hosted.main_smoke_nodeid()
            diagnostic["suite"] = "xray-diagnostic"
            diagnostic["scope"] = "hosted-native-diagnostic"
            diagnostic["diagnostic_only"] = True
            diagnostic_row = next(row for row in diagnostic["application_receipt"]["tests"]
                                  if row["nodeid"] == diagnostic_node)
            diagnostic["tests"] = {"nodeids": [diagnostic_node], "tests": 1,
                                    "failures": 0, "errors": 0, "skipped": 0}
            diagnostic["application_receipt"]["tests"] = [diagnostic_row]
            path.write_text(json.dumps(diagnostic), encoding="utf-8")
            self.assertEqual([], aggregate_hosted.validate_main_smoke_receipt(
                path, plan, runtime_inputs, hosted_paths=[], required_partitions=[]))
            for field, value in (("scope", "hosted-native-process"),
                                 ("diagnostic_only", False), ("diagnostic_only", None)):
                changed = json.loads(json.dumps(diagnostic))
                if value is None:
                    changed.pop(field, None)
                else:
                    changed[field] = value
                path.write_text(json.dumps(changed), encoding="utf-8")
                self.assertTrue(aggregate_hosted.validate_main_smoke_receipt(
                    path, plan, runtime_inputs, hosted_paths=[], required_partitions=[]),
                    f"diagnostic receipt accepted invalid outer {field}")
            ordinary_diagnostic_flag = json.loads(json.dumps(receipt))
            ordinary_diagnostic_flag["diagnostic_only"] = True
            path.write_text(json.dumps(ordinary_diagnostic_flag), encoding="utf-8")
            self.assertTrue(aggregate_hosted.validate_main_smoke_receipt(
                path, plan, runtime_inputs, hosted_paths=[path],
                required_partitions=["functional-ordinary"]),
                "ordinary partition accepted diagnostic-only marker")
            path.write_text(json.dumps(diagnostic), encoding="utf-8")
            grouped, partition_problems = aggregate_hosted.group_hosted_receipts(
                [path], ["functional-ordinary", "provider-cohort"], 1)
            self.assertEqual([], grouped["functional-ordinary"])
            self.assertEqual([], grouped["provider-cohort"])
            self.assertEqual(3, len(partition_problems))
            diagnostic_cases = (
                ("source_revision", "e" * 40), ("plan_digest", "f" * 64),
                ("cleanup", "uncertain"),
            )
            for field, value in diagnostic_cases:
                changed = json.loads(json.dumps(diagnostic))
                changed[field] = value
                path.write_text(json.dumps(changed), encoding="utf-8")
                self.assertTrue(aggregate_hosted.validate_main_smoke_receipt(
                    path, plan, runtime_inputs, hosted_paths=[], required_partitions=[]))
            invalid_digest = json.loads(json.dumps(diagnostic))
            invalid_digest["profile_sha256"] = "0" * 64
            invalid_digest["application_receipt"]["profile_sha256"] = "0" * 64
            path.write_text(json.dumps(invalid_digest), encoding="utf-8")
            self.assertTrue(aggregate_hosted.validate_main_smoke_receipt(
                path, plan, runtime_inputs, hosted_paths=[], required_partitions=[]))
            wrong_inputs = json.loads(json.dumps(runtime_inputs))
            wrong_inputs["native"]["xray"]["binary_sha256"] = "0" * 64
            path.write_text(json.dumps(diagnostic), encoding="utf-8")
            self.assertTrue(aggregate_hosted.validate_main_smoke_receipt(
                path, plan, wrong_inputs, hosted_paths=[], required_partitions=[]))
            skipped = json.loads(json.dumps(diagnostic))
            skipped["tests"]["skipped"] = 1
            skipped["application_receipt"]["tests"][0]["phases"]["call"] = "skipped"
            skipped["application_receipt"]["tests"][0]["status"] = "skipped"
            path.write_text(json.dumps(skipped), encoding="utf-8")
            self.assertTrue(aggregate_hosted.validate_main_smoke_receipt(
                path, plan, runtime_inputs, hosted_paths=[], required_partitions=[]))
            unexpected = json.loads(json.dumps(diagnostic))
            unexpected_node = "tests/application_acceptance/test_xray_api.py::unexpected_node"
            unexpected["tests"]["nodeids"] = [unexpected_node]
            unexpected["application_receipt"]["tests"][0]["nodeid"] = unexpected_node
            path.write_text(json.dumps(unexpected), encoding="utf-8")
            self.assertTrue(aggregate_hosted.validate_main_smoke_receipt(
                path, plan, runtime_inputs, hosted_paths=[], required_partitions=[]))
            path.write_text(json.dumps(receipt), encoding="utf-8")
            self.assertEqual([path], aggregate_hosted.group_hosted_receipts(
                [path], ["functional-ordinary"], 1)[0]["functional-ordinary"])
            alias = json.loads(json.dumps(receipt))
            alias["suite"] = "functional"
            path.write_text(json.dumps(alias), encoding="utf-8")
            self.assertTrue(aggregate_hosted.validate_functional_receipt(
                path, plan, expected, runtime_inputs, suite="functional-ordinary"))
            self.assertTrue(aggregate_hosted.group_hosted_receipts(
                [path], ["functional-ordinary"], 1)[1])
            repeat = json.loads(json.dumps(receipt))
            repeat["suite_nonce"] = "f" * 32
            repeat["application_receipt"]["suite_nonce"] = repeat["suite_nonce"]
            repeat["profile"]["suite_nonce"] = repeat["suite_nonce"]
            repeat_profile_sha = hashlib.sha256((json.dumps(
                repeat["profile"], sort_keys=True, indent=2) + "\n").encode()).hexdigest()
            repeat["profile_sha256"] = repeat_profile_sha
            repeat["application_receipt"]["profile_sha256"] = repeat_profile_sha
            path.write_text(json.dumps(repeat), encoding="utf-8")
            self.assertEqual([], aggregate_hosted.validate_functional_receipt(
                path, plan, expected, runtime_inputs, suite="functional-ordinary"))
            partitions = aggregate_hosted.hosted_partition_nodeids()
            for suite, profile_name in (("provider-cohort", "hosted-kernel-dataplane"),
                                        ("packet-diagnostic", "hosted-kernel-packet")):
                partition_receipt = json.loads(json.dumps(receipt))
                ids = sorted(partitions[suite])
                partition_receipt["suite"] = suite
                partition_receipt["scope"] = profile_name
                # Kernel and packet receipts carry this marker in accepted hosted runs;
                # it does not change their independently validated execution scope.
                partition_receipt["diagnostic_only"] = True
                partition_receipt["tests"] = {"nodeids": ids, "tests": len(ids),
                                               "failures": 0, "errors": 0, "skipped": 0}
                partition_receipt["profile"]["profile"] = profile_name
                partition_receipt["application_receipt"]["scope"] = profile_name
                partition_receipt["application_receipt"]["tests"] = [
                    {"nodeid": nodeid, "status": "passed",
                     "phases": {phase: "passed" for phase in ("setup", "call", "teardown")}}
                    for nodeid in ids]
                digest = hashlib.sha256((json.dumps(partition_receipt["profile"], sort_keys=True,
                                                    indent=2) + "\n").encode()).hexdigest()
                partition_receipt["profile_sha256"] = digest
                partition_receipt["application_receipt"]["profile_sha256"] = digest
                path.write_text(json.dumps(partition_receipt), encoding="utf-8")
                self.assertEqual([], aggregate_hosted.validate_functional_receipt(
                    path, plan, set(ids), runtime_inputs, suite=suite))
            for field, value in (("source_revision", "e" * 40), ("plan_digest", "f" * 64)):
                changed = dict(receipt)
                changed[field] = value
                path.write_text(json.dumps(changed), encoding="utf-8")
                self.assertTrue(aggregate_hosted.validate_functional_receipt(
                    path, plan, expected, runtime_inputs, suite="functional-ordinary"))
            changed = json.loads(json.dumps(receipt))
            changed["application_receipt"]["tests"][0]["phases"]["teardown"] = "skipped"
            path.write_text(json.dumps(changed), encoding="utf-8")
            self.assertTrue(aggregate_hosted.validate_functional_receipt(
                path, plan, expected, runtime_inputs, suite="functional-ordinary"))
            changed = json.loads(json.dumps(receipt))
            changed["profile"]["xray"]["version"] = "latest"
            path.write_text(json.dumps(changed), encoding="utf-8")
            self.assertTrue(aggregate_hosted.validate_functional_receipt(
                path, plan, expected, runtime_inputs, suite="functional-ordinary"))

    def test_verified_docker_xray_receipt_contributes_only_its_exact_native_node(self):
        source = "a" * 40
        digest = "b" * 64
        nonce = "c" * 32
        image_id = "sha256:" + "d" * 64
        nodeid = "backend/tests/test_xray_native_readback.py::test_loaded_client_readback_against_isolated_xray_26_2_6"
        profile = {
            "schema": "fwrouter-qualified-docker-xray-profile/v1", "profile": "qualified-docker-xray",
            "source_revision": source, "plan_digest": digest, "suite_nonce": nonce,
            "image_id": image_id, "xray_binary_sha256": provision.BINARY_SHA256["xray"],
            "xray_version": provision.INPUTS["xray"]["version"],
        }
        profile_sha = hashlib.sha256((json.dumps(profile, sort_keys=True, indent=2) + "\n").encode()).hexdigest()
        receipt = {
            "schema": "fwrouter-qualified-docker-xray-receipt/v1", "scope": "qualified-docker-xray",
            "status": "passed", "source_revision": source, "plan_digest": digest,
            "suite_nonce": nonce, "cleanup": "owned_resources_removed", "image_id": image_id,
            "expected_ids": [nodeid], "xray_binary_sha256": provision.BINARY_SHA256["xray"],
            "profile_sha256": profile_sha,
            "tests": {"node_status": {nodeid: "passed"}, "passed": 1, "failed": 0,
                      "skipped": 0, "contract_valid": True},
        }
        runtime_inputs = {"native": {"xray": {"binary_sha256": provision.BINARY_SHA256["xray"]}}}
        plan = {"source_commit": source, "plan_digest": digest}
        with tempfile.TemporaryDirectory(prefix="fwrouter-xray-receipt-contract-") as temp:
            path = Path(temp) / "qualified-xray-docker-receipt.json"
            path.write_text(json.dumps(receipt), encoding="utf-8")
            path.with_name("profile.json").write_text(json.dumps(profile), encoding="utf-8")
            problems, statuses = aggregate_hosted.validate_xray_docker_receipt(path, plan, runtime_inputs)
            self.assertEqual([], problems)
            self.assertEqual({nodeid: "passed"}, statuses)
            receipt["plan_digest"] = "e" * 64
            path.write_text(json.dumps(receipt), encoding="utf-8")
            problems, statuses = aggregate_hosted.validate_xray_docker_receipt(path, plan, runtime_inputs)
        self.assertTrue(problems)
        self.assertEqual({}, statuses)

    def test_verified_receipt_families_share_a_distinct_nonce_namespace(self):
        self.assertTrue(aggregate_hosted.run_nonces_are_distinct(["a" * 32, "b" * 32]))
        self.assertFalse(aggregate_hosted.run_nonces_are_distinct(["a" * 32, "a" * 32]))

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

    def test_packet_profile_declares_and_validates_native_dns_runtime_fields(self):
        profile_source = PROFILE_PATH.read_text(encoding="utf-8")
        profile_tree = ast.parse(profile_source, filename=str(PROFILE_PATH))
        load_profile = next(node for node in profile_tree.body
                            if isinstance(node, ast.FunctionDef) and node.name == "load_profile")
        load_source = ast.get_source_segment(profile_source, load_profile) or ""
        packet_verifier = next(node for node in profile_tree.body
                               if isinstance(node, ast.FunctionDef)
                               and node.name == "_verify_packet_router_profile")
        verifier_source = ast.get_source_segment(profile_source, packet_verifier) or ""
        self.assertIn('"profile_owner_uid", "dns_runtime"', load_source)
        self.assertIn('dns = profile.get("dns_runtime")', verifier_source)
        self.assertIn('"bind9_dnsutils_package_version"', verifier_source)
        self.assertIn('"dnsmasq_nftset_required"', verifier_source)
        version_matcher = next(node for node in profile_tree.body
                               if isinstance(node, ast.FunctionDef)
                               and node.name == "_dig_version_matches")
        version_namespace = {}
        exec(compile(ast.Module(body=[version_matcher], type_ignores=[]),
                     "acceptance-profile-dig-version-contract.py", "exec"), version_namespace)
        matches = version_namespace["_dig_version_matches"]
        self.assertTrue(matches(0, b"DiG 9.18.49-1~deb12u1", b""))
        self.assertTrue(matches(0, b"", b"DiG 9.18.49-1~deb12u1\n"))
        self.assertFalse(matches(0, b"wrong", b"version"))
        self.assertFalse(matches(1, b"DiG 9.18.49-1~deb12u1", b""))
        self.assertFalse(matches(0, b"DiG 9.18.49\xff", b""))

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
        runtime_tree = ast.parse(runtime_code, filename="acceptance-runtime-preflight.py")
        dig_version_helpers = [node for node in ast.walk(runtime_tree)
                               if isinstance(node, ast.FunctionDef)
                               and node.name == "_dig_version_matches"]
        self.assertEqual(len(dig_version_helpers), 1)
        dig_version_helper = dig_version_helpers[0]
        dig_namespace = {}
        exec(compile(ast.Module(body=[dig_version_helper], type_ignores=[]),
                     "acceptance-dig-version-contract.py", "exec"), dig_namespace)
        matches = dig_namespace["_dig_version_matches"]
        self.assertTrue(matches(0, b"DiG 9.18.49-1~deb12u1", b""))
        self.assertTrue(matches(0, b"", b"DiG 9.18.49-1~deb12u1\n"))
        self.assertFalse(matches(0, b"other-version", b"also-other-version"))
        self.assertFalse(matches(1, b"DiG 9.18.49-1~deb12u1", b""))
        self.assertFalse(matches(0, b"DiG 9.18.49\xff", b""))
        self.assertIn('return returncode==0 and output.startswith("DiG 9.18.49")', runtime_code)
        self.assertIn('assert _dig_version_matches(dig.returncode,dig.stdout,dig.stderr)', runtime_code)
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

    def test_packet_kernel_preflight_has_narrow_nft_bound_and_records_failure_before_rejecting_output(self):
        code = launcher.kernel_preflight_code()
        tree = ast.parse(code, filename="acceptance-kernel-preflight.py")
        run_node = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "run")
        code_namespace = {
            "subprocess": __import__("subprocess"),
            "pathlib": __import__("pathlib"),
            "env": {},
            "p": {"profile": "hosted-kernel-packet"},
            "redact": lambda value: value[-1024:],
            "last_command": "", "last_stderr": "", "last_exit": None,
            "last_stdout_bytes": 0, "last_stderr_bytes": 0,
        }
        isolated = ast.Module(body=[run_node], type_ignores=[])
        exec(compile(isolated, "kernel-preflight-run-contract.py", "exec"), code_namespace)

        def run(profile, argv, stdout=b"", stderr=b"", status=0):
            code_namespace.update(p={"profile": profile}, last_command="", last_exit=None,
                                  last_stdout_bytes=0, last_stderr_bytes=0)
            with mock.patch.object(code_namespace["subprocess"], "run",
                                   return_value=SimpleNamespace(returncode=status, stdout=stdout, stderr=stderr)):
                result = code_namespace["run"](argv)
            return result, dict(code_namespace)

        exact_ruleset = ["/usr/sbin/nft", "-j", "list", "ruleset"]
        result, observed = run("hosted-kernel-packet", exact_ruleset, stdout=b"x" * 64000)
        self.assertEqual(0, result.returncode)
        self.assertEqual("nft -j list ruleset", observed["last_command"])
        self.assertEqual(64000, observed["last_stdout_bytes"])
        self.assertEqual(0, observed["last_stderr_bytes"])
        _, failed = run("hosted-kernel-packet", ["/usr/sbin/ip", "-j", "-4", "route", "show"],
                        stdout=b"bounded", status=17)
        self.assertEqual("ip -j -4 route show", failed["last_command"])
        self.assertEqual(17, failed["last_exit"])
        self.assertEqual(7, failed["last_stdout_bytes"])

        with self.assertRaises(AssertionError):
            code_namespace.update(p={"profile": "hosted-kernel-packet"}, last_command="", last_exit=None,
                                  last_stdout_bytes=0, last_stderr_bytes=0)
            with mock.patch.object(code_namespace["subprocess"], "run",
                                   return_value=SimpleNamespace(returncode=0, stdout=b"x" * 9000, stderr=b"")):
                code_namespace["run"](["/usr/bin/dpkg-query", "-W", "-f=version", "nftables"])
        self.assertEqual("dpkg-query -W -f=version nftables", code_namespace["last_command"])
        self.assertEqual(9000, code_namespace["last_stdout_bytes"])

        for profile, argv, output_size in (("hosted-kernel-packet", exact_ruleset, 65537),
                                           ("hosted-kernel-packet", ["/usr/sbin/nft", "-j", "list", "table"], 9000),
                                           ("hosted-kernel-dataplane", exact_ruleset, 9000),
                                           ("hosted-kernel-packet", ["/tmp/nft", "-j", "list", "ruleset"], 9000)):
            with self.subTest(profile=profile, argv=argv):
                code_namespace.update(p={"profile": profile}, last_command="", last_exit=None,
                                      last_stdout_bytes=0, last_stderr_bytes=0)
                with mock.patch.object(code_namespace["subprocess"], "run",
                                       return_value=SimpleNamespace(returncode=0,
                                                                    stdout=b"x" * output_size, stderr=b"")):
                    with self.assertRaises(AssertionError):
                        code_namespace["run"](argv)
                self.assertEqual(" ".join([Path(argv[0]).name, *argv[1:]]), code_namespace["last_command"])
                self.assertEqual(output_size, code_namespace["last_stdout_bytes"])

        with self.assertRaises(AssertionError):
            code_namespace.update(p={"profile": "hosted-kernel-packet"}, last_command="", last_exit=None,
                                  last_stdout_bytes=0, last_stderr_bytes=0)
            with mock.patch.object(code_namespace["subprocess"], "run",
                                   return_value=SimpleNamespace(returncode=0, stdout=b"", stderr=b"e" * 4097)):
                code_namespace["run"](exact_ruleset)
        self.assertEqual("nft -j list ruleset", code_namespace["last_command"])
        self.assertEqual(4097, code_namespace["last_stderr_bytes"])

        top_try = next(node for node in tree.body if isinstance(node, ast.Try) and node.finalbody)
        primary_handler = next(handler for handler in top_try.handlers
                               if isinstance(handler.type, ast.Name) and handler.type.id == "Exception")
        receipt_assignments = {}
        for node in ast.walk(ast.Module(body=primary_handler.body, type_ignores=[])):
            if not isinstance(node, ast.Assign) or len(node.targets) != 1 or not isinstance(node.targets[0], ast.Subscript):
                continue
            target = node.targets[0]
            if isinstance(target.value, ast.Name) and target.value.id == "result" and isinstance(target.slice, ast.Constant):
                receipt_assignments[target.slice.value] = node.value
        for field, variable in (("failure_stdout_bytes", "last_stdout_bytes"),
                                ("failure_stderr_bytes", "last_stderr_bytes")):
            expression = receipt_assignments[field]
            self.assertIsInstance(expression, ast.IfExp)
            self.assertIsInstance(expression.test, ast.Name)
            self.assertEqual("last_command", expression.test.id)
            self.assertIsInstance(expression.body, ast.Name)
            self.assertEqual(variable, expression.body.id)
            self.assertIsInstance(expression.orelse, ast.Constant)
            self.assertIsNone(expression.orelse.value)

        cleanup_try = next(node for node in top_try.finalbody if isinstance(node, ast.Try))
        cleanup_failure_branch = next(node for node in ast.walk(ast.Module(
            body=cleanup_try.body, type_ignores=[])) if isinstance(node, ast.If)
            and isinstance(node.test, ast.UnaryOp) and isinstance(node.test.op, ast.Not)
            and isinstance(node.test.operand, ast.Name) and node.test.operand.id == "clean")
        for original_stage, original_type in (("nft_tproxy_apply_readback", "AssertionError"), (None, None)):
            state = {"result": {"failure_stage": original_stage, "failure_type": original_type},
                     "clean": False, "failed": original_type}
            exec(compile(ast.Module(body=[cleanup_failure_branch], type_ignores=[]),
                         "kernel-preflight-cleanup-contract.py", "exec"), state)
            self.assertEqual("CleanupVerificationFailed", state["result"]["cleanup_failure_type"])
            if original_type is not None:
                self.assertEqual(original_stage, state["result"]["failure_stage"])
                self.assertEqual(original_type, state["result"]["failure_type"])
            else:
                self.assertEqual("cleanup", state["result"]["failure_stage"])
                self.assertEqual("CleanupVerificationFailed", state["result"]["failure_type"])

        cleanup_handler = next(handler for handler in cleanup_try.handlers
                               if isinstance(handler.type, ast.Name) and handler.type.id == "Exception")
        for original_type, expected_stage in (("AssertionError", "nft_tproxy_apply_readback"), (None, "cleanup")):
            state = {"result": {"failure_stage": "nft_tproxy_apply_readback" if original_type else None,
                                "failure_type": original_type},
                     "failed": original_type, "exc": OSError("synthetic")}
            exec(compile(ast.Module(body=cleanup_handler.body, type_ignores=[]),
                         "kernel-preflight-cleanup-except-contract.py", "exec"), state)
            self.assertEqual("OSError", state["result"]["cleanup_failure_type"])
            self.assertEqual(expected_stage, state["result"]["failure_stage"])

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
        for suite in ("functional-ordinary", "recovery", "reentry-diagnostic",
                      "identity-diagnostic", "runtime-scale-diagnostic"):
            with self.subTest(suite=suite), tempfile.TemporaryDirectory(prefix="fwrouter-acceptance-junit-") as temp:
                expected = launcher.expected_acceptance_nodeids(suite)
                root = ET.Element("testsuite")
                for nodeid in sorted(expected):
                    path, _, name = nodeid.partition("::")
                    ET.SubElement(root, "testcase", {
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
                ET.SubElement(root, "testcase", {
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
        self.assertEqual([("functional-ordinary", "recovery", "xray-diagnostic", "provider-diagnostic",
                           "browser-diagnostic", "provider-cohort", "fence-diagnostic", "target-diagnostic",
                           "recovery-diagnostic", "reentry-diagnostic", "kernel-preflight",
                           "kernel-recovery-diagnostic", "packet-diagnostic", "identity-diagnostic",
                           "runtime-scale-diagnostic", "final-regressions-diagnostic")], suite_choices)
        suite_defaults = [
            ast.literal_eval(keyword.value)
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "add_argument" and node.args
            and isinstance(node.args[0], ast.Constant) and node.args[0].value == "--suite"
            for keyword in node.keywords if keyword.arg == "default"
        ]
        self.assertEqual(["functional-ordinary"], suite_defaults)
        self.assertIn("'functional' is ambiguous", source)

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
                         "enter_emergency_direct", "try_verified_reentry", "provider_reentry_result",
                         "_apply_override_under_policy",
                         "_run_pipeline_for_state", "select_vpn_auto_server",
                         "override_pipeline_caught_exception", "exception_type", "limit = 32",
                         "The real Core apply result nests its bounded taxonomy under dataplane.",
                         'dataplane.get("error_code")', 'for key in ("stage", "error_stage")',
                         '"dataplane_capability", "enforcement_level"'):
            self.assertIn(required, observer_source)
        self.assertNotIn("str(exc)", observer_source)
        self.assertNotIn("error_message", observer_source)

    def test_reentry_diagnostic_is_exact_two_kernel_provider_reentry_nodes(self):
        expected = {
            "tests/application_acceptance/test_core_provider_mihomo.py::test_provider_reentry_rejects_old_probe_after_real_mihomo_incarnation_change",
            "tests/application_acceptance/test_core_provider_mihomo.py::test_provider_reentry_fences_concurrent_public_exclusive_intent_change",
        }
        self.assertEqual(expected, launcher.expected_acceptance_nodeids("reentry-diagnostic"))
        self.assertEqual(expected, set(launcher._REENTRY_DIAGNOSTIC_NODEIDS))
        self.assertIn("reentry-diagnostic", launcher._KERNEL_DATAPLANE_SUITES)
        self.assertNotIn("reentry-diagnostic", launcher._KERNEL_SUITES)
        with self.assertRaises(launcher.NotRun):
            launcher.expected_acceptance_nodeids("reentry-diagnostic-extra")

        catalog_rows = (launcher.expected_acceptance_nodeids("functional-ordinary")
                        | launcher.expected_acceptance_nodeids("provider-cohort"))
        self.assertTrue(expected.issubset(catalog_rows))
        scenarios = json.loads((Path(__file__).parents[2] / "tests/acceptance/scenarios.json").read_text())
        selected_scenarios = {row["nodeid"]: row for row in scenarios["scenarios"]
                              if row["nodeid"] in expected}
        self.assertEqual(expected, set(selected_scenarios))
        self.assertTrue(all(row["suite"] == "functional" and row["level"] == "L3"
                            for row in selected_scenarios.values()))
        manifest = json.loads((Path(__file__).parents[1] / "gates/manifest.json").read_text())
        kernel_rows = {row["nodeid"] for row in manifest["node_execution_profiles"]
                       if row["suite"] == "provider-cohort"
                       and row["execution_profile"] == "hosted-kernel-dataplane"}
        self.assertTrue(expected.issubset(kernel_rows))

        failed = [{"nodeid": nodeid, "status": "failed",
                   "phases": {"setup": "passed", "call": "failed", "teardown": "passed"}}
                  for nodeid in sorted(expected)]
        launcher.validate_suite_node_receipt(failed, "reentry-diagnostic", sorted(expected))
        skipped = [dict(row, status="skipped") for row in failed]
        with self.assertRaises(launcher.NotRun):
            launcher.validate_suite_node_receipt(skipped, "reentry-diagnostic", sorted(expected))
        expanded = [*failed, {**failed[0], "nodeid": "unexpected-extra-node"}]
        with self.assertRaises(launcher.NotRun):
            launcher.validate_suite_node_receipt(expanded, "reentry-diagnostic", sorted(expected))

        launcher_source = LAUNCHER_PATH.read_text(encoding="utf-8")
        suite_choices = [
            ast.literal_eval(keyword.value)
            for node in ast.walk(ast.parse(launcher_source))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "add_argument" and node.args
            and isinstance(node.args[0], ast.Constant) and node.args[0].value == "--suite"
            for keyword in node.keywords if keyword.arg == "choices"
        ]
        self.assertEqual(1, len(suite_choices))
        self.assertIn("reentry-diagnostic", suite_choices[0])
        workflow = (Path(__file__).parents[2] / ".github/workflows/phase-d-validation.yml").read_text(
            encoding="utf-8")
        self.assertIn('"ci:validate-reentry": "reentry-diagnostic"', workflow)
        self.assertIn("needs.select-stage.outputs.stage == 'reentry-diagnostic'", workflow)
        self.assertIn('if [[ "$VALIDATION_STAGE" == reentry-diagnostic ]]; then suite=reentry-diagnostic; fi',
                      workflow)
        selective_nodeids = (
            "tests/test_dataplane_live.py::test_live_global_mode_classifies_normal_and_degraded_selective_defaults",
            "tests/test_dataplane_live.py::test_live_global_mode_keeps_conflicting_selective_defaults_ambiguous",
            "tests/test_dataplane_live.py::test_live_global_mode_does_not_infer_default_from_selective_marker_alone",
            "tests/test_dataplane_global.py::test_global_vpn_preflight_allows_prepare_with_empty_reject_selector_without_egress",
            "tests/test_dataplane_global.py::test_selective_vpn_policy_prepare_is_separate_from_egress_verification",
            "tests/test_apply_pipeline.py::test_render_owned_table_candidate_fail_closes_selective_vpn_default_until_policy_ready",
            "tests/test_apply_pipeline.py::test_apply_pipeline_selective_degraded_blocks_vpn_sets_and_fails_closed_for_vpn_default",
            "tests/test_mihomo_config_runtime.py::test_reconcile_mihomo_selective_default_fast_requires_full_compile_when_udp_guard_sequence_changes",
            "tests/test_mihomo_config_runtime.py::test_mihomo_fingerprint_ignores_derived_auto_selection_but_tracks_generation_inputs",
            "tests/test_mihomo_config_runtime.py::test_mihomo_publication_fence_rejects_newer_selection_with_stable_derived_input_hash",
            "tests/test_routing_drift_reapply.py::test_set_selective_default_reapplies_when_live_dataplane_drift_is_detected",
            "tests/test_routing_drift_reapply.py::test_set_selective_default_does_not_retry_unmarked_fast_failure",
        )
        backend_root = Path(__file__).parents[2] / "backend"
        for nodeid in selective_nodeids:
            relative_path = nodeid.split("::", 1)[0]
            self.assertTrue(relative_path.startswith("tests/"), nodeid)
            self.assertTrue((backend_root / relative_path).is_file(), nodeid)
        self.assertIn("Run exact selective live-mode and preflight regressions", workflow)
        self.assertIn("needs.select-stage.outputs.stage == 'reentry-diagnostic' ||\n"
                      "          needs.select-stage.outputs.stage == 'packet-diagnostic'", workflow)
        step_start = workflow.index("      - name: Run exact selective live-mode and preflight regressions")
        step_end = workflow.find("\n      - name:", step_start + 1)
        self.assertNotEqual(-1, step_end)
        selected_step = workflow[step_start:step_end]
        self.assertEqual(list(selective_nodeids), re.findall(
            r"tests/(?:test_dataplane_live|test_dataplane_global|test_apply_pipeline|"
            r"test_mihomo_config_runtime|test_routing_drift_reapply)\.py::[A-Za-z0-9_]+",
            selected_step))
        self.assertIn('if [[ "$VALIDATION_STAGE" == recovery-diagnostic ]]; then suite=recovery-diagnostic; fi',
                      workflow)

    def test_selective_live_mode_regressions_reject_ambiguous_and_unknown_defaults(self):
        test_source = (Path(__file__).parents[2] / "backend/tests/test_dataplane_live.py").read_text(
            encoding="utf-8")
        tree = ast.parse(test_source)
        test_functions = {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}
        self.assertEqual({
            "test_live_global_mode_classifies_normal_and_degraded_selective_defaults",
            "test_live_global_mode_keeps_conflicting_selective_defaults_ambiguous",
            "test_live_global_mode_does_not_infer_default_from_selective_marker_alone",
            "test_applied_nft_markers_detect_stale_reserved_lan_ingress_rules",
            "test_applied_nft_markers_ignore_current_lan_and_unrelated_rescue_comments",
        }, test_functions)
        source = ast.get_source_segment(test_source, next(
            node for node in tree.body if isinstance(node, ast.FunctionDef)
            and node.name == "test_live_global_mode_keeps_conflicting_selective_defaults_ambiguous"))
        self.assertIsNotNone(source)
        self.assertIn('("unknown", None)', source)
        self.assertIn('expected_selective_default="direct"', source)
        self.assertIn('expected_selective_default="vpn"', source)
        manifest = json.loads((Path(__file__).parents[1] / "gates/manifest.json").read_text())
        registered = [row for row in manifest["test_files"]
                      if row["path"] == "backend/tests/test_dataplane_live.py"]
        self.assertEqual(1, len(registered))
        self.assertEqual("routing", registered[0]["domain"])
        self.assertEqual("L1", registered[0]["primary_level"])
        self.assertFalse(registered[0]["native"])
        self.assertEqual("none", registered[0]["network"])

    def test_packet_diagnostic_nodes_are_fixed_and_registered(self):
        nodeid = (
            "tests/application_acceptance/test_packet_dataplane.py::"
            "test_core_vpn_emergency_direct_reentry_forwards_owned_tcp_udp_dns_without_leaks"
        )
        legacy_nodeid = (
            "tests/application_acceptance/test_packet_dataplane.py::"
            "test_verified_legacy_admission_survives_normal_startup_without_reapply_or_refresh"
        )
        scale_nodeid = (
            "tests/application_acceptance/test_runtime_scale.py::"
            "test_large_mihomo_health_readback_stays_bounded_under_concurrent_api_load"
        )
        fixed_nodeid = "tests/application_acceptance/test_packet_dataplane.py::test_fixed_legacy_admission_survives_two_normal_startups"
        expected_nodes = {nodeid, legacy_nodeid, fixed_nodeid, scale_nodeid}
        self.assertEqual(expected_nodes, launcher.expected_acceptance_nodeids("packet-diagnostic"))
        with self.assertRaises(launcher.NotRun):
            launcher.expected_acceptance_nodeids("packet-diagnostic-extra")
        failed = [
            {"nodeid": nodeid, "status": "failed",
             "phases": {"setup": "passed", "call": "failed", "teardown": "passed"}},
            {"nodeid": legacy_nodeid, "status": "passed",
             "phases": {"setup": "passed", "call": "passed", "teardown": "passed"}},
            {"nodeid": scale_nodeid, "status": "passed",
             "phases": {"setup": "passed", "call": "passed", "teardown": "passed"}},
            {"nodeid": fixed_nodeid, "status": "passed",
             "phases": {"setup": "passed", "call": "passed", "teardown": "passed"}},
        ]
        launcher.validate_suite_node_receipt(failed, "packet-diagnostic", sorted(expected_nodes))
        skipped = [dict(row, status="skipped") if row["nodeid"] == scale_nodeid else row for row in failed]
        with self.assertRaises(launcher.NotRun):
            launcher.validate_suite_node_receipt(skipped, "packet-diagnostic", sorted(expected_nodes))
        self.assertEqual(expected_nodes, launcher.expected_acceptance_nodeids("packet-diagnostic"))
        packet_source = (Path(__file__).parents[1] / "application_acceptance" / "test_packet_dataplane.py").read_text(
            encoding="utf-8")
        packet_tree = ast.parse(packet_source)
        packet_case = next(node for node in packet_tree.body
                           if isinstance(node, ast.FunctionDef)
                           and node.name == "test_core_vpn_emergency_direct_reentry_forwards_owned_tcp_udp_dns_without_leaks")
        packet_case_source = ast.get_source_segment(packet_source, packet_case) or ""
        self.assertIn("provider_calls_after_reentry == provider_calls_before_reentry", packet_case_source)
        self.assertNotIn("bridge.snapshot_calls() == []", packet_case_source)
        self.assertIn('"provider_api_call_summary": reentry_provider_call_summary', packet_case_source)
        self.assertIn('"provider_api_calls_before": len(provider_calls_before_reentry)', packet_case_source)
        self.assertIn('"provider_api_calls_after": len(provider_calls_after_reentry)', packet_case_source)
        workflow = (Path(__file__).parents[2] / ".github/workflows/phase-d-validation.yml").read_text(
            encoding="utf-8")
        self.assertIn('"ci:validate-packet": "packet-diagnostic"', workflow)
        self.assertIn('if [[ "$VALIDATION_STAGE" == packet-diagnostic ]]; then suite=packet-diagnostic; fi', workflow)

    def test_identity_and_runtime_scale_diagnostics_have_separate_fixed_nodes(self):
        identity = (
            "tests/application_acceptance/test_xray_generation.py::"
            "test_xray_reviewed_identity_migration_preserves_credentials_through_refresh_restart_recovery"
        )
        scale = (
            "tests/application_acceptance/test_runtime_scale.py::"
            "test_large_mihomo_health_readback_stays_bounded_under_concurrent_api_load"
        )
        self.assertEqual({identity}, launcher.expected_acceptance_nodeids("identity-diagnostic"))
        self.assertEqual({scale}, launcher.expected_acceptance_nodeids("runtime-scale-diagnostic"))
        self.assertNotIn(identity, launcher.expected_acceptance_nodeids("provider-cohort"))
        self.assertIn(identity, launcher.expected_acceptance_nodeids("functional-ordinary"))
        with self.assertRaises(launcher.NotRun):
            launcher.expected_acceptance_nodeids("identity-diagnostic-extra")
        source = LAUNCHER_PATH.read_text(encoding="utf-8")
        self.assertIn('_IDENTITY_DIAGNOSTIC_NODEIDS = (', source)
        self.assertIn('_RUNTIME_SCALE_DIAGNOSTIC_NODEIDS = (', source)
        self.assertIn('packet_mode = suite in {_PACKET_DIAGNOSTIC_SUITE, _RUNTIME_SCALE_DIAGNOSTIC_SUITE}', source)
        self.assertIn('_RUNTIME_SCALE_DIAGNOSTIC_SUITE}', source)
        workflow = (Path(__file__).parents[2] / ".github/workflows/phase-d-validation.yml").read_text(
            encoding="utf-8")
        self.assertIn('"ci:validate-identities": "identity-diagnostic"', workflow)
        self.assertIn("Run identity migration L3 regression nodes in the isolated gate", workflow)
        self.assertIn("python tests/gates/gate.py subset-plan --domain xray --level L3", workflow)
        self.assertIn("tests/gates/gate.py run --plan \"$root/xray-l3-plan.json\"", workflow)
        self.assertIn("Retain sanitized identity qualification evidence", workflow)
        for artifact in (
            "subscription-l1-report.json", "subscription-l1-run.log", "migration-l3.xml",
            "migration-l3-report.json", "migration-l3.log", "xray-l3-plan.json",
            "xray-l3-report.json", "xray-l3-run.log",
        ):
            self.assertIn(f"phase-d-identities/{artifact}", workflow)
        evidence_start = workflow.index("Retain sanitized identity qualification evidence")
        evidence_end = workflow.find("      - name:", evidence_start)
        evidence_step = workflow[evidence_start:] if evidence_end < 0 else workflow[evidence_start:evidence_end]
        self.assertIn("if: always() && needs.select-stage.outputs.stage == 'identity-diagnostic'", evidence_step)
        self.assertNotIn("fwrouter-acceptance-", evidence_step)
        self.assertNotIn("phase-d-identities/**", evidence_step)
        exact_runner = (Path(__file__).parents[1] / "gates" / "run_exact_nodes.py").read_text(
            encoding="utf-8")
        self.assertIn("gate.redact_failure_output(output)", exact_runner)
        self.assertIn("gate.sanitize_junit_artifact(", exact_runner)
        self.assertIn(
            "tests/test_db_migrations.py::test_database_upgrade_cli_is_explicit_backed_up_and_idempotent",
            workflow,
        )
        self.assertIn(
            "tests/test_db_migrations.py::test_database_upgrade_cli_refuses_schema_24_structural_drift_without_backup",
            workflow,
        )
        self.assertIn(
            "tests/test_subscription_refresh_outcomes.py::test_staged_profile_verification_runs_outside_writer_guard",
            workflow,
        )
        self.assertIn(
            "tests/test_subscription_refresh_outcomes.py::test_staged_profile_failed_terminal_verification_keeps_own_core_revision_for_rollback",
            workflow,
        )
        self.assertIn("Run reviewed identity migration and two cold startup scale receipts", workflow)
        self.assertIn('run_launcher runtime-scale-diagnostic "$root/startup-attempt-1.log"', workflow)
        self.assertIn('run_launcher runtime-scale-diagnostic "$root/startup-attempt-2.log"', workflow)
        self.assertIn('output.mkdir(parents=True, exist_ok=False)', workflow)
        self.assertIn('"runtime-scale-metrics.json"', workflow)
        self.assertIn('"hard_budget_seconds": 45', workflow)
        self.assertIn('"engineering_margin_goal_seconds": 40', workflow)
        self.assertIn('"engineering_margin_status": "demonstrated" if margin_demonstrated else "not_demonstrated"', workflow)
        self.assertIn('"startup_margin_claim": "at_least_5_seconds" if margin_demonstrated else "none"', workflow)

    def test_dns53_junit_projection_is_compact_and_excludes_duplicate_or_raw_proofs(self):
        packet_path = Path(__file__).parents[1] / "application_acceptance" / "test_packet_dataplane.py"
        packet_source = packet_path.read_text(encoding="utf-8")
        packet_tree = ast.parse(packet_source, filename=str(packet_path))
        selected = [node for node in packet_tree.body if isinstance(node, ast.FunctionDef)
                    and node.name in {"_dns53_phase_junit_projection", "_record_phase_diagnostic"}]
        self.assertEqual({node.name for node in selected}, {
            "_dns53_phase_junit_projection", "_record_phase_diagnostic",
        })
        namespace = {
            "Any": __import__("typing").Any,
            "json": json,
            "ROUTER_WAN_IP": "198.18.240.1",
            "MAX_PHASE_DIAGNOSTIC_BYTES": 4096,
        }
        exec(compile(ast.Module(body=selected, type_ignores=[]), str(packet_path), "exec"), namespace)
        proof = {
            "phase": "native-dns53-under-global-vpn",
            "client_queries": {"answers": {
                "probe.vpn.test_udp": "198.51.100.77", "probe.vpn.test_tcp": "198.51.100.77",
                "probe.direct.test_udp": "203.0.113.53", "probe.direct.test_tcp": "203.0.113.53",
            }},
            "external_resolver_queries": {"outcomes": {"udp": "router_nxdomain", "tcp": "router_nxdomain"}},
            "resolved_user_flows": {"results": {
                "vpn": {"answer": "198.51.100.77", "tcp": True, "udp": True},
                "direct": {"answer": "203.0.113.53", "tcp": True, "udp": True},
            }},
            "dns_selected_endpoint_flow_delta": {
                "vpn_domain_tcp": 1, "vpn_domain_udp": 1,
                "direct_domain_tcp": 1, "direct_domain_udp": 1,
            },
            "dns_selected_endpoint_peer_proof": {
                "vpn_domain_tcp_local": True, "vpn_domain_udp_local": True,
                "direct_domain_tcp_local": True, "direct_domain_udp_local": True,
            },
            "resolved_flow_core_delta": {
                "vpn_tcp_classified": 2, "vpn_udp_classified": 1, "vpn_udp_handoff_packets": 1,
                "unused_secret": "RAW_COUNTER_SENTINEL",
            },
            "resolved_flow_direct_capture_delta": {"service_tcp_router_direct": 0},
            "native_dnsmasq": {
                "raw_native_payload": "RAW_NATIVE_SENTINEL" * 1000,
                "nft_probe_targets": [
                    {"domain": "vpn.test", "ok": True, "resolved": True, "materialized_count": 1,
                     "raw": "RAW_TARGET_SENTINEL"},
                    {"domain": "direct.test", "ok": True, "resolved": True, "materialized_count": 2},
                ],
            },
            "native_vpn_before": {"raw_runtime_snapshot": "RAW_RUNTIME_SENTINEL" * 1000},
            "native_vpn_after": {"raw_runtime_snapshot": "RAW_RUNTIME_SENTINEL" * 1000},
            "native_vpn_stable": True, "selection_stable": True, "api_status": 200,
            "capture_router_to_owned_upstream": {"udp_upstream": 2, "tcp_upstream": 1},
            "endpoint_observations_delta": {"udp": 2, "vpn_name": 1, "direct_name": 1},
            "endpoint_reply_delta": {"udp": 2, "vpn_name": 1, "direct_name": 1},
            "endpoint_peers": ["198.18.240.1"],
            "router_guard_delta": {
                "router_dns_vpn_direct_output": {"packets": 0},
                "router_dns_vpn_direct_forward": {"packets": 0},
                "router_dns_udp_input": {"packets": 1},
                "router_dns_tcp_input": {"packets": 1},
                "router_dns_udp_output": {"packets": 1},
                "raw": "RAW_GUARD_SENTINEL",
            },
            "selective_direct_priority": {
                "last_peer_classes": {
                    "vpn_domain_tcp": "service_vip", "vpn_domain_udp": "service_vip",
                    "direct_domain_tcp": "client", "direct_domain_udp": "client",
                },
            },
            "cleanup": {"http_status": 200, "process_reaped": True, "owned_paths_cleaned": True},
        }
        original_native = proof["native_dnsmasq"]["raw_native_payload"]
        projected = namespace["_dns53_phase_junit_projection"](proof)
        self.assertEqual(projected["resolved_flow_core_delta"]["vpn_tcp_classified"], 2)
        self.assertEqual(projected["native_nftset_probe_targets"][1]["materialized_count"], 2)
        self.assertEqual(proof["native_dnsmasq"]["raw_native_payload"], original_native)
        recorded = []
        namespace["_record_phase_diagnostic"](lambda name, value: recorded.append((name, value)), projected)
        self.assertEqual(recorded[0][0], "packet_phase_native_dns53_under_global_vpn")
        encoded = recorded[0][1]
        self.assertLessEqual(len(encoded.encode("ascii")), 4096)
        self.assertIn('"native_dnsmasq_reference":"dns53-native-daemon"', encoded)
        self.assertIn('"native_vpn_stable":true', encoded)
        self.assertIn('"selective_direct_priority_peer_classes":{"direct_domain_tcp":"client",'
                      '"direct_domain_udp":"client","vpn_domain_tcp":"service_vip",'
                      '"vpn_domain_udp":"service_vip"}', encoded)
        for secret in ("RAW_NATIVE_SENTINEL", "RAW_RUNTIME_SENTINEL", "RAW_TARGET_SENTINEL",
                       "RAW_COUNTER_SENTINEL", "RAW_GUARD_SENTINEL"):
            self.assertNotIn(secret, encoded)
        self.assertNotIn('"native_dnsmasq":', encoded)
        self.assertNotIn('"native_vpn_before":', encoded)
        self.assertNotIn('"endpoint_peers":', encoded)
        proof["selective_direct_priority"]["last_peer_classes"]["vpn_domain_tcp"] = "RAW_PEER_SENTINEL"
        proof["selective_direct_priority"]["last_peer_classes"]["direct_domain_udp"] = None
        redacted = namespace["_dns53_phase_junit_projection"](proof)
        self.assertEqual(redacted["selective_direct_priority_peer_classes"]["vpn_domain_tcp"], "other")
        self.assertEqual(redacted["selective_direct_priority_peer_classes"]["direct_domain_udp"], "missing")
        self.assertNotIn("RAW_PEER_SENTINEL", json.dumps(redacted))
        proof["resolved_flow_core_delta"].update({
            "vpn_tcp_classified": 1_000_001,
            "vpn_udp_classified": -1,
            "vpn_udp_handoff_packets": True,
        })
        proof["native_dnsmasq"]["nft_probe_targets"][0]["materialized_count"] = 1_000_001
        bounded = namespace["_dns53_phase_junit_projection"](proof)
        self.assertEqual(bounded["resolved_flow_core_delta"], {
            "vpn_tcp_classified": None, "vpn_udp_classified": None, "vpn_udp_handoff_packets": None,
        })
        self.assertIsNone(bounded["native_nftset_probe_targets"][0]["materialized_count"])

    def test_provider_reentry_checkpoint_junit_diagnostic_is_closed_and_redacted(self):
        packet_path = Path(__file__).parents[1] / "application_acceptance" / "test_packet_dataplane.py"
        packet_tree = ast.parse(packet_path.read_text(encoding="utf-8"), filename=str(packet_path))
        recorder = next(
            node for node in packet_tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "_record_phase_diagnostic"
        )
        namespace = {
            "Any": Any,
            "json": json,
            "MAX_PHASE_DIAGNOSTIC_BYTES": 4096,
        }
        exec(compile(ast.Module(body=[recorder], type_ignores=[]), str(packet_path), "exec"), namespace)
        diagnostic = {
            "phase": "provider-reentry-checkpoint-recovery",
            "checkpoint_recovered_before_reentry_apply": True,
            "inventory_verified": True,
            "inventory_digest_preserved": True,
            "identity_digest_preserved": True,
            "routing_mode": "vpn",
            "live_global_mode": "vpn",
        }
        recorded = []
        namespace["_record_phase_diagnostic"](
            lambda name, value: recorded.append((name, value)), diagnostic,
        )
        self.assertEqual(recorded, [("packet_phase_provider_reentry_checkpoint_recovery", json.dumps(
            diagnostic, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        ))])
        self.assertNotIn("uuid", recorded[0][1].lower())
        self.assertNotIn("token", recorded[0][1].lower())

        with self.assertRaisesRegex(AssertionError, "unknown field"):
            namespace["_record_phase_diagnostic"](
                lambda *_args: self.fail("rejected diagnostic must not be recorded"),
                {**diagnostic, "raw_secret": "must-not-leak"},
            )
        with self.assertRaisesRegex(AssertionError, "unknown routing mode"):
            namespace["_record_phase_diagnostic"](
                lambda *_args: self.fail("rejected diagnostic must not be recorded"),
                {**diagnostic, "routing_mode": "user-controlled-value"},
            )
        with self.assertRaisesRegex(AssertionError, "non-boolean"):
            namespace["_record_phase_diagnostic"](
                lambda *_args: self.fail("rejected diagnostic must not be recorded"),
                {**diagnostic, "inventory_verified": 1},
            )
        with self.assertRaisesRegex(AssertionError, "unknown fixed phase"):
            namespace["_record_phase_diagnostic"](
                lambda *_args: self.fail("rejected diagnostic must not be recorded"),
                {**diagnostic, "phase": "arbitrary-user-phase"},
            )

    def test_xray_generation_rollback_observer_is_bounded_and_installed(self):
        worker_source = WORKER_PATH.read_text(encoding="utf-8")
        worker_tree = ast.parse(worker_source, filename=str(WORKER_PATH))
        observer = next(
            node for node in worker_tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "_install_acceptance_xray_generation_rollback_observer"
        )
        observer_source = ast.get_source_segment(worker_source, observer) or ""
        for required in (
            "FWROUTER_ENVIRONMENT", "FWROUTER_APPLICATION_ACCEPTANCE_ROOT",
            "_restore_xray_generation_checkpoint", "_publish_xray_generation_rollback_mihomo_udp_receipt",
            "_publish_mihomo_udp_receipt_with_owner", "_native_udp_policy_proof",
            "write_mihomo_reconcile_fingerprint_state", "checkpoint_facts", "owner_callback_all_matched",
            "native_top_level_rules_match", "selector_membership_matches", "guarded_rules_match",
            '"selection_readback"', '"xray_runtime_readback"', '"mihomo_runtime_readback"',
            '"source_fingerprint_matches"', '"preimage_hash_matches"',
            '"selection_revision_matches"', '"runtime_incarnation_matches"',
            '"active_hash_matches"', '"input_fingerprint_present"',
            '"checkpoint_pre_restore_runtime_matches"',
            '"FWROUTER_ACCEPTANCE_XRAY_ROLLBACK "', 'limit = 16', 'max_bytes = 2048',
        ):
            self.assertIn(required, observer_source)
        for forbidden in ("error_message", "str(exc)", '"expected_mihomo_sha":',
                          '"expected_mihomo_runtime":', '"checkpoint_path":'):
            self.assertNotIn(forbidden, observer_source)
        self.assertIn('acceptance_profile.get("profile") == "hosted-kernel-packet"', worker_source)
        for required in ("_recover_checkpoint_from_current_projection", "selection_error_code"):
            self.assertIn(required, observer_source)
        checkpoint_helper = next(
            node for node in worker_tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "_read_bounded_generation_checkpoint_facts"
        )
        checkpoint_source = ast.get_source_segment(worker_source, checkpoint_helper) or ""
        for required in ("O_NOFOLLOW", "os.geteuid()", "1024 * 1024", "checkpoint_phase",
                         "checkpoint_selection_fence_matches", "checkpoint_inventory_binding_matches"):
            self.assertIn(required, checkpoint_source)
        install_calls = [node for node in ast.walk(worker_tree)
                         if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                         and node.func.id == "_install_acceptance_xray_generation_rollback_observer"]
        self.assertEqual(1, len(install_calls))
        recovery_controller = next(node for node in ast.walk(worker_tree)
                                   if isinstance(node, ast.FunctionDef)
                                   and node.name == "acceptance_provider_recovery_controller")
        self.assertLess(install_calls[0].lineno, recovery_controller.lineno)

    def test_packet_manual_apply_exception_observer_redacts_and_preserves_handler(self):
        worker_source = WORKER_PATH.read_text(encoding="utf-8")
        worker_tree = ast.parse(worker_source, filename=str(WORKER_PATH))
        helper = next(node for node in worker_tree.body
                      if isinstance(node, ast.FunctionDef)
                      and node.name == "_acceptance_job_exception_summary")
        observer = next(node for node in worker_tree.body
                        if isinstance(node, ast.FunctionDef)
                        and node.name == "_install_acceptance_apply_exception_observer")
        namespace = {"BaseException": BaseException, "Any": __import__("typing").Any, "Path": Path,
                     "re": __import__("re"), "traceback": __import__("traceback")}
        exec(compile(ast.Module(body=[helper], type_ignores=[]), str(WORKER_PATH), "exec"), namespace)
        fixture_namespace = {}
        exec(compile("def fixture_apply():\n    raise RuntimeError('token=secret /private/path')\n",
                     "/workspace/backend/fwrouter_api/services/dnsmasq.py", "exec"), fixture_namespace)
        try:
            fixture_namespace["fixture_apply"]()
        except RuntimeError as exc:
            summary = namespace["_acceptance_job_exception_summary"](exc)
        self.assertEqual(summary["exception_class"], "RuntimeError")
        self.assertEqual(summary["source_frames"][-1]["module"], "fwrouter_api.services.dnsmasq")
        self.assertEqual(summary["source_frames"][-1]["function"], "fixture_apply")
        self.assertIs(type(summary["source_frames"][-1]["line"]), int)
        self.assertLessEqual(len(summary["source_frames"]), 4)
        self.assertEqual(set(summary["source_frames"][-1]), {"module", "function", "line"})
        self.assertNotIn("secret", json.dumps(summary))
        self.assertNotIn("/private/path", json.dumps(summary))
        class TOKEN_Error(Exception):
            pass
        self.assertEqual(namespace["_acceptance_job_exception_summary"](TOKEN_Error("sensitive")), {
            "exception_class": "OtherException", "source_frames": "outside_fwrouter_api",
        })
        outside_namespace = {}
        exec(compile("def outside_apply():\n    raise RuntimeError('private')\n",
                     "/tmp/backend/fwrouter_api/services/dnsmasq.py", "exec"), outside_namespace)
        try:
            outside_namespace["outside_apply"]()
        except RuntimeError as exc:
            self.assertEqual(namespace["_acceptance_job_exception_summary"](exc)["source_frames"],
                             "outside_fwrouter_api")

        class FakeManager:
            def __init__(self, handler):
                self.handler = handler

            def _get_handler(self, _job_type):
                return self.handler

        expected_error = RuntimeError("token=secret /private/path")
        def raising_handler(job):
            job_input = job.get("input") if isinstance(job.get("input"), dict) else {}
            if (job_input.get("intent") != "apply_manual_rules"
                    or job.get("requested_by") != "packet-dns53"):
                return "unobserved"
            raise expected_error

        manager = FakeManager(raising_handler)
        observer_namespace = {
            "Any": __import__("typing").Any, "os": __import__("os"), "json": json,
            "_acceptance_job_exception_summary": namespace["_acceptance_job_exception_summary"],
        }
        original_import = __import__
        def acceptance_import(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "fwrouter_api.jobs.manager":
                return SimpleNamespace(get_default_job_manager=lambda: manager)
            return original_import(name, globals, locals, fromlist, level)
        observer_namespace["__builtins__"] = {**builtins.__dict__, "__import__": acceptance_import}
        exec(compile(ast.Module(body=[observer], type_ignores=[]), str(WORKER_PATH), "exec"),
             observer_namespace)
        with mock.patch.dict("os.environ", {
                "FWROUTER_ENVIRONMENT": "test",
                "FWROUTER_APPLICATION_ACCEPTANCE_ROOT": "/tmp/fwrouter-application-acceptance",
        }), mock.patch("sys.stdout", io.StringIO()) as captured:
            observer_namespace["_install_acceptance_apply_exception_observer"]()
            observed = manager._get_handler("apply_mutation")
            self.assertEqual(observed({"input": {"intent": "other"}, "requested_by": "packet-dns53"}),
                             "unobserved")
            self.assertEqual(observed({"input": {"intent": "apply_manual_rules"},
                                       "requested_by": "another-actor"}), "unobserved")
            self.assertEqual(captured.getvalue(), "")
            with self.assertRaises(RuntimeError) as raised:
                observed({"input": {"intent": "apply_manual_rules"}, "requested_by": "packet-dns53"})
            self.assertIs(raised.exception, expected_error)
            event_line = captured.getvalue().strip()
        event_json = event_line.removeprefix("FWROUTER_ACCEPTANCE_APPLY_JOB_EXCEPTION ")
        event = json.loads(event_json)
        self.assertEqual(event["exception_class"], "RuntimeError")
        self.assertNotIn("secret", event_line)
        self.assertNotIn("/private/path", event_line)
        self.assertNotIn("/workspace/backend", event_line)
        observer_source = ast.get_source_segment(worker_source, observer) or ""
        for required in ('"apply_mutation"', '"apply_manual_rules"', '"packet-dns53"',
                         '"FWROUTER_ACCEPTANCE_APPLY_JOB_EXCEPTION "',
                         '_acceptance_job_exception_summary(exc)', "raise"):
            self.assertIn(required, observer_source)
        for forbidden in ("str(exc)", "error_message", "filename", "job_id", "payload"):
            self.assertNotIn(forbidden, observer_source)
        install_calls = [node for node in ast.walk(worker_tree)
                         if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                         and node.func.id == "_install_acceptance_apply_exception_observer"]
        self.assertEqual(1, len(install_calls))

    def test_packet_live_mode_probe_observer_is_bounded_and_preserves_probe_result(self):
        worker_source = WORKER_PATH.read_text(encoding="utf-8")
        worker_tree = ast.parse(worker_source, filename=str(WORKER_PATH))
        observer = next(node for node in worker_tree.body
                        if isinstance(node, ast.FunctionDef)
                        and node.name == "_install_acceptance_live_mode_probe_observer")
        calls = []
        original_result = {
            "ok": True, "mode": "selective", "selective_default": "vpn",
            "error_code": None,
            "raw_chain": ('chain fwrouter_classify {\n'
                          ' ip daddr @vpn_ipv4 goto fwrouter_vpn comment '
                          '"scoped selective default vpn: SECRET_SENTINEL"\n'
                          ' goto fwrouter_vpn_full comment "selective default vpn"\n}'),
        }
        expected_error = RuntimeError("SECRET_SENTINEL")
        raise_probe = {"enabled": False}

        def original_probe():
            calls.append("called")
            if raise_probe["enabled"]:
                raise expected_error
            return original_result

        apply_service = SimpleNamespace(probe_live_global_mode=original_probe)
        original_import = __import__

        def acceptance_import(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "fwrouter_api.services":
                return SimpleNamespace(apply=apply_service)
            return original_import(name, globals, locals, fromlist, level)

        namespace = {
            "Any": __import__("typing").Any, "os": __import__("os"), "json": json,
            "__builtins__": {**builtins.__dict__, "__import__": acceptance_import},
        }
        exec(compile(ast.Module(body=[observer], type_ignores=[]), str(WORKER_PATH), "exec"), namespace)
        with mock.patch.dict("os.environ", {
                "FWROUTER_ENVIRONMENT": "test",
                "FWROUTER_APPLICATION_ACCEPTANCE_ROOT": "/tmp/fwrouter-application-acceptance",
        }), mock.patch("sys.stdout", io.StringIO()) as captured:
            namespace["_install_acceptance_live_mode_probe_observer"]()
            observed_probe = apply_service.probe_live_global_mode
            for _ in range(66):
                self.assertIs(observed_probe(), original_result)
        lines = captured.getvalue().splitlines()
        self.assertEqual(66, len(calls))
        self.assertEqual(64, len(lines))
        prefix = "FWROUTER_ACCEPTANCE_LIVE_MODE_PROBE "
        self.assertTrue(all(line.startswith(prefix) for line in lines))
        events = [json.loads(line.removeprefix(prefix)) for line in lines]
        self.assertEqual(list(range(1, 65)), [event["sequence"] for event in events])
        self.assertTrue(all(event == {
            "schema": "fwrouter-acceptance-live-mode-probe/v1", "sequence": event["sequence"],
            "ok": True, "mode": "selective", "selective_default": "vpn", "error_code": None,
            "comment_categories": {
                "classify_chain_present": True,
                "global_direct": False,
                "global_vpn": False,
                "global_selective_default_vpn": True,
                "global_selective_default_direct": False,
                "global_selective_degraded_default_direct": False,
                "scoped_selective_rules": True,
                "scoped_selective_default_vpn": True,
                "scoped_selective_default_direct": False,
                "scoped_selective_degraded_default_direct": False,
            },
        } for event in events))
        self.assertNotIn("SECRET_SENTINEL", captured.getvalue())
        self.assertNotIn("raw_chain", captured.getvalue())

        raise_probe["enabled"] = True
        with self.assertRaises(RuntimeError) as raised:
            observed_probe()
        self.assertIs(raised.exception, expected_error)
        observer_source = ast.get_source_segment(worker_source, observer) or ""
        for required in ("original_probe()", "return result",
                         '"FWROUTER_ACCEPTANCE_LIVE_MODE_PROBE "', 'state["count"] < 64'):
            self.assertIn(required, observer_source)
        for forbidden in ("json.dumps(result)", "str(result)", "repr(result)", "traceback",
                          "SECRET_SENTINEL"):
            self.assertNotIn(forbidden, observer_source)
        install_calls = [node for node in ast.walk(worker_tree)
                         if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                         and node.func.id == "_install_acceptance_live_mode_probe_observer"]
        self.assertEqual(1, len(install_calls))
        self.assertIn('if acceptance_profile.get("profile") == "hosted-kernel-packet":\n'
                      '        _install_acceptance_xray_generation_rollback_observer()\n'
                      '        _install_acceptance_xray_checkpoint_lifecycle_observer()\n'
                      '        _install_acceptance_apply_exception_observer()\n'
                      '        _install_acceptance_live_mode_probe_observer()', worker_source)

    def test_packet_preflight_observer_is_bounded_redacted_and_preserves_results(self):
        worker_source = WORKER_PATH.read_text(encoding="utf-8")
        worker_tree = ast.parse(worker_source, filename=str(WORKER_PATH))
        observer = next(node for node in worker_tree.body
                        if isinstance(node, ast.FunctionDef)
                        and node.name == "_install_acceptance_preflight_observer")
        calls = {"manifest": 0, "apply": 0}
        result = {
            "selective_rules": {"requires_vpn_runtime": True},
            "selective_vpn_ready": False,
            "selective_vpn_policy_ready": True,
            "selective_degraded": True,
            "selective_degraded_missing": ["vpn_external_path_not_verified", "PRIVATE_REASON_SENTINEL"],
            "missing_by_mode": {
                "selective": ["vpn_external_path_not_verified", "PRIVATE_REASON_SENTINEL"],
                "vpn": ["PRIVATE_REASON_SENTINEL"],
            },
            "vpn_external_path_verified": False,
            "vpn_policy_installed": True,
            "vpn_egress_target_present": False,
            "vpn_egress_ready": False,
        }
        fail = {"enabled": False}
        expected_error = RuntimeError("PREFLIGHT_ERROR_SENTINEL")

        def original_preflight(origin):
            def call(**kwargs):
                calls[origin] += 1
                if fail["enabled"] and origin == "apply":
                    raise expected_error
                return result
            return call

        manifest_service = SimpleNamespace(build_global_preflight=original_preflight("manifest"))
        apply_service = SimpleNamespace(build_global_preflight=original_preflight("apply"))
        original_import = __import__

        def acceptance_import(name, globals=None, locals=None, fromlist=(), level=0):
            if name == "fwrouter_api.services":
                return SimpleNamespace(apply=apply_service, routing_manifest=manifest_service)
            return original_import(name, globals, locals, fromlist, level)

        namespace = {
            "Any": __import__("typing").Any, "os": __import__("os"), "json": json,
            "__builtins__": {**builtins.__dict__, "__import__": acceptance_import},
        }
        exec(compile(ast.Module(body=[observer], type_ignores=[]), str(WORKER_PATH), "exec"), namespace)
        with mock.patch.dict("os.environ", {
                "FWROUTER_ENVIRONMENT": "test",
                "FWROUTER_APPLICATION_ACCEPTANCE_ROOT": "/tmp/fwrouter-application-acceptance",
        }), mock.patch("sys.stdout", io.StringIO()) as captured:
            namespace["_install_acceptance_preflight_observer"]()
            routing = {"applied_mode": "selective", "desired_mode": "vpn"}
            check_details = {"vpn_external_path_verified": True}
            for _ in range(33):
                self.assertIs(manifest_service.build_global_preflight(
                    routing=routing, check_details=check_details, require_runtime_verify=False), result)
                self.assertIs(apply_service.build_global_preflight(
                    routing=routing, check_details=check_details, require_runtime_verify=True), result)
            lines = captured.getvalue().splitlines()
            self.assertEqual(64, len(lines))
            prefix = "FWROUTER_ACCEPTANCE_PREFLIGHT "
            self.assertTrue(all(line.startswith(prefix) for line in lines))
            events = [json.loads(line.removeprefix(prefix)) for line in lines]
            self.assertEqual(list(range(1, 65)), [event["sequence"] for event in events])
            self.assertEqual({"manifest", "apply"}, {event["origin"] for event in events})
            expected_keys = {
                "schema", "sequence", "origin", "routing_mode", "require_runtime_verify",
                "selective_requires_vpn_runtime", "selective_vpn_ready", "selective_degraded",
                "selective_vpn_policy_ready",
                "selective_degraded_missing", "selective_degraded_unknown_missing_count",
                "selective_missing", "selective_unknown_missing_count", "vpn_missing",
                "vpn_unknown_missing_count", "check_external_path_verified",
                "preflight_external_path_verified", "vpn_policy_installed",
                "vpn_egress_target_present", "vpn_egress_ready",
            }
            self.assertTrue(all(set(event) == expected_keys for event in events))
            self.assertTrue(all(event["schema"] == "fwrouter-acceptance-preflight/v1"
                                and event["check_external_path_verified"] is True
                                and event["preflight_external_path_verified"] is False
                                and event["vpn_policy_installed"] is True
                                and event["vpn_egress_target_present"] is False
                                and event["selective_vpn_policy_ready"] is True
                                for event in events))
            self.assertTrue(all(event["routing_mode"] == "selective" for event in events))
            self.assertTrue(all(event["selective_degraded_unknown_missing_count"] == 1
                                and event["selective_unknown_missing_count"] == 1
                                and event["vpn_unknown_missing_count"] == 1
                                for event in events))
            self.assertTrue(all(event["selective_missing"] == ["vpn_external_path_not_verified"]
                                for event in events))
            self.assertNotIn("PRIVATE_REASON_SENTINEL", captured.getvalue())
            fail["enabled"] = True
            with self.assertRaises(RuntimeError) as raised:
                apply_service.build_global_preflight(routing=routing)
            self.assertIs(raised.exception, expected_error)
            self.assertEqual(64, len(captured.getvalue().splitlines()))
        self.assertEqual(33, calls["manifest"])
        self.assertEqual(34, calls["apply"])
        observer_source = ast.get_source_segment(worker_source, observer) or ""
        for required in ("original_preflight(*args, **kwargs)", "return original_result",
                         'state["count"] < 64', '"selective_degraded_unknown_missing_count"',
                         '"selective_unknown_missing_count"', '"vpn_unknown_missing_count"',
                         '"FWROUTER_ACCEPTANCE_PREFLIGHT "'):
            self.assertIn(required, observer_source)
        for forbidden in ("json.dumps(original_result)", "str(original_result)",
                          "traceback", "PRIVATE_REASON_SENTINEL"):
            self.assertNotIn(forbidden, observer_source)
        install_calls = [node for node in ast.walk(worker_tree)
                         if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                         and node.func.id == "_install_acceptance_preflight_observer"]
        self.assertEqual(1, len(install_calls))
        self.assertIn('        _install_acceptance_preflight_observer()', worker_source)

    def test_packet_dnsmasq_reconcile_diagnostic_is_bounded_and_preserves_result(self):
        worker_source = WORKER_PATH.read_text(encoding="utf-8")
        worker_tree = ast.parse(worker_source, filename=str(WORKER_PATH))
        observer = next(node for node in ast.walk(worker_tree)
                        if isinstance(node, ast.FunctionDef)
                        and node.name == "observed_reconcile_dnsmasq_rules")
        expected = {
            "ok": False,
            "error_code": "DNSMASQ_RESTART_FAILED",
            "restart_required": True,
            "restart_reason": "config_changed",
            "selective_status": {"ok": False, "message": "private nested text"},
            "message": "sensitive text must not escape",
            "stderr": "private detail",
        }
        original_result = expected.copy()
        namespace = {
            "Any": __import__("typing").Any,
            "current": {},
            "original_reconcile_dnsmasq_rules": lambda *args, **kwargs: original_result,
        }
        exec(compile(ast.Module(body=[observer], type_ignores=[]), str(WORKER_PATH), "exec"), namespace)
        observed_fn = namespace["observed_reconcile_dnsmasq_rules"]
        observed_result = observed_fn("fixed", force_restart_reason="test")
        self.assertIs(observed_result, original_result)
        self.assertEqual(namespace["current"]["reconcile_result"], {
            "ok": False,
            "error_code": "DNSMASQ_RESTART_FAILED",
            "restart_required": True,
            "restart_reason": "config_changed",
            "selective_status_ok": False,
            "artifact_read": None,
        })
        self.assertNotIn("message", namespace["current"]["reconcile_result"])
        self.assertNotIn("stderr", namespace["current"]["reconcile_result"])
        self.assertNotIn("private", json.dumps(namespace["current"]["reconcile_result"]))
        unknown_code_result = {"ok": True, "error_code": "TOKEN_SECRET", "restart_required": False,
                               "restart_reason": None, "selective_status": {"ok": True}}
        namespace["original_reconcile_dnsmasq_rules"] = lambda *_args, **_kwargs: unknown_code_result
        self.assertIs(observed_fn(), unknown_code_result)
        self.assertEqual(namespace["current"]["reconcile_result"], {
            "ok": True, "error_code": None, "restart_required": False,
            "restart_reason": None, "selective_status_ok": True, "artifact_read": None,
        })
        raised = ValueError("fixture failure")
        namespace["original_reconcile_dnsmasq_rules"] = lambda *_args, **_kwargs: (_ for _ in ()).throw(raised)
        with self.assertRaises(ValueError) as caught:
            observed_fn("failure-path")
        self.assertIs(caught.exception, raised)

        cache_observer = next(node for node in ast.walk(worker_tree)
                              if isinstance(node, ast.FunctionDef)
                              and node.name == "_packet_dns_effective_artifact_observation")
        reader_observer = next(node for node in ast.walk(worker_tree)
                               if isinstance(node, ast.FunctionDef)
                               and node.name == "observed_read_effective_rules_artifact")
        cache_key = "dataplane_global.effective_rules_artifact"
        cached_none = (time.monotonic() + 30, None)
        uncached_artifact = {"rules": [{"kind": "domain", "value": "vpn.test"}]}
        reader_path = Path("/fixture/state/generated/rules/effective-rules.json")
        settings = SimpleNamespace(paths=SimpleNamespace(
            generated_dir=Path("/fixture/state/generated"), state_dir=Path("/fixture/state")))
        fake_cache = ModuleType("fwrouter_api.services.live_probe_cache")
        fake_cache._CACHE = {cache_key: cached_none}
        fake_cache._INFLIGHT = {}
        fake_cache._CACHE_LOCK = threading.Lock()
        fake_data = ModuleType("fwrouter_api.services.dataplane_global")
        fake_data._read_effective_rules_artifact_uncached = lambda: uncached_artifact
        fake_rules = ModuleType("fwrouter_api.services.rules")
        fake_rules.get_rules_state = lambda: {"effective_json_path": str(reader_path)}
        fake_rules.get_manual_rules_texts = mock.Mock(side_effect=AssertionError("diagnostic export adds artifact decode"))
        fake_services = ModuleType("fwrouter_api.services")
        fake_services.__path__ = []
        fake_services.dataplane_global = fake_data
        fake_services.rules = fake_rules
        fake_config = ModuleType("fwrouter_api.core.config")
        fake_config.get_settings = lambda: settings
        fake_core = ModuleType("fwrouter_api.core")
        fake_core.__path__ = []
        fake_package = ModuleType("fwrouter_api")
        fake_package.__path__ = []
        fake_modules = {
            "fwrouter_api": fake_package, "fwrouter_api.core": fake_core,
            "fwrouter_api.core.config": fake_config, "fwrouter_api.services": fake_services,
            "fwrouter_api.services.dataplane_global": fake_data,
            "fwrouter_api.services.rules": fake_rules,
            "fwrouter_api.services.live_probe_cache": fake_cache,
        }
        cache_namespace = {"Any": __import__("typing").Any, "Path": Path, "time": __import__("time")}
        exec(compile(ast.Module(body=[cache_observer, reader_observer], type_ignores=[]),
                     str(WORKER_PATH), "exec"), cache_namespace)
        reader_calls = []
        cache_namespace["current"] = {}
        cache_namespace["original_read_effective_rules_artifact"] = (
            lambda *args, **kwargs: reader_calls.append((args, kwargs)) or None
        )
        with mock.patch.dict(sys.modules, fake_modules):
            reader_result = cache_namespace["observed_read_effective_rules_artifact"](fresh=True)
        self.assertIsNone(reader_result)
        self.assertEqual(reader_calls, [((), {"fresh": True})])
        cache_result = cache_namespace["current"]["artifact_read_observation"]
        self.assertEqual(cache_result, {
            "cache_entry_present": True, "cache_inflight_present": False,
            "cached_artifact_present": False, "uncached_artifact_present": True,
            "cache_entry_valid_at_reader": True,
            "cached_matches_uncached": False, "writer_path_matches_reader": True,
            "writer_path_exists": False, "reader_returned_artifact": False,
            "uncached_presence_matches_reader_result": False,
            "cached_presence_matches_reader_result": True,
        })
        self.assertIs(fake_cache._CACHE[cache_key], cached_none)
        fake_rules.get_manual_rules_texts.assert_not_called()

        reader_error = OSError("fixture reader error")
        reader_calls.clear()
        cache_namespace["original_read_effective_rules_artifact"] = (
            lambda *args, **kwargs: reader_calls.append((args, kwargs))
            or (_ for _ in ()).throw(reader_error)
        )
        with mock.patch.dict(sys.modules, fake_modules):
            with self.assertRaises(OSError) as caught_reader:
                cache_namespace["observed_read_effective_rules_artifact"](fresh=True)
        self.assertIs(caught_reader.exception, reader_error)
        self.assertEqual(reader_calls, [((), {"fresh": True})])
        self.assertIsNone(cache_namespace["current"]["artifact_read_observation"]["reader_returned_artifact"])

        missing_cache = ModuleType("fwrouter_api.services.live_probe_cache")
        missing_cache._CACHE = {}
        missing_cache._INFLIGHT = {}
        missing_cache._CACHE_LOCK = threading.Lock()
        fake_modules["fwrouter_api.services.live_probe_cache"] = missing_cache
        with mock.patch.dict(sys.modules, fake_modules):
            absent_result = cache_namespace["_packet_dns_effective_artifact_observation"]()
        self.assertFalse(absent_result["cache_entry_present"])
        self.assertIsNone(absent_result["cached_artifact_present"])
        self.assertIsNone(absent_result["cached_matches_uncached"])
        self.assertTrue(absent_result["uncached_artifact_present"])
        self.assertFalse(absent_result["cache_inflight_present"])

        packet_source = (Path(__file__).parents[1] / "application_acceptance" /
                         "test_packet_dataplane.py").read_text(encoding="utf-8")
        packet_tree = ast.parse(packet_source)
        packet_function = next(node for node in packet_tree.body
                               if isinstance(node, ast.FunctionDef)
                               and node.name == "_probe_dns53_policy_phase")
        packet_function_source = ast.get_source_segment(packet_source, packet_function) or ""
        for required in ('"phase": "dns53-native-daemon"', '"config_test_status"',
                         '"config_test_returncode"', '"config_test_error_code"',
                         '"reconcile_result": reconcile_result',
                         '"readiness_probe_timeout"', '"artifact_read"',
                         '"writer_path_matches_reader"', '"cached_matches_uncached"',
                         '"reader_returned_artifact"', '"cache_entry_valid_at_reader"',
                         '"cached_presence_matches_reader_result"'):
            self.assertIn(required, packet_function_source)
        worker_tree = ast.parse(worker_source, filename=str(WORKER_PATH))
        packet_status = next(node for node in ast.walk(worker_tree)
                             if isinstance(node, ast.FunctionDef) and node.name == "packet_dns53_status")
        packet_status_source = ast.get_source_segment(worker_source, packet_status) or ""
        self.assertEqual(packet_status_source.count("_inspect_dnsmasq_selective_status_uncached()"), 1)
        self.assertNotIn("inspect_dnsmasq_selective_status()", packet_status_source)
        self.assertEqual(packet_function_source.count("/__acceptance/dns53/status"), 1)
        status_read_try = next(
            node for node in ast.walk(packet_function)
            if isinstance(node, ast.Try)
            and any(isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                    and call.func.id == "http_json"
                    and any(isinstance(part, ast.Constant) and isinstance(part.value, str)
                            and "/__acceptance/dns53/status" in part.value
                            for arg in call.args for part in ast.walk(arg))
                    for call in ast.walk(node))
        )
        self.assertEqual(len(status_read_try.handlers), 1)
        status_handler = status_read_try.handlers[0]
        self.assertIsInstance(status_handler.type, ast.Tuple)
        self.assertEqual(
            [(item.id if isinstance(item, ast.Name) else ast.unparse(item))
             for item in status_handler.type.elts],
            ["TimeoutError", "OSError", "urllib.error.URLError"],
        )
        handler_calls = [node for node in ast.walk(status_handler) if isinstance(node, ast.Call)]
        handler_names = [node.func.id for node in handler_calls if isinstance(node.func, ast.Name)]
        self.assertIn("_role_json", handler_names)
        self.assertIn("_record_phase_diagnostic", handler_names)
        self.assertNotIn("http_json", handler_names)
        record_line = next(node.lineno for node in handler_calls
                           if isinstance(node.func, ast.Name) and node.func.id == "_record_phase_diagnostic")
        rethrow_lines = [node.lineno for node in ast.walk(status_handler)
                         if isinstance(node, ast.Raise) and node.exc is None]
        self.assertTrue(rethrow_lines)
        self.assertLess(record_line, min(rethrow_lines))
        self.assertIn('"status_read_error"', packet_function_source)
        self.assertIn('"endpoint_status_read_delta"', packet_function_source)
        self.assertIn('"endpoint_during_manual_apply"', packet_function_source)
        for required in ('"dns_capture_missing_codes"', '"dns_capture_rule_count_capped"',
                         '"dns_capture_rules_present"', '"nftset_probe_status"',
                         '"nftset_probe_target_count_capped"', '"nft_probe_targets"',
                         '"not_applicable"'):
            self.assertIn(required, packet_status_source)
        for required in ('endpoint_before_manual_apply =', 'endpoint_after_manual_apply =',
                         '"endpoint_during_manual_apply"', '"dns_capture_missing_codes"',
                         '"dns_capture_missing_unknown_count_capped"', '"dns_capture_missing_truncated"',
                         '"dns_capture_rules_present"', '"nft_probe_targets"',
                         '"nftset_probe_target_count_capped"', '"nftset_probe_targets_truncated"',
                         'endpoint_reply_delta["vpn_name"]'):
            self.assertIn(required, packet_function_source)
        self.assertNotIn("diagnostic_tail", packet_function_source)

    def test_packet_handoff_alias_is_qualified_local_and_removed(self):
        worker_source = WORKER_PATH.read_text(encoding="utf-8")
        worker_tree = ast.parse(worker_source, filename=str(WORKER_PATH))
        helper = next(node for node in worker_tree.body
                      if isinstance(node, ast.FunctionDef)
                      and node.name == "_install_packet_handoff_loopback_alias")
        helper_source = ast.get_source_segment(worker_source, helper) or ""
        for required in (
            "FWROUTER_ENVIRONMENT", "FWROUTER_APPLICATION_ACCEPTANCE_ROOT",
            'profile.get("profile") != "hosted-kernel-packet"',
            'network.get("role") != "router"', '"host_netns_inode_sha256"',
            '"172.18.0.1"', '"ip", "address", "add"', '"ip", "address", "del"',
            '"type") != "local"', 'route[0].get("dev") != "lo"',
            '"packet handoff alias remained after cleanup"',
            '"FWROUTER_ACCEPTANCE_HANDOFF_ALIAS "', '"install_verified"',
            '"cleanup_verified"', 'loopback_address_matches=True', 'local_route_matches=True',
            'loopback_address_absent=True',
        ):
            self.assertIn(required, helper_source)
        for forbidden in ('"ip", "route", "add"', '"ip", "route", "replace"',
                          '"host"', 'network_mode: host'):
            self.assertNotIn(forbidden, helper_source)
        main = next(node for node in worker_tree.body
                    if isinstance(node, ast.FunctionDef) and node.name == "main")
        main_source = ast.get_source_segment(worker_source, main) or ""
        self.assertIn('profile.get("profile") == "hosted-kernel-packet"', main_source)
        self.assertIn("remove_handoff_alias()", main_source)
        self.assertIn("finally:", main_source)

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
            "base_image_pull_seconds": 2.5,
            "base_image_pull_count": 1,
            "compose_build_seconds": 7.5,
            "built_image_bytes": 123456,
        }
        self.assertTrue(launcher.valid_image_measurements(measurements))
        legacy_measurements = {key: value for key, value in measurements.items()
                               if key not in {"base_image_pull_seconds", "base_image_pull_count"}}
        self.assertTrue(launcher.valid_image_measurements(legacy_measurements))
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

    def test_dependency_reference_requires_exact_digest_and_lock_binding(self):
        lock = json.loads(launcher.DEPENDENCY_LOCK_PATH.read_text(encoding="utf-8"))
        consumer = json.loads(launcher.DEPENDENCY_CONSUMER_LOCK_PATH.read_text(encoding="utf-8"))
        lock_digest = hashlib.sha256(json.dumps(
            lock, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")).hexdigest()
        env = {"FWROUTER_ACCEPTANCE_DEPENDENCY_IMAGE": consumer["image_ref"],
               "FWROUTER_ACCEPTANCE_DEPENDENCY_LOCK_SHA256": lock_digest}
        with tempfile.TemporaryDirectory(prefix="fwrouter-dependency-lock-contract-") as temporary:
            dependency_path = Path(temporary) / "dependency.json"
            consumer_path = Path(temporary) / "consumer.json"
            dependency_path.write_text(json.dumps(lock), encoding="utf-8")
            consumer_path.write_text(json.dumps(consumer), encoding="utf-8")
            with mock.patch.object(launcher, "DEPENDENCY_LOCK_PATH", dependency_path), \
                    mock.patch.object(launcher, "DEPENDENCY_CONSUMER_LOCK_PATH", consumer_path):
                self.assertEqual((consumer["image_ref"], lock_digest),
                                 launcher.validate_dependency_image_reference(env))
                invalid_digest = dict(consumer, image_ref="ghcr.io/coxf0rd/fwrouter-acceptance-dependencies:latest")
                consumer_path.write_text(json.dumps(invalid_digest), encoding="utf-8")
                invalid_env = dict(env, FWROUTER_ACCEPTANCE_DEPENDENCY_IMAGE=invalid_digest["image_ref"])
                with self.assertRaises(launcher.NotRun):
                    launcher.validate_dependency_image_reference(invalid_env)
                invalid_lock = dict(consumer, lock_sha256="0" * 64)
                consumer_path.write_text(json.dumps(invalid_lock), encoding="utf-8")
                with self.assertRaises(launcher.NotRun):
                    launcher.validate_dependency_image_reference(env)

    def test_dependency_base_image_requires_digest_arch_label_repo_and_size(self):
        image_ref = "ghcr.io/coxf0rd/fwrouter-acceptance-dependencies@sha256:" + "a" * 64
        lock_digest = "b" * 64
        image = {"Id": "sha256:" + "c" * 64, "RepoDigests": [image_ref],
                 "Os": "linux", "Architecture": "amd64", "Size": 1024,
                 "Config": {"Labels": {
                     "io.fwrouter.acceptance.dependencies.lock-sha256": lock_digest}}}
        qualified = launcher.validate_dependency_base_image([image], image_ref=image_ref,
                                                              lock_digest=lock_digest)
        self.assertEqual(image["Id"], qualified["image_id"])
        self.assertEqual(1024, qualified["size_bytes"])
        mutations = (
            ("Architecture", "arm64"),
            ("Os", "windows"),
            ("RepoDigests", ["ghcr.io/coxf0rd/fwrouter-acceptance-dependencies@sha256:" + "d" * 64]),
            ("Size", launcher.DOCKER_ROOT_MAX_DEPENDENCY_IMAGE_BYTES + 1),
        )
        for field, bad_value in mutations:
            invalid = json.loads(json.dumps(image))
            invalid[field] = bad_value
            with self.subTest(field=field), self.assertRaises(launcher.NotRun):
                launcher.validate_dependency_base_image([invalid], image_ref=image_ref,
                                                         lock_digest=lock_digest)
        wrong_label = json.loads(json.dumps(image))
        wrong_label["Config"]["Labels"]["io.fwrouter.acceptance.dependencies.lock-sha256"] = "0" * 64
        with self.assertRaises(launcher.NotRun):
            launcher.validate_dependency_base_image([wrong_label], image_ref=image_ref,
                                                     lock_digest=lock_digest)

    def test_docker_root_measurement_uses_info_path_and_backing_filesystem_capacity(self):
        with tempfile.TemporaryDirectory(prefix="fwrouter-docker-root-contract-") as temporary:
            root_path = Path(temporary)
            info = SimpleNamespace(returncode=0, stdout=(str(root_path) + "\n").encode("utf-8"), stderr=b"")
            fs = SimpleNamespace(f_blocks=100, f_frsize=4096, f_bavail=25, f_bfree=30)
            with mock.patch.object(launcher.subprocess, "run", return_value=info) as docker_info, \
                    mock.patch.object(launcher.os, "statvfs", return_value=fs):
                measured = launcher.measure_docker_root("/usr/bin/docker", cwd=root_path, env={"PATH": "/bin"})
            docker_info.assert_called_once_with(
                ["/usr/bin/docker", "info", "--format", "{{.DockerRootDir}}"], cwd=root_path,
                env={"PATH": "/bin"}, capture_output=True, timeout=10, check=False)
            self.assertEqual(100 * 4096, measured["total_bytes"])
            self.assertEqual(70 * 4096, measured["used_bytes"])
            self.assertEqual(25 * 4096, measured["available_bytes"])
            self.assertEqual(hashlib.sha256(str(root_path.resolve()).encode()).hexdigest(),
                             measured["path_sha256"])
            self.assertNotIn(str(root_path), json.dumps(measured))
            low_space = dict(measured, available_bytes=1)
            with mock.patch.object(launcher, "measure_docker_root", return_value=low_space):
                with self.assertRaises(launcher.NotRun):
                    launcher.record_docker_root_sample({}, "after_build", docker="docker", cwd=root_path,
                                                       env={}, minimum_free=2)

    def test_dependency_receipt_requires_all_phase_samples_and_does_not_claim_peak(self):
        sample = {"path_sha256": "a" * 64, "device_major": 8, "device_minor": 1,
                  "total_bytes": 1000, "used_bytes": 500, "available_bytes": 400}
        measurements = {
            "base_image_present_before_build": False, "base_image_inspect_seconds": 0.1,
            "base_image_pull_seconds": 1.0, "base_image_pull_count": 1,
            "compose_build_seconds": 2.0, "built_image_bytes": 100,
            "docker_root_budget_bytes": launcher.DOCKER_ROOT_COLD_PULL_MIN_FREE_BYTES,
            "docker_root_samples": {stage: dict(sample) for stage in
                                    ("before_pull", "after_pull", "after_build", "after_tests", "after_cleanup")},
        }
        self.assertTrue(launcher.valid_image_measurements(measurements, require_complete_docker_root=True))
        for stage in ("after_tests", "after_cleanup"):
            incomplete = json.loads(json.dumps(measurements))
            del incomplete["docker_root_samples"][stage]
            self.assertFalse(launcher.valid_image_measurements(incomplete, require_complete_docker_root=True))
            self.assertTrue(launcher.valid_image_measurements(incomplete))
        malformed = json.loads(json.dumps(measurements))
        malformed["docker_root_samples"]["after_tests"]["private_path"] = "/tmp/docker-root"
        self.assertFalse(launcher.valid_image_measurements(malformed))
        self.assertNotIn("peak", " ".join(measurements["docker_root_samples"]))

    def test_kernel_provider_cohort_remains_exact_without_startup_scale(self):
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
            "tests/application_acceptance/test_xray_generation.py::test_unverified_subscription_inventory_stays_pending_across_startup_until_native_retry",
        }
        self.assertEqual(12, len(expected))
        self.assertEqual(expected, set(launcher._PROVIDER_COHORT_NODEIDS))
        self.assertEqual(expected, launcher.expected_acceptance_nodeids("provider-cohort"))
        self.assertIn("provider-cohort", launcher._KERNEL_DATAPLANE_SUITES)
        gate_manifest = json.loads((Path(__file__).parents[1] / "gates/manifest.json").read_text())
        cohort_profiles = {row["nodeid"] for row in gate_manifest["node_execution_profiles"]
                           if row["suite"] == "provider-cohort"
                           and row["execution_profile"] == "hosted-kernel-dataplane"}
        self.assertEqual(expected, cohort_profiles)
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

    def test_success_diagnostics_retain_only_first_and_exact_emergency_reentry(self):
        conftest_path = APPLICATION_ACCEPTANCE_CONFTEST
        tree = ast.parse(conftest_path.read_text(encoding="utf-8"), filename=str(conftest_path))
        critical_definition = next(
            node for node in tree.body
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name)
                    and target.id == "_CRITICAL_SUCCESS_DIAGNOSTIC_NODEIDS"
                    for target in node.targets)
        )
        retention_helper = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "_retain_acceptance_diagnostic"
        )
        acceptance_stack = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "acceptance_stack"
        )
        self.assertEqual(1, sum(
            isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == "_retain_acceptance_diagnostic"
            for node in ast.walk(acceptance_stack)
        ))
        self.assertFalse(any(isinstance(node, (ast.Import, ast.ImportFrom))
                             for node in ast.walk(retention_helper)))
        namespace = {}
        exec(compile(ast.Module(body=[critical_definition, retention_helper], type_ignores=[]),
                     str(conftest_path), "exec"), namespace)
        retain = namespace["_retain_acceptance_diagnostic"]
        critical_nodeid = (
            "tests/application_acceptance/test_core_provider_mihomo.py::"
            "test_confirmed_provider_failure_applies_emergency_direct_and_failed_reentry_stays_direct"
        )
        self.assertEqual({critical_nodeid}, namespace["_CRITICAL_SUCCESS_DIAGNOSTIC_NODEIDS"])
        self.assertTrue(retain("first-success", failed=False, has_records=False))
        self.assertTrue(retain("failed-node", failed=True, has_records=True))
        self.assertTrue(retain(critical_nodeid, failed=False, has_records=True))
        self.assertFalse(retain("ordinary-later-success", failed=False, has_records=True))

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
        tool_schema = schema["properties"]["kernel_dataplane"]["properties"]["tool_package_versions"]
        self.assertEqual(set(launcher.KERNEL_PACKET_TOOL_PACKAGE_VERSIONS), set(tool_schema["required"]))
        self.assertFalse(tool_schema["additionalProperties"])

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
                "PortBindings": {}, "PidsLimit": 256, "Memory": 2560 * 1024**2,
                "MemorySwap": 2560 * 1024**2,
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
            for memory, memory_swap in (
                (2560 * 1024**2 + 1, 2560 * 1024**2 + 1),
                (0, 0),
                (2560 * 1024**2, 2561 * 1024**2),
                (2560 * 1024**2, 2560 * 1024**2 - 1),
            ):
                invalid_resources = json.loads(json.dumps(base))
                invalid_resources["HostConfig"]["Memory"] = memory
                invalid_resources["HostConfig"]["MemorySwap"] = memory_swap
                with self.subTest(memory=memory, memory_swap=memory_swap), self.assertRaises(launcher.NotRun):
                    launcher.validate_container_inspect(
                        invalid_resources, project="project", run_id="run", profile_path=profile,
                        image_id=base["Image"],
                    )
            missing_swap = json.loads(json.dumps(base))
            missing_swap["HostConfig"].pop("MemorySwap")
            with self.assertRaises(launcher.NotRun):
                launcher.validate_container_inspect(
                    missing_swap, project="project", run_id="run", profile_path=profile,
                    image_id=base["Image"],
                )
            wrong_handoff = json.loads(json.dumps(base))
            wrong_handoff["NetworkSettings"]["Networks"]["project_isolated"] = {
                "IPAddress": "172.18.0.9", "Gateway": launcher.GENERAL_NETWORK["gateway"]}
            with self.assertRaises(launcher.NotRun):
                launcher.validate_container_inspect(wrong_handoff, project="project", run_id="run",
                                                    profile_path=profile, image_id=base["Image"])
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
        }, "Internal": True, "EnableIPv6": False, "Options": {}, "Containers": {
            "other-id": {"IPv4Address": "172.18.0.5/24"}},
            "IPAM": {"Config": [{"Subnet": launcher.GENERAL_NETWORK["subnet"],
                                   "Gateway": launcher.GENERAL_NETWORK["gateway"]}]}}
        with self.assertRaises(launcher.NotRun):
            launcher.validate_network_inspect(network, project="project", run_id="run", container_id="owned-id")
        empty_network = json.loads(json.dumps(network))
        empty_network["Containers"] = {}
        launcher.validate_network_inspect(empty_network, project="project", run_id="run", container_id="owned-id")
        started_network = json.loads(json.dumps(empty_network))
        started_network["Containers"] = {"owned-id": {"IPv4Address": "172.18.0.1/24"}}
        launcher.validate_network_inspect(started_network, project="project", run_id="run",
                                          container_id="owned-id", require_container_attached=True)
        wrong_address = json.loads(json.dumps(started_network))
        wrong_address["Containers"]["owned-id"]["IPv4Address"] = "172.18.0.2/24"
        with self.assertRaises(launcher.NotRun):
            launcher.validate_network_inspect(wrong_address, project="project", run_id="run",
                                              container_id="owned-id", require_container_attached=True)
        wrong_ipam = json.loads(json.dumps(empty_network))
        wrong_ipam["IPAM"]["Config"][0]["Gateway"] = "172.18.0.1"
        with self.assertRaises(launcher.NotRun):
            launcher.validate_network_inspect(wrong_ipam, project="project", run_id="run")
        with self.assertRaises(launcher.NotRun):
            launcher.validate_network_inspect(empty_network, project="project", run_id="run",
                                              container_id="owned-id", require_container_attached=True)

    def test_compose_config_rejects_host_integration_and_extra_environment(self):
        run_id = "run-id"
        dependency_ref = "ghcr.io/coxf0rd/fwrouter-acceptance-dependencies@sha256:" + "a" * 64
        dockerfile = launcher.DEPENDENCY_APPLICATION_DOCKERFILE
        with tempfile.TemporaryDirectory(prefix="fwrouter-acceptance-compose-") as temp:
            profile = Path(temp) / "profile.json"
            profile.write_text("{}")
            service = {
                "cap_drop": ["ALL"], "read_only": True, "user": "10001:10001", "restart": "no",
                "init": True, "security_opt": ["no-new-privileges:true"], "memswap_limit": "2560m",
                "tmpfs": ["/tmp:rw,noexec,nosuid,nodev,size=512m,mode=1777"],
                "command": ["-c", "import signal; signal.pause()"],
                "labels": {launcher.OWNER_LABEL: launcher.OWNER_VALUE, launcher.RUN_LABEL: run_id},
                "build": {"dockerfile": dockerfile, "args": {"BASE_IMAGE": dependency_ref}},
                "networks": {"isolated": {"ipv4_address": launcher.GENERAL_NETWORK["address"]}},
                "volumes": [{"source": str(profile),
                    "target": "/run/fwrouter-acceptance/profile.json", "read_only": True}],
                "mem_limit": "2560m", "pids_limit": 256, "cpus": 2.0,
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
                "internal": True, "enable_ipv6": False,
                "ipam": {"config": [{"subnet": launcher.GENERAL_NETWORK["subnet"],
                                      "gateway": launcher.GENERAL_NETWORK["gateway"]}]},
                "labels": {launcher.OWNER_LABEL: launcher.OWNER_VALUE,
                                               launcher.RUN_LABEL: run_id}}}}
            launcher.validate_compose_config(config, run_id=run_id, profile_path=profile,
                                             base_image_ref=dependency_ref, application_dockerfile=dockerfile)
            for memory, memory_swap in (
                ("2561m", "2561m"), ("0", "0"), ("2560m", "2561m"), ("2560m", "")
            ):
                invalid_resources = json.loads(json.dumps(config))
                invalid_resources["services"]["application"]["mem_limit"] = memory
                invalid_resources["services"]["application"]["memswap_limit"] = memory_swap
                with self.subTest(memory=memory, memory_swap=memory_swap), self.assertRaises(launcher.NotRun):
                    launcher.validate_compose_config(
                        invalid_resources, run_id=run_id, profile_path=profile,
                        base_image_ref=dependency_ref, application_dockerfile=dockerfile,
                    )
            missing_limit = json.loads(json.dumps(config))
            missing_limit["services"]["application"].pop("mem_limit")
            with self.assertRaises(launcher.NotRun):
                launcher.validate_compose_config(
                    missing_limit, run_id=run_id, profile_path=profile,
                    base_image_ref=dependency_ref, application_dockerfile=dockerfile,
                )
            missing_swap = json.loads(json.dumps(config))
            missing_swap["services"]["application"].pop("memswap_limit")
            with self.assertRaises(launcher.NotRun):
                launcher.validate_compose_config(
                    missing_swap, run_id=run_id, profile_path=profile,
                    base_image_ref=dependency_ref, application_dockerfile=dockerfile,
                )
            wrong_base = json.loads(json.dumps(config))
            wrong_base["services"]["application"]["build"]["args"]["BASE_IMAGE"] = "python:3.12-slim"
            with self.assertRaises(launcher.NotRun):
                launcher.validate_compose_config(wrong_base, run_id=run_id, profile_path=profile,
                                                 base_image_ref=dependency_ref,
                                                 application_dockerfile=dockerfile)
            wrong_dockerfile = json.loads(json.dumps(config))
            wrong_dockerfile["services"]["application"]["build"]["dockerfile"] = launcher.LEGACY_APPLICATION_DOCKERFILE
            with self.assertRaises(launcher.NotRun):
                launcher.validate_compose_config(wrong_dockerfile, run_id=run_id, profile_path=profile,
                                                 base_image_ref=dependency_ref,
                                                 application_dockerfile=dockerfile)
            with self.assertRaises(TypeError):
                launcher.validate_compose_config(config, run_id=run_id, profile_path=profile)
            wrong_handoff = json.loads(json.dumps(config))
            wrong_handoff["services"]["application"]["networks"]["isolated"]["ipv4_address"] = "172.18.0.2"
            with self.assertRaises(launcher.NotRun):
                launcher.validate_compose_config(wrong_handoff, run_id=run_id, profile_path=profile,
                                                 base_image_ref=dependency_ref, application_dockerfile=dockerfile)
            wrong_subnet = json.loads(json.dumps(config))
            wrong_subnet["networks"]["isolated"]["ipam"]["config"][0]["subnet"] = "172.19.0.0/24"
            with self.assertRaises(launcher.NotRun):
                launcher.validate_compose_config(wrong_subnet, run_id=run_id, profile_path=profile,
                                                 base_image_ref=dependency_ref, application_dockerfile=dockerfile)
            polluted = json.loads(json.dumps(config))
            polluted["services"]["application"]["environment"]["HTTP_PROXY"] = "http://example.invalid"
            with self.assertRaises(launcher.NotRun):
                launcher.validate_compose_config(polluted, run_id=run_id, profile_path=profile,
                                                 base_image_ref=dependency_ref, application_dockerfile=dockerfile)
            host_port = json.loads(json.dumps(config))
            host_port["services"]["application"]["ports"] = ["10085:10085"]
            with self.assertRaises(launcher.NotRun):
                launcher.validate_compose_config(host_port, run_id=run_id, profile_path=profile,
                                                 base_image_ref=dependency_ref, application_dockerfile=dockerfile)
            kernel = json.loads(json.dumps(config))
            kernel["services"]["application"]["user"] = "0:0"
            kernel["services"]["application"]["cap_add"] = ["NET_ADMIN"]
            kernel["services"]["application"]["build"] = {"args": {
                "BASE_IMAGE": dependency_ref},
                "dockerfile": launcher.DEPENDENCY_APPLICATION_DOCKERFILE}
            launcher.validate_compose_config(kernel, run_id=run_id, profile_path=profile, kernel_preflight=True,
                                             base_image_ref=dependency_ref,
                                             application_dockerfile=launcher.DEPENDENCY_APPLICATION_DOCKERFILE)
            extra_capability = json.loads(json.dumps(kernel))
            extra_capability["services"]["application"]["cap_add"] = ["NET_ADMIN", "SYS_ADMIN"]
            with self.assertRaises(launcher.NotRun):
                launcher.validate_compose_config(extra_capability, run_id=run_id,
                                                 profile_path=profile, kernel_preflight=True,
                                                 base_image_ref=dependency_ref,
                                                 application_dockerfile=launcher.DEPENDENCY_APPLICATION_DOCKERFILE)
            legacy_kernel = json.loads(json.dumps(kernel))
            legacy_kernel["services"]["application"]["build"]["args"]["FWROUTER_KERNEL_PREFLIGHT"] = "1"
            with self.assertRaises(launcher.NotRun):
                launcher.validate_compose_config(legacy_kernel, run_id=run_id, profile_path=profile,
                                                 kernel_preflight=True, base_image_ref=dependency_ref,
                                                 application_dockerfile=launcher.DEPENDENCY_APPLICATION_DOCKERFILE)
            user_downgrade = json.loads(json.dumps(kernel))
            user_downgrade["services"]["application"]["user"] = "10001:10001"
            with self.assertRaises(launcher.NotRun):
                launcher.validate_compose_config(user_downgrade, run_id=run_id,
                                                 profile_path=profile, kernel_preflight=True,
                                                 base_image_ref=dependency_ref,
                                                 application_dockerfile=launcher.DEPENDENCY_APPLICATION_DOCKERFILE)

    def test_general_handoff_host_preflight_refuses_overlapping_routes(self):
        success = SimpleNamespace(returncode=0, stdout=b'[{"dst":"10.0.0.0/8"}]', stderr=b"")
        with mock.patch.object(launcher.shutil, "which", return_value="/usr/sbin/ip"), \
                mock.patch.object(launcher.subprocess, "run", return_value=success):
            self.assertEqual({"subnet": "172.18.0.0/24", "overlapping_route_count": 0},
                             launcher.general_network_host_preflight())
        overlap = SimpleNamespace(returncode=0, stdout=b'[{"dst":"172.18.0.0/16"}]', stderr=b"")
        with mock.patch.object(launcher.shutil, "which", return_value="/usr/sbin/ip"), \
                mock.patch.object(launcher.subprocess, "run", return_value=overlap):
            with self.assertRaises(launcher.NotRun):
                launcher.general_network_host_preflight()
        with mock.patch.object(launcher.shutil, "which", return_value=None):
            with self.assertRaises(launcher.NotRun):
                launcher.general_network_host_preflight()

    def test_profile_schema_has_no_unbound_authorization_or_port_fields(self):
        schema = json.loads((LAUNCHER_PATH.parent / "profile_schema.json").read_text())
        required = set(schema["required"])
        self.assertEqual({"schema", "profile", "source_revision", "plan_digest", "xray", "mihomo", "chromium",
                          "playwright_python", "baseline_xray_config_sha256", "ui_tree_sha256", "suite_nonce",
                          "mihomo_handoff"}, required)
        self.assertNotIn("loopback_ports", schema["properties"])
        self.assertNotIn("authorization", schema["properties"])
        self.assertEqual(launcher.GENERAL_NETWORK, acceptance_profile.MIHOMO_HANDOFF)
        self.assertEqual({"address", "subnet", "gateway"},
                         set(schema["properties"]["mihomo_handoff"]["required"]))
        for key, value in acceptance_profile.MIHOMO_HANDOFF.items():
            self.assertEqual(value, schema["properties"]["mihomo_handoff"]["properties"][key]["const"])

    def test_kernel_profile_and_source_allowlist_are_exact_and_tool_pins_match_lock(self):
        schema = json.loads((LAUNCHER_PATH.parent / "kernel_profile_schema.json").read_text(encoding="utf-8"))
        required = set(schema["required"])
        self.assertIn("kernel_preflight", required)
        kernel = schema["properties"]["kernel_preflight"]
        self.assertFalse(kernel["additionalProperties"])
        lock = json.loads((LAUNCHER_PATH.parent / "dependency-image.lock.json").read_text(encoding="utf-8"))
        self.assertEqual(launcher.KERNEL_PACKET_TOOL_PACKAGE_VERSIONS,
                         {item.split("=", 1)[0]: item.split("=", 1)[1]
                          for item in lock["apt_packages"] if "=" in item})
        self.assertEqual(set(launcher.KERNEL_PACKET_TOOL_PACKAGE_VERSIONS),
                         set(kernel["properties"]["tool_package_versions"]["required"]))
        scripts = kernel["properties"]["dataplane_scripts"]["required"]
        self.assertEqual(set(launcher.KERNEL_SCRIPT_SOURCES), set(scripts))
        for path in launcher.KERNEL_SCRIPT_SOURCES:
            self.assertTrue(launcher._safe_source(path))
        self.assertFalse(launcher._safe_source("host/libexec/fwrouter/other-script.sh"))
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


    def test_worker_lifespan_releases_native_fixtures_before_completion_marker(self):
        import asyncio
        from contextlib import asynccontextmanager

        source = (launcher.ROOT / "tests/application_acceptance/worker.py").read_text()
        tree = ast.parse(source)
        observed = next(node for node in ast.walk(tree)
                        if isinstance(node, ast.AsyncFunctionDef) and node.name == "observed_lifespan")
        for fail in (False, True):
            events = []

            @asynccontextmanager
            async def original(_application):
                try:
                    yield "actual-lifespan-state"
                finally:
                    events.append("application_cleanup")

            namespace = {
                "asynccontextmanager": asynccontextmanager, "original_lifespan": original,
                "threading": SimpleNamespace(enumerate=lambda: []),
                "cleanup_native_fixtures": lambda: events.append("native_cleanup"),
                "print": lambda message, **_kwargs: events.append(message),
            }
            exec(compile(ast.Module(body=[observed], type_ignores=[]), "worker-lifespan-contract", "exec"), namespace)

            async def exercise():
                async with namespace["observed_lifespan"](object()) as state:
                    self.assertEqual("actual-lifespan-state", state)
                    if fail:
                        raise ValueError("intentional application exception")

            if fail:
                with self.assertRaisesRegex(ValueError, "intentional application exception"):
                    asyncio.run(exercise())
                self.assertEqual(["application_cleanup", "native_cleanup"], events)
            else:
                asyncio.run(exercise())
                self.assertEqual(["application_cleanup", "native_cleanup",
                                  "FWROUTER_ACCEPTANCE_LIFESPAN_SHUTDOWN_COMPLETE"], events)


class BindingOriginDiagnosticContract(unittest.TestCase):
    def test_bounded_summary_never_exports_identity_or_arbitrary_metadata(self):
        from application_acceptance.xray_support import binding_identity_summary
        expected = {("owned-id", "owned-email")}
        rows = [{"client_uuid": "owned-id", "client_email": "owned-email"}] * 2
        rows += [{"subject_id": f"private-subject-{i}", "client_uuid": f"private-uuid-{i}",
                  "client_email": f"private-email-{i}"} for i in range(6)]
        result = binding_identity_summary(rows, expected, lambda key: {
            "subject_type": "private-type", "subject_role": "private-role", "is_active": 1,
            "metadata_json": "private-token"})
        self.assertEqual(8, result["row_count"])
        self.assertEqual(1, result["duplicate_count"])
        self.assertEqual(6, result["unexpected_count"])
        self.assertEqual(4, len(result["unexpected_origins"]))
        self.assertNotIn("private", json.dumps(result))
        self.assertEqual(0, result["missing_count"])


class ScaleInboundOwnershipContract(unittest.TestCase):
    def helper(self):
        source = Path(__file__).parents[1] / "application_acceptance" / "test_runtime_scale.py"
        tree = ast.parse(source.read_text())
        node = next(item for item in tree.body if isinstance(item, ast.FunctionDef)
                    and item.name == "_scale_xray_config_with_managed_clients")
        namespace = {"json": json, "Any": object}
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), "exec"), namespace)
        return namespace[node.name]

    def test_scale_owns_exact_clients_and_preserves_api(self):
        fixture = Path(__file__).parents[1] / "application_acceptance" / "fixtures" / "xray.initial.json"
        original = json.loads(fixture.read_text())
        before = json.dumps(original, sort_keys=True)
        clients = [{"id": "test-owned-client", "email": "owned@example.invalid"}]
        result = self.helper()(original, clients)
        self.assertEqual(before, json.dumps(original, sort_keys=True))
        self.assertEqual(["fwrouter-api", "vless-ws"], [row["tag"] for row in result["inbounds"]])
        self.assertEqual(clients, result["inbounds"][1]["settings"]["clients"])
        self.assertEqual(original["inbounds"][0], result["inbounds"][0])
        self.assertFalse(any(row.get("inboundTag") == ["acceptance-provider-upstream"]
                             for row in result["routing"]["rules"]))
        clients[0]["id"] = "mutated-input"
        self.assertEqual("test-owned-client", result["inbounds"][1]["settings"]["clients"][0]["id"])

    def test_unexpected_client_inbound_is_refused(self):
        fixture = Path(__file__).parents[1] / "application_acceptance" / "fixtures" / "xray.initial.json"
        config = json.loads(fixture.read_text())
        config["inbounds"].append({"tag": "unexpected", "settings": {"clients": [{"id": "unowned"}]}})
        with self.assertRaisesRegex(AssertionError, "unexpected client-bearing"):
            self.helper()(config, [])


class RuntimeScaleFailureDiagnosticsContract(unittest.TestCase):
    def setUp(self):
        self.source = Path(__file__).parents[1] / "application_acceptance" / "test_runtime_scale.py"
        self.tree = ast.parse(self.source.read_text(encoding="utf-8"))
        self.function = next(
            item for item in self.tree.body
            if isinstance(item, ast.FunctionDef)
            and item.name == "_read_worker_resource_snapshot"
        )

    def test_disappeared_worker_uses_parent_cgroup_and_keeps_only_bounded_fields(self):
        namespace = {
            "Any": Any,
            "Path": Path,
            "os": SimpleNamespace(getpid=lambda: 99),
            "_read_rss_kib": lambda *_args, **_kwargs: None,
            "_read_process_cpu_seconds": lambda _pid: None,
        }
        exec(compile(ast.Module(body=[self.function], type_ignores=[]), str(self.source), "exec"), namespace)

        original_read_text = Path.read_text
        original_exists = Path.exists

        def read_text(path, *, encoding=None):
            value = str(path)
            if value == "/proc/99/cgroup":
                return "0::/private/credential-shaped-path\n"
            if value.endswith("/memory.current"):
                return "123456\n"
            if value.endswith("/memory.peak"):
                return "234567\n"
            if value.endswith("/memory.max"):
                return "1048576\n"
            if value.endswith("/memory.events"):
                return "low 0\nhigh 0\nmax 4\noom 2\noom_kill 1\n"
            if value.startswith("/proc/42/"):
                raise FileNotFoundError(value)
            return original_read_text(path, encoding=encoding)

        def exists(path):
            if str(path) == "/proc/42/cgroup":
                return False
            return original_exists(path)

        worker = SimpleNamespace(pid=42, poll=lambda: -9)
        with mock.patch.object(Path, "read_text", read_text), mock.patch.object(Path, "exists", exists):
            result = namespace["_read_worker_resource_snapshot"](worker)

        self.assertEqual(-9, result["worker_returncode"])
        self.assertEqual(1, result["cgroup"]["events"]["oom_kill"])
        self.assertEqual(123456, result["cgroup"]["current"])
        self.assertNotIn("credential-shaped-path", json.dumps(result))
        self.assertEqual({
            "worker_pid", "worker_returncode", "process_rss_kib", "process_hwm_kib",
            "process_cpu_seconds", "process_threads", "cgroup",
        }, set(result))

    def test_failed_wave_redacts_exception_text_and_reraises_same_exception(self):
        test_function = next(
            item for item in self.tree.body
            if isinstance(item, ast.FunctionDef)
            and item.name == "test_large_mihomo_health_readback_stays_bounded_under_concurrent_api_load"
        )
        handler = next(
            node for node in ast.walk(test_function)
            if isinstance(node, ast.Try)
            and any(isinstance(item, ast.ExceptHandler)
                    and isinstance(item.type, ast.Name) and item.type.id == "BaseException"
                    and item.name == "exc" for item in node.handlers)
            and any(isinstance(item, ast.Assign)
                    and isinstance(item.value, ast.Call)
                    and isinstance(item.value.func, ast.Name)
                    and item.value.func.id == "_run_six_client_wave" for item in node.body)
        )
        sentinel = RuntimeError("private exception detail " + ("x" * 10000))
        observation: dict[str, Any] = {}

        def fail_wave(_api):
            raise sentinel

        namespace = {
            "wave_observation": observation,
            "active_worker": object(),
            "api": "http://127.0.0.1:5000",
            "_run_six_client_wave": fail_wave,
            "_read_worker_resource_snapshot": lambda _worker: {"worker_returncode": -9},
            "BaseException": BaseException,
            "type": type,
        }
        with self.assertRaises(RuntimeError) as raised:
            exec(compile(ast.Module(body=[handler], type_ignores=[]), str(self.source), "exec"), namespace)

        self.assertIs(sentinel, raised.exception)
        self.assertEqual({"type": "RuntimeError"}, observation["failure"])
        self.assertNotIn("private exception detail", json.dumps(observation))
        self.assertEqual({"worker_returncode": -9}, observation["after"])


class StartupResourceObservationContract(unittest.TestCase):
    def setUp(self):
        self.source = Path(__file__).parents[1] / "application_acceptance" / "test_runtime_scale.py"
        self.tree = ast.parse(self.source.read_text(encoding="utf-8"))

    def _helpers(self):
        names = {
            "_STARTUP_CGROUP_MEMORY_EVENTS", "_STARTUP_CGROUP_CPU_FIELDS", "_STARTUP_CGROUP_IO_FIELDS",
            "_STARTUP_CGROUP_PRESSURE_FILES", "_STARTUP_PROC_STATUS_FIELDS", "_STARTUP_PROC_IO_FIELDS",
            "_STARTUP_RESOURCE_SAMPLE_INTERVAL_SECONDS", "_STARTUP_RESOURCE_SAMPLE_CAPACITY",
            "_read_bounded_ascii", "_parse_startup_keyed_counters", "_read_startup_worker_resource_snapshot",
        }
        body = []
        for item in self.tree.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name in names:
                body.append(item)
            elif isinstance(item, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id in names for target in item.targets
            ):
                body.append(item)
        namespace: dict[str, Any] = {"Path": Path, "os": os, "re": re, "Any": Any}
        exec(compile(ast.Module(body=body, type_ignores=[]), str(self.source), "exec"), namespace)
        return namespace

    def test_owned_process_cgroup_snapshot_aggregates_allowlisted_scalars_only(self):
        helpers = self._helpers()
        with tempfile.TemporaryDirectory(prefix="fwrouter-startup-resource-synthetic-") as temp:
            root = Path(temp)
            pid = 123
            proc = root / "proc" / str(pid)
            cgroot = root / "cgroup"
            cg = cgroot / "owned-container"
            proc.mkdir(parents=True)
            cg.mkdir(parents=True)
            stat_fields = ["S"] + ["0"] * 21
            stat_fields[7], stat_fields[9], stat_fields[11], stat_fields[12] = "12", "3", "70", "30"
            (proc / "stat").write_text("123 (synthetic api worker) " + " ".join(stat_fields), encoding="ascii")
            (proc / "status").write_text(
                "Name:\tsynthetic\nVmRSS:\t2048 kB\nVmHWM:\t4096 kB\nThreads:\t7\n", encoding="ascii",
            )
            (proc / "io").write_text("read_bytes: 1000\nwrite_bytes: 2000\nrchar: 9\n", encoding="ascii")
            (proc / "schedstat").write_text("101 202 3\n", encoding="ascii")
            (proc / "cgroup").write_text("0::/owned-container\n", encoding="ascii")
            (cg / "cpu.stat").write_text(
                "usage_usec 5000\nuser_usec 3000\nsystem_usec 2000\nnr_periods 10\n"
                "nr_throttled 2\nthrottled_usec 1500000\nignored_secret 999\n", encoding="ascii",
            )
            (cg / "cpu.max").write_text("150000 100000\n", encoding="ascii")
            (cg / "memory.current").write_text("123456\n", encoding="ascii")
            (cg / "memory.peak").write_text("234567\n", encoding="ascii")
            (cg / "memory.max").write_text("max\n", encoding="ascii")
            (cg / "memory.events").write_text("low 0\nmax 4\noom 1\noom_kill 1\nsecret 99\n", encoding="ascii")
            (cg / "cpu.pressure").write_text("some avg10=0.10 avg60=0.20 total=1234\nfull avg10=0.01 total=34\n", encoding="ascii")
            (cg / "memory.pressure").write_text("some avg10=0.00 total=456\n", encoding="ascii")
            (cg / "io.pressure").write_text("some avg10=0.00 total=789\n", encoding="ascii")
            (cg / "io.stat").write_text(
                "8:0 rbytes=10 wbytes=20 rios=1 wios=2 dbytes=3 dios=4\n"
                "8:16 rbytes=5 wbytes=7 rios=3 wios=4\n", encoding="ascii",
            )

            result = helpers["_read_startup_worker_resource_snapshot"](
                pid, proc_root=root / "proc", cgroup_root=cgroot,
            )

        self.assertEqual("owned_acceptance_container", result["cgroup_scope"])
        self.assertEqual(2048, result["process_rss_kib"])
        self.assertEqual(4096, result["process_hwm_kib"])
        self.assertEqual(7, result["process_threads"])
        self.assertEqual(12, result["minor_faults"])
        self.assertEqual(3, result["major_faults"])
        self.assertEqual(1000, result["process_io_bytes_read"])
        self.assertEqual(2000, result["process_io_bytes_written"])
        self.assertEqual("pid_leader_thread_only", result["main_thread_sched_scope"])
        self.assertEqual(3, result["main_thread_sched_runqueue_count"])
        self.assertEqual(1.5, result["cgroup"]["cpu"]["throttled_seconds"])
        self.assertEqual({"quota_us": 150000, "period_us": 100000}, result["cgroup"]["cpu_max"])
        self.assertEqual({"current_bytes": 123456, "peak_bytes": 234567, "max_bytes": "max"},
                         result["cgroup"]["memory"])
        self.assertEqual(1, result["cgroup"]["memory_events"]["oom_kill"])
        self.assertEqual({"cpu_some_total_us": 1234, "cpu_full_total_us": 34,
                          "memory_some_total_us": 456, "io_some_total_us": 789},
                         result["cgroup"]["psi_total_us"])
        self.assertEqual(15, result["cgroup"]["io_bytes_read"])
        self.assertEqual(27, result["cgroup"]["io_bytes_written"])
        self.assertEqual(4, result["cgroup"]["io_read_ops"])
        self.assertEqual(6, result["cgroup"]["io_write_ops"])
        self.assertNotIn("8:0", json.dumps(result))
        self.assertNotIn("owned-container", json.dumps(result))
        self.assertNotIn("avg10", json.dumps(result))
        self.assertNotIn("ignored_secret", json.dumps(result))

    def test_missing_or_malformed_owned_files_remain_unknown_without_host_fallback(self):
        helpers = self._helpers()
        with tempfile.TemporaryDirectory(prefix="fwrouter-startup-resource-missing-") as temp:
            root = Path(temp)
            proc = root / "proc" / "124"
            cgroot = root / "cgroup"
            (cgroot / "container").mkdir(parents=True)
            proc.mkdir(parents=True)
            (proc / "cgroup").write_text("0::/container\n", encoding="ascii")
            (proc / "stat").write_text("broken (record) not-numeric\n", encoding="ascii")
            (proc / "status").write_text("VmRSS: invalid kB\nVmHWM: 99 bytes\nThreads: nope\n", encoding="ascii")
            (proc / "io").write_text("read_bytes: NaN\nwrite_bytes: -1\nsecret_path: /never/emit\n", encoding="ascii")
            missing_files = helpers["_read_startup_worker_resource_snapshot"](
                124, proc_root=root / "proc", cgroup_root=cgroot,
            )
            (proc / "cgroup").write_text("0::/../../credential-shaped-path\n", encoding="ascii")
            result = helpers["_read_startup_worker_resource_snapshot"](
                124, proc_root=root / "proc", cgroup_root=cgroot,
            )
            partial = cgroot / "partial"
            partial.mkdir()
            (partial / "io.stat").write_text("8:0 wbytes=12 dbytes=4\n", encoding="ascii")
            (proc / "cgroup").write_text("0::/partial\n", encoding="ascii")
            partial_io = helpers["_read_startup_worker_resource_snapshot"](
                124, proc_root=root / "proc", cgroup_root=cgroot,
            )
        self.assertIsNone(result["process_cpu_seconds"])
        self.assertIsNone(result["process_rss_kib"])
        self.assertIsNone(result["process_hwm_kib"])
        self.assertIsNone(result["process_io_bytes_read"])
        self.assertIsNone(result["cgroup"])
        self.assertIsNone(missing_files["cgroup"]["cpu"])
        self.assertIsNone(missing_files["cgroup"]["cpu_max"])
        self.assertEqual({"current_bytes": None, "peak_bytes": None, "max_bytes": None},
                         missing_files["cgroup"]["memory"])
        self.assertIsNone(missing_files["cgroup"]["psi_total_us"])
        self.assertIsNone(missing_files["cgroup"]["io_bytes_read"])
        self.assertIsNone(partial_io["cgroup"]["io_bytes_read"])
        self.assertEqual(12, partial_io["cgroup"]["io_bytes_written"])
        self.assertIsNone(partial_io["cgroup"]["io_read_ops"])
        self.assertIsNone(partial_io["cgroup"]["io_write_ops"])
        self.assertEqual("owned_acceptance_container", result["cgroup_scope"])
        self.assertNotIn("credential-shaped-path", json.dumps(result))
        self.assertNotIn("/never/emit", json.dumps(result))
        with self.assertRaises(ValueError):
            helpers["_read_startup_worker_resource_snapshot"](1, proc_root=root / "proc", cgroup_root=cgroot)

    def test_startup_sampler_freezes_retains_only_last_bounded_ring_and_exports_probe_cost(self):
        helpers = self._helpers()
        sampler_node = next(item for item in self.tree.body
                            if isinstance(item, ast.ClassDef) and item.name == "_RssSampler")
        namespace = {
            **helpers, "Any": Any, "Path": Path, "time": time, "threading": threading,
            "deque": __import__("collections").deque,
            "_read_rss_kib": lambda *_args, **_kwargs: None,
            "_read_process_cpu_seconds": lambda _pid: None,
            "_read_startup_worker_resource_snapshot": lambda pid: {
                "process_cpu_seconds": 1.0, "cgroup_scope": "owned_acceptance_container", "cgroup": None,
            },
        }
        exec(compile(ast.Module(body=[sampler_node], type_ignores=[]), str(self.source), "exec"), namespace)
        sampler = namespace["_RssSampler"](125)
        sampler._startup_started = time.monotonic()
        sampler._startup_next_sample = sampler._startup_started
        for _ in range(70):
            sampler._sample_startup_resources_if_due(force=True)
        sampler.freeze_startup_resources(trigger="start_worker_raised")
        frozen = sampler.startup_resource_observations()
        retained = frozen["samples"]
        self.assertEqual("frozen", frozen["status"])
        self.assertEqual("start_worker_raised", frozen["freeze_trigger"])
        self.assertEqual(64, frozen["sample_capacity"])
        self.assertEqual(64, frozen["sample_count_retained"])
        self.assertEqual(71, frozen["probe_read_cost"]["sample_count"])
        self.assertTrue(all(item["cgroup_scope"] == "owned_acceptance_container" for item in retained))
        self.assertFalse(sampler._startup_active)
        frozen_elapsed = frozen["startup_elapsed_seconds"]
        sample_count = frozen["probe_read_cost"]["sample_count"]
        time.sleep(0.02)
        sampler._sample_startup_resources_if_due(force=True)
        after_wait = sampler.startup_resource_observations()
        self.assertEqual(frozen_elapsed, after_wait["startup_elapsed_seconds"])
        self.assertEqual(sample_count, after_wait["probe_read_cost"]["sample_count"])
        self.assertEqual(64, after_wait["sample_count_retained"])
        self.assertEqual(64, len(retained))

        def probe_failure(_pid):
            raise OSError("/synthetic/secret/path")

        namespace["_read_startup_worker_resource_snapshot"] = probe_failure
        failed_probe_sampler = namespace["_RssSampler"](126)
        failed_probe_sampler._startup_started = time.monotonic()
        failed_probe_sampler._startup_next_sample = failed_probe_sampler._startup_started
        failed_probe_sampler.freeze_startup_resources(trigger="start_worker_raised")
        failed_probe = failed_probe_sampler.startup_resource_observations()
        self.assertEqual("OSError", failed_probe["samples"][-1]["snapshot_error_type"])
        self.assertNotIn("/synthetic/secret/path", json.dumps(failed_probe))

    def test_startup_failure_path_freezes_before_outer_metrics_export(self):
        source = self.source.read_text(encoding="utf-8")
        self.assertIn("sampler.freeze_startup_resources(trigger=startup_freeze_trigger)", source)
        self.assertIn('metrics["startup_resource_observations"] = sampler.startup_resource_observations()', source)
        scale_test = next(item for item in self.tree.body
                          if isinstance(item, ast.FunctionDef)
                          and item.name == "test_large_mihomo_health_readback_stays_bounded_under_concurrent_api_load")
        start_worker_call = next(node for node in ast.walk(scale_test)
                                 if isinstance(node, ast.Call) and isinstance(node.func, ast.Subscript)
                                 and isinstance(node.func.value, ast.Name) and node.func.value.id == "stack"
                                 and isinstance(node.func.slice, ast.Constant)
                                 and node.func.slice.value == "start_worker")
        startup_try = min((node for node in ast.walk(scale_test)
                           if isinstance(node, ast.Try) and start_worker_call in ast.walk(node)),
                          key=lambda node: node.end_lineno - node.lineno)
        self.assertTrue(any(isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)
                            and child.func.attr == "freeze_startup_resources"
                            for final_statement in startup_try.finalbody
                            for child in ast.walk(final_statement)))
        outer_finally = next(node for node in ast.walk(scale_test) if isinstance(node, ast.Try)
                             and any(isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)
                                     and child.func.attr == "stop"
                                     for final_statement in node.finalbody
                                     for child in ast.walk(final_statement)))
        self.assertTrue(any(isinstance(child, ast.Assign)
                            and any(isinstance(target, ast.Subscript)
                                    and isinstance(target.slice, ast.Constant)
                                    and target.slice.value == "startup_resource_observations"
                                    for target in child.targets)
                            for final_statement in outer_finally.finalbody
                            for child in ast.walk(final_statement)))


class SharedReceiptObserverContractTests(unittest.TestCase):
    def test_actual_observer_forwards_routing_context_and_owner_proof(self):
        source = Path(__file__).parents[1] / "application_acceptance" / "worker.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        function = next(node for node in ast.walk(tree)
                        if isinstance(node, ast.FunctionDef) and node.name == "observed_publish")
        for rollback in (False, True):
            for routing in (None, {"server_mode": "fixed", "desired_fixed_server_id": "synthetic"}):
                with self.subTest(rollback=rollback, routing=routing):
                    calls, events, owner_calls = [], [], []
                    def owner(fingerprint):
                        owner_calls.append(fingerprint)
                        return fingerprint == "proof-digest"
                    def publish(**kwargs):
                        calls.append(kwargs)
                        return kwargs["owner_matches"]("proof-digest")
                    namespace = {"Any": Any, "local": SimpleNamespace(rollback=rollback),
                                 "original_publish": publish,
                                 "emit": lambda event, facts: events.append((event, facts))}
                    exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), "exec"), namespace)
                    result = namespace["observed_publish"](
                        expected_mihomo_runtime="synthetic-incarnation", expected_mihomo_sha="a" * 64,
                        reconcile_reason="xray_generation_rollback" if rollback else "xray_staged_generation",
                        operation_id="synthetic-operation", owner_matches=owner, routing_snapshot=routing,
                    )
                    self.assertTrue(result)
                    self.assertEqual(1, len(calls))
                    self.assertEqual(routing, calls[0].get("routing_snapshot"))
                    self.assertEqual(["proof-digest"], owner_calls)
                    if rollback:
                        self.assertEqual(1, events[0][1]["owner_callback_count"])
                    self.assertNotIn("desired_fixed_server_id", json.dumps(events))


class StartupCausalityInstrumentationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("fwrouter_startup_causality_contract", STARTUP_CAUSALITY_PATH)
        assert spec and spec.loader
        cls.instrumentation = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.instrumentation)

    def _recorder(self, receipt: Path):
        recorder = self.instrumentation.StartupCausality(receipt)
        recorder.enabled = True
        return recorder

    def test_json_wrappers_preserve_values_errors_and_phase_counts(self):
        with tempfile.TemporaryDirectory() as temporary:
            receipt = Path(temporary) / "receipt.json"
            recorder = self._recorder(receipt)
            json_module = SimpleNamespace(loads=json.loads, dumps=json.dumps, load=json.load, dump=json.dump)
            recorder.observe_json(json_module)
            recorder.recording = True
            value = {"synthetic": [1, "two", False]}
            encoded = json_module.dumps(value, sort_keys=True)
            self.assertEqual(json.dumps(value, sort_keys=True), encoded)
            self.assertEqual(value, json_module.loads(encoded))
            with self.assertRaises(json.JSONDecodeError):
                json_module.loads("{")
            recorder.set_phase("intended")
            json_module.loads(encoded)
            recorder.recording = False
            recorder.restore()
            self.assertEqual(1, recorder.phases["foundation"]["json"]["dumps"]["calls"])
            self.assertEqual(2, recorder.phases["foundation"]["json"]["loads"]["calls"])
            self.assertEqual(1, recorder.phases["intended"]["json"]["loads"]["calls"])

    def test_json_large_byte_inputs_are_unobserved_outside_recording_window(self):
        with tempfile.TemporaryDirectory() as temporary:
            recorder = self._recorder(Path(temporary) / "receipt.json")
            json_module = SimpleNamespace(loads=json.loads, dumps=json.dumps, load=json.load, dump=json.dump)
            recorder.observe_json(json_module)
            payload = bytearray(b'{"rows":["' + b"x" * (70 * 1024) + b'"]}')
            self.assertEqual(1, len(json_module.loads(payload)["rows"]))
            self.assertEqual({}, recorder.phases)
            recorder.recording = True
            json_module.loads(payload)
            observed_samples = recorder.hash_samples
            observed_bytes = recorder.hash_bytes
            recorder.recording = False
            json_module.loads(payload)
            self.assertEqual(1, observed_samples)
            self.assertEqual(observed_samples, recorder.hash_samples)
            self.assertEqual(observed_bytes, recorder.hash_bytes)
            self.assertEqual(1, recorder.phases["foundation"]["json"]["loads"]["calls"])
            recorder.restore()

    def test_sql_fingerprint_observer_cost_does_not_enter_statement_timer(self):
        recorder = self._recorder(Path(tempfile.gettempdir()) / "unused-startup-causality.json")
        recorder.recording = True
        original_fingerprint = recorder._statement_fingerprint

        def delayed_fingerprint(label, sql):
            time.sleep(0.04)
            return original_fingerprint(label, sql)

        recorder._statement_fingerprint = delayed_fingerprint
        result = recorder.operation("sqlite", "statement.SELECT", 20, lambda sql: sql,
                                    "SELECT synthetic")
        self.assertEqual("SELECT synthetic", result)
        row = recorder.phases["foundation"]["sqlite"]["statement.SELECT"]
        self.assertGreaterEqual(recorder.operation_observer_wall, 0.04)
        self.assertLess(row["wall_seconds"], 0.02)
        self.assertGreater(row["wall_seconds"], 0)

    def test_sqlite_observation_preserves_transactions_errors_and_redacts_sql_values(self):
        with tempfile.TemporaryDirectory() as temporary:
            receipt = Path(temporary) / "receipt.json"
            recorder = self._recorder(receipt)
            sqlite_module = SimpleNamespace(connect=sqlite3.connect, Connection=sqlite3.Connection,
                                            Cursor=sqlite3.Cursor)
            recorder.observe_sqlite(sqlite_module)
            recorder.recording = True
            connection = sqlite_module.connect(str(Path(temporary) / "db.sqlite"))
            connection.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY, value TEXT UNIQUE)")
            connection.execute("INSERT INTO sample(value) VALUES (?)", ("CANARY_SQL_VALUE",))
            connection.commit()
            cursor = connection.execute("SELECT value FROM sample WHERE value=?", ("CANARY_SQL_VALUE",))
            self.assertEqual("CANARY_SQL_VALUE", cursor.fetchone()[0])
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("INSERT INTO sample(value) VALUES (?)", ("CANARY_SQL_VALUE",))
            connection.rollback()
            with self.assertRaises(sqlite3.IntegrityError):
                with connection:
                    connection.execute("INSERT INTO sample(value) VALUES (?)", ("CANARY_SQL_VALUE",))
            connection.close()
            recorder.recording = False
            recorder.snapshot()
            recorder.restore()
            text = receipt.read_text(encoding="ascii")
            self.assertNotIn("CANARY_SQL_VALUE", text)
            self.assertNotIn("CREATE TABLE sample", text)
            self.assertIn('"statement.SELECT"', text)
            self.assertGreater(recorder.cache["transaction_commit_transition"], 0)
            self.assertEqual(1, recorder.cache["transaction_context_exit_transition"])
            self.assertEqual(1, recorder.cache["transaction_context_exit_rollback"])
            self.assertGreater(recorder.phases["foundation"]["sqlite"]["fetch.fetchone"]["calls"], 0)
            self.assertEqual(0o600, stat.S_IMODE(receipt.stat().st_mode))

    def test_cache_labels_report_hits_and_coalescing_as_observed_or_ambiguous(self):
        cache_path = Path(__file__).parents[1] / ".." / "backend" / "fwrouter_api" / "services" / "live_probe_cache.py"
        spec = importlib.util.spec_from_file_location("fwrouter_startup_causality_cache_fixture", cache_path.resolve())
        assert spec and spec.loader
        cache_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cache_module)
        with tempfile.TemporaryDirectory() as temporary:
            recorder = self._recorder(Path(temporary) / "receipt.json")
            recorder.observe_cache(cache_module)
            recorder.recording = True
            calls = []
            loader = lambda: calls.append("loaded") or {"ok": True}
            self.assertEqual({"ok": True}, cache_module.get_live_probe_cache(
                "synthetic.cache", ttl_seconds=30, loader=loader))
            self.assertEqual({"ok": True}, cache_module.get_live_probe_cache(
                "synthetic.cache", ttl_seconds=30, loader=loader))
            self.assertEqual(["loaded"], calls)
            event = threading.Event()
            event.set()
            cache_module._INFLIGHT["synthetic.coalesced"] = (event, {"value": "coalesced-value"})
            self.assertEqual("coalesced-value", cache_module.get_live_probe_cache(
                "synthetic.coalesced", ttl_seconds=0, loader=loader))
            del cache_module._INFLIGHT["synthetic.coalesced"]
            recorder.recording = False
            recorder.restore()
            self.assertEqual(1, recorder.cache["get.loader_or_ambiguous"])
            self.assertEqual(1, recorder.cache["get.direct_hit"])
            self.assertEqual(1, recorder.cache["get.coalesced_or_race"])
            self.assertGreaterEqual(recorder.cache["entry_present_after_get"], 2)
            self.assertEqual(1, recorder.cache["admission_outcome_ambiguous"])
            self.assertEqual(1, recorder.cache["loader_calls"])
            self.assertEqual(1, recorder.cache["loader_return_size_last"])
            self.assertEqual("dict", recorder.cache["loader_return_type_last"])

    def test_receipt_write_failure_is_bounded_and_worker_guard_is_explicit(self):
        with tempfile.TemporaryDirectory() as temporary:
            receipt_directory = Path(temporary) / "receipt-is-a-directory"
            receipt_directory.mkdir()
            recorder = self._recorder(receipt_directory)
            recorder.snapshot()
            self.assertIn(recorder.snapshot_error, {"IsADirectoryError", "OSError"})
        worker_source = (Path(__file__).parents[1] / "application_acceptance" / "worker.py").read_text(
            encoding="utf-8")
        self.assertIn('os.environ.get("FWROUTER_STARTUP_TASKS_ENABLED") == "1"', worker_source)
        self.assertIn('os.environ.get("FWROUTER_STARTUP_RECOVERY_ENABLED") == "1"', worker_source)
        self.assertIn('startup_causality.completed = True', worker_source)
        self.assertIn('startup_causality.restore()', worker_source)
        source = STARTUP_CAUSALITY_PATH.read_text(encoding="utf-8")
        self.assertIn('"sql_text_retained": False', source)
        self.assertIn('"sql_parameters_retained": False', source)
        self.assertIn('"call_logs": False', source)

    def test_json_alias_patching_is_fwrouter_scoped_and_restored(self):
        with tempfile.TemporaryDirectory() as temporary:
            recorder = self._recorder(Path(temporary) / "receipt.json")
            json_module = SimpleNamespace(loads=json.loads, dumps=json.dumps, load=json.load, dump=json.dump)
            app_alias = ModuleType("fwrouter_api.synthetic_alias_contract")
            app_alias.decode = json.loads
            external_alias = ModuleType("external.synthetic_alias_contract")
            external_alias.decode = json.loads
            sys.modules[app_alias.__name__] = app_alias
            sys.modules[external_alias.__name__] = external_alias
            try:
                recorder.observe_json(json_module)
                recorder.observe_loaded_json_aliases()
                self.assertIsNot(app_alias.decode, json.loads)
                self.assertIs(external_alias.decode, json.loads)
                recorder.restore()
                self.assertIs(app_alias.decode, json.loads)
            finally:
                sys.modules.pop(app_alias.__name__, None)
                sys.modules.pop(external_alias.__name__, None)

    def test_group_and_synthetic_digest_budgets_are_bounded(self):
        with tempfile.TemporaryDirectory() as temporary:
            recorder = self._recorder(Path(temporary) / "receipt.json")
            recorder.recording = True
            for index in range(self.instrumentation.MAX_GROUPS + 3):
                recorder.operation("boundary", f"fixed-{index}", 1, lambda: None)
            self.assertEqual(self.instrumentation.MAX_GROUPS,
                             len(recorder.phases["foundation"]["boundary"]))
            self.assertIn("other_capped", recorder.phases["foundation"]["boundary"])
            previous_budget = self.instrumentation.MAX_HASH_BYTES
            try:
                self.instrumentation.MAX_HASH_BYTES = 4
                digest = recorder.hash_synthetic_bytes(b"synthetic")
                self.assertIsNone(digest)
                recorder.hash_bytes = 0
                digest = recorder.hash_synthetic_bytes(b"data")
                self.assertRegex(digest or "", r"^[0-9a-f]{64}$")
                self.assertIsNone(recorder.hash_synthetic_bytes(b"x"))
            finally:
                self.instrumentation.MAX_HASH_BYTES = previous_budget
            recorder.recording = False

    def test_sqlite_custom_factory_remains_unobserved_without_substitution(self):
        with tempfile.TemporaryDirectory() as temporary:
            recorder = self._recorder(Path(temporary) / "receipt.json")
            sqlite_module = SimpleNamespace(connect=sqlite3.connect, Connection=sqlite3.Connection,
                                            Cursor=sqlite3.Cursor)
            recorder.observe_sqlite(sqlite_module)
            class CustomConnection(sqlite3.Connection):
                pass
            recorder.recording = True
            connection = sqlite_module.connect(str(Path(temporary) / "custom.sqlite"),
                                               factory=CustomConnection)
            self.assertIs(type(connection), CustomConnection)
            connection.close()
            recorder.recording = False
            self.assertEqual(1, recorder.cache["sqlite_custom_factory_unobserved"])
            recorder.restore()

    def test_repeated_large_json_and_sql_templates_are_counted_without_retention(self):
        with tempfile.TemporaryDirectory() as temporary:
            receipt = Path(temporary) / "receipt.json"
            recorder = self._recorder(receipt)
            json_module = SimpleNamespace(loads=json.loads, dumps=json.dumps, load=json.load, dump=json.dump)
            recorder.observe_json(json_module)
            recorder.recording = True
            payload = json.dumps({"rows": ["synthetic-row" * 128] * 48})
            self.assertGreaterEqual(len(payload), 64 * 1024)
            json_module.loads(payload)
            json_module.loads(payload)
            recorder.set_phase("xray")
            json_module.loads(payload)

            sqlite_module = SimpleNamespace(connect=sqlite3.connect, Connection=sqlite3.Connection,
                                            Cursor=sqlite3.Cursor)
            recorder.observe_sqlite(sqlite_module)
            connection = sqlite_module.connect(":memory:")
            connection.execute("CREATE TABLE data (value TEXT)")
            connection.execute("INSERT INTO data(value) VALUES ('PRIVATE_SQL_LITERAL_A')")
            connection.execute("INSERT INTO data(value) VALUES ('PRIVATE_SQL_LITERAL_B')")
            connection.close()
            recorder.recording = False
            recorder.snapshot()
            text = receipt.read_text(encoding="ascii")
            self.assertEqual(1, recorder.digest_phase_stats["foundation"]["repeated_calls"])
            self.assertEqual(1, recorder.digest_phase_stats["foundation"]["unique_count"])
            self.assertEqual(1, recorder.digest_phase_stats["xray"]["unique_count"])
            self.assertGreaterEqual(recorder.statement_repeated_calls, 1)
            self.assertNotIn("PRIVATE_SQL_LITERAL", text)
            self.assertNotIn(hashlib.sha256(payload.encode()).hexdigest(), text)
            self.assertIn('"digest_values_retained":false', text)
            self.assertIn('"identifiers_or_sql_retained":false', text)
            recorder.restore()


if __name__ == "__main__":
    unittest.main(verbosity=2)
