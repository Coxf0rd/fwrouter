"""Cooperative process admission for controlled offline database maintenance.

This is a host-local barrier for supported FWRouter entrypoints.  It does not
claim to exclude an administrator running arbitrary SQLite/systemd commands.
"""
from __future__ import annotations

from contextlib import contextmanager
import contextvars
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import threading
from typing import Iterator
import uuid


DEFAULT_STATE_ROOT = Path("/var/lib/fwrouter-v2")
LEASE_FD = 8
LOCK_UNITS = (
    "fwrouter-api.service",
    "fwrouter-xray-sub-gateway.service",
    "fwrouter-docker-subject-events.service",
    "fwrouter-jobs-retention-dry-run.service",
    "fwrouter-maintenance.service",
    "fwrouter-subscription-refresh.service",
    "fwrouter-traffic-collect.service",
    "fwrouter-jobs-retention-dry-run.timer",
    "fwrouter-maintenance.timer",
    "fwrouter-subscription-refresh.timer",
    "fwrouter-traffic-collect.timer",
)
MAX_CHECKPOINT_BYTES = 2 * 1024 * 1024
REBUILD_PHASES = ("pending", "candidate_ready", "source_quiesced", "published", "verified")
TRUSTED_HOST_UID = 0


def _trusted_uid() -> int:
    """UID required for host lifecycle policy and lease files (test-patchable)."""
    return TRUSTED_HOST_UID


class MaintenanceAdmissionError(RuntimeError):
    """A supported process cannot prove its lifecycle admission."""


class _OfflineGrant:
    __slots__ = ("fd", "lifecycle_fd", "operation_id", "state_root", "pid", "thread_id", "active")

    def __init__(self, fd: int, lifecycle_fd: int, operation_id: str, state_root: Path) -> None:
        self.fd = fd
        self.lifecycle_fd = lifecycle_fd
        self.operation_id = operation_id
        self.state_root = state_root
        self.pid = os.getpid()
        self.thread_id = threading.get_ident()
        self.active = True

    def revoke(self) -> None:
        self.active = False


_OFFLINE_GRANT: contextvars.ContextVar[_OfflineGrant | None] = contextvars.ContextVar(
    "fwrouter_offline_rebuild_grant", default=None,
)


def _lifecycle_paths(state_root: Path) -> tuple[Path, Path, Path]:
    rollout_dir = state_root / "state" / "controlled-rollout"
    return rollout_dir, rollout_dir / "pending.json", rollout_dir / ".maintenance.lock"


def _verify_lifecycle_lock_fd(fd: int, state_root: Path) -> None:
    rollout_dir, _checkpoint_path, _lease_path = _lifecycle_paths(state_root)
    expected_path = rollout_dir / ".lock"
    try:
        expected = os.open(expected_path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
                           | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise MaintenanceAdmissionError("controlled lifecycle lock is unavailable") from exc
    try:
        actual_info = os.fstat(fd)
        expected_info = os.fstat(expected)
        named = expected_path.lstat()
        if (not stat.S_ISREG(actual_info.st_mode) or actual_info.st_uid != _trusted_uid()
                or stat.S_IMODE(actual_info.st_mode) != 0o600 or actual_info.st_nlink != 1
                or (actual_info.st_dev, actual_info.st_ino) != (expected_info.st_dev, expected_info.st_ino)
                or (named.st_dev, named.st_ino) != (expected_info.st_dev, expected_info.st_ino)):
            raise MaintenanceAdmissionError("controlled lifecycle lock descriptor is invalid")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise MaintenanceAdmissionError("controlled lifecycle lock is not held") from exc
    finally:
        os.close(expected)


def _validate_directory(path: Path, *, create: bool) -> bool:
    if not path.is_absolute() or ".." in path.parts:
        raise MaintenanceAdmissionError("maintenance state path is invalid")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            if not create:
                return False
            try:
                current.mkdir(mode=0o700)
                if current == path:
                    os.chmod(current, 0o700, follow_symlinks=False)
            except OSError as exc:
                raise MaintenanceAdmissionError("cannot create maintenance state directory") from exc
            info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise MaintenanceAdmissionError("maintenance state path contains a symlink or non-directory")
        if current == path:
            if info.st_uid != _trusted_uid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise MaintenanceAdmissionError("maintenance state directory has unsafe owner or mode")
        elif info.st_uid not in {0, os.geteuid()}:
            raise MaintenanceAdmissionError("maintenance path crosses an unexpected owner")
        elif stat.S_IMODE(info.st_mode) & 0o022 and not (
            info.st_uid == 0 and info.st_mode & stat.S_ISVTX
        ):
            raise MaintenanceAdmissionError("maintenance path crosses a writable untrusted directory")
    return True


def _open_lease(state_root: Path, *, create: bool) -> int:
    rollout_dir, _checkpoint_path, lease_path = _lifecycle_paths(state_root)
    _validate_directory(rollout_dir, create=create)
    flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    if create:
        flags |= os.O_CREAT
    try:
        fd = os.open(lease_path, flags, 0o600)
        info = os.fstat(fd)
        named = lease_path.lstat()
    except OSError as exc:
        raise MaintenanceAdmissionError("cannot safely open maintenance lease") from exc
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != _trusted_uid()
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
            or stat.S_ISLNK(named.st_mode) or (info.st_dev, info.st_ino) != (named.st_dev, named.st_ino)):
        os.close(fd)
        raise MaintenanceAdmissionError("maintenance lease has unsafe type, owner, mode, or path")
    return fd


