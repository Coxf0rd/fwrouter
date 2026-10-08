"""Fail-closed Python capability setup for routine FWRouter tests.

This is a process-level safety layer for reviewed tests, not an OS sandbox.
Run integration/native/recovery scenarios only in a separately qualified
disposable environment without production authority.
"""

from __future__ import annotations

import atexit
import os
import shutil
import socket
import stat
import sys
import tempfile
from pathlib import Path
from urllib.parse import unquote, urlsplit


PROTECTED_PRODUCTION_ROOTS = tuple(
    Path(value)
    for value in (
        "/opt/fwrouter-api",
        "/opt/fwrouter-mihomo",
        "/opt/fwrouter-xray",
        "/etc/fwrouter",
        "/etc/systemd/system/fwrouter-api.service",
        "/etc/systemd/system/fwrouter-mihomo.service",
        "/etc/systemd/system/fwrouter-xray.service",
        "/var/lib/fwrouter-v2",
        "/var/log/fwrouter",
        "/run/fwrouter-v2",
        "/root/.ssh",
        "/root/.aws",
        "/root/.config",
        "/root/.docker",
        "/root/.kube",
        "/root/.netrc",
    )
)

OWNED_ROOT: Path | None = None
STATE_ROOT: Path | None = None
COORDINATOR_OWNED = False
COORDINATOR_MARKER = ".fwrouter-gate-test-root-owned"
COORDINATOR_MARKER_CONTENT = "FWROUTER_GATE_TEST_ROOT_V1\n"


def path_is_protected(value: object) -> bool:
    if not isinstance(value, (str, bytes, os.PathLike)):
        return False
    try:
        candidate = Path(os.fsdecode(value))
        if not candidate.is_absolute():
            candidate = Path.cwd() / candidate
        candidate = Path(os.path.realpath(candidate))
    except (OSError, TypeError, ValueError):
        return True
    if any(candidate == root or root in candidate.parents for root in PROTECTED_PRODUCTION_ROOTS):
        return True
    return candidate.name.startswith(".env") and candidate.name not in {
        ".env.example", ".env.sample", ".env.template",
    }


def path_is_owned(value: object) -> bool:
    if OWNED_ROOT is None or not isinstance(value, (str, bytes, os.PathLike)):
        return False
    try:
        candidate = Path(os.fsdecode(value))
        if not candidate.is_absolute():
            candidate = Path.cwd() / candidate
        candidate = Path(os.path.realpath(candidate))
    except (OSError, TypeError, ValueError):
        return False
    return candidate == OWNED_ROOT or OWNED_ROOT in candidate.parents


def _sqlite_path(value: object) -> object:
    if not isinstance(value, str) or not value.startswith("file:"):
        return value
    parsed = urlsplit(value)
    path = unquote(parsed.path)
    if parsed.netloc not in ("", "localhost"):
        return value
    return path


def _audit_path(value: object, dir_fd: object = None) -> object:
    if not isinstance(value, (str, bytes, os.PathLike)):
        return value
    path = Path(os.fsdecode(value))
    if path.is_absolute() or dir_fd in (None, -1):
        return value
    try:
        base = Path(os.readlink(f"/proc/self/fd/{int(dir_fd)}"))
    except (OSError, TypeError, ValueError):
        return None
    return base / path


