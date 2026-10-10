"""Fail-closed validation of the orchestrator's read-only acceptance profile."""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
from pathlib import Path
from typing import Any
from importlib import metadata
import socket


PROFILE_PATH = Path("/run/fwrouter-acceptance/profile.json")
PROFILE_SCHEMA = "fwrouter-acceptance-profile/v2"
SHA256 = re.compile(r"^[0-9a-f]{64}$")
REVISION = re.compile(r"^[0-9a-f]{40}$")
NONCE = re.compile(r"^[0-9a-f]{32}$")
EXPECTED_BINARIES = {
    "xray": Path("/opt/fwrouter-test/bin/xray"),
    "mihomo": Path("/opt/fwrouter-test/bin/mihomo"),
    "chromium": Path("/opt/fwrouter-test/bin/chromium"),
}
EXPECTED_BINARIES["chromium"] = Path("/opt/fwrouter-test/chromium/chrome-linux64/chrome")
VERSION_COMMANDS = {
    "xray": ["version"],
    "mihomo": ["-v"],
    "chromium": ["--version"],
}


class ProfileError(RuntimeError):
    pass


def _qualification_error(facts: dict[str, Any]) -> str | None:
    """Pure fail-closed environment predicate, testable without host effects."""
    if facts.get("uid") != 10001 or facts.get("gid") != 10001:
        return "acceptance requires the fixed non-root uid/gid 10001:10001"
    if str(facts.get("hostname", "")).strip().lower() == "minisk":
        return "acceptance refuses the production hostname"
    for key, message in (
        ("docker_marker_regular", "container marker is missing or invalid"),
        ("production_api_absent", "production API installation is present"),
        ("readonly_root", "container root filesystem is not read-only"),
        ("readonly_profile_mount", "acceptance profile mount is not read-only"),
        ("fixed_workspace", "acceptance workspace is not the fixed /workspace tree"),
    ):
        if facts.get(key) is not True:
            return message
    return None


def _require_host_qualification(profile_path: Path) -> None:
    if profile_path != PROFILE_PATH:
        raise ProfileError("acceptance profile must use the fixed container mount path")
    try:
        docker_info = Path("/.dockerenv").lstat()
        docker_regular = stat.S_ISREG(docker_info.st_mode) and not Path("/.dockerenv").is_symlink()
        root_readonly = bool(os.statvfs("/").f_flag & getattr(os, "ST_RDONLY", 1))
        # Docker's single-file bind mount is read-only at the file mountpoint;
        # checking its parent would inspect /run's unrelated mount flags.
        profile_readonly = bool(os.statvfs(str(profile_path)).f_flag & getattr(os, "ST_RDONLY", 1))
        workspace = Path("/workspace")
        fixed_workspace = workspace.is_dir() and workspace.resolve(strict=True) == workspace
        facts = {
            "uid": os.getuid(), "gid": os.getgid(), "hostname": socket.gethostname(),
            "docker_marker_regular": docker_regular,
            "production_api_absent": not Path("/opt/fwrouter-api").exists() and not Path("/opt/fwrouter-api").is_symlink(),
            "readonly_root": root_readonly, "readonly_profile_mount": profile_readonly,
            "fixed_workspace": fixed_workspace,
        }
    except OSError as exc:
        raise ProfileError("could not establish hosted-container qualification") from exc
    reason = _qualification_error(facts)
    if reason:
        raise ProfileError(reason)