def _read_checkpoint(state_root: Path) -> dict[str, object] | None:
    rollout_dir, checkpoint_path, _lease_path = _lifecycle_paths(state_root)
    try:
        if not _validate_directory(rollout_dir, create=False):
            return None
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(checkpoint_path, flags)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise MaintenanceAdmissionError("cannot safely open lifecycle checkpoint") from exc
    try:
        info = os.fstat(fd)
        named = checkpoint_path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != _trusted_uid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
                or info.st_size <= 0 or info.st_size > MAX_CHECKPOINT_BYTES
                or stat.S_ISLNK(named.st_mode) or (info.st_dev, info.st_ino) != (named.st_dev, named.st_ino)):
            raise MaintenanceAdmissionError("lifecycle checkpoint has unsafe type, owner, mode, or size")
        raw = os.read(fd, MAX_CHECKPOINT_BYTES + 1)
        after = os.fstat(fd)
        if (len(raw) != info.st_size or len(raw) > MAX_CHECKPOINT_BYTES
                or (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
            raise MaintenanceAdmissionError("lifecycle checkpoint changed while being read")
    finally:
        os.close(fd)
    def unique_pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate lifecycle key")
            result[key] = value
        return result

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_pairs)
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise MaintenanceAdmissionError("lifecycle checkpoint is malformed") from exc
    if not isinstance(value, dict):
        raise MaintenanceAdmissionError("lifecycle checkpoint is malformed")
    return value


def startup_allowed(state_root: Path = DEFAULT_STATE_ROOT) -> bool:
    """Return false for unverified maintenance or a mismatched rollout tree."""
    checkpoint = _read_checkpoint(state_root)
    if checkpoint is None:
        return True
    operation = checkpoint.get("maintenance_operation")
    if operation is None:
        # An ordinary source rollout publishes its target backend digest before
        # the installer overlays files.  Bind startup to the executing package
        # tree so boot/restart cannot run a mixed old/new backend after a crash.
        schema = checkpoint.get("schema")
        if (checkpoint.get("status") not in {"pending", "complete"}
                or type(schema) is not int
                or schema not in {1, 2, 3, 4}):
            return False
        if schema == 4:
            # Schema 4 carries an explicit fixed-container Mihomo quiescence
            # record. During backend installation the API may start only after
            # that same lifecycle operation has durably confirmed quiescence
            # (or after its explicit release). Do not accept missing, partial,
            # pending-stop, or pending-release records as ordinary startup.
            runtime = checkpoint.get("runtime_quiescence")
            if "runtime_quiescence" not in checkpoint:
                return False
            if runtime is not None and not _startup_runtime_quiescence_valid(
                    runtime, checkpoint.get("operation_id")):
                return False
        expected = checkpoint.get("backend_manifest_sha256")
        if not isinstance(expected, str) or re.fullmatch(r"[0-9a-f]{64}", expected) is None:
            return False
        try:
            from fwrouter_api.services import native_proof

            actual = native_proof.backend_manifest_sha256(native_proof._backend_root())
        except Exception:
            # Missing modules and malformed/partial backend trees fail closed.
            return False
        return actual == expected
    if (not isinstance(operation, dict) or operation.get("kind") != "database_rebuild"
            or checkpoint.get("status") not in {"pending", "complete"}):
        return False
    try:
        operation_id = str(uuid.UUID(str(checkpoint.get("operation_id"))))
    except (ValueError, TypeError, AttributeError):
        return False
    fields = set(operation)
    if fields not in ({"kind", "status"}, {"kind", "status", "backup"}):
        return False
    backup = operation.get("backup")
    if operation.get("status") != "verified" or backup is None:
        return False
    return _valid_backup_pin(backup, operation_id)


def _startup_runtime_quiescence_valid(value: object, operation_id: object) -> bool:
    """Validate the schema-4 lifecycle admission subset without side effects."""
    fields = {
        "kind", "phase", "operation_id", "source_manifest_sha256",
        "unit_active_state", "unit_sub_state", "unit_enabled_state",
        "container_id", "image_id", "config_image", "compose_config_hash",
        "compose_file_sha256", "cache_before", "cache_after",
    }
    if not isinstance(value, dict) or set(value) != fields:
        return False
    try:
        normalized_operation = str(uuid.UUID(str(operation_id)))
    except (ValueError, TypeError, AttributeError):
        return False
    if (value.get("kind") != "mihomo_existing_container/v1"
            or value.get("operation_id") != normalized_operation
            or value.get("phase") not in {"quiesced", "released"}
            or value.get("unit_active_state") not in {"active", "inactive"}
            or value.get("unit_sub_state") not in {"exited", "dead"}
            or value.get("unit_enabled_state") not in {"enabled", "disabled"}
            or (value.get("unit_active_state"), value.get("unit_sub_state")) not in {
                ("active", "exited"), ("inactive", "dead")}):
        return False
    for name in ("source_manifest_sha256", "compose_config_hash", "compose_file_sha256"):
        digest = value.get(name)
        if (not isinstance(digest, str) or len(digest) != 64
                or any(char not in "0123456789abcdef" for char in digest)):
            return False
    for name in ("cache_before", "cache_after"):
        cache = value.get(name)
        if (not isinstance(cache, dict) or set(cache) != {"sha256", "size"}
                or not isinstance(cache.get("sha256"), str)
                or re.fullmatch(r"[0-9a-f]{64}", cache["sha256"]) is None
                or type(cache.get("size")) is not int or cache["size"] < 0):
            return False
    image = value.get("image_id")
    container = value.get("container_id")
    return (isinstance(image, str) and image.startswith("sha256:")
            and len(image) == 71
            and all(char in "0123456789abcdef" for char in image[7:])
            and isinstance(container, str) and len(container) == 64
            and all(char in "0123456789abcdef" for char in container.lower())
            and value.get("config_image") == "metacubex/mihomo:v1.19.31")


def _offline_operation(state_root: Path) -> str:
    checkpoint = _read_checkpoint(state_root)
    if checkpoint is None or checkpoint.get("status") != "pending":
        raise MaintenanceAdmissionError("no pending offline rebuild operation is recorded")
    operation = checkpoint.get("maintenance_operation")
    fields = set(operation) if isinstance(operation, dict) else set()
    if (not isinstance(operation, dict) or fields not in ({"kind", "status"}, {"kind", "status", "backup"})
            or operation.get("kind") != "database_rebuild"
            or operation.get("status") not in REBUILD_PHASES[:-1]):
        raise MaintenanceAdmissionError("lifecycle checkpoint does not authorize offline rebuild")
    try:
        operation_id = str(uuid.UUID(str(checkpoint.get("operation_id"))))
    except (ValueError, TypeError, AttributeError) as exc:
        raise MaintenanceAdmissionError("lifecycle operation identity is invalid") from exc
    if checkpoint.get("operation_id") != operation_id:
        raise MaintenanceAdmissionError("lifecycle operation identity is invalid")
    backup = operation.get("backup")
    if backup is not None and not _valid_backup_pin(backup, operation_id):
        raise MaintenanceAdmissionError("lifecycle backup identity is invalid")
    if operation.get("status") != "pending" and backup is None:
        raise MaintenanceAdmissionError("offline rebuild phase has no pinned backup")
    return operation_id


def _valid_backup_pin(backup: object, operation_id: str) -> bool:
    if not isinstance(backup, dict) or set(backup) != {"filename", "sha256"}:
        return False
    filename = backup.get("filename")
    digest = backup.get("sha256")
    expected_prefix = f"fwrouter.db.rebuild-{uuid.UUID(operation_id).hex}."
    return (isinstance(filename, str) and filename == Path(filename).name
            and filename.startswith(expected_prefix) and filename.endswith(".bak")
            and isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest) is not None)


