from __future__ import annotations

import hashlib
import base64
import json
import os
import socket
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from dataclasses import replace
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parents[1] / "acceptance"))
import host_qualification as host


class FakeGuest:
    def __init__(self, qemu, qemu_img, guest, work, limits, expected_guest_os_version, *, fail=None):
        self.fail = fail
        self.work = work
        self.cleaned = False

    def start(self):
        if self.fail == "start":
            raise host.QualificationError("synthetic start failure")

    def boot_id(self):
        if self.fail == "boot":
            raise host.QualificationError("synthetic boot timeout")
        return "boot-one" if not getattr(self, "rebooted", False) else "boot-two"

    def assert_systemd(self):
        if self.fail == "systemd" or (self.fail == "systemd_post_reboot" and self.rebooted):
            raise host.QualificationError("synthetic non-systemd guest")

    def assert_guest_identity(self):
        if self.fail == "debian_identity":
            raise host.QualificationError("synthetic Debian identity mismatch")

    def reboot(self, previous_boot_id):
        if previous_boot_id != "boot-one":
            raise host.QualificationError("synthetic stale boot identity")
        if self.fail == "reboot":
            raise host.QualificationError("synthetic reconnect timeout")
        self.rebooted = True

    def cleanup(self):
        self.cleaned = True
        if self.fail == "cleanup":
            raise host.QualificationError("synthetic process leak")


class HostQualificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.qemu_path = root / "qemu"
        self.qemu_path.write_bytes(b"qualified-qemu-test-fixture")
        self.qemu_path.chmod(0o700)
        self.qemu_img_path = root / "qemu-img"
        self.qemu_img_path.write_bytes(b"qualified-qemu-img-test-fixture")
        self.qemu_img_path.chmod(0o700)
        self.guest_path = root / "debian.img"
        self.guest_path.write_bytes(b"pinned-debian-image-fixture")
        self.qemu = host.Pin(self.qemu_path, hashlib.sha256(self.qemu_path.read_bytes()).hexdigest())
        self.qemu_img = host.Pin(self.qemu_img_path,
                                 hashlib.sha256(self.qemu_img_path.read_bytes()).hexdigest())
        self.guest = host.Pin(self.guest_path, hashlib.sha256(self.guest_path.read_bytes()).hexdigest())
        source_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=host.launcher.ROOT,
                                             text=True).strip()
        self.source_sha = source_sha
        self.env = mock.patch.dict(os.environ, {
            "GITHUB_ACTIONS": "true", "RUNNER_ENVIRONMENT": "github-hosted", "RUNNER_OS": "Linux",
            "GITHUB_REPOSITORY": "Coxf0rd/fwrouter", "GITHUB_SHA": source_sha,
            "GITHUB_WORKFLOW": "Host qualification", "GITHUB_EVENT_NAME": "workflow_dispatch",
            "GITHUB_REF": "refs/heads/stage6/ci-test-architecture", "GITHUB_RUN_ID": "123456",
            "GITHUB_RUN_ATTEMPT": "2",
        })
        self.env.start()
        self.run = host.VerifiedRun(
            verifier="external-github-verifier/v1", repository="Coxf0rd/fwrouter",
            source_sha=source_sha, workflow="Host qualification", event="workflow_dispatch",
            ref="refs/heads/stage6/ci-test-architecture", run_id="123456", attempt=2,
            runner_environment="github-hosted", runner_os="Linux", runner_class="ubuntu-22.04-x64",
            image_revision="runner-image-revision-fixture", nonce="f" * 32,
            qemu_sha256=self.qemu.sha256, qemu_img_sha256=self.qemu_img.sha256,
            guest_sha256=self.guest.sha256,
            guest_source="https://example.invalid/debian/image.raw",
            guest_os_id="debian", guest_os_version="12",
            hosted=True, sudo_passwordless=True,
            capabilities=frozenset({"qemu-tcg", "isolated-user-network", "controller-cleanup"}),
            available_cpus=4, available_memory_mib=8192, available_disk_mib=14336,
        )
        original_run = host.subprocess.run

        def synthetic_clean_git(argv, *args, **kwargs):
            if argv[0] == "git" and argv[1] == "rev-parse":
                return subprocess.CompletedProcess(argv, 0, stdout=(self.source_sha + "\n").encode(), stderr=b"")
            if argv[0] == "git" and argv[1] == "status":
                return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")
            if argv[0] == "git" and argv[1] == "ls-files":
                return subprocess.CompletedProcess(
                    argv, 0,
                    stdout=b"tests/acceptance/host_qualification.py\ntests/acceptance/launcher.py\n",
                    stderr=b"")
            return original_run(argv, *args, **kwargs)

        self.git_patch = mock.patch.object(host.subprocess, "run", side_effect=synthetic_clean_git)
        self.git_patch.start()

    def tearDown(self):
        self.git_patch.stop()
        self.env.stop()
        self.temp.cleanup()

    def test_absent_or_mismatched_github_context_fails_before_guest_factory(self):
        for run in (None, self.run):
            with self.subTest(run_missing=run is None):
                with mock.patch.dict(os.environ, {"GITHUB_RUN_ID": "other"}):
                    with self.assertRaises(host.QualificationError):
                        host.qualify(run, self.qemu, self.qemu_img, self.guest,
                                     guest_factory=mock.Mock(side_effect=AssertionError("launched")))

    def test_missing_or_invalid_capability_pin_and_headroom_fail_closed(self):
        bad_capability = replace(self.run, capabilities=frozenset())
        bad_digest = host.Pin(self.guest_path, "0" * 64)
        bad_qemu_img = host.Pin(self.qemu_img_path, "0" * 64)
        low_headroom = replace(self.run, available_cpus=2)
        for run, qemu, qemu_img, guest in (
                (bad_capability, self.qemu, self.qemu_img, self.guest),
                (self.run, self.qemu, self.qemu_img, bad_digest),
                (low_headroom, self.qemu, self.qemu_img, self.guest),
                (self.run, None, self.qemu_img, self.guest),
                (self.run, self.qemu, None, self.guest)):
            with self.subTest(run=run, qemu=qemu, guest=guest):
                with self.assertRaises(host.QualificationError):
                    host.admit(run, qemu, qemu_img, guest, host.Limits())
        with self.assertRaises(host.QualificationError):
            host.admit(self.run, self.qemu, bad_qemu_img, self.guest, host.Limits())

    def test_timeouts_must_be_finite_and_bounded(self):
        for value in (float("nan"), float("inf"), -float("inf"), 0, 601):
            with self.subTest(timeout=value):
                with self.assertRaises(host.QualificationError):
                    host.admit(self.run, self.qemu, self.qemu_img, self.guest,
                               host.Limits(boot_timeout=value))

    def test_missing_pin_is_structured_admission_incomplete(self):
        missing = host.Pin(Path(self.temp.name) / "missing-qemu", self.qemu.sha256)
        with self.assertRaises(host.QualificationError) as caught:
            host.qualify(self.run, missing, self.qemu_img, self.guest)
        receipt = json.loads(str(caught.exception))
        self.assertEqual(receipt["status"], "INCOMPLETE")
        self.assertEqual(receipt["phase"], "admission")
        self.assertIn("QEMU pin could not be read", receipt["reason"])
        self.assertNotIn(str(self.temp.name), receipt["reason"])

    def test_checkout_source_mismatch_is_rejected(self):
        wrong_source = replace(self.run, source_sha="b" * 40)
        with mock.patch.dict(os.environ, {"GITHUB_SHA": "b" * 40}):
            with self.assertRaisesRegex(host.QualificationError, "checked-out source"):
                host.admit(wrong_source, self.qemu, self.qemu_img, self.guest, host.Limits())

    def test_dirty_checkout_is_rejected_even_when_head_sha_matches(self):
        source_sha = self.run.source_sha

        def dirty_git(argv, **kwargs):
            if argv[1] == "rev-parse":
                return subprocess.CompletedProcess(argv, 0, stdout=(source_sha + "\n").encode(), stderr=b"")
            if argv[1] == "status":
                return subprocess.CompletedProcess(argv, 0, stdout=b"?? untracked-controller.py\n", stderr=b"")
            return subprocess.CompletedProcess(
                argv, 0,
                stdout=b"tests/acceptance/host_qualification.py\ntests/acceptance/launcher.py\n",
                stderr=b"")

        with mock.patch.object(host.subprocess, "run", side_effect=dirty_git):
            with self.assertRaisesRegex(host.QualificationError, "checked-out source"):
                host.admit(self.run, self.qemu, self.qemu_img, self.guest, host.Limits())

    def test_successful_controller_flow_is_nonpromotional_and_cleans_up(self):
        instances = []

        def factory(*args, **kwargs):
            guest = FakeGuest(*args, **kwargs)
            instances.append(guest)
            return guest

        receipt = host.qualify(self.run, self.qemu, self.qemu_img, self.guest, guest_factory=factory)
        self.assertEqual(receipt["status"], "INCOMPLETE")
        self.assertEqual(receipt["evidence_scope"], "synthetic_controller_fixture")
        self.assertTrue(receipt["requires_external_provenance_verification"])
        self.assertFalse(receipt["deploy_eligibility"])
        self.assertEqual(receipt["input_sha256"]["qemu_img"], self.qemu_img.sha256)
        self.assertEqual(receipt["capabilities"], sorted(self.run.capabilities))
        self.assertEqual(receipt["verifier_reported_host_resources"]["cpus"], self.run.available_cpus)
        self.assertEqual(receipt["guest_resource_limits"]["memory_mib"], host.Limits().memory_mib)
        self.assertTrue(receipt["controller_cleanup_completed"])
        self.assertTrue(receipt["workspace_removed"])
        self.assertTrue(instances[0].cleaned)

    def test_missing_github_context_fails_before_guest_factory(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(host.QualificationError):
                host.qualify(self.run, self.qemu, self.qemu_img, self.guest,
                             guest_factory=mock.Mock(side_effect=AssertionError("launched")))

    def test_admission_failure_is_structured_incomplete_and_nonpromotional(self):
        with mock.patch.dict(os.environ, {"RUNNER_ENVIRONMENT": "self-hosted"}):
            with self.assertRaises(host.QualificationError) as caught:
                host.qualify(self.run, self.qemu, self.qemu_img, self.guest,
                             guest_factory=mock.Mock(side_effect=AssertionError("launched")))
        receipt = json.loads(str(caught.exception))
        self.assertEqual(receipt["status"], "INCOMPLETE")
        self.assertEqual(receipt["phase"], "admission")
        self.assertEqual(receipt["source_sha"], self.run.source_sha)
        self.assertEqual(receipt["run_id"], self.run.run_id)
        self.assertEqual(receipt["attempt"], self.run.attempt)
        self.assertTrue(receipt["requires_external_provenance_verification"])
        self.assertFalse(receipt["deploy_eligibility"])

    def test_guest_boot_systemd_reboot_and_cleanup_failures_never_pass(self):
        for failure in ("start", "boot", "systemd", "reboot", "systemd_post_reboot",
                        "debian_identity", "cleanup"):
            with self.subTest(failure=failure):
                instances = []

                def factory(*args, **kwargs):
                    guest = FakeGuest(*args, **kwargs, fail=failure)
                    instances.append(guest)
                    return guest

                with self.assertRaises(host.QualificationError):
                    host.qualify(self.run, self.qemu, self.qemu_img, self.guest, guest_factory=factory)
                self.assertTrue(instances[0].cleaned)
                self.assertEqual(instances[0].work.exists(), failure == "cleanup")
                shutil.rmtree(instances[0].work, ignore_errors=True)

    def test_qemu_guest_uuid_debian_identity_and_systemd_probes_are_pinned(self):
        guest = host.QemuGuest(self.qemu_path, self.qemu_img_path, self.guest_path,
                               Path(self.temp.name), host.Limits(), "12")
        expected_boot = str(uuid.uuid4())
        with mock.patch.object(guest, "_exec_until", side_effect=[expected_boot + "\n"]):
            self.assertEqual(guest.boot_id(), expected_boot)
        with mock.patch.object(guest, "_exec_until", return_value="not-a-uuid\n"):
            with self.assertRaisesRegex(host.QualificationError, "not a UUID"):
                guest.boot_id()
        with mock.patch.object(guest, "_exec_until", return_value='ID=debian\nVERSION_ID="12"\n') as execute:
            guest.assert_guest_identity()
        self.assertEqual(execute.call_args.args[0], ["/bin/cat", "/etc/os-release"])
        with mock.patch.object(guest, "_exec_until", side_effect=["systemd\n", "running\n"]) as execute:
            guest.assert_systemd()
        self.assertEqual([call.args[0] for call in execute.call_args_list], [
            ["/bin/cat", "/proc/1/comm"], ["/bin/systemctl", "is-system-running"]])
        wrong_version = host.QemuGuest(self.qemu_path, self.qemu_img_path, self.guest_path,
                                       Path(self.temp.name), host.Limits(), "13")
        with mock.patch.object(wrong_version, "_exec_until", return_value="ID=debian\nVERSION_ID=12\n"):
            with self.assertRaisesRegex(host.QualificationError, "differs from its verified pin"):
                wrong_version.assert_guest_identity()

    def test_qemu_guest_cleanup_reaps_only_its_owned_process_group(self):
        guest = host.QemuGuest(self.qemu_path, self.qemu_img_path, self.guest_path,
                               Path(self.temp.name), host.Limits(cleanup_timeout=2), "12")
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True,
                                 env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"})
        guest.process = child
        owned_pid = child.pid
        guest.cleanup()
        self.assertIsNotNone(child.poll())
        with self.assertRaises(ProcessLookupError):
            os.killpg(owned_pid, 0)

    def test_qemu_guest_reboot_rechecks_new_boot_and_required_readiness(self):
        guest = host.QemuGuest(self.qemu_path, self.qemu_img_path, self.guest_path,
                               Path(self.temp.name), host.Limits(), "12")
        old_boot, new_boot = str(uuid.uuid4()), str(uuid.uuid4())
        with mock.patch.object(guest, "_exec_until", side_effect=host.QualificationError("agent reconnect")), \
             mock.patch.object(guest, "boot_id", return_value=new_boot) as boot, \
             mock.patch.object(guest, "assert_systemd") as systemd, \
             mock.patch.object(guest, "assert_guest_identity") as identity:
            guest.reboot(old_boot)
        self.assertEqual(boot.call_args.kwargs["deadline"], systemd.call_args.kwargs["deadline"])
        self.assertEqual(boot.call_args.kwargs["deadline"], identity.call_args.kwargs["deadline"])
        self.assertGreater(boot.call_args.kwargs["deadline"], time.monotonic())

    def test_real_command_spec_is_tcg_and_user_network_without_host_passthrough(self):
        guest = host.QemuGuest(self.qemu_path, self.qemu_img_path, self.guest_path,
                               Path(self.temp.name), host.Limits(), "12")
        fake_process = mock.Mock()
        fake_process.returncode = 0
        info = mock.Mock(returncode=0, stdout=b'{"format":"raw","virtual-size":2097152}')
        with mock.patch.object(host.subprocess, "run", side_effect=[info, fake_process]) as run, \
             mock.patch.object(host.subprocess, "Popen", return_value=mock.Mock()) as popen:
            guest.start()
        self.assertEqual(run.call_args_list[0].args[0][0], str(self.qemu_img_path))
        argv = popen.call_args.args[0]
        self.assertIn("q35,accel=tcg", argv)
        self.assertIn("user,id=net0,restrict=on", argv)
        self.assertEqual(popen.call_args.kwargs["env"],
                         {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"})
        self.assertFalse(any("hostfwd" in str(arg) or "host_device" in str(arg) for arg in argv))
        self.assertFalse(any(arg in {"-hostfwd", "-netdev", "host", "-device", "vfio-pci"}
                             and str(arg) == "host" for arg in argv))
        self.assertNotIn("-no-reboot", argv)

    def test_guest_backing_chain_or_disk_overrun_is_rejected_before_qemu(self):
        guest = host.QemuGuest(self.qemu_path, self.qemu_img_path, self.guest_path,
                               Path(self.temp.name), host.Limits(disk_mib=1024), "12")
        for raw in (b'{"format":"raw","virtual-size":2097152,"backing-filename":"base.raw"}',
                    b'{"format":"raw","virtual-size":1073741825}'):
            with self.subTest(image_info=raw):
                info = mock.Mock(returncode=0, stdout=raw)
                with mock.patch.object(host.subprocess, "run", return_value=info), \
                     mock.patch.object(host.subprocess, "Popen") as popen:
                    with self.assertRaises(host.QualificationError):
                        guest.start()
                popen.assert_not_called()

    def test_qga_delimited_sync_frames_command_and_closes_descriptor(self):
        agent_socket = Path(self.temp.name) / "qga.sock"
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(agent_socket))
        listener.listen(1)
        guest = host.QemuGuest(self.qemu_path, self.qemu_img_path, self.guest_path,
                               Path(self.temp.name), host.Limits(), "12")
        guest.process = mock.Mock()
        guest.process.poll.return_value = None
        exchanges = []
        server_errors = []

        def serve():
            try:
                connection, _ = listener.accept()
                with connection:
                    connection.settimeout(2)
                    self.assertEqual(connection.recv(1), b"\xff")
                    stream = connection.makefile("rb")
                    sync = json.loads(stream.readline(8193))
                    exchanges.append(sync)
                    sync_id = sync["arguments"]["id"]
                    connection.sendall(b"stale-partial-frame\n\xff" +
                                       json.dumps({"return": sync_id}).encode() + b"\n")
                    ping = json.loads(stream.readline(8193))
                    exchanges.append(ping)
                    connection.sendall(b'{"return":{}}\n')
                    execute = json.loads(stream.readline(8193))
                    exchanges.append(execute)
                    connection.sendall(b'{"return":{"pid":77}}\n')
                    status = json.loads(stream.readline(8193))
                    exchanges.append(status)
                    output = base64.b64encode(b"guest-output\n").decode()
                    connection.sendall((json.dumps({"return": {"exited": True, "exitcode": 0,
                                                        "out-data": output}}) + "\n").encode())
                    self.assertEqual(connection.recv(1), b"")
                    stream.close()
            except Exception as exc:
                server_errors.append(exc)
            finally:
                listener.close()

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        try:
            output = guest._exec(["/bin/echo", "synthetic"], 2)
        except host.QualificationError as exc:
            thread.join(timeout=3)
            self.fail(f"QGA socket exchange failed: {exc}; seen={exchanges}; server={server_errors}")
        thread.join(timeout=3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(server_errors, [])
        self.assertEqual(output, "guest-output\n")
        self.assertEqual([item["execute"] for item in exchanges],
                         ["guest-sync-delimited", "guest-ping", "guest-exec", "guest-exec-status"])
        self.assertEqual(exchanges[2]["arguments"]["path"], "/bin/echo")

    def test_qga_partial_slow_drip_frame_cannot_extend_absolute_deadline(self):
        sender, receiver = socket.socketpair()
        reader = host._QGAReader(receiver)

        def drip():
            try:
                for byte in b'{"return":0}\n':
                    sender.sendall(bytes((byte,)))
                    time.sleep(0.025)
            except OSError:
                pass
            finally:
                sender.close()

        thread = threading.Thread(target=drip, daemon=True)
        thread.start()
        started = time.monotonic()
        try:
            with self.assertRaisesRegex(host.QualificationError, "deadline"):
                reader.readline(started + 0.06)
        finally:
            receiver.close()
        elapsed = time.monotonic() - started
        thread.join(timeout=1)
        self.assertFalse(thread.is_alive())
        self.assertLess(elapsed, 0.16)

    def test_reboot_uses_one_deadline_instead_of_boot_timeout_per_retry(self):
        limits = host.Limits(boot_timeout=600, reboot_timeout=0.25, command_timeout=0.1)
        guest = host.QemuGuest(self.qemu_path, self.qemu_img_path, self.guest_path,
                               Path(self.temp.name), limits, "12")
        deadlines = []
        started = time.monotonic()
        boot_id = str(uuid.uuid4())

        def execute(command, deadline):
            deadlines.append((command, deadline))
            if command[-1] == "reboot":
                raise host.QualificationError("synthetic guest-agent close during reboot")
            raise host.QualificationError("synthetic agent remains unavailable")

        with mock.patch.object(guest, "_exec_until", side_effect=execute), \
             mock.patch.object(host.time, "sleep", side_effect=lambda _: None):
            with self.assertRaisesRegex(host.QualificationError, "reboot/reconnection timed out"):
                guest.reboot(boot_id)
        self.assertGreaterEqual(len(deadlines), 2)
        reboot_deadline = deadlines[0][1]
        self.assertLessEqual(reboot_deadline - started, limits.command_timeout + 0.02)
        self.assertTrue(all(deadline <= started + limits.reboot_timeout + 0.02
                            for _, deadline in deadlines))


if __name__ == "__main__":
    unittest.main()
