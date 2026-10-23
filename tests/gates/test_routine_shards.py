"""Contract tests for deterministic, fail-closed routine shard coverage."""
from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gate
import routine_shards


RUN_ID = "123456"
ATTEMPT = "2"
SOURCE = "a" * 40


def _resource() -> dict:
    return {"cpu_count": 2, "memory": {"MemAvailable_bytes": 1024},
            "disk_total_bytes": 4096, "disk_used_bytes": 1024, "disk_free_bytes": 3072}


def _fixture(*, mandatory_all=False):
    manifest = gate.load_manifest()
    selected = []
    for group in gate.ROUTINE_SHARD_GROUPS:
        row = next(item for item in manifest["test_files"]
                   if item["execution_group"] == group)
        selected.append(row["path"])
    anchors = sorted(manifest["policies"]["L5"]["anchors"]) if mandatory_all else []
    selected = sorted(set(selected) | set(anchors))
    plan = {
        "source_commit": SOURCE, "plan_digest": "b" * 64,
        "manifest_digest": gate.canonical_digest(manifest), "manifest_version": manifest["version"],
        "selected_files": selected, "required_levels": ["L0", "L1"],
        "required_execution_profiles": [], "mandatory_suite_required": mandatory_all,
        "domains": [], "regression_policy": {"level": "L5", "reasons": [], "anchors": anchors},
    }
    grouped = routine_shards.expected_by_group(plan, manifest)
    reports = {}
    for group, rows in grouped.items():
        executions = []
        shard_status = {}
        for row in rows:
            local_nodes = sorted(
                f"{row['path']}::{override['node']}"
                for override in manifest.get("node_overrides", [])
                if override.get("path") == row["path"]
                and "execution_profile" in override and override.get("execution_profile") is None
            ) if row.get("execution_profile") else []
            nodes = local_nodes if row.get("execution_profile") else [row["path"] + "::test_case"]
            node_status = {node: "passed" for node in nodes}
            shard_status.update(node_status)
            executions.append({
                "suite": row["id"], "path": row["path"],
                "primary_level": row["primary_level"],
                "domain": row["domain"], "execution_group": group,
                "execution_profile": row.get("execution_profile"),
                "status": "deferred_remote_hosted" if row.get("execution_profile") else "passed",
                "elapsed_seconds": 0.25, "collected_node_ids": nodes, "executed_node_ids": nodes,
                "node_status": node_status,
                **({"routine_node_status": node_status} if row.get("execution_profile") and nodes else {}),
            })
        report = gate._routine_shard_report(
            plan, manifest, group, rows, executions, shard_status, [],
            l0_owned=group == gate.ROUTINE_SHARD_GROUPS[0], l0_ok=True,
            l0_elapsed=0.1, l0_problems=[])
        report.update({"run_id": RUN_ID, "run_attempt": ATTEMPT,
                       "resources_before": _resource(), "resources_after": _resource(),
                       "process_peak": {"max_rss_kib": 12345},
                       "cleanup": "verified_owned_gate_temp_removed"})
        report.pop("shard_digest", None)
        report["shard_digest"] = gate.canonical_digest(report)
        reports[group] = report
    return plan, manifest, grouped, reports


