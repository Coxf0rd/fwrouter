#!/usr/bin/env python3
"""Fail-closed, opt-in disposable QEMU/TCG host qualification controller.

The controller owns the QEMU process and an ephemeral overlay. GitHub run
provenance is deliberately supplied by a separate trusted verifier; environment
variables or this module's input record are not cryptographic attestations.
"""
from __future__ import annotations

import hashlib
import json
import os
import base64
import contextlib
import math
import shutil
import signal
import socket
import stat
import subprocess
import tempfile
import time
import secrets
import shlex
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

import launcher


class QualificationError(RuntimeError):
    """Qualification could not be admitted or completed."""


def _identity(run: "VerifiedRun | None") -> dict[str, Any]:
    if run is None:
        return {"identity_available": False}
    return {"identity_available": True, "source_sha": run.source_sha,
            "repository": run.repository, "workflow": run.workflow, "event": run.event,
            "ref": run.ref, "run_id": run.run_id, "attempt": run.attempt,
            "nonce": run.nonce, "runner_class": run.runner_class,
            "runner_environment": run.runner_environment, "runner_os": run.runner_os,
            "guest_os_id": run.guest_os_id, "guest_os_version": run.guest_os_version,
            "image_revision": run.image_revision}


@dataclass(frozen=True)
class VerifiedRun:
    """Identity asserted by an independently authenticated run verifier."""

    verifier: str
    repository: str
    source_sha: str
    workflow: str
    event: str
    ref: str
    run_id: str
    attempt: int
    runner_environment: str
    runner_os: str
    runner_class: str
    image_revision: str
    nonce: str
    qemu_sha256: str
    qemu_img_sha256: str
    guest_sha256: str
    guest_source: str
    guest_os_id: str
    guest_os_version: str
    hosted: bool
    sudo_passwordless: bool
    capabilities: frozenset[str]
    available_cpus: int
    available_memory_mib: int
    available_disk_mib: int


@dataclass(frozen=True)
class Pin:
    path: Path
    sha256: str


@dataclass(frozen=True)
class Limits:
    vcpus: int = 2
    memory_mib: int = 2048
    disk_mib: int = 4096
    boot_timeout: float = 120
    reboot_timeout: float = 90
    command_timeout: float = 10
    cleanup_timeout: float = 10


class _QGAReader:
    """Bounded line reader that recomputes socket timeout against one deadline."""

    def __init__(self, connection: socket.socket):
        self.connection = connection
        self.buffer = bytearray()

    def _receive(self, deadline: float, max_bytes: int) -> None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise QualificationError("guest agent frame deadline expired")
        self.connection.settimeout(remaining)
        try:
            data = self.connection.recv(min(4096, max_bytes))
        except socket.timeout as exc:
            raise QualificationError("guest agent frame deadline expired") from exc
        if not data:
            raise QualificationError("guest agent closed before completing a frame")
        self.buffer.extend(data)

    def discard_to_sentinel(self, deadline: float, *, max_discard: int = 65536) -> None:
        discarded = 0
        while True:
            sentinel = self.buffer.find(b"\xff")
            if sentinel >= 0:
                discarded += sentinel + 1
                if discarded > max_discard:
                    raise QualificationError("guest agent synchronization exceeded its frame limit")
                del self.buffer[:sentinel + 1]
                return
            discarded += len(self.buffer)
            self.buffer.clear()
            if discarded >= max_discard:
                raise QualificationError("guest agent synchronization exceeded its frame limit")
            self._receive(deadline, max_discard - discarded)

    def readline(self, deadline: float, *, max_bytes: int = 8192) -> bytes:
        while True:
            newline = self.buffer.find(b"\n")
            if newline >= 0:
                if newline + 1 > max_bytes:
                    raise QualificationError("guest agent response exceeded its frame limit")
                frame = bytes(self.buffer[:newline + 1])
                del self.buffer[:newline + 1]
                return frame
            if len(self.buffer) >= max_bytes:
                raise QualificationError("guest agent response exceeded its frame limit")
            self._receive(deadline, max_bytes - len(self.buffer))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _validate_pin(pin: Pin, label: str) -> Path:
    if not isinstance(pin.sha256, str) or len(pin.sha256) != 64 or any(c not in "0123456789abcdef" for c in pin.sha256):
        raise QualificationError(f"{label} pin must be a lowercase SHA-256 digest")
    try:
        info = pin.path.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise QualificationError(f"{label} pin must name a regular non-symlink file")
        path = pin.path.resolve(strict=True)
        if _sha256(path) != pin.sha256:
            raise QualificationError(f"{label} digest does not match its pin")
        return path
    except QualificationError:
        raise
    except (OSError, TypeError, ValueError) as exc:
        raise QualificationError(f"{label} pin could not be read") from exc


