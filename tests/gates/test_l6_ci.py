"""Full-regression trigger, selection, repeats and evidence failure contracts."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gate
import l6_ci
import routine_shards
from test_routine_shards import _fixture, RUN_ID, ATTEMPT


class L6ContractTests(unittest.TestCase):
    def test_dispatch_main_and_branch_are_distinct(self):
        sha = 'a' * 40
        self.assertEqual('main-regression', l6_ci.trigger('workflow_dispatch', 'refs/heads/main', 'RUN L6', 'main', sha, sha))
        self.assertEqual('branch-regression-non-promotional', l6_ci.trigger('workflow_dispatch', 'refs/heads/test', 'RUN L6', 'branch', sha, sha))
        self.assertEqual('main-regression', l6_ci.trigger('schedule', 'refs/heads/main', '', '', '', sha))

    def test_invalid_trigger_acknowledgement_source_or_scope_fails(self):
        sha = 'a' * 40
        for event, ref, ack, mode, expected in (
            ('pull_request', 'refs/pull/1/merge', 'RUN L6', 'branch', sha),
            ('workflow_dispatch', 'refs/heads/main', '', 'main', sha),
            ('workflow_dispatch', 'refs/heads/main', 'RUN L6', 'branch', sha),
            ('workflow_dispatch', 'refs/heads/feature', 'RUN L6', 'main', sha),
            ('workflow_dispatch', 'refs/heads/main', 'RUN L6', 'main', 'b' * 40),
            ('schedule', 'refs/heads/feature', '', '', ''),
        ):
            with self.subTest(event=event, ref=ref, mode=mode), self.assertRaises(gate.GateError):
                l6_ci.trigger(event, ref, ack, mode, expected, sha)

    def test_all_required_jobs_must_succeed(self):
        good = {role: {'result': 'success'} for role in l6_ci.ROLES}
        l6_ci.check_jobs(good)
        for role in l6_ci.ROLES:
            for result in ('failure', 'cancelled', 'skipped', 'timed_out', None):
                changed = copy.deepcopy(good); changed[role]['result'] = result
                with self.subTest(role=role, result=result), self.assertRaises(gate.GateError):
                    l6_ci.check_jobs(changed)
            changed = copy.deepcopy(good); changed.pop(role)
            with self.assertRaises(gate.GateError):l6_ci.check_jobs(changed)
        with self.assertRaises(gate.GateError):l6_ci.check_jobs(dict(good, extra={'result': 'success'}))

    def test_every_profile_has_explicit_independent_repeat_owner(self):
        children = l6_ci.expected_children()
        for role in l6_ci.HOSTED:
            self.assertEqual((role + '-r1', role + '-r2'), children[role])
        all_children = [child for names in children.values() for child in names]
        self.assertEqual(len(all_children), len(set(all_children)))
        self.assertEqual(18, len(all_children))

    def test_l6_merge_preserves_all_safe_coverage_and_l5_anchors(self):
        plan, manifest, _, reports = _fixture(mandatory_all=True)
        plan['full_suite_required'] = True
        plan['mandatory_suite_required'] = False
        plan['regression_policy'] = {'level': 'L6', 'reasons': ['manual/nightly/release'], 'anchors': []}
        merged = routine_shards.merge_reports(plan, manifest, reports, run_id=RUN_ID, attempt=ATTEMPT)
        self.assertEqual('L6_full_suite', merged['evidence_scope'])
        self.assertEqual(len(manifest['policies']['L5']['anchors']), merged['l5_coverage']['required_count'])
        self.assertFalse(merged['eligible'])
        plan['selected_files'].remove(manifest['policies']['L5']['anchors'][0])
        with self.assertRaises(gate.GateError):
            routine_shards.merge_reports(plan, manifest, reports, run_id=RUN_ID, attempt=ATTEMPT)

    def test_worker_requires_explicit_full_plan_mode(self):
        plan = {'full_suite_required': False}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'plan.json'; path.write_text(json.dumps(plan))
            with patch.object(l6_ci.parallel.strict, 'read_json', return_value=plan), patch.object(gate, 'ensure_plan'):
                with self.assertRaisesRegex(gate.GateError, 'complete immutable'):
                    l6_ci.parallel.load_plan(path, l6=True)

    def test_l6_workflow_is_read_only_bounded_and_publication_is_required(self):
        import yaml
        value = yaml.safe_load((gate.ROOT / '.github/workflows/test-full-suite.yml').read_text())
        self.assertEqual({'contents': 'read'}, value['permissions'])
        self.assertEqual(set(l6_ci.ROLES), set(value['jobs']['l6']['needs']))
        self.assertEqual('always()', value['jobs']['l6']['if'])
        self.assertFalse(value['concurrency']['cancel-in-progress'])
        uploads = [step['with'] for job in value['jobs'].values() for step in job['steps']
                   if step.get('uses', '').startswith('actions/upload-artifact@')]
        self.assertEqual({1, 3, 7}, {row['retention-days'] for row in uploads})
        self.assertTrue(all(row['if-no-files-found'] == 'error' for row in uploads))
        for role in l6_ci.HOSTED:
            self.assertEqual(['1', '2'], value['jobs'][role]['strategy']['matrix']['repeat'])
        self.assertEqual(4, value['jobs']['unit']['strategy']['max-parallel'])
        self.assertNotIn('pull_request_target', value.get('on', value.get(True, {})))

    def test_l6_pinned_workflow_source_is_the_only_extra_hidden_path(self):
        self.assertEqual("verifier-source/.github/workflows/test-full-suite.yml",
                         l6_ci.bundle.safe_path("verifier-source/.github/workflows/test-full-suite.yml").as_posix())
        with self.assertRaises(gate.GateError):
            l6_ci.bundle.safe_path("verifier-source/.github/workflows/untrusted.yml")

    def test_stacked_pr_uses_same_read_only_mandatory_workflow(self):
        import yaml
        value = yaml.safe_load((gate.ROOT / '.github/workflows/test-gates.yml').read_text())
        triggers = value.get('on', value.get(True))
        # Keep this existing test method/node ID stable; PR #40 is now in main.
        self.assertEqual(['main'], triggers['pull_request']['branches'])
        self.assertEqual({'contents': 'read'}, value['permissions'])
        self.assertNotIn('pull_request_target', triggers)

    def test_l6_producers_reject_other_sha_attempt_and_modified_receipt(self):
        plan = {'source_commit': 'a' * 40, 'plan_digest': 'b' * 64,
                'manifest_digest': 'c' * 64, 'manifest_version': 1}
        environment = {'GITHUB_RUN_ID': RUN_ID, 'GITHUB_RUN_ATTEMPT': ATTEMPT}
        with tempfile.TemporaryDirectory() as tmp, patch.dict(l6_ci.os.environ, environment):
            root = Path(tmp); (root / 'receipt.json').write_text('{}')
            l6_ci.parallel.seal(root, 'hosted', plan, 'success')
            l6_ci.parallel.verify_producer(root, 'hosted', plan)
            for key, value in [('source_commit', 'd' * 40), ('plan_digest', 'd' * 64),
                               ('manifest_digest', 'd' * 64)]:
                changed = dict(plan, **{key: value})
                with self.subTest(key=key), self.assertRaises(gate.GateError):
                    l6_ci.parallel.verify_producer(root, 'hosted', changed)
            with patch.dict(l6_ci.os.environ, {'GITHUB_RUN_ATTEMPT': '99'}), self.assertRaises(gate.GateError):
                l6_ci.parallel.verify_producer(root, 'hosted', plan)
            (root / 'receipt.json').write_text('{"tampered":true}')
            with self.assertRaises(gate.GateError):
                l6_ci.parallel.verify_producer(root, 'hosted', plan)

    def test_bundle_requires_independent_identity_and_existing_evidence(self):
        from types import SimpleNamespace
        sha = 'a' * 40
        with tempfile.TemporaryDirectory() as tmp:
            args = SimpleNamespace(bundle=Path(tmp), expected_sha=sha, run_id='1', attempt='1',
                                   event='workflow_dispatch', ref='refs/heads/feature')
            with patch.object(gate, 'git', side_effect=[sha, '']):
                with self.assertRaises((OSError, gate.GateError)):
                    l6_ci.verify(args)  # Expired/missing evidence cannot be inferred from green metadata.
            metadata = {'schema': l6_ci.SCHEMA, 'repository': 'Coxf0rd/fwrouter',
                        'workflow': l6_ci.WORKFLOW, 'workflow_path': '.github/workflows/test-full-suite.yml',
                        'source_commit': sha, 'run_id': '1', 'run_attempt': '2',
                        'event': args.event, 'ref': args.ref, 'minimum_repeats': 2,
                        'deploy_eligibility': False}
            (Path(tmp) / 'l6-metadata.json').write_text(json.dumps(metadata))
            with patch.object(gate, 'git', side_effect=[sha, '']), self.assertRaisesRegex(gate.GateError, 'identity'):
                l6_ci.verify(args)

    def test_l6_full_manifest_excludes_only_l7_and_destructive_suites(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as tmp, patch.object(gate, 'print_json'):
            target = Path(tmp) / 'plan.json'
            gate.command_full_suite_plan(SimpleNamespace(manual=True, manifest=str(gate.MANIFEST), include_native=True, output=str(target)))
            plan = json.loads(target.read_text())
        expected = sorted(row['path'] for row in gate.load_manifest()['test_files']
                          if row['primary_level'] != 'L7' and not row.get('destructive'))
        self.assertEqual(expected, plan['selected_files'])
        self.assertTrue(plan['full_suite_required'])
        self.assertFalse(any(row['path'] in expected and row['primary_level'] == 'L7' for row in gate.load_manifest()['test_files']))


if __name__ == '__main__':
    unittest.main()