def _deny_unsafe_python_capabilities(event: str, args: tuple[object, ...]) -> None:
    if event in {
        "subprocess.Popen", "os.system", "os.exec", "os.posix_spawn", "os.spawn",
        "os.fork", "os.forkpty", "os.vfork",
    }:
        raise PermissionError(f"FWRouter tests deny process execution: {event}")
    if event.startswith("socket."):
        # asyncio may create an AF_UNIX socket pair. No connect/bind/listen or
        # other socket constructor is allowed.
        if event == "socket.__new__" and args and args[1] == socket.AF_UNIX:
            return
        raise PermissionError(f"FWRouter tests deny network/UNIX socket capability: {event}")
    if event == "open" and args:
        path = args[0]
        mode = args[1] if len(args) > 1 else "r"
        flags = args[2] if len(args) > 2 else 0
        write_flags = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
        writing = any(token in str(mode) for token in ("w", "a", "x", "+")) or (
            isinstance(flags, int) and bool(flags & write_flags)
        )
        null_device = (
            isinstance(path, (str, bytes, os.PathLike))
            and os.fsdecode(path) == os.devnull
        )
        if path_is_protected(path) or (writing and not path_is_owned(path) and not null_device):
            raise PermissionError(f"FWRouter tests deny file access outside owned state: {path}")
    if event == "sqlite3.connect" and args:
        path = _sqlite_path(args[0])
        if path not in (":memory:", "") and not path_is_owned(path):
            raise PermissionError(f"FWRouter tests deny SQLite outside owned state: {args[0]}")
    if event in {"os.listdir", "os.scandir"} and args and path_is_protected(args[0]):
        raise PermissionError(
            "FWRouter tests deny filesystem inspection of production path: "
            f"{args[0]}"
        )
    mutation_paths: tuple[object, ...] = ()
    if event in {"os.remove", "os.rmdir"} and args:
        mutation_paths = (_audit_path(args[0], args[1] if len(args) > 1 else None),)
    elif event == "os.chmod" and args:
        mutation_paths = (_audit_path(args[0], args[2] if len(args) > 2 else None),)
    elif event == "os.chown" and args:
        mutation_paths = (_audit_path(args[0], args[3] if len(args) > 3 else None),)
    elif event == "os.truncate" and args:
        mutation_paths = (_audit_path(args[0]),)
    elif event == "os.mkdir" and args:
        mutation_paths = (_audit_path(args[0], args[2] if len(args) > 2 else None),)
    elif event == "os.rename" and len(args) >= 2:
        mutation_paths = (
            _audit_path(args[0], args[2] if len(args) > 2 else None),
            _audit_path(args[1], args[3] if len(args) > 3 else None),
        )
    elif event in {"os.link", "os.symlink"} and len(args) >= 2:
        mutation_paths = (_audit_path(args[0]), _audit_path(args[1]))
    if mutation_paths and not all(path_is_owned(path) for path in mutation_paths):
        raise PermissionError(
            "FWRouter tests deny filesystem mutation outside owned state: "
            f"{mutation_paths}"
        )


def cleanup_owned_root() -> None:
    if os.environ.get("FWROUTER_PYTEST_KEEP_ARTIFACTS") == "1":
        return
    if OWNED_ROOT is None:
        return
    if COORDINATOR_OWNED:
        return
    marker = OWNED_ROOT / ".fwrouter-test-run-owned"
    if marker.is_file() and OWNED_ROOT != Path("/"):
        try:
            shutil.rmtree(OWNED_ROOT)
        except FileNotFoundError:
            return
        if OWNED_ROOT.exists():
            raise RuntimeError(f"failed to clean owned FWRouter test root: {OWNED_ROOT}")


def _validated_coordinator_root() -> tuple[Path, Path | None] | None:
    """Accept only a private, current-user coordinator root and in-root receipt."""
    raw_root = os.environ.get("FWROUTER_PYTEST_COORDINATOR_ROOT")
    if raw_root is None:
        if os.environ.get("FWROUTER_GATE_NODE_REPORT"):
            raise RuntimeError("gate node report requires a validated coordinator root")
        return None
    candidate = Path(raw_root)
    if not candidate.is_absolute() or candidate.is_symlink():
        raise RuntimeError("coordinator root must be an absolute, non-symlink directory")
    root = candidate.resolve(strict=True)
    root_stat = root.stat()
    if (
        not root.is_dir()
        or Path("/tmp").resolve() not in (root, *root.parents)
        or root_stat.st_uid != os.geteuid()
        or stat.S_IMODE(root_stat.st_mode) & 0o022
    ):
        raise RuntimeError("coordinator root must be a private directory owned by this user")
    marker = root / COORDINATOR_MARKER
    marker_stat = marker.lstat()
    if (
        not stat.S_ISREG(marker_stat.st_mode)
        or marker_stat.st_uid != os.geteuid()
        or stat.S_IMODE(marker_stat.st_mode) & 0o022
        or marker.read_text(encoding="utf-8") != COORDINATOR_MARKER_CONTENT
    ):
        raise RuntimeError("coordinator root ownership marker is invalid")
    report_raw = os.environ.get("FWROUTER_GATE_NODE_REPORT")
    report: Path | None = None
    if report_raw:
        report_candidate = Path(report_raw)
        if not report_candidate.is_absolute() or report_candidate.is_symlink():
            raise RuntimeError("gate node report path must be absolute")
        report = report_candidate.resolve(strict=False)
        if report == root or root not in report.parents or not report.parent.is_dir():
            raise RuntimeError("gate node report must be under the validated coordinator root")
        if report.exists() and (report.is_symlink() or not report.is_file()):
            raise RuntimeError("gate node report must be a regular file")
    return root, report