def _require_units_stopped() -> None:
    for unit in LOCK_UNITS:
        try:
            result = subprocess.run(
                ["/usr/bin/systemctl", "is-active", unit], check=False,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise MaintenanceAdmissionError("cannot verify FWRouter unit quiescence") from exc
        state = result.stdout.strip()
        if result.returncode == 0 and state == "active":
            raise MaintenanceAdmissionError("FWRouter writer unit is still active")
        if result.returncode not in {3, 4} or state not in {"inactive", "failed", "unknown", ""}:
            raise MaintenanceAdmissionError("cannot verify FWRouter unit quiescence")


def acquire_shared_lease_fd(state_root: Path = DEFAULT_STATE_ROOT) -> int:
    """Acquire a shared lease for one supported installer process; caller closes it."""
    fd = _open_lease(state_root, create=True)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH)
        if not startup_allowed(state_root):
            raise MaintenanceAdmissionError("startup is blocked by pending maintenance or backend manifest mismatch")
        return fd
    except BaseException:
        os.close(fd)
        raise


def verify_shared_lease_fd(fd: int, state_root: Path = DEFAULT_STATE_ROOT) -> None:
    expected = _open_lease(state_root, create=False)
    try:
        actual_info = os.fstat(fd)
        expected_info = os.fstat(expected)
        if ((actual_info.st_dev, actual_info.st_ino) != (expected_info.st_dev, expected_info.st_ino)
                or not stat.S_ISREG(actual_info.st_mode) or actual_info.st_uid != _trusted_uid()
                or stat.S_IMODE(actual_info.st_mode) != 0o600 or actual_info.st_nlink != 1):
            raise MaintenanceAdmissionError("inherited maintenance lease descriptor is invalid")
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise MaintenanceAdmissionError("inherited maintenance lease is not owned by this process") from exc
        if not startup_allowed(state_root):
            raise MaintenanceAdmissionError("startup is blocked by pending maintenance or backend manifest mismatch")
    finally:
        os.close(expected)


