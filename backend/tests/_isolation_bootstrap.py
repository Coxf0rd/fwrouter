"""Fail-closed Python capability setup for routine FWRouter tests.

This is a process-level safety layer for reviewed tests, not an OS sandbox.
Run integration/native/recovery scenarios only in a separately qualified
disposable environment without production authority.
"""

from __future__ import annotations

import atexit
import hashlib
import json
import os
import re
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
QUALIFIED_CHILD_PROFILE: dict[str, object] | None = None
QUALIFIED_CHILD_PROFILE_SHA256: str | None = None
QUALIFIED_DOCKER_XRAY_PROFILE: dict[str, object] | None = None
QUALIFIED_DOCKER_XRAY_PROFILE_SHA256: str | None = None
QUALIFIED_DOCKER_XRAY_PROFILE_PATH: str | None = None
QUALIFIED_CHILD_TEST_FILES = {
    "/workspace/backend/tests/test_protocol_native_validation.py",
    "/workspace/backend/tests/test_traffic_accounting.py",
    "/workspace/backend/tests/test_vpn_auto_writer_guard.py",
    "/workspace/backend/tests/test_xray.py",
    "/workspace/backend/tests/test_xray_default_runner_archive.py",
}
QUALIFIED_CHILD_XRAY_FILES = {
    "/workspace/backend/tests/test_vpn_auto_writer_guard.py",
    "/workspace/backend/tests/test_xray.py",
    "/workspace/backend/tests/test_xray_default_runner_archive.py",
}
QUALIFIED_DOCKER_XRAY_FILE: str | None = None


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


def _mount_options_for(target: str) -> set[str]:
    try:
        lines = Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines()
    except OSError:
        return set()
    for line in lines:
        fields = line.split()
        if len(fields) > 5 and fields[4].replace("\\040", " ") == target:
            return set(fields[5].split(","))
    return set()