class RoutineShardContractTests(unittest.TestCase):
    def test_manifest_assigns_each_file_to_one_reviewed_execution_group(self):
        manifest = gate.load_manifest()
        self.assertEqual([], gate.check_catalog(manifest))
        paths = [row["path"] for row in manifest["test_files"]]
        self.assertEqual(len(paths), len(set(paths)))
        for row in manifest["test_files"]:
            self.assertIn(row["execution_group"], gate.ROUTINE_SHARD_GROUPS)
            self.assertIn(row["domain"], gate.ROUTINE_SHARD_DOMAINS[row["execution_group"]])

    def test_plan_partition_and_merge_are_exhaustive_disjoint_and_non_eligible(self):
        plan, manifest, grouped, reports = _fixture()
        flattened = [row["path"] for rows in grouped.values() for row in rows]
        self.assertEqual(set(plan["selected_files"]), set(flattened))
        self.assertEqual(len(flattened), len(set(flattened)))
        merged = routine_shards.merge_reports(plan, manifest, reports, run_id=RUN_ID, attempt=ATTEMPT)
        self.assertEqual(set(plan["selected_files"]),
                         {row["path"] for row in merged["executions"] if row["path"] != "@L0"})
        self.assertFalse(merged["eligible"])
        self.assertIn("strict aggregate validates hosted/native evidence", merged["eligibility_note"])

    def test_l5_anchors_are_selected_and_executed_under_their_primary_owner(self):
        plan, manifest, _, reports = _fixture(mandatory_all=True)
        merged = routine_shards.merge_reports(plan, manifest, reports, run_id=RUN_ID, attempt=ATTEMPT)
        expected = sorted(manifest["policies"]["L5"]["anchors"])
        self.assertEqual("passed", merged["l5_coverage"]["status"])
        self.assertEqual(expected, [row["path"] for row in merged["l5_coverage"]["anchors"]])
        self.assertEqual(len(expected), merged["l5_coverage"]["required_count"])
        for row in merged["l5_coverage"]["anchors"]:
            self.assertEqual("passed", row["status"])
            self.assertTrue(row["node_ids"])
            self.assertEqual(row["execution_group"], next(
                item["execution_group"] for item in manifest["test_files"] if item["path"] == row["path"]))

    def test_l5_policy_cannot_drop_or_invent_anchors(self):
        plan, manifest, _, reports = _fixture(mandatory_all=True)
        plan["regression_policy"]["anchors"] = plan["regression_policy"]["anchors"][:-1]
        with self.assertRaisesRegex(gate.GateError, "L5 anchors/reasons"):
            routine_shards.merge_reports(plan, manifest, reports, run_id=RUN_ID, attempt=ATTEMPT)

    def test_l5_anchor_cannot_be_removed_from_selected_plan(self):
        plan, manifest, _, reports = _fixture(mandatory_all=True)
        plan["selected_files"].remove(plan["regression_policy"]["anchors"][0])
        with self.assertRaises(gate.GateError):
            routine_shards.merge_reports(plan, manifest, reports, run_id=RUN_ID, attempt=ATTEMPT)

    def test_missing_or_unexpected_shard_blocks_merge(self):
        plan, manifest, grouped, reports = _fixture()
        missing = dict(reports)
        missing.pop(gate.ROUTINE_SHARD_GROUPS[-1])
        with self.assertRaisesRegex(gate.GateError, "incomplete or unexpected"):
            routine_shards.merge_reports(plan, manifest, missing, run_id=RUN_ID, attempt=ATTEMPT)
        extra = dict(reports, foreign={})
        with self.assertRaisesRegex(gate.GateError, "incomplete or unexpected"):
            routine_shards.merge_reports(plan, manifest, extra, run_id=RUN_ID, attempt=ATTEMPT)

    def test_wrong_source_plan_manifest_and_attempt_block_even_when_resigned(self):
        plan, manifest, grouped, reports = _fixture()
        for field, value in (("source_commit", "c" * 40), ("plan_digest", "d" * 64),
                             ("manifest_digest", "e" * 64), ("run_attempt", "1")):
            mutated = copy.deepcopy(reports)
            row = mutated[gate.ROUTINE_SHARD_GROUPS[0]]
            row[field] = value
            row["shard_digest"] = gate.canonical_digest({k: v for k, v in row.items() if k != "shard_digest"})
            with self.subTest(field=field), self.assertRaisesRegex(gate.GateError, "does not match"):
                routine_shards.merge_reports(plan, manifest, mutated, run_id=RUN_ID, attempt=ATTEMPT)

    def test_missing_extra_duplicate_or_skipped_suite_is_rejected(self):
        plan, manifest, _, reports = _fixture()
        for mode in ("missing", "extra", "duplicate", "skip"):
            mutated = copy.deepcopy(reports)
            group = next(name for name in gate.ROUTINE_SHARD_GROUPS if mutated[name]["executions"])
            row = mutated[group]
            if mode == "missing":
                row["executions"].pop()
                row["collected_paths"].pop()
            elif mode == "extra":
                row["executions"].append(copy.deepcopy(row["executions"][0]))
                row["collected_paths"].append(row["collected_paths"][0])
            elif mode == "duplicate":
                row["executions"].append(copy.deepcopy(row["executions"][0]))
                row["collected_paths"].append(row["collected_paths"][0])
            else:
                row["executions"][0]["status"] = "skipped"
            row["shard_digest"] = gate.canonical_digest({k: v for k, v in row.items() if k != "shard_digest"})
            with self.subTest(mode=mode), self.assertRaises(gate.GateError):
                routine_shards.merge_reports(plan, manifest, mutated, run_id=RUN_ID, attempt=ATTEMPT)

    def test_node_ids_are_unique_complete_and_exactly_once_across_shards(self):
        plan, manifest, grouped, reports = _fixture()
        group = gate.ROUTINE_SHARD_GROUPS[0]
        report = reports[group]
        owner = grouped[group][0]
        node = owner["path"] + "::test_case"
        report["node_status"] = {node: "passed"}
        report["expected_node_ids"] = [node, node]
        report["collected_node_ids"] = [node, node]
        report["executed_node_ids"] = [node, node]
        report["node_status_count"] = report["unique_node_status_count"] = 1
        execution = report["executions"][0]
        execution.update({"collected_node_ids": [node, node], "executed_node_ids": [node, node],
                          "node_status": {node: "passed"}})
        report["shard_digest"] = gate.canonical_digest({k: v for k, v in report.items() if k != "shard_digest"})
        with self.assertRaisesRegex(gate.GateError, "missing, duplicated or malformed"):
            routine_shards.merge_reports(plan, manifest, reports, run_id=RUN_ID, attempt=ATTEMPT)

    def test_node_id_must_belong_to_the_declared_suite_path(self):
        plan, manifest, grouped, reports = _fixture()
        group = gate.ROUTINE_SHARD_GROUPS[0]
        row = reports[group]
        execution = row["executions"][0]
        good_node = execution["path"] + "::test_case"
        wrong_node = "backend/tests/unrelated.py::test_case"
        row["node_status"] = {wrong_node: "passed"}
        row["expected_node_ids"] = row["collected_node_ids"] = row["executed_node_ids"] = [wrong_node]
        row["node_status_count"] = row["unique_node_status_count"] = 1
        execution["collected_node_ids"] = execution["executed_node_ids"] = [wrong_node]
        execution["node_status"] = {wrong_node: "passed"}
        self.assertNotEqual(good_node, wrong_node)
        row["shard_digest"] = gate.canonical_digest({k: v for k, v in row.items() if k != "shard_digest"})
        with self.assertRaisesRegex(gate.GateError, "attributed to the wrong suite path"):
            routine_shards.merge_reports(plan, manifest, reports, run_id=RUN_ID, attempt=ATTEMPT)

    def test_l0_has_one_explicit_owner_and_cleanup_resources_are_required(self):
        plan, manifest, _, reports = _fixture()
        group = gate.ROUTINE_SHARD_GROUPS[1]
        row = reports[group]
        row["l0"] = {"owned": True, "status": "passed", "elapsed_seconds": 0.1,
                     "checks": ["git diff --check"], "problems": []}
        row["shard_digest"] = gate.canonical_digest({k: v for k, v in row.items() if k != "shard_digest"})
        with self.assertRaisesRegex(gate.GateError, "L0 ownership"):
            routine_shards.merge_reports(plan, manifest, reports, run_id=RUN_ID, attempt=ATTEMPT)
        row["l0"] = {"owned": False, "status": "not_owned", "elapsed_seconds": 0.0,
                     "checks": [], "problems": []}
        row["cleanup"] = "unknown"
        row["shard_digest"] = gate.canonical_digest({k: v for k, v in row.items() if k != "shard_digest"})
        with self.assertRaisesRegex(gate.GateError, "resource or cleanup"):
            routine_shards.merge_reports(plan, manifest, reports, run_id=RUN_ID, attempt=ATTEMPT)

    def test_pytest_node_receipt_requires_exact_collection_and_one_terminal_execution(self):
        node = "backend/tests/test_contract.py::TestContract::test_roundtrip[param]"
        good = {"schema_version": 2, "collected_nodeids": [node],
                "node_status": {node: "passed"},
                "node_executions": {node: {"start_count": 1, "call_report_count": 1,
                                            "terminal_report_count": 1, "status": "passed"}}}
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "nodes.json"
            path.write_text(json.dumps(good), encoding="utf-8")
            self.assertEqual({node: "passed"}, gate.node_status_from_plugin(path, 8192))
            for mutate in (
                lambda row: row["node_executions"].pop(node),
                lambda row: row["node_executions"][node].update(start_count=2),
                lambda row: row["node_executions"][node].update(terminal_report_count=2),
                lambda row: row["node_status"].update({node: "skipped"}),
                lambda row: row.update(collected_nodeids=[node, node]),
            ):
                candidate = copy.deepcopy(good)
                mutate(candidate)
                path.write_text(json.dumps(candidate), encoding="utf-8")
                with self.assertRaises(gate.GateError):
                    gate.node_status_from_plugin(path, 8192)

    def test_tap_requires_observed_plan_and_rejects_duplicate_or_skipped_cases(self):
        path = "ui/tests/example.test.js"
        passing = ("# Subtest: example.test.js\n"
                   "    ok 1 - first\n"
                   "ok 1 - example.test.js\n"
                   "1..1\n")
        statuses, aggregate = gate.node_tap_results(passing, path, require_observed=True)
        self.assertEqual("passed", aggregate)
        self.assertEqual(["passed"], list(statuses.values()))
        skipped = ("# Subtest: example.test.js\n"
                   "    ok 1 - first # SKIP platform\n"
                   "ok 1 - example.test.js\n"
                   "1..1\n")
        self.assertIn("skipped", gate.node_tap_results(skipped, path, require_observed=True)[0].values())
        with self.assertRaisesRegex(gate.GateError, "plan is missing"):
            gate.node_tap_results(passing.replace("1..1\n", ""), path, require_observed=True)
        duplicate = passing.replace("    ok 1 - first\n", "    ok 1 - first\n    ok 2 - first\n")
        with self.assertRaisesRegex(gate.GateError, "duplicate test case IDs"):
            gate.node_tap_results(duplicate, path, require_observed=True)


if __name__ == "__main__":
    unittest.main()