def _verify_exclusive_lease_fd(fd: int, state_root: Path = DEFAULT_STATE_ROOT) -> None:
    """Verify the caller still owns the existing root-owned EX lease descriptor.

    The descriptor is borrowed: this function neither closes it nor changes its
    lock mode. It is intended for narrowly scoped lifecycle-owner operations
    that must prove exclusive admission without creating another lock authority.
    """
    if os.geteuid() != _trusted_uid():
        raise MaintenanceAdmissionError("exclusive maintenance admission requires root")
    expected = _open_lease(state_root, create=False)
    try:
        actual_info = os.fstat(fd)
        expected_info = os.fstat(expected)
        named = _lifecycle_paths(state_root)[2].lstat()
        if ((actual_info.st_dev, actual_info.st_ino) != (expected_info.st_dev, expected_info.st_ino)
                or (named.st_dev, named.st_ino) != (expected_info.st_dev, expected_info.st_ino)
                or not stat.S_ISREG(actual_info.st_mode) or actual_info.st_uid != _trusted_uid()
                or stat.S_IMODE(actual_info.st_mode) != 0o600 or actual_info.st_nlink != 1):
            raise MaintenanceAdmissionError("inherited exclusive maintenance lease descriptor is invalid")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise MaintenanceAdmissionError("inherited exclusive maintenance lease is not held") from exc
    finally:
        os.close(expected)


@contextmanager
def runtime_shared_lease(state_root: Path = DEFAULT_STATE_ROOT) -> Iterator[None]:
    """Hold a shared lease across the API process lifetime."""
    fd = acquire_shared_lease_fd(state_root)
    try:
        yield
    finally:
        os.close(fd)