def admit(run: VerifiedRun | None, qemu: Pin | None, qemu_img: Pin | None,
          guest: Pin | None, limits: Limits) -> tuple[Path, Path, Path]:
    """Check every admission condition before any process or disk is created."""
    if run is None:
        raise QualificationError("external GitHub run verification is required")
    if (not run.verifier or not run.repository or not run.hosted
            or run.runner_class in {"", "self-hosted", "ubuntu-slim"}
            or not run.image_revision or not run.sudo_passwordless
            or not isinstance(run.source_sha, str) or len(run.source_sha) != 40
            or any(c not in "0123456789abcdef" for c in run.source_sha)
            or not run.run_id or not isinstance(run.attempt, int) or isinstance(run.attempt, bool)
            or run.attempt < 1):
        raise QualificationError("run identity or hosted-runner admission is incomplete")
    expected = {
        "hosted": run.hosted, "runner_environment": run.runner_environment,
        "runner_os": run.runner_os, "repository": run.repository,
        "source_sha": run.source_sha, "workflow": run.workflow,
        "event": run.event, "ref": run.ref, "run_id": run.run_id,
        "attempt": str(run.attempt),
    }
    if launcher.github_run_context_mismatches(dict(os.environ), expected):
        raise QualificationError("verified run record does not match the actual GitHub hosted context")
    if run.runner_environment != "github-hosted" or run.runner_os != "Linux":
        raise QualificationError("host qualification requires a GitHub-hosted Linux runner")
    try:
        checkout = subprocess.run(["git", "rev-parse", "HEAD"], cwd=launcher.ROOT,
                                  capture_output=True, timeout=3, check=False)
        cleanliness = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"],
                                     cwd=launcher.ROOT, capture_output=True, timeout=3, check=False)
        tracked = subprocess.run(["git", "ls-files", "--error-unmatch",
                                  "tests/acceptance/host_qualification.py",
                                  "tests/acceptance/launcher.py"], cwd=launcher.ROOT,
                                 capture_output=True, timeout=3, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise QualificationError("checked-out source identity could not be verified") from exc
    if (checkout.returncode or checkout.stdout.decode("ascii", "ignore").strip() != run.source_sha
            or cleanliness.returncode or cleanliness.stdout or tracked.returncode):
        raise QualificationError("verified source SHA does not match the checked-out source")
    if (qemu is None or qemu_img is None or len(run.nonce) < 32
            or run.qemu_sha256 != qemu.sha256 or run.qemu_img_sha256 != qemu_img.sha256
            or len(run.nonce) != 32 or any(c not in "0123456789abcdef" for c in run.nonce)
            or not run.guest_source):
        raise QualificationError("verified nonce or pinned input digests are incomplete")
    if guest is None or run.guest_sha256 != guest.sha256:
        raise QualificationError("verified guest image digest does not match its pin")
    if run.guest_os_id != "debian" or not run.guest_os_version:
        raise QualificationError("verified Debian guest identity/version is required")
    if not isinstance(run.guest_source, str):
        raise QualificationError("verified guest source is missing")
    source_url = urlsplit(run.guest_source)
    if (source_url.scheme != "https" or not source_url.hostname or source_url.username
            or source_url.password or source_url.query or source_url.fragment):
        raise QualificationError("guest source must be a credential-free HTTPS URL")
    required = {"qemu-tcg", "isolated-user-network", "controller-cleanup"}
    if (not isinstance(run.capabilities, (set, frozenset))
            or any(not isinstance(capability, str) for capability in run.capabilities)
            or not required.issubset(run.capabilities)):
        raise QualificationError("required host qualification capabilities are absent")
    timeout_values = (limits.boot_timeout, limits.reboot_timeout,
                      limits.command_timeout, limits.cleanup_timeout)
    timeout_maxima = (600, 300, 60, 60)
    if (not all(isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(value) and 0 < value <= maximum
                for value, maximum in zip(timeout_values, timeout_maxima))
            or not isinstance(limits.vcpus, int) or isinstance(limits.vcpus, bool)
            or not isinstance(limits.memory_mib, int) or isinstance(limits.memory_mib, bool)
            or not isinstance(limits.disk_mib, int) or isinstance(limits.disk_mib, bool)
            or limits.vcpus < 1 or limits.vcpus > 4 or limits.memory_mib < 512
            or limits.memory_mib > 8192 or limits.disk_mib < 1024 or limits.disk_mib > 8192):
        raise QualificationError("resource limits are outside controller bounds")
    if (run.available_cpus < limits.vcpus + 1
            or run.available_memory_mib < limits.memory_mib + 1024
            or run.available_disk_mib < limits.disk_mib + 2048):
        raise QualificationError("verified runner headroom is insufficient")
    if qemu is None or qemu_img is None or guest is None:
        raise QualificationError("pinned QEMU, qemu-img, and Debian guest image are required")
    qemu_path = _validate_pin(qemu, "QEMU")
    qemu_img_path = _validate_pin(qemu_img, "qemu-img")
    guest_path = _validate_pin(guest, "guest image")
    if not os.access(qemu_path, os.X_OK) or not os.access(qemu_img_path, os.X_OK):
        raise QualificationError("pinned QEMU tools must be executable")
    return qemu_path, qemu_img_path, guest_path


class QemuGuest:
    """Bounded QEMU process with guest-agent-only control and no host mounts."""

    def __init__(self, qemu: Path, qemu_img: Path, guest: Path, work: Path,
                 limits: Limits, expected_guest_os_version: str):
        self.qemu, self.guest, self.work, self.limits = qemu, guest, work, limits
        self.qemu_img = qemu_img
        self.expected_guest_os_version = expected_guest_os_version
        self.process: subprocess.Popen[bytes] | None = None
        self.agent_path = work / "qga.sock"
        self.overlay = work / "guest-overlay.qcow2"

    def start(self) -> None:
        minimal_env = {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"}
        info = subprocess.run([str(self.qemu_img), "info", "--output=json", str(self.guest)],
                              capture_output=True, env=minimal_env,
                              timeout=self.limits.command_timeout, check=False)
        if info.returncode:
            raise QualificationError("guest image format could not be read")
        try:
            image_info = json.loads(info.stdout)
            image_format = image_info["format"]
            image_size = image_info["virtual-size"]
        except (ValueError, KeyError, TypeError) as exc:
            raise QualificationError("guest image format response is invalid") from exc
        if image_format != "raw" or image_info.get("backing-filename") or image_info.get("full-backing-filename"):
            raise QualificationError("guest image must be a standalone raw image without backing files")
        if not isinstance(image_size, int) or image_size <= 0 or image_size > self.limits.disk_mib * 1024 * 1024:
            raise QualificationError("guest image size exceeds the bounded overlay disk allocation")
        result = subprocess.run([str(self.qemu_img), "create", "-f", "qcow2", "-F", "raw",
                                 "-b", str(self.guest), "-o", f"size={image_size}",
                                 str(self.overlay)],
                                capture_output=True, env=minimal_env,
                                timeout=self.limits.command_timeout, check=False)
        if result.returncode:
            raise QualificationError("ephemeral guest overlay could not be created")
        argv = [str(self.qemu), "-machine", "q35,accel=tcg", "-m", str(self.limits.memory_mib),
                "-smp", str(self.limits.vcpus), "-nodefaults", "-no-user-config", "-nographic",
                "-monitor", "none",
                "-drive", f"file={self.overlay},if=virtio,format=qcow2",
                "-netdev", "user,id=net0,restrict=on", "-device", "virtio-net-pci,netdev=net0",
                "-chardev", f"socket,path={self.agent_path},server=on,wait=off,id=qga0",
                "-device", "virtio-serial", "-device",
                "virtserialport,chardev=qga0,name=org.qemu.guest_agent.0"]
        self.process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL, start_new_session=True,
                                        env=minimal_env)

    @staticmethod
    def _read_response(reader: _QGAReader, deadline: float) -> dict[str, Any]:
        raw = reader.readline(deadline)
        if not raw.endswith(b"\n"):
            raise QualificationError("guest agent response exceeded its frame limit")
        try:
            response = json.loads(raw)
        except (ValueError, json.JSONDecodeError) as exc:
            raise QualificationError("guest agent response was not one JSON frame") from exc
        if not isinstance(response, dict):
            raise QualificationError("guest agent response was not an object")
        if "error" in response or "return" not in response:
            raise QualificationError("guest agent command returned an error")
        return response

    @staticmethod
    def _request(connection: socket.socket, execute: str,
                 arguments: dict[str, Any] | None = None) -> None:
        request = {"execute": execute}
        if arguments is not None:
            request["arguments"] = arguments
        connection.sendall((json.dumps(request, separators=(",", ":")) + "\n").encode("ascii"))

    @staticmethod
    def _set_deadline(connection: socket.socket, deadline: float) -> None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise QualificationError("guest-agent operation deadline expired")
        connection.settimeout(remaining)

    @contextlib.contextmanager
    def _agent(self, deadline: float):
        """Connect with QGA's delimited synchronization before parsing frames."""
        while time.monotonic() < deadline:
            if self.process is None or self.process.poll() is not None:
                raise QualificationError("QEMU exited before guest readiness")
            connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            ready = False
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    connection.close()
                    break
                connection.settimeout(min(1.0, remaining))
                connection.connect(str(self.agent_path))
                reader = _QGAReader(connection)
                # QGA's JSON integer field is signed 64-bit.
                sync_id = secrets.randbits(63)
                # QGA requires a leading sentinel to discard any partially read
                # frame, then ignores input until it returns this sync ID.
                connection.sendall(b"\xff")
                self._request(connection, "guest-sync-delimited", {"id": sync_id})
                reader.discard_to_sentinel(deadline)
                self._set_deadline(connection, deadline)
                sync_reply = self._read_response(reader, deadline)
                if sync_reply["return"] != sync_id:
                    raise QualificationError("guest agent synchronization identity did not match")
                self._request(connection, "guest-ping")
                self._set_deadline(connection, deadline)
                self._read_response(reader, deadline)
                ready = True
            except OSError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(min(0.1, max(0, deadline - time.monotonic())))
            except QualificationError:
                connection.close()
                raise
            if ready:
                try:
                    yield connection, reader
                finally:
                    connection.close()
                return
            connection.close()
        raise QualificationError("guest agent readiness timed out")

    def _exec_until(self, command: list[str], deadline: float) -> str:
        try:
            with self._agent(deadline) as (agent, reader):
                self._set_deadline(agent, deadline)
                self._request(agent, "guest-exec", {
                    "path": command[0], "arg": command[1:], "capture-output": True})
                self._set_deadline(agent, deadline)
                pid = self._read_response(reader, deadline)["return"].get("pid")
                if not isinstance(pid, int):
                    raise QualificationError("guest agent did not start the bounded probe")
                while time.monotonic() < deadline:
                    self._set_deadline(agent, deadline)
                    self._request(agent, "guest-exec-status", {"pid": pid})
                    self._set_deadline(agent, deadline)
                    status = self._read_response(reader, deadline)["return"]
                    if status.get("exited"):
                        try:
                            output = base64.b64decode(status.get("out-data", ""), validate=True).decode("utf-8", "replace")
                        except (ValueError, TypeError):
                            raise QualificationError("guest agent returned invalid encoded output")
                        if status.get("exitcode") != 0:
                            raise QualificationError("guest probe returned nonzero status")
                        return output[:4096]
                    time.sleep(min(0.1, max(0, deadline - time.monotonic())))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise QualificationError("guest agent request or response failed") from exc
        raise QualificationError("guest command timed out")

    def _exec(self, command: list[str], timeout: float) -> str:
        return self._exec_until(command, time.monotonic() + timeout)

    def boot_id(self, *, deadline: float | None = None) -> str:
        until = deadline if deadline is not None else time.monotonic() + self.limits.boot_timeout
        value = self._exec_until(["/bin/cat", "/proc/sys/kernel/random/boot_id"], until).strip()
        try:
            return str(uuid.UUID(value))
        except (ValueError, AttributeError) as exc:
            raise QualificationError("guest boot identity was not a UUID") from exc

    def assert_guest_identity(self, *, deadline: float | None = None) -> None:
        until = deadline if deadline is not None else time.monotonic() + self.limits.boot_timeout
        release = self._exec_until(["/bin/cat", "/etc/os-release"], until)
        fields: dict[str, str] = {}
        try:
            for line in release.splitlines():
                key, separator, value = line.partition("=")
                if separator and key in {"ID", "VERSION_ID"}:
                    parsed = shlex.split(value, posix=True)
                    if len(parsed) != 1:
                        raise ValueError("invalid os-release value")
                    fields[key] = parsed[0]
        except ValueError as exc:
            raise QualificationError("guest os-release identity is malformed") from exc
        if fields.get("ID") != "debian" or fields.get("VERSION_ID") != self.expected_guest_os_version:
            raise QualificationError("guest Debian identity/version differs from its verified pin")

    def assert_systemd(self, *, deadline: float | None = None) -> None:
        until = deadline if deadline is not None else time.monotonic() + self.limits.boot_timeout
        pid1 = self._exec_until(["/bin/cat", "/proc/1/comm"], until).strip()
        if pid1 != "systemd":
            raise QualificationError("guest PID 1 is not systemd")
        state = self._exec_until(["/bin/systemctl", "is-system-running"], until).strip()
        if state != "running":
            raise QualificationError("guest systemd is not fully running")

    def reboot(self, previous_boot_id: str) -> None:
        deadline = time.monotonic() + self.limits.reboot_timeout
        try:
            self._exec_until(["/bin/systemctl", "reboot"],
                             min(deadline, time.monotonic() + self.limits.command_timeout))
        except QualificationError:
            # A clean reboot can close QGA before guest-exec-status is returned.
            pass
        while time.monotonic() < deadline:
            try:
                current = self.boot_id(deadline=deadline)
                if current != previous_boot_id:
                    self.assert_systemd(deadline=deadline)
                    self.assert_guest_identity(deadline=deadline)
                    return
            except QualificationError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(0.1, remaining))
        raise QualificationError("guest clean reboot/reconnection timed out")

    def cleanup(self) -> None:
        process, self.process = self.process, None
        if process is None:
            return
        def group_alive() -> bool:
            try:
                os.killpg(process.pid, 0)
                return True
            except ProcessLookupError:
                return False

        cleanup_deadline = time.monotonic() + self.limits.cleanup_timeout
        terminate_deadline = time.monotonic() + self.limits.cleanup_timeout / 2
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        while group_alive() and time.monotonic() < terminate_deadline:
            process.poll()  # reap the QEMU leader while checking its whole owned group
            time.sleep(min(0.05, max(0, terminate_deadline - time.monotonic())))
        if group_alive():
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            while group_alive() and time.monotonic() < cleanup_deadline:
                process.poll()
                time.sleep(min(0.05, max(0, cleanup_deadline - time.monotonic())))
        try:
            process.wait(timeout=max(0.001, cleanup_deadline - time.monotonic()))
        except subprocess.TimeoutExpired as exc:
            raise QualificationError("QEMU process survived bounded cleanup") from exc
        if group_alive():
            raise QualificationError("owned QEMU process group survived bounded cleanup")