def configure_test_process() -> tuple[Path, Path]:
    """Set owned runtime paths and install guards before importing the app."""
    global OWNED_ROOT, STATE_ROOT, COORDINATOR_OWNED
    if any(name == "fwrouter_api" or name.startswith("fwrouter_api.") for name in sys.modules):
        raise RuntimeError("FWRouter application modules were imported before test isolation")
    if OWNED_ROOT is not None:
        return OWNED_ROOT, STATE_ROOT  # type: ignore[return-value]

    coordinator = _validated_coordinator_root()
    COORDINATOR_OWNED = coordinator is not None
    if coordinator is None:
        # A hostile inherited TMPDIR must never redirect setup before env scrub.
        OWNED_ROOT = Path(tempfile.mkdtemp(prefix="fwrouter-pytest-", dir="/tmp")).resolve()
        (OWNED_ROOT / ".fwrouter-test-run-owned").touch()
    else:
        OWNED_ROOT, _report_path = coordinator
    STATE_ROOT = OWNED_ROOT / "state"
    if STATE_ROOT.is_symlink():
        raise RuntimeError("FWRouter test state root cannot be a symlink")
    STATE_ROOT.mkdir(exist_ok=True)
    if not STATE_ROOT.is_dir() or STATE_ROOT.stat().st_uid != os.geteuid():
        raise RuntimeError("FWRouter test state root must be an owned directory")

    # Host FWROUTER_* values can contain secrets, runtime paths or unexpected
    # scheduler settings. Tests add synthetic overrides explicitly as needed.
    keep_artifacts = os.environ.get("FWROUTER_PYTEST_KEEP_ARTIFACTS") == "1"
    for directory in ("tmp", "home", "config", "cache", "data"):
        path = OWNED_ROOT / directory
        if path.is_symlink():
            raise RuntimeError(f"FWRouter test directory cannot be a symlink: {path.name}")
        path.mkdir(exist_ok=True)
        if not path.is_dir() or path.stat().st_uid != os.geteuid():
            raise RuntimeError(f"FWRouter test directory must be owned: {path.name}")
    os.environ.clear()
    os.environ.update(
        {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "TZ": "UTC",
            "HOME": str(OWNED_ROOT / "home"),
            "XDG_CONFIG_HOME": str(OWNED_ROOT / "config"),
            "XDG_CACHE_HOME": str(OWNED_ROOT / "cache"),
            "XDG_DATA_HOME": str(OWNED_ROOT / "data"),
            "TMPDIR": str(OWNED_ROOT / "tmp"),
            "FWROUTER_PYTEST_OWNED_DIR": str(OWNED_ROOT),
            "FWROUTER_STATE_DIR": str(STATE_ROOT),
            "FWROUTER_ENVIRONMENT": "test",
            "FWROUTER_STARTUP_TASKS_ENABLED": "false",
            "FWROUTER_STARTUP_RECOVERY_ENABLED": "false",
            "FWROUTER_WATCHDOG_SCHEDULER_ENABLED": "false",
            "FWROUTER_MAINTENANCE_SCHEDULER_ENABLED": "false",
            "FWROUTER_MEMBER_PROBE_SCHEDULER_ENABLED": "false",
            "FWROUTER_ACTIVE_OBSERVATION_SCHEDULER_ENABLED": "false",
            "FWROUTER_SUBJECT_INVENTORY_SCHEDULER_ENABLED": "false",
            "FWROUTER_EXTERNAL_COLLECTOR_SCHEDULER_ENABLED": "false",
            "FWROUTER_RUNTIME_CONVERGENCE_SCHEDULER_ENABLED": "false",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    if coordinator is not None and coordinator[1] is not None:
        os.environ["FWROUTER_GATE_NODE_REPORT"] = str(coordinator[1])
    if keep_artifacts:
        os.environ["FWROUTER_PYTEST_KEEP_ARTIFACTS"] = "1"
    sys.dont_write_bytecode = True
    sys.addaudithook(_deny_unsafe_python_capabilities)
    atexit.register(cleanup_owned_root)
    return OWNED_ROOT, STATE_ROOT