def _validate_core_owner_complete_checkpoint(
    checkpoint: dict[str, object] | None,
    *,
    expected_installed_backend_manifest_sha256: str,
) -> None:
    """Admit the immutable source tool only against a completed schema-2 OLD tree.

    The general startup gate intentionally compares the executing backend with
    the lifecycle checkpoint. This narrowly scoped owner command executes a
    separately pinned source tool while repairing the receipt for an installed
    OLD backend, so it validates both identities independently instead.
    """
    if checkpoint is None:
        raise MaintenanceAdmissionError("CORE_OWNER_LIFECYCLE_CHECKPOINT_REQUIRED")
    expected_keys = {
        "schema", "operation_id", "status", "created_at", "backend_manifest",
        "backend_manifest_sha256", "active_units", "pending_units",
        "initial_backend_manifest_sha256", "source_transitions",
    }
    if (set(checkpoint) != expected_keys or type(checkpoint.get("schema")) is not int
            or checkpoint.get("schema") != 2
            or checkpoint.get("status") != "complete"):
        raise MaintenanceAdmissionError("CORE_OWNER_COMPLETE_SCHEMA2_CHECKPOINT_REQUIRED")
    try:
        operation_id = str(uuid.UUID(str(checkpoint.get("operation_id"))))
    except (ValueError, TypeError, AttributeError) as exc:
        raise MaintenanceAdmissionError("CORE_OWNER_CHECKPOINT_INVALID") from exc
    created_at = checkpoint.get("created_at")
    manifest = checkpoint.get("backend_manifest")
    digest = checkpoint.get("backend_manifest_sha256")
    initial = checkpoint.get("initial_backend_manifest_sha256")
    transitions = checkpoint.get("source_transitions")
    if (not isinstance(created_at, str) or not created_at
            or not isinstance(manifest, list) or not manifest
            or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
            or not isinstance(initial, str) or not re.fullmatch(r"[0-9a-f]{64}", initial)
            or not isinstance(transitions, list) or len(transitions) > 16):
        raise MaintenanceAdmissionError("CORE_OWNER_CHECKPOINT_INVALID")
    paths: list[str] = []
    total_size = 0
    if len(manifest) > 10_000:
        raise MaintenanceAdmissionError("CORE_OWNER_CHECKPOINT_INVALID")
    for entry in manifest:
        name = entry.get("path") if isinstance(entry, dict) else None
        relative = PurePosixPath(name) if isinstance(name, str) else PurePosixPath(".")
        if (not isinstance(entry, dict) or set(entry) != {"path", "size", "sha256"}
                or not isinstance(name, str)
                or not name or relative.is_absolute() or relative.as_posix() != name
                or any(part in {"", ".", ".."} for part in relative.parts)
                or any(part in {".env", ".venv", ".pytest_cache", "__pycache__", ".git"}
                       or part.startswith(".env.")
                       or part.endswith((".pyc", ".pyo", ".db", ".sqlite", ".sqlite3", ".bak", ".tar.zst"))
                       or ".bak-" in part for part in relative.parts)
                or type(entry.get("size")) is not int or not 0 <= entry["size"] <= 512 * 1024 * 1024
                or not isinstance(entry.get("sha256"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])):
            raise MaintenanceAdmissionError("CORE_OWNER_CHECKPOINT_INVALID")
        total_size += entry["size"]
        if total_size > 512 * 1024 * 1024:
            raise MaintenanceAdmissionError("CORE_OWNER_CHECKPOINT_INVALID")
        paths.append(entry["path"])
    if paths != sorted(set(paths)):
        raise MaintenanceAdmissionError("CORE_OWNER_CHECKPOINT_INVALID")
    manifest_digest = hashlib.sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    ).hexdigest()
    if manifest_digest != digest:
        raise MaintenanceAdmissionError("CORE_OWNER_CHECKPOINT_INVALID")
    previous = initial
    for transition in transitions:
        if (not isinstance(transition, dict) or set(transition) != {
                "operation_id", "from_manifest_sha256", "to_manifest_sha256", "created_at"}):
            raise MaintenanceAdmissionError("CORE_OWNER_CHECKPOINT_INVALID")
        try:
            transition_id = str(uuid.UUID(str(transition.get("operation_id"))))
        except (ValueError, TypeError, AttributeError) as exc:
            raise MaintenanceAdmissionError("CORE_OWNER_CHECKPOINT_INVALID") from exc
        from_digest = transition.get("from_manifest_sha256")
        to_digest = transition.get("to_manifest_sha256")
        if (transition_id != operation_id or from_digest != previous
                or not isinstance(to_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", to_digest)
                or to_digest == from_digest
                or not isinstance(transition.get("created_at"), str)
                or not transition["created_at"]):
            raise MaintenanceAdmissionError("CORE_OWNER_CHECKPOINT_INVALID")
        previous = to_digest
    units = checkpoint.get("active_units")
    pending = checkpoint.get("pending_units")
    if (previous != digest or not isinstance(units, dict) or set(units) != set(LOCK_UNITS)
            or any(not isinstance(name, str) or type(active) is not bool
                   for name, active in units.items())
            or not isinstance(pending, list)
            or any(not isinstance(unit, str) or unit not in LOCK_UNITS for unit in pending)
            or len(pending) != len(set(pending))):
        raise MaintenanceAdmissionError("CORE_OWNER_CHECKPOINT_INVALID")
    if digest != expected_installed_backend_manifest_sha256:
        raise MaintenanceAdmissionError("CORE_OWNER_INSTALLED_BACKEND_CHECKPOINT_MISMATCH")


@contextmanager
def core_owner_shared_lease(
    *,
    expected_tool_manifest_sha256: str,
    expected_installed_backend_manifest_sha256: str,
    state_root: Path = DEFAULT_STATE_ROOT,
) -> Iterator[None]:
    """Hold the existing shared lease for a Core owner tool pinned separately from OLD.

    Unlike ``runtime_shared_lease``, this admits only a complete ordinary schema-2
    lifecycle checkpoint matching the fixed installed backend. The executing
    tool must independently match its supplied exact manifest pin. No checkpoint,
    selector, fence or installed file is written by this admission.
    """
    if os.geteuid() != _trusted_uid():
        raise MaintenanceAdmissionError("CORE_OWNER_ROOT_REQUIRED")
    if (not re.fullmatch(r"[0-9a-f]{64}", expected_tool_manifest_sha256)
            or not re.fullmatch(r"[0-9a-f]{64}", expected_installed_backend_manifest_sha256)):
        raise MaintenanceAdmissionError("CORE_OWNER_MANIFEST_PIN_INVALID")
    try:
        from fwrouter_api.services import native_proof

        tool_digest = native_proof.backend_manifest_sha256(native_proof._backend_root())
        installed_digest = native_proof.backend_manifest_sha256(native_proof._INSTALLED_BACKEND_ROOT)
    except Exception as exc:
        raise MaintenanceAdmissionError("CORE_OWNER_MANIFEST_UNAVAILABLE") from exc
    if tool_digest != expected_tool_manifest_sha256:
        raise MaintenanceAdmissionError("CORE_OWNER_TOOL_MANIFEST_MISMATCH")
    if installed_digest != expected_installed_backend_manifest_sha256:
        raise MaintenanceAdmissionError("CORE_OWNER_INSTALLED_BACKEND_MANIFEST_MISMATCH")

    fd = _open_lease(state_root, create=True)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH)
        _validate_core_owner_complete_checkpoint(
            _read_checkpoint(state_root),
            expected_installed_backend_manifest_sha256=expected_installed_backend_manifest_sha256,
        )
        yield
    finally:
        os.close(fd)