def qualify(run: VerifiedRun | None, qemu: Pin | None, qemu_img: Pin | None, guest: Pin | None,
            limits: Limits = Limits(), *, guest_factory: Callable[..., Any] = QemuGuest) -> dict[str, Any]:
    """Run capability, real systemd boot, clean reboot and independent cleanup checks."""
    try:
        qemu_path, qemu_img_path, guest_path = admit(run, qemu, qemu_img, guest, limits)
    except QualificationError as exc:
        blocked = {**_identity(run), "profile": "hosted-qemu-tcg", "status": "INCOMPLETE",
                   "phase": "admission", "reason": str(exc)[:256],
                   "requires_external_provenance_verification": True, "deploy_eligibility": False}
        raise QualificationError(json.dumps(blocked, sort_keys=True)) from exc
    guest_source = urlsplit(run.guest_source)
    identity = {**_identity(run), "profile": "hosted-qemu-tcg",
                "guest_source_origin": f"https://{guest_source.hostname}",
                "guest_source_sha256": hashlib.sha256(run.guest_source.encode("utf-8")).hexdigest(),
                "requires_external_provenance_verification": True, "deploy_eligibility": False,
                "capabilities": sorted(run.capabilities),
                "verifier_reported_host_resources": {"cpus": run.available_cpus,
                                                     "memory_mib": run.available_memory_mib,
                                                     "disk_mib": run.available_disk_mib,
                                                     "reported_by": run.verifier,
                                                     "controller_verified": False},
                "guest_resource_limits": {"vcpus": limits.vcpus,
                                          "memory_mib": limits.memory_mib,
                                          "disk_mib": limits.disk_mib},
                "local_host_resource_measurement": False,
                "input_sha256": {"qemu": qemu.sha256, "qemu_img": qemu_img.sha256,
                                 "guest": guest.sha256}}
    work_parent = Path(tempfile.mkdtemp(prefix="fwrouter-host-qualification-"))
    instance = None
    failure: str | None = None
    phase = "guest_start"
    first_boot = second_boot = None
    try:
        instance = guest_factory(qemu_path, qemu_img_path, guest_path, work_parent, limits,
                                 run.guest_os_version)
        instance.start()
        phase = "guest_boot"
        first_boot = instance.boot_id()
        if not first_boot:
            raise QualificationError("guest boot identity is empty")
        instance.assert_guest_identity()
        phase = "systemd_readback"
        instance.assert_systemd()
        phase = "guest_reboot"
        instance.reboot(first_boot)
        phase = "post_reboot_readback"
        second_boot = instance.boot_id()
        if not second_boot or first_boot == second_boot:
            raise QualificationError("guest reboot did not establish a new boot identity")
    except Exception as exc:
        failure = f"{type(exc).__name__}: {str(exc)[:256]}"
    finally:
        cleanup_failure = None
        guest_cleanup_proven = instance is None
        workspace_removed = False
        if instance is not None:
            try:
                instance.cleanup()
                guest_cleanup_proven = True
            except Exception as exc:
                cleanup_failure = f"{type(exc).__name__}: {str(exc)[:256]}"
        if guest_cleanup_proven and work_parent.exists():
            try:
                shutil.rmtree(work_parent)
                workspace_removed = not work_parent.exists()
            except OSError as exc:
                cleanup_failure = cleanup_failure or f"workspace cleanup failed: {type(exc).__name__}"
        elif work_parent.exists():
            cleanup_failure = cleanup_failure or "owned workspace retained because guest process cleanup is unproven"
        if work_parent.exists() and not cleanup_failure:
            cleanup_failure = cleanup_failure or "ephemeral guest state remains"
    clean = failure is None and cleanup_failure is None
    real_guest = guest_factory is QemuGuest
    result = {**identity, "status": "PASS" if clean and real_guest else
              ("FAIL" if not clean else "INCOMPLETE"),
              "phase": "complete" if clean else (phase if failure else "cleanup"),
              "evidence_scope": "real_guest_capability" if real_guest else "synthetic_controller_fixture",
              "qualification_eligible": False,
              "failure": failure, "cleanup_failure": cleanup_failure,
              "controller_cleanup_completed": guest_cleanup_proven,
              "workspace_removed": workspace_removed,
              "first_boot_id": first_boot, "second_boot_id": second_boot,
              "boot_identity_changed": bool(first_boot and second_boot and first_boot != second_boot)}
    if result["status"] != "PASS":
        if result["status"] == "INCOMPLETE":
            result["reason"] = "synthetic guest factory cannot establish host qualification"
            return result
        raise QualificationError(json.dumps(result, sort_keys=True))
    return result