def _sha(path: Path) -> str:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or path.is_symlink() or not os.access(path, os.X_OK):
        raise ProfileError(f"profile binary is not a regular executable: {path.name}")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _observed_version(path: Path, args: list[str], expected: str) -> str:
    try:
        completed = subprocess.run(
            [str(path), *args], check=False, capture_output=True, text=True, timeout=8,
            stdin=subprocess.DEVNULL,
            env={"PATH": "/usr/bin:/bin", "HOME": "/tmp", "TMPDIR": "/tmp", "LANG": "C.UTF-8", "TZ": "UTC"},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProfileError(f"could not read pinned {path.name} version") from exc
    output = f"{completed.stdout}\n{completed.stderr}".strip()[:4096]
    if completed.returncode != 0 or expected not in output:
        raise ProfileError(f"observed {path.name} version does not match the profile")
    return output


def _ui_digest(root: Path) -> str:
    digest = hashlib.sha256()
    ui_root = root.resolve(strict=True)
    files = sorted(path for path in ui_root.rglob("*") if path.is_file() or path.is_symlink())
    for path in files:
        if path.is_symlink() or not path.is_file():
            raise ProfileError("UI tree contains a symlink or non-regular file")
        relative = "ui/" + path.relative_to(ui_root).as_posix()
        digest.update(relative.encode("utf-8") + b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _source_manifest_digest(workspace: Path) -> str:
    digest = hashlib.sha256()
    roots = [workspace / "backend/fwrouter_api", workspace / "backend/tests", workspace / "ui",
             workspace / "tests/application_acceptance", workspace / "tests/acceptance",
             workspace / "backend/pyproject.toml", workspace / "tests/gates/requirements-ci.txt",
             workspace / "host/libexec/fwrouter/traffic-collect.sh",
             workspace / "host/libexec/fwrouter/dataplane-common.sh",
             workspace / "host/libexec/fwrouter/dataplane-check.sh",
             workspace / "host/libexec/fwrouter/dataplane-apply.sh",
             workspace / "host/libexec/fwrouter/dataplane-rollback.sh"]
    files: list[Path] = []
    for root in roots:
        if root.is_file():
            files.append(root)
            continue
        if not root.is_dir():
            continue
        files.extend(path for path in root.rglob("*") if path.is_file() or path.is_symlink())
    for path in sorted(set(files), key=lambda value: value.relative_to(workspace).as_posix()):
        if path.is_symlink() or not path.is_file():
            raise ProfileError("acceptance source context contains a symlink or non-regular file")
        relative = path.relative_to(workspace).as_posix()
        if relative.startswith("native/") or "/__pycache__/" in f"/{relative}/":
            continue
        digest.update(relative.encode("utf-8") + b"\0")
        digest.update(path.read_bytes())
    return digest.hexdigest()


def load_profile(path: Path = PROFILE_PATH) -> tuple[dict[str, Any], str]:
    # Must run before profile parsing, binary hashing, or version subprocesses.
    _require_host_qualification(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or path.is_symlink():
        raise ProfileError("acceptance profile must be a regular, non-symlink file")
    raw = path.read_bytes()
    if len(raw) > 64 * 1024:
        raise ProfileError("acceptance profile exceeds size limit")
    try:
        profile = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ProfileError("acceptance profile is invalid JSON") from exc
    expected_top = {"schema", "profile", "source_revision", "plan_digest", "xray", "mihomo", "chromium",
                    "playwright_python", "baseline_xray_config_sha256", "ui_tree_sha256", "suite_nonce"}
    if not isinstance(profile, dict) or set(profile) != expected_top:
        raise ProfileError("acceptance profile fields do not match v2")
    if profile["schema"] != PROFILE_SCHEMA or profile["profile"] != "hosted-native-process":
        raise ProfileError("unsupported acceptance profile")
    if not REVISION.fullmatch(str(profile["source_revision"])):
        raise ProfileError("source revision is not a full commit id")
    if not SHA256.fullmatch(str(profile["plan_digest"])):
        raise ProfileError("affected gate plan digest is invalid")
    if not NONCE.fullmatch(str(profile["suite_nonce"])):
        raise ProfileError("suite nonce is invalid")
    for key in ("baseline_xray_config_sha256", "ui_tree_sha256"):
        if not SHA256.fullmatch(str(profile[key])):
            raise ProfileError(f"{key} is invalid")
    for name, expected_path in EXPECTED_BINARIES.items():
        item = profile[name]
        required = {"path", "sha256", "version", "bundle_sha256"} if name == "chromium" else {"path", "sha256", "version"}
        if not isinstance(item, dict) or set(item) != required:
            raise ProfileError(f"{name} binary record is invalid")
        path_value = Path(str(item["path"]))
        env_name = "FWROUTER_BROWSER_EXECUTABLE" if name == "chromium" else f"FWROUTER_{name.upper()}_BINARY"
        if path_value != expected_path or os.environ.get(env_name) != str(expected_path):
            raise ProfileError(f"{name} binary path does not match the pinned container path")
        if not SHA256.fullmatch(str(item["sha256"])) or _sha(path_value) != item["sha256"]:
            raise ProfileError(f"{name} binary digest does not match the profile")
        if not str(item["version"]).strip():
            raise ProfileError(f"{name} binary version is missing")
        if name == "chromium" and not SHA256.fullmatch(str(item["bundle_sha256"])):
            raise ProfileError("Chromium bundle digest is invalid")
        _observed_version(path_value, VERSION_COMMANDS[name], str(item["version"]))
    try:
        observed_playwright = metadata.version("playwright")
    except metadata.PackageNotFoundError as exc:
        raise ProfileError("pinned Playwright Python package is not installed") from exc
    if observed_playwright != str(profile["playwright_python"]):
        raise ProfileError("Playwright Python version does not match the profile")
    fixture = Path(__file__).with_name("fixtures") / "xray.initial.json"
    if hashlib.sha256(fixture.read_bytes()).hexdigest() != profile["baseline_xray_config_sha256"]:
        raise ProfileError("baseline Xray fixture digest does not match the profile")
    ui_root = Path("/workspace/ui") if Path("/workspace/ui").is_dir() else Path(__file__).resolve().parents[2] / "ui"
    if _ui_digest(ui_root) != profile["ui_tree_sha256"]:
        raise ProfileError("observed UI tree digest does not match the profile")
    revision_file = Path("/workspace/.fwrouter-acceptance-revision")
    if not revision_file.is_file() or revision_file.is_symlink():
        raise ProfileError("acceptance build revision metadata is missing")
    try:
        revision = json.loads(revision_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProfileError("acceptance build revision metadata is invalid") from exc
    if not isinstance(revision, dict) or set(revision) != {"source_revision", "ui_tree_sha256", "source_manifest_sha256"}:
        raise ProfileError("acceptance build revision metadata has unexpected fields")
    if revision.get("source_revision") != profile["source_revision"] or revision.get("ui_tree_sha256") != profile["ui_tree_sha256"]:
        raise ProfileError("acceptance build revision metadata does not match the profile")
    manifest_digest = revision.get("source_manifest_sha256")
    workspace = Path("/workspace")
    if not SHA256.fullmatch(str(manifest_digest)) or _source_manifest_digest(workspace) != manifest_digest:
        raise ProfileError("observed source manifest does not match the immutable build metadata")
    return profile, hashlib.sha256(raw).hexdigest()