@contextmanager
def _exclusive_rebuild_lease(
    fd: int, lifecycle_fd: int, *, owned: bool, state_root: Path,
) -> Iterator[str]:
    if os.geteuid() != _trusted_uid():
        raise MaintenanceAdmissionError("offline rebuild admission requires root")
    operation_id = _offline_operation(state_root)
    _verify_lifecycle_lock_fd(lifecycle_fd, state_root)
    try:
        try:
            _verify_exclusive_lease_fd(fd, state_root)
        except MaintenanceAdmissionError as exc:
            if "is not held" in str(exc):
                raise MaintenanceAdmissionError(
                    "another supported FWRouter process holds the maintenance lease",
                ) from exc
            raise
        _require_units_stopped()
        # Check marker again after acquiring EX: a racing shared entrant either
        # completed before us or will observe the still-pending marker afterward.
        if _offline_operation(state_root) != operation_id:
            raise MaintenanceAdmissionError("offline lifecycle operation changed while acquiring lease")
        grant = _OfflineGrant(fd, lifecycle_fd, operation_id, state_root)
        token = _OFFLINE_GRANT.set(grant)
        try:
            yield operation_id
        finally:
            grant.revoke()
            _OFFLINE_GRANT.reset(token)
    finally:
        if owned:
            os.close(fd)


@contextmanager
def offline_rebuild_lease(
    inherited_fd: int | None, lifecycle_lock_fd: int | None, *,
    state_root: Path = DEFAULT_STATE_ROOT,
) -> Iterator[str]:
    """Accept only the descriptor inherited from the controlled lifecycle owner."""
    if type(inherited_fd) is not int or inherited_fd < 0:
        raise MaintenanceAdmissionError("offline rebuild requires an inherited lifecycle lease")
    if type(lifecycle_lock_fd) is not int or lifecycle_lock_fd < 0:
        raise MaintenanceAdmissionError("offline rebuild requires the held lifecycle lock")
    with _exclusive_rebuild_lease(
        inherited_fd, lifecycle_lock_fd, owned=False, state_root=state_root,
    ) as operation_id:
        yield operation_id


