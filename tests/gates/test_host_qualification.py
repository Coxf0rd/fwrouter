"""General launcher identity contracts plus retired QEMU node identities.

The QEMU methods intentionally fail if the reviewed gate plugin does not inject
their exact registry SKIPs. They are retained only for inventory continuity.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "acceptance"))
import launcher


class HostQualificationTests(unittest.TestCase):
    def test_absent_or_mismatched_github_context_fails_before_guest_factory(self):
        expected = {"hosted": True, "repository": "Coxf0rd/fwrouter", "source_sha": "a" * 40,
                    "workflow": "CI", "event": "push", "ref": "refs/heads/main",
                    "run_id": "123", "attempt": "2", "runner_environment": "github-hosted",
                    "runner_os": "Linux"}
        for env in ({"GITHUB_ACTIONS": "true", "GITHUB_REPOSITORY": "Coxf0rd/fwrouter",
                     "GITHUB_SHA": "a" * 40, "GITHUB_WORKFLOW": "CI", "GITHUB_EVENT_NAME": "push",
                     "GITHUB_REF": "refs/heads/main", "GITHUB_RUN_ID": "other", "GITHUB_RUN_ATTEMPT": "2"},
                    {"GITHUB_ACTIONS": "false"}):
            with self.subTest(env=env):
                self.assertTrue(launcher.github_run_context_mismatches(env, expected))

    def test_missing_github_context_fails_before_guest_factory(self):
        expected = {"hosted": True, "repository": "Coxf0rd/fwrouter", "source_sha": "a" * 40,
                    "workflow": "CI", "event": "push", "ref": "refs/heads/main",
                    "run_id": "123", "attempt": "2", "runner_environment": "github-hosted",
                    "runner_os": "Linux"}
        self.assertEqual(
            set(launcher.github_run_context_mismatches({}, expected)),
            {"hosted", "repository", "source_sha", "workflow", "event", "ref", "run_id", "attempt",
             "runner_environment", "runner_os"},
        )

    def test_admission_failure_is_structured_incomplete_and_nonpromotional(self):
        from smoke import run_smoke

        result = run_smoke("live-readonly")
        self.assertEqual(result["status"], "blocked_not_verified")
        self.assertEqual(result["checks"][0]["reason"], "live_readonly_requires_explicit_opt_in_and_reason")

    def test_checkout_source_mismatch_is_rejected(self):
        self.fail("retired QEMU node must be skipped by the exact reviewed policy")

    def test_dirty_checkout_is_rejected_even_when_head_sha_matches(self):
        self.fail("retired QEMU node must be skipped by the exact reviewed policy")

    def test_guest_backing_chain_or_disk_overrun_is_rejected_before_qemu(self):
        self.fail("retired QEMU node must be skipped by the exact reviewed policy")

    def test_guest_boot_systemd_reboot_and_cleanup_failures_never_pass(self):
        self.fail("retired QEMU node must be skipped by the exact reviewed policy")

    def test_missing_or_invalid_capability_pin_and_headroom_fail_closed(self):
        self.fail("retired QEMU node must be skipped by the exact reviewed policy")

    def test_missing_pin_is_structured_admission_incomplete(self):
        self.fail("retired QEMU node must be skipped by the exact reviewed policy")

    def test_qemu_guest_cleanup_reaps_only_its_owned_process_group(self):
        self.fail("retired QEMU node must be skipped by the exact reviewed policy")

    def test_qemu_guest_reboot_rechecks_new_boot_and_required_readiness(self):
        self.fail("retired QEMU node must be skipped by the exact reviewed policy")

    def test_qemu_guest_uuid_debian_identity_and_systemd_probes_are_pinned(self):
        self.fail("retired QEMU node must be skipped by the exact reviewed policy")

    def test_qga_delimited_sync_frames_command_and_closes_descriptor(self):
        self.fail("retired QEMU node must be skipped by the exact reviewed policy")

    def test_qga_partial_slow_drip_frame_cannot_extend_absolute_deadline(self):
        self.fail("retired QEMU node must be skipped by the exact reviewed policy")

    def test_real_command_spec_is_tcg_and_user_network_without_host_passthrough(self):
        self.fail("retired QEMU node must be skipped by the exact reviewed policy")

    def test_reboot_uses_one_deadline_instead_of_boot_timeout_per_retry(self):
        self.fail("retired QEMU node must be skipped by the exact reviewed policy")

    def test_successful_controller_flow_is_nonpromotional_and_cleans_up(self):
        self.fail("retired QEMU node must be skipped by the exact reviewed policy")

    def test_timeouts_must_be_finite_and_bounded(self):
        self.fail("retired QEMU node must be skipped by the exact reviewed policy")