def _load_qualified_child_profile() -> tuple[dict[str, object] | None, str | None]:
    raw = os.environ.get("FWROUTER_QUALIFIED_CHILD_PROFILE")
    if raw is None:
        return None, None
    if raw != "/run/fwrouter-acceptance/qualified-child-profile.json":
        raise RuntimeError("qualified child profile path is not the reviewed read-only mount")
    path = Path(raw)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o222 or "ro" not in _mount_options_for(raw):
        raise RuntimeError("qualified child profile must be a read-only regular-file mount")
    payload = path.read_bytes()
    if len(payload) > 256 * 1024:
        raise RuntimeError("qualified child profile exceeds its size bound")
    try:
        profile = json.loads(payload)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("qualified child profile is invalid JSON") from exc
    if (not isinstance(profile, dict)
            or profile.get("schema") != "fwrouter-qualified-child-profile/v1"
            or profile.get("profile") != "qualified-child-process"
            or not isinstance(profile.get("source_revision"), str)
            or not re.fullmatch(r"[0-9a-f]{40,64}", profile.get("source_revision", ""))
            or not isinstance(profile.get("plan_digest"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", profile.get("plan_digest", ""))
            or not isinstance(profile.get("suite_nonce"), str)
            or not re.fullmatch(r"[0-9a-f]{32}", profile.get("suite_nonce", ""))):
        raise RuntimeError("qualified child profile identity is incomplete")
    if (os.getuid() != 10001 or os.getgid() != 10001 or not Path("/.dockerenv").is_file()
            or "ro" not in _mount_options_for("/") or Path("/var/run/docker.sock").exists()):
        raise RuntimeError("qualified child process is not in the required non-root read-only container")
    try:
        status = Path("/proc/self/status").read_text(encoding="ascii")
    except OSError as exc:
        raise RuntimeError("qualified child process capabilities cannot be inspected") from exc
    fields = dict(line.split(":", 1) for line in status.splitlines() if ":" in line)
    if fields.get("CapEff", "").strip() != "0000000000000000" or fields.get("NoNewPrivs", "").strip() != "1":
        raise RuntimeError("qualified child process has capabilities or lacks no-new-privileges")
    mihomo = profile.get("mihomo")
    if (not isinstance(mihomo, dict) or mihomo.get("path") != "/opt/fwrouter-test/bin/mihomo"
            or os.environ.get("FWROUTER_TEST_MIHOMO_BINARY") != mihomo.get("path")):
        raise RuntimeError("qualified child profile lacks the pinned local Mihomo input")
    binary = Path(str(mihomo["path"]))
    digest = hashlib.sha256(binary.read_bytes()).hexdigest() if binary.is_file() and not binary.is_symlink() else ""
    if not re.fullmatch(r"[0-9a-f]{64}", str(mihomo.get("sha256", ""))) or digest != mihomo.get("sha256"):
        raise RuntimeError("qualified child Mihomo binary digest does not match the reviewed profile")
    try:
        revision = json.loads(Path("/workspace/.fwrouter-acceptance-revision").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("qualified child source revision marker is unavailable") from exc
    if (revision.get("source_revision") != profile.get("source_revision")
            or revision.get("source_manifest_sha256") != profile.get("source_manifest_sha256")):
        raise RuntimeError("qualified child profile does not match the source revision manifest")
    source_hash = hashlib.sha256()
    source_files: list[Path] = []
    for base in ("backend/fwrouter_api", "backend/tests", "ui", "tests/application_acceptance", "tests/acceptance"):
        root = Path("/workspace") / base
        members = list(root.rglob("*"))
        if any(member.is_symlink() for member in members):
            raise RuntimeError("qualified child source tree contains a symlink")
        source_files.extend(member for member in members if member.is_file()
                            and "/__pycache__/" not in f"/{member.relative_to('/workspace').as_posix()}/")
    source_files.extend((Path("/workspace/backend/pyproject.toml"),
                         Path("/workspace/tests/gates/requirements-ci.txt"),
                         Path("/workspace/host/libexec/fwrouter/traffic-collect.sh")))
    for member in sorted(source_files, key=lambda item: item.relative_to("/workspace").as_posix()):
        relative = member.relative_to("/workspace").as_posix()
        source_hash.update(relative.encode("utf-8") + b"\0")
        with member.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                source_hash.update(block)
    if source_hash.hexdigest() != profile.get("source_manifest_sha256"):
        raise RuntimeError("qualified child checked-out source digest does not match the profile")
    return profile, hashlib.sha256(payload).hexdigest()


def _qualified_child_test_file() -> str | None:
    frame = sys._getframe(1)
    try:
        while frame:
            filename = os.path.realpath(frame.f_code.co_filename)
            if filename in QUALIFIED_CHILD_TEST_FILES or filename == QUALIFIED_DOCKER_XRAY_FILE:
                return filename
            frame = frame.f_back
    finally:
        del frame
    return None


def _qualified_child_process_allowed(args: tuple[object, ...]) -> bool:
    filename = _qualified_child_test_file()
    if filename is None:
        return False
    # The audit event signature is (executable, args, cwd, env).
    command = args[1] if len(args) > 1 else None
    if not isinstance(command, (list, tuple)) or not command:
        return False
    values = [os.fsdecode(value) if isinstance(value, (str, bytes, os.PathLike)) else "" for value in command]
    if filename.endswith("test_protocol_native_validation.py"):
        binary = "/opt/fwrouter-test/bin/mihomo"
        if values == [binary, "-v"]:
            return True
        if (len(values) == 6 and values[0:2] == [binary, "-t"] and values[2] == "-d"
                and values[4] == "-f" and path_is_owned(values[3]) and path_is_owned(values[5])):
            return True
        return False
    if filename.endswith("test_traffic_accounting.py"):
        return values == ["/workspace/host/libexec/fwrouter/traffic-collect.sh"]
    if filename in QUALIFIED_CHILD_XRAY_FILES:
        return values[0] == sys.executable and len(values) >= 3 and values[1] == "-c"
    return False


def _qualified_docker_xray_process_allowed(args: tuple[object, ...]) -> bool:
    if QUALIFIED_DOCKER_XRAY_PROFILE is None or QUALIFIED_DOCKER_XRAY_FILE is None:
        return False
    if _qualified_child_test_file() != QUALIFIED_DOCKER_XRAY_FILE:
        return False
    command = args[1] if len(args) > 1 else None
    if not isinstance(command, (list, tuple)):
        return False
    values = [os.fsdecode(value) if isinstance(value, (str, bytes, os.PathLike)) else "" for value in command]
    if not values or values[0] != "docker":
        return False
    image_id = str(QUALIFIED_DOCKER_XRAY_PROFILE["image_id"])
    container_name = str(QUALIFIED_DOCKER_XRAY_PROFILE["container_name"])
    if values == ["docker", "run", "--pull=never", "--rm", "--network", "none", image_id, "version"]:
        return True
    if (len(values) == 26 and values[:2] == ["docker", "run"]
            and values[2:10] == ["--pull=never", "--detach", "--rm", "--network", "none",
                                  "--cpus=0.5", "--memory=128m", "--pids-limit=64"]
            and values[10:14] == ["--cap-drop=ALL", "--security-opt=no-new-privileges", "--user", "65534:65534"]
            and values[14:18] == ["--label", "io.fwrouter.acceptance.owner=fwrouter-test-harness-v1",
                                  "--label", f"io.fwrouter.acceptance.run={QUALIFIED_DOCKER_XRAY_PROFILE['suite_nonce']}"]
            and values[18:20] == ["--name", container_name] and values[20] == "-v"
            and values[22] == image_id and values[23:] == ["run", "-config", "/etc/xray/config.json"]):
        bind = values[21]
        if bind.endswith(":/etc/xray/config.json:ro") and path_is_owned(bind.split(":", 1)[0]):
            return True
    if values[:2] == ["docker", "restart"] and len(values) == 3 and values[2] == container_name:
        return True
    if values[:2] == ["docker", "stop"] and len(values) == 3 and values[2] == container_name:
        return True
    if values[:2] == ["docker", "exec"] and len(values) == 9 and values[2] == container_name:
        return values[3:] == ["xray", "api", "inbounduser", "--server=127.0.0.1:10085", "-timeout=3", "-tag=vless-ws"]
    if values[:2] == ["docker", "inspect"] and len(values) == 5 and values[4] == container_name:
        return values[2] == "--format" and values[3] in {
            "{{.Id}}", "{{.Id}}|{{.State.Running}}|{{.State.StartedAt}}",
        }
    if values[:2] == ["docker", "cp"] and len(values) == 4 and values[2].split(":", 1)[0] == container_name:
        if values[2] != f"{values[2].split(':', 1)[0]}:/etc/xray/config.json":
            return False
        return values[3] == "-" or path_is_owned(values[3])
    return False


def _load_qualified_docker_xray_profile() -> tuple[dict[str, object] | None, str | None]:
    global QUALIFIED_DOCKER_XRAY_FILE, QUALIFIED_DOCKER_XRAY_PROFILE_PATH
    raw = os.environ.get("FWROUTER_QUALIFIED_DOCKER_XRAY_PROFILE")
    if raw is None:
        return None, None
    workspace = Path(os.environ.get("GITHUB_WORKSPACE", "/nonexistent")).resolve(strict=True)
    runner_temp = Path(os.environ.get("RUNNER_TEMP", "/nonexistent")).resolve(strict=True)
    expected_path = runner_temp / "fwrouter-xray-native-profile" / "qualified-xray-profile.json"
    if (Path(raw) != expected_path or os.environ.get("GITHUB_ACTIONS") != "true"
            or os.environ.get("RUNNER_ENVIRONMENT") != "github-hosted" or os.getuid() in (0, None)
            or not (Path("/imagegeneration").exists() or Path("/etc/runner-image").is_file())
            or os.uname().nodename.lower().startswith("minisk")
            or not workspace.is_relative_to(Path("/home/runner/work"))
            or not runner_temp.is_relative_to(Path("/home/runner/work/_temp"))):
        raise RuntimeError("qualified Docker Xray profile is not running on a standard hosted runner")
    if Path(__file__).resolve().parents[2] != workspace:
        raise RuntimeError("qualified Docker Xray checkout differs from GITHUB_WORKSPACE")
    QUALIFIED_DOCKER_XRAY_FILE = str(workspace / "backend/tests/test_xray_native_readback.py")
    for marker in (
        Path("/opt/fwrouter-api"), Path("/opt/fwrouter-xray"), Path("/opt/fwrouter-mihomo"),
        Path("/var/lib/fwrouter-v2"), Path("/run/fwrouter-v2"),
        Path("/etc/systemd/system/fwrouter-api.service"),
    ):
        if marker.exists():
            raise RuntimeError("qualified Docker Xray runner has a protected production marker")
    if os.environ.get("DOCKER_HOST"):
        raise RuntimeError("qualified Docker Xray profile forbids a remote Docker daemon")
    path = Path(raw)
    QUALIFIED_DOCKER_XRAY_PROFILE_PATH = str(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o222:
        raise RuntimeError("qualified Docker Xray profile must be a private read-only regular file")
    payload = path.read_bytes()
    if len(payload) > 256 * 1024:
        raise RuntimeError("qualified Docker Xray profile exceeds its size bound")
    try:
        profile = json.loads(payload)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("qualified Docker Xray profile is invalid JSON") from exc
    if (not isinstance(profile, dict)
            or profile.get("schema") != "fwrouter-qualified-docker-xray-profile/v1"
            or profile.get("profile") != "qualified-docker-xray"
            or profile.get("source_revision") != __import__("subprocess").run(
                ["git", "rev-parse", "HEAD"], cwd=workspace, check=True,
                capture_output=True, text=True, timeout=10).stdout.strip()
            or not re.fullmatch(r"[0-9a-f]{64}", str(profile.get("plan_digest", "")))
            or not re.fullmatch(r"[0-9a-f]{64}", str(profile.get("source_manifest_sha256", "")))
            or not re.fullmatch(r"[0-9a-f]{32}", str(profile.get("suite_nonce", "")))
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", str(profile.get("image_id", "")))
            or profile.get("container_name") != f"native-xray-readback-test-{str(profile.get('suite_nonce', ''))[:12]}"
            or profile.get("owned_root") != f"/tmp/fwrouter-xray-native-{profile.get('suite_nonce')}"
            or profile.get("xray_binary_sha256") != "3f650abf1fc4a4fbf5abe7fc9990a2658020907cd984214e9c075b4b00989fea"
            or profile.get("xray_version") != "26.2.6"):
        raise RuntimeError("qualified Docker Xray profile identity is incomplete or stale")
    owned_root = Path(str(profile["owned_root"]))
    if not owned_root.is_relative_to(Path("/tmp")) or os.environ.get("FWROUTER_PYTEST_COORDINATOR_ROOT") != str(owned_root):
        raise RuntimeError("qualified Docker Xray owned test root is outside /tmp")
    try:
        root_info = owned_root.lstat()
        marker = owned_root / COORDINATOR_MARKER
        marker_info = marker.lstat()
    except OSError as exc:
        raise RuntimeError("qualified Docker Xray owned test root is unavailable") from exc
    if (not stat.S_ISDIR(root_info.st_mode) or root_info.st_uid != os.getuid()
            or stat.S_IMODE(root_info.st_mode) & 0o077
            or not stat.S_ISREG(marker_info.st_mode) or marker_info.st_uid != os.getuid()
            or marker.read_text(encoding="utf-8") != COORDINATOR_MARKER_CONTENT):
        raise RuntimeError("qualified Docker Xray test root ownership marker is invalid")
    source_files: list[Path] = []
    root = workspace
    for base in ("backend/fwrouter_api", "backend/tests", "ui", "tests/application_acceptance", "tests/acceptance"):
        tree = root / base
        members = list(tree.rglob("*"))
        if any(item.is_symlink() for item in members):
            raise RuntimeError("qualified Docker Xray source tree contains a symlink")
        source_files.extend(item for item in members if item.is_file()
                            and "/__pycache__/" not in f"/{item.relative_to(root).as_posix()}/")
    source_files.extend((root / "backend/pyproject.toml", root / "tests/gates/requirements-ci.txt",
                         root / "host/libexec/fwrouter/traffic-collect.sh"))
    digest = hashlib.sha256()
    for item in sorted(source_files, key=lambda value: value.relative_to(root).as_posix()):
        relative = item.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8") + b"\0")
        with item.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    if digest.hexdigest() != profile["source_manifest_sha256"]:
        raise RuntimeError("qualified Docker Xray source manifest does not match this checkout")
    xray_binary = profile.get("xray_binary")
    runner_temp = Path(os.environ.get("RUNNER_TEMP", "/nonexistent")).resolve(strict=True)
    if (not isinstance(xray_binary, str) or xray_binary != os.environ.get("FWROUTER_ACCEPTANCE_XRAY_BINARY")
            or not Path(xray_binary).is_relative_to(runner_temp)
            or os.environ.get("FWROUTER_ACCEPTANCE_XRAY_SHA256") != profile["xray_binary_sha256"]
            or os.environ.get("FWROUTER_ACCEPTANCE_XRAY_VERSION") != profile["xray_version"]
            or not Path(xray_binary).is_file()
            or hashlib.sha256(Path(xray_binary).read_bytes()).hexdigest() != profile["xray_binary_sha256"]):
        raise RuntimeError("qualified Docker Xray source binary digest does not match the profile")
    docker = shutil.which("docker", path="/usr/local/bin:/usr/bin:/bin")
    if docker is None:
        raise RuntimeError("qualified Docker Xray profile has no local Docker CLI")
    context = __import__("subprocess").run([docker, "context", "inspect"], check=True,
        capture_output=True, text=True, timeout=10)
    try:
        contexts = json.loads(context.stdout)
        endpoint = contexts[0]["Endpoints"]["docker"]["Host"]
    except (IndexError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("qualified Docker Xray Docker context is not inspectable") from exc
    if not endpoint.startswith("unix:///var/run/"):
        raise RuntimeError("qualified Docker Xray profile requires the local hosted Docker daemon")
    image = __import__("subprocess").run([docker, "image", "inspect", profile["image_id"]],
        check=True, capture_output=True, text=True, timeout=10)
    try:
        image_info = json.loads(image.stdout)[0]
        labels = image_info["Config"]["Labels"]
    except (IndexError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("qualified Docker Xray local image is not inspectable") from exc
    if (image_info.get("Id") != profile["image_id"]
            or labels.get("io.fwrouter.acceptance.owner") != "fwrouter-test-harness-v1"
            or labels.get("io.fwrouter.acceptance.run") != profile["suite_nonce"]
            or image_info.get("Architecture") != "amd64" or image_info.get("Os") != "linux"):
        raise RuntimeError("qualified Docker Xray test image ownership/platform does not match profile")
    return profile, hashlib.sha256(payload).hexdigest()


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
        if QUALIFIED_CHILD_PROFILE is not None and event == "subprocess.Popen" and _qualified_child_process_allowed(args):
            return
        if QUALIFIED_DOCKER_XRAY_PROFILE is not None and event == "subprocess.Popen" and _qualified_docker_xray_process_allowed(args):
            return
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
    global QUALIFIED_CHILD_PROFILE, QUALIFIED_CHILD_PROFILE_SHA256
    global QUALIFIED_DOCKER_XRAY_PROFILE, QUALIFIED_DOCKER_XRAY_PROFILE_SHA256
    if any(name == "fwrouter_api" or name.startswith("fwrouter_api.") for name in sys.modules):
        raise RuntimeError("FWRouter application modules were imported before test isolation")
    if OWNED_ROOT is not None:
        return OWNED_ROOT, STATE_ROOT  # type: ignore[return-value]

    QUALIFIED_CHILD_PROFILE, QUALIFIED_CHILD_PROFILE_SHA256 = _load_qualified_child_profile()
    QUALIFIED_DOCKER_XRAY_PROFILE, QUALIFIED_DOCKER_XRAY_PROFILE_SHA256 = _load_qualified_docker_xray_profile()
    if QUALIFIED_CHILD_PROFILE is not None and QUALIFIED_DOCKER_XRAY_PROFILE is not None:
        raise RuntimeError("qualified child and Docker Xray profiles cannot be combined")

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
    if QUALIFIED_CHILD_PROFILE is not None:
        mihomo = QUALIFIED_CHILD_PROFILE["mihomo"]
        assert isinstance(mihomo, dict)
        os.environ["FWROUTER_QUALIFIED_CHILD_PROFILE"] = "/run/fwrouter-acceptance/qualified-child-profile.json"
        os.environ["FWROUTER_TEST_MIHOMO_BINARY"] = str(mihomo["path"])
    if QUALIFIED_DOCKER_XRAY_PROFILE is not None:
        assert QUALIFIED_DOCKER_XRAY_PROFILE_PATH is not None
        os.environ["FWROUTER_QUALIFIED_DOCKER_XRAY_PROFILE"] = QUALIFIED_DOCKER_XRAY_PROFILE_PATH
        os.environ["FWROUTER_XRAY_TEST_IMAGE"] = str(QUALIFIED_DOCKER_XRAY_PROFILE["image_id"])
        os.environ["FWROUTER_XRAY_TEST_RUN_ID"] = str(QUALIFIED_DOCKER_XRAY_PROFILE["suite_nonce"])
    if keep_artifacts:
        os.environ["FWROUTER_PYTEST_KEEP_ARTIFACTS"] = "1"
    sys.dont_write_bytecode = True
    sys.addaudithook(_deny_unsafe_python_capabilities)
    atexit.register(cleanup_owned_root)
    return OWNED_ROOT, STATE_ROOT