@contextmanager
def _lifecycle_owner_offline_rebuild_lease(
    lifecycle_lock_fd: int, *, state_root: Path = DEFAULT_STATE_ROOT,
) -> Iterator[str]:
    """Private issuer used by controlled_backend_lifecycle.py only."""
    fd = _open_lease(state_root, create=True)
    try:
        with _exclusive_rebuild_lease(
            fd, lifecycle_lock_fd, owned=False, state_root=state_root,
        ) as operation_id:
            yield operation_id
    finally:
        os.close(fd)


def offline_rebuild_is_held() -> bool:
    grant = _OFFLINE_GRANT.get()
    if (grant is None or not grant.active or grant.pid != os.getpid()
            or grant.thread_id != threading.get_ident()):
        return False
    try:
        info = os.fstat(grant.fd)
        _rollout_dir, _checkpoint_path, lease_path = _lifecycle_paths(grant.state_root)
        lease_info = lease_path.lstat()
        _verify_lifecycle_lock_fd(grant.lifecycle_fd, grant.state_root)
        try:
            fcntl.flock(grant.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        if ((info.st_dev, info.st_ino) != (lease_info.st_dev, lease_info.st_ino)
                or not stat.S_ISREG(info.st_mode) or info.st_uid != _trusted_uid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
                or _offline_operation(grant.state_root) != grant.operation_id):
            return False
        return True
    except (OSError, MaintenanceAdmissionError):
        return False


def active_offline_rebuild_fd() -> int:
    """Return the held lifecycle FD for the controlled owner's child process."""
    if not offline_rebuild_is_held():
        raise MaintenanceAdmissionError("offline rebuild lease is not held")
    grant = _OFFLINE_GRANT.get()
    assert grant is not None
    return grant.fd


def active_offline_rebuild_operation_id() -> str:
    if not offline_rebuild_is_held():
        raise MaintenanceAdmissionError("offline rebuild lease is not held")
    grant = _OFFLINE_GRANT.get()
    assert grant is not None
    return grant.operation_id


def active_offline_rebuild_state_root() -> Path:
    """Return the fixed host-state root only for a currently held rebuild."""
    if not offline_rebuild_is_held():
        raise MaintenanceAdmissionError("offline rebuild state root is unavailable")
    grant = _OFFLINE_GRANT.get()
    assert grant is not None
    return grant.state_root


def offline_rebuild_phase() -> str:
    if not offline_rebuild_is_held():
        raise MaintenanceAdmissionError("offline rebuild lease is not held")
    grant = _OFFLINE_GRANT.get()
    assert grant is not None
    checkpoint = _read_checkpoint(grant.state_root)
    operation = checkpoint.get("maintenance_operation") if checkpoint else None
    if not isinstance(operation, dict) or operation.get("kind") != "database_rebuild":
        raise MaintenanceAdmissionError("offline lifecycle operation is unavailable")
    _offline_operation(grant.state_root)
    return str(operation.get("status"))


def offline_rebuild_backup_pin() -> dict[str, str] | None:
    if not offline_rebuild_is_held():
        raise MaintenanceAdmissionError("offline rebuild lease is not held")
    grant = _OFFLINE_GRANT.get()
    assert grant is not None
    _offline_operation(grant.state_root)
    checkpoint = _read_checkpoint(grant.state_root)
    if checkpoint is None:
        raise MaintenanceAdmissionError("offline lifecycle operation is unavailable")
    operation = checkpoint.get("maintenance_operation") if checkpoint else None
    if not isinstance(operation, dict):
        raise MaintenanceAdmissionError("offline lifecycle operation is unavailable")
    backup = operation.get("backup")
    if backup is None:
        return None
    if not _valid_backup_pin(backup, grant.operation_id):
        raise MaintenanceAdmissionError("lifecycle backup identity is invalid")
    return {"filename": str(backup["filename"]), "sha256": str(backup["sha256"])}


def record_offline_rebuild_backup_pin(filename: str, sha256: str) -> None:
    """Pin the original pre-rebuild backup before candidate construction."""
    if not offline_rebuild_is_held():
        raise MaintenanceAdmissionError("offline rebuild lease is not held")
    grant = _OFFLINE_GRANT.get()
    assert grant is not None
    _offline_operation(grant.state_root)
    checkpoint = _read_checkpoint(grant.state_root)
    operation = checkpoint.get("maintenance_operation") if checkpoint else None
    if not isinstance(operation, dict) or operation.get("status") != "pending":
        raise MaintenanceAdmissionError("backup can only be pinned before candidate preparation")
    pin = {"filename": filename, "sha256": sha256}
    if not _valid_backup_pin(pin, grant.operation_id):
        raise MaintenanceAdmissionError("backup pin is invalid")
    rollout_dir, checkpoint_path, _lease_path = _lifecycle_paths(grant.state_root)
    current = checkpoint
    current_operation = current.get("maintenance_operation")
    if not isinstance(current_operation, dict):
        raise MaintenanceAdmissionError("offline lifecycle operation is unavailable")
    existing = current_operation.get("backup")
    if existing is not None:
        if existing != pin:
            raise MaintenanceAdmissionError("offline backup pin changed")
        return
    if set(current_operation) == {"kind", "status"}:
        current_operation = {**current_operation, "backup": None}
    if set(current_operation) != {"kind", "status", "backup"}:
        raise MaintenanceAdmissionError("offline lifecycle operation schema is invalid")
    current_operation["backup"] = pin
    current["maintenance_operation"] = current_operation
    _publish_lifecycle_checkpoint(rollout_dir, checkpoint_path, current, grant.operation_id)


def record_offline_rebuild_phase(phase: str) -> None:
    """Durably advance the existing lifecycle checkpoint during forward recovery."""
    if phase not in REBUILD_PHASES[1:-1] or not offline_rebuild_is_held():
        raise MaintenanceAdmissionError("offline rebuild phase cannot be advanced")
    grant = _OFFLINE_GRANT.get()
    assert grant is not None
    rollout_dir, checkpoint_path, _lease_path = _lifecycle_paths(grant.state_root)
    current = _read_checkpoint(grant.state_root)
    if (current is None or current.get("status") != "pending"
            or current.get("operation_id") != grant.operation_id):
        raise MaintenanceAdmissionError("offline lifecycle checkpoint identity changed")
    operation = current.get("maintenance_operation")
    if (not isinstance(operation, dict)
            or set(operation) not in ({"kind", "status"}, {"kind", "status", "backup"})
            or operation.get("kind") != "database_rebuild"
            or operation.get("status") not in REBUILD_PHASES[:-1]):
        raise MaintenanceAdmissionError("offline lifecycle checkpoint phase is invalid")
    pin = operation.get("backup")
    if pin is None or not _valid_backup_pin(pin, grant.operation_id):
        raise MaintenanceAdmissionError("offline rebuild phase requires its pinned backup")
    if REBUILD_PHASES.index(phase) < REBUILD_PHASES.index(str(operation["status"])):
        raise MaintenanceAdmissionError("offline lifecycle checkpoint cannot move backward")
    current["maintenance_operation"] = {**operation, "status": phase}
    _publish_lifecycle_checkpoint(rollout_dir, checkpoint_path, current, grant.operation_id)


def _publish_lifecycle_checkpoint(
    rollout_dir: Path, checkpoint_path: Path, current: dict[str, object], operation_id: str,
) -> None:
    raw = (json.dumps(current, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()
    # A killed writer may leave a private, unpublished file. Never adopt it
    # as authority or let it permanently block this operation's forward retry.
    temp = rollout_dir / f".pending-{operation_id}-{uuid.uuid4().hex}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(temp, flags, 0o600)
    except FileExistsError:
        raise MaintenanceAdmissionError("lifecycle checkpoint temporary name collision") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != _trusted_uid() or info.st_nlink != 1:
            raise MaintenanceAdmissionError("lifecycle checkpoint temp is unsafe")
        remaining = memoryview(raw)
        while remaining:
            written = os.write(fd, remaining)
            if written <= 0:
                raise OSError("LIFECYCLE_CHECKPOINT_SHORT_WRITE")
            remaining = remaining[written:]
        os.fsync(fd)
        named = temp.lstat()
        if (named.st_dev, named.st_ino) != (info.st_dev, info.st_ino):
            raise MaintenanceAdmissionError("lifecycle checkpoint temp was substituted")
        os.replace(temp, checkpoint_path)
        dir_fd = os.open(rollout_dir, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except BaseException:
        try:
            temp.unlink()
        except OSError:
            pass
        raise
    finally:
        os.close(fd)
