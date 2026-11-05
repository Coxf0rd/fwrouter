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

    def test_compact_summary_separates_retired_and_host_skips_without_case_dump(self):
        qemu_node = next(node for node, policy in visibility.gate.SKIP_POLICY_REGISTRY.items()
                         if policy['reason_code'] == 'retired_qemu')
        host_node = visibility.gate.MANUAL_HOST_NODEID
        qemu_policy = {**visibility.gate.SKIP_POLICY_REGISTRY[qemu_node],
                       'pytest_reason': visibility.gate.node_skip_reason(
                           visibility.gate.SKIP_POLICY_REGISTRY[qemu_node])}
        host_policy = {**visibility.gate.SKIP_POLICY_REGISTRY[host_node],
                       'pytest_reason': visibility.gate.node_skip_reason(
                           visibility.gate.SKIP_POLICY_REGISTRY[host_node])}
        executions = [
            {'node_id': 'tests/example.py::test_pass', 'status': 'passed'},
            {'node_id': 'tests/example.py::test_pass', 'status': 'passed'},
            {'node_id': 'tests/example.py::test_failed|`<tag>`', 'status': 'failed'},
            {'node_id': qemu_node, 'status': 'skipped', 'skip_reason': qemu_policy},
            {'node_id': host_node, 'status': 'skipped', 'skip_reason': host_policy},
        ]
        executions.extend({'node_id': f'tests/flood.py::test_{index}', 'status': 'failed'}
                          for index in range(100))
        report = {
            'qualification_status': 'incomplete_or_failed', 'host_qualification_status': 'NOT_VERIFIED',
            'source_commit': 'a' * 40, 'run_id': '123', 'run_attempt': '2',
            'unique_test_coverage': len({item['node_id'] for item in executions}),
            'total_test_executions': len(executions),
            'statuses': {'passed': 2, 'failed': 101, 'skipped': 2, 'blocked': 0, 'not_run': 0},
            'discovered_suite_file_count': 500, 'selected_suite_file_count': 4, 'observed_selected_suite_file_count': 3,
            'not_run_selected_files': ['tests/unobserved.py'], 'omitted_files': ['MUST_NOT_BE_RENDERED'],
            'visibility_digest': 'b' * 64,
            'l5_coverage': {'status': 'passed', 'required_count': 1,
                            'anchors': [{'status': 'passed', 'node_ids': [f'private_l5.py::test_{i}' for i in range(2500)]}]},
            'levels': {}, 'groups': [{'shard_group': f'group-{i}', 'duration_seconds': 1.25,
                                      'status': 'passed'} for i in range(30)],
            'diagnostic_limitations': ['token=SECRET_SENTINEL\nline two'],
            'executions': executions,
        }
        rendered = visibility.summary(report, profile='PR required qualification',
                                      run_url='https://github.com/example/repo/actions/runs/123')
        self.assertIn('Host qualification: **NOT_VERIFIED**', rendered)
        self.assertIn('Retired QEMU SKIPs: retired_qemu (1)', rendered)
        self.assertIn('Active host SKIPs (host qualification remains NOT_VERIFIED): host_environment_unavailable (1)', rendered)
        self.assertIn('selected suite files: **4**', rendered)
        self.assertIn('Discovered suite files (manifest): **500**', rendered)
        self.assertIn('Observed case IDs:', rendered)
        self.assertIn('repeated executions', rendered)
        self.assertIn('Actual executed PASS+FAIL case executions (SKIP excluded): **103**', rendered)
        self.assertIn('SKIP 2', rendered)
        self.assertIn('Active host SKIPs (host qualification remains NOT_VERIFIED): host_environment_unavailable (1)', rendered)
        self.assertIn('Retired QEMU SKIPs: retired_qemu (1)', rendered)
        self.assertIn('L5 regression anchors: 1/1 passed (passed)', rendered)
        self.assertIn('Line/branch coverage: **NOT COLLECTED**', rendered)
        self.assertIn('test_failed&#124;&#96;&lt;tag&gt;&#96;', rendered)
        self.assertNotIn('MUST_NOT_BE_RENDERED', rendered)
        self.assertNotIn('SECRET_SENTINEL', rendered)
        self.assertNotIn('test_99', rendered)
        self.assertNotIn('private_l5.py', rendered)
        self.assertNotIn('test_2499', rendered)
        self.assertLess(len(rendered.encode()), 16 * 1024)

    def test_l5_summary_never_expands_large_anchor_node_id_arrays(self):
        report = {
            'qualification_status': 'passed', 'host_qualification_status': 'NOT_VERIFIED',
            'source_commit': 'a' * 40, 'run_id': '123', 'run_attempt': '2',
            'unique_test_coverage': 0, 'total_test_executions': 0,
            'discovered_suite_file_count': 100, 'selected_suite_file_count': 0,
            'observed_selected_suite_file_count': 0,
            'statuses': {'passed': 0, 'failed': 0, 'skipped': 0, 'blocked': 0, 'not_run': 0},
            'not_run_selected_files': [], 'levels': {}, 'groups': [], 'executions': [],
            'diagnostic_limitations': [],
            'l5_coverage': {'status': 'passed', 'required_count': 1,
                            'anchors': [{'status': 'passed', 'node_ids': [f'last_l5.py::test_{i}' for i in range(2500)]}]},
        }
        rendered = visibility.summary(report)
        self.assertIn('L5 regression anchors: 1/1 passed (passed)', rendered)
        self.assertNotIn('last_l5.py', rendered)
        self.assertNotIn('test_2499', rendered)
        self.assertLess(len(rendered.encode()), 16 * 1024)

    def test_partial_routine_failure_is_current_attempt_diagnostic_only(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
                'os.environ', {'GITHUB_RUN_ID': '123', 'GITHUB_RUN_ATTEMPT': '2'}):
            root = Path(temporary); evidence = root / 'evidence'; required = root / 'required'
            artifact = evidence / 'fwrouter-v2-unit-core-state-123-2'
            artifact.mkdir(parents=True); required.mkdir()
            manifest = visibility.gate.load_manifest()
            skip_paths = {node.split('::', 1)[0] for node in visibility.gate.SKIP_POLICY_REGISTRY}
            suite = next(row for row in manifest['test_files']
                         if row['execution_group'] == 'core-state'
                         and row.get('execution_profile') is None
                         and row['path'] not in skip_paths)
            node = suite['path'] + '::test_failure'
            plan = {'source_commit': 'a' * 40, 'plan_digest': 'b' * 64,
                    'manifest_digest': visibility.gate.canonical_digest(manifest),
                    'manifest_version': manifest['version'], 'selected_files': [suite['path']]}
            shard = {'source_commit': plan['source_commit'], 'plan_digest': plan['plan_digest'],
                     'manifest_digest': plan['manifest_digest'], 'manifest_version': plan['manifest_version'],
                     'shard_group': 'core-state', 'run_id': '123', 'run_attempt': '2',
                     'node_status': {node: 'failed'}, 'executed_node_ids': [node],
                     'executions': [{'path': suite['path'], 'execution_group': 'core-state'}],
                     'authorized_skip_reasons': {}, 'duration_seconds': 1.5,
                     'cleanup': 'verified', 'status': 'failed'}
            shard['shard_digest'] = visibility.gate.canonical_digest(shard)
            shard_path = artifact / 'shard.json'; shard_path.write_text(json.dumps(shard), encoding='utf-8')
            producer = {'schema': 'fwrouter-parallel-producer/v1', 'role': 'unit', 'outcome': 'failure',
                        'source_commit': plan['source_commit'], 'plan_digest': plan['plan_digest'],
                        'manifest_digest': plan['manifest_digest'], 'manifest_version': plan['manifest_version'],
                        'run_id': '123', 'run_attempt': '2',
                        'files': {'shard.json': {'bytes': shard_path.stat().st_size,
                                                 'sha256': __import__('hashlib').sha256(shard_path.read_bytes()).hexdigest()}}}
            producer['producer_digest'] = visibility.gate.canonical_digest(producer)
            (artifact / 'producer.json').write_text(json.dumps(producer), encoding='utf-8')
            value = visibility.collect(evidence, required, plan)
            self.assertEqual('incomplete_or_failed', value['qualification_status'])
            self.assertEqual('NOT_VERIFIED', value['host_qualification_status'])
            self.assertEqual('123', value['run_id'])
            self.assertEqual('2', value['run_attempt'])
            self.assertEqual(1, value['statuses']['failed'])
            self.assertEqual(node, value['executions'][0]['node_id'])
            self.assertIn('missing for', ' '.join(value['diagnostic_limitations']))

            with patch.dict('os.environ', {'GITHUB_RUN_ATTEMPT': '3'}):
                stale = visibility.collect(evidence, required, plan)
            self.assertEqual(0, stale['total_test_executions'])
            self.assertEqual('INVALID_OR_UNAVAILABLE', stale['host_qualification_status'])

    def test_unsafe_input_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root / 'real').write_text('{}'); (root / 'link').symlink_to(root / 'real')
            with self.assertRaises(visibility.gate.GateError):
                visibility.read(root / 'link')


if __name__ == '__main__':
    unittest.main()
