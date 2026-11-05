"""Observed-case reporting cannot fabricate qualification or count expected IDs."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import visibility_report as visibility


class VisibilityTests(unittest.TestCase):
    def test_authorized_skip_reason_is_visible_and_host_qualification_stays_separate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); evidence = root / "evidence"; required = root / "required"
            unit_dir = evidence / "unit"; unit_dir.mkdir(parents=True); required.mkdir()
            manifest = visibility.gate.load_manifest()
            plan = {"source_commit": "a" * 40, "plan_digest": "b" * 64,
                    "manifest_digest": "c" * 64,
                    "selected_files": ["tests/gates/test_manual_host_smoke.py"]}
            node = visibility.gate.MANUAL_HOST_NODEID
            policy = visibility.gate.SKIP_POLICY_REGISTRY[node]
            reason = {**policy, "pytest_reason": visibility.gate.node_skip_reason(policy)}
            unit = {"host_qualification_status": "NOT_VERIFIED",
                    "node_status": {node: "skipped"},
                    "authorized_skip_reasons": {node: reason},
                    "executions": [{"path": "tests/gates/test_manual_host_smoke.py",
                                    "node_status": {node: "skipped"}, "executed_node_ids": [node]}]}
            (unit_dir / "fwrouter-affected-unit.json").write_text(json.dumps(unit))
            (required / "required-result.json").write_text(json.dumps({"eligible": True}))
            (required / "fwrouter-aggregate.json").write_text(json.dumps({"eligible": True}))
            with patch.object(visibility.gate, "load_manifest", return_value=manifest):
                value = visibility.collect(evidence, required, plan)
            self.assertEqual("NOT_VERIFIED", value["host_qualification_status"])
            self.assertEqual("passed", value["qualification_status"])
            self.assertEqual(reason, value["executions"][0]["skip_reason"])

    def test_invalid_selected_manual_skip_demotes_displayed_qualification(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); evidence = root / "evidence"; required = root / "required"
            unit_dir = evidence / "unit"; unit_dir.mkdir(parents=True); required.mkdir()
            manifest = visibility.gate.load_manifest()
            plan = {"source_commit": "a" * 40, "plan_digest": "b" * 64,
                    "manifest_digest": "c" * 64,
                    "selected_files": ["tests/gates/test_manual_host_smoke.py"]}
            node = visibility.gate.MANUAL_HOST_NODEID
            unit = {"host_qualification_status": "NOT_VERIFIED",
                    "node_status": {node: "skipped"}, "authorized_skip_reasons": {},
                    "executions": [{"path": "tests/gates/test_manual_host_smoke.py",
                                    "node_status": {node: "skipped"}, "executed_node_ids": [node]}]}
            (unit_dir / "fwrouter-affected-unit.json").write_text(json.dumps(unit))
            (required / "required-result.json").write_text(json.dumps({"eligible": True}))
            (required / "fwrouter-aggregate.json").write_text(json.dumps({"eligible": True}))
            with patch.object(visibility.gate, "load_manifest", return_value=manifest):
                value = visibility.collect(evidence, required, plan)
            self.assertEqual("incomplete_or_failed", value["qualification_status"])
            self.assertEqual("INVALID_OR_UNAVAILABLE", value["host_qualification_status"])
            self.assertEqual("passed", value["strict_aggregate_status"])
            self.assertTrue(value["required_result_eligible"])
            self.assertTrue(value["strict_aggregate_eligible"])
            self.assertIn("unit skip-policy validation failed", " ".join(value["diagnostic_limitations"]))
            self.assertNotIn("Qualification: **passed**", visibility.summary(value))

    def test_expected_native_ids_do_not_establish_execution(self):
        self.assertEqual({}, visibility.receipt_nodes({'status': 'passed', 'tests': {
            'expected_ids': ['x.py::test_x'], 'passed': 1}}))

    def test_hosted_requires_all_observed_junit_results_pass(self):
        value = {'status': 'passed', 'tests': {'nodeids': ['x.py::test_x'],
                 'tests': 1, 'errors': 0, 'failures': 0, 'skipped': 0}}
        self.assertEqual({'x.py::test_x': 'passed'}, visibility.receipt_nodes(value))
        for field in ('errors', 'failures', 'skipped', 'tests'):
            changed = copy.deepcopy(value)
            changed['tests'][field] = 2
            self.assertEqual({}, visibility.receipt_nodes(changed))
        value['tests']['nodeids'] *= 2
        self.assertEqual({}, visibility.receipt_nodes(value))

    def test_native_failed_and_skipped_statuses_are_visible(self):
        result = visibility.receipt_nodes({'tests': {'node_status': {
            'x::a': 'failed', 'x::b': 'skipped', 'x::c': 'injected'}}})
        self.assertEqual({'x::a': 'failed', 'x::b': 'skipped', 'x::c': 'blocked'}, result)

    def test_unique_coverage_differs_from_repeat_executions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            evidence, required = root / 'evidence', root / 'required'
            evidence.mkdir(); required.mkdir()
            manifest = {'test_files': [{'path': 'x.py', 'primary_level': 'L3',
                        'domain': 'core', 'execution_profile': 'hosted-isolated-compose'}]}
            plan = {'source_commit': 'a' * 40, 'plan_digest': 'b' * 64,
                    'manifest_digest': 'c' * 64, 'selected_files': ['x.py']}
            for repeat in (1, 2):
                target = evidence / str(repeat); target.mkdir()
                (target / 'hosted-acceptance-report.json').write_text(json.dumps({
                    'status': 'passed', 'suite': 'functional', 'suite_nonce': str(repeat),
                    'cleanup': 'owned_resources_removed', 'tests': {'nodeids': ['x.py::test_x'],
                    'tests': 1, 'errors': 0, 'failures': 0, 'skipped': 0}}))
            with patch.object(visibility.gate, 'load_manifest', return_value=manifest):
                result = visibility.collect(evidence, required, plan)
            self.assertEqual(1, result['unique_test_coverage'])
            self.assertEqual(2, result['total_test_executions'])
            self.assertEqual('incomplete_or_failed', result['qualification_status'])
            self.assertIn('strict aggregate authority', visibility.summary(result))
            self.assertEqual([], result['not_run_selected_files'])

    def test_duplicate_receipt_copy_does_not_count_as_an_independent_repeat(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); evidence = root / 'e'; required = root / 'r'
            evidence.mkdir(); required.mkdir()
            receipt = {'status': 'passed', 'suite_nonce': 'same', 'tests': {'node_status': {'x.py::test_x': 'passed'}}}
            for name in ('qcp', 'profiles'):
                child = evidence / name; child.mkdir()
                (child / 'qualified-child-receipt.json').write_text(json.dumps(receipt))
            with patch.object(visibility.gate, 'load_manifest', return_value={'test_files': []}):
                value = visibility.collect(evidence, required, {'selected_files': []})
            self.assertEqual(1, value['total_test_executions'])

    def test_missing_receipts_remain_not_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root / 'e').mkdir(); (root / 'r').mkdir()
            manifest = {'test_files': [{'path': 'a.py'}, {'path': 'b.py'}]}
            with patch.object(visibility.gate, 'load_manifest', return_value=manifest):
                value = visibility.collect(root / 'e', root / 'r', {'selected_files': ['a.py']})
            self.assertEqual(0, value['total_test_executions'])
            self.assertEqual(['a.py'], value['not_run_selected_files'])
            self.assertEqual([{'path': 'b.py', 'reason': 'not selected by immutable plan'}], value['omitted_files'])

    def test_unsafe_input_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root / 'real').write_text('{}'); (root / 'link').symlink_to(root / 'real')
            with self.assertRaises(visibility.gate.GateError):
                visibility.read(root / 'link')


if __name__ == '__main__':
    unittest.main()
