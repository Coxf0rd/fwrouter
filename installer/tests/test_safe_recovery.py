from __future__ import annotations

import importlib.util
import io
import json
import os as os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
from unittest.mock import patch

import pytest


TOOL = Path(__file__).parents[1] / "safe_recovery.py"
SPEC = importlib.util.spec_from_file_location("fwrouter_safe_recovery", TOOL)
assert SPEC and SPEC.loader
recovery = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(recovery)
REPO = TOOL.parents[1]


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], check=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return result.stdout.strip()


def _source_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "source"
    (repo / "backend/scripts").mkdir(parents=True)
    (repo / "installer").mkdir()
    shutil.copyfile(REPO / "installer/install.sh", repo / "installer/install.sh")
    shutil.copyfile(REPO / "installer/obsolete-backend-paths.txt",
                    repo / "installer/obsolete-backend-paths.txt")
    shutil.copyfile(REPO / "installer/component_install_lock.py",
                    repo / "installer/component_install_lock.py")
    (repo / "backend/fwrouter_api").mkdir()
    (repo / "backend/fwrouter_api/__init__.py").write_text("SAFE_RECOVERY_FIXTURE = True\n")
    (repo / "backend/.gitignore").write_text("secret.env\n__pycache__/\n*.bak\n")
    bootstrap = """#!/bin/sh
set -eu
mkdir -p "$1" "$2" "$3"
"""
    (repo / "backend/scripts/bootstrap-state.sh").write_text(bootstrap)
    (repo / "backend/scripts/bootstrap-state.sh").chmod(0o755)
    _git(repo, "init", "--quiet")
    (repo / "installer/install.sh").chmod(0o755)
    _git(repo, "config", "user.email", "safe-recovery@example.invalid")
    _git(repo, "config", "user.name", "Safe Recovery Test")
    _git(repo, "add", "backend", "installer/install.sh", "installer/obsolete-backend-paths.txt",
         "installer/component_install_lock.py")
    _git(repo, "commit", "--quiet", "-m", "fixture")
    return repo


def _receipt(repo: Path, *, status: str = "passed", source_sha: str | None = None,
             digest: str | None = None) -> Path:
    sha = recovery._git_source_identity(repo)
    entries = recovery._entries(recovery._tracked_source_files(repo))
    payload = {
        "schema": recovery.RECEIPT_SCHEMA,
        "receipt_id": "hosted-run-fixture-1",
        "status": status,
        "source_sha": source_sha or sha,
        "source_manifest_sha256": digest or recovery._entries_digest(entries),
        "hosted_run_id": "123456789",
        "hosted_run_url": "https://github.com/example/fwrouter/actions/runs/123456789",
        "qualification_scope": "component_only_fixture",
        "fixture_note": "synthetic test input; it is not hosted or runtime evidence",
        "gates": {name: "passed" for name in recovery.REQUIRED_GATES},
    }
    path = repo.parent / "hosted-qualification.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _artifact(tmp_path: Path, *, receipt_status: str = "passed") -> tuple[Path, Path, Path]:
    repo = _source_repo(tmp_path)
    receipt = _receipt(repo, status=receipt_status)
    artifact = tmp_path / "recovery-artifact"
    recovery.prepare(repo, artifact, receipt)
    return repo, artifact, receipt


def _private_target(label: str) -> tuple[Path, Path]:
    private_root = Path(tempfile.mkdtemp(prefix=f"fwrouter-safe-recovery-{label}-",
                                          dir=tempfile.gettempdir()))
    private_root.chmod(0o700)
    target = private_root / "target"
    target.mkdir(mode=0o700)
    return private_root, target


def test_prepare_verify_and_repeat_isolated_stage_preserve_state_and_host_sentinels(tmp_path: Path) -> None:
    _, artifact, receipt = _artifact(tmp_path)
    manifest, _ = recovery.verify(artifact, receipt)
    private_root, target = _private_target("repeat")
    try:
        database = target / "var/lib/fwrouter-v2/fwrouter.db"
        database.parent.mkdir(parents=True)
        database.write_bytes(b"current-db-intent\0")
        database.chmod(0o600)
        env = target / "opt/fwrouter-api/.env"
        env.parent.mkdir(parents=True)
        env.write_bytes(b"PRIVATE_SENTINEL=preserved\n")
        env.chmod(0o600)
        host_guard = target / "usr/local/libexec/fwrouter/traffic-collect-api.sh"
        host_guard.parent.mkdir(parents=True)
        host_guard.write_bytes(b"LIVE_READINESS_GUARD_SENTINEL\n")
        obsolete_trace = target / "opt/fwrouter-api/fwrouter_api/services/startup_trace.py"
        obsolete_trace.parent.mkdir(parents=True)
        obsolete_trace.write_text("ARCHIVED_TRACE_HELPER = True\n")

        stage_receipt = recovery.stage(artifact, receipt, target)
        assert (target / "opt/fwrouter-api/fwrouter_api/__init__.py").read_text() == "SAFE_RECOVERY_FIXTURE = True\n"
        assert database.read_bytes() == b"current-db-intent\0"
        assert env.read_bytes() == b"PRIVATE_SENTINEL=preserved\n"
        assert host_guard.read_bytes() == b"LIVE_READINESS_GUARD_SENTINEL\n"
        assert not obsolete_trace.exists()
        assert stage_receipt["runtime_qualification"] == "not_established"
        assert ".env" in stage_receipt["retained_extra_files"]

        recovery.stage(artifact, receipt, target)
        assert database.read_bytes() == b"current-db-intent\0"
        assert env.read_bytes() == b"PRIVATE_SENTINEL=preserved\n"
        assert host_guard.read_bytes() == b"LIVE_READINESS_GUARD_SENTINEL\n"
        assert manifest["source_sha"] == json.loads(receipt.read_text())["source_sha"]
    finally:
        private_root.chmod(0o700)
        shutil.rmtree(private_root)


def test_prepare_rejects_unqualified_or_incompatible_hosted_receipt(tmp_path: Path) -> None:
    repo = _source_repo(tmp_path)
    artifact = tmp_path / "must-not-exist"
    unqualified = _receipt(repo, status="failed")
    with pytest.raises(recovery.RecoveryError, match="unqualified"):
        recovery.prepare(repo, artifact, unqualified)
    assert not artifact.exists()

    wrong_sha = _receipt(repo, source_sha="0" * 40)
    with pytest.raises(recovery.RecoveryError, match="another source"):
        recovery.prepare(repo, artifact, wrong_sha)
    assert not artifact.exists()

    wrong_digest = _receipt(repo, digest="f" * 64)
    with pytest.raises(recovery.RecoveryError, match="another source"):
        recovery.prepare(repo, artifact, wrong_digest)
    assert not artifact.exists()


def test_prepare_rejects_missing_component_installer_lock_helper(tmp_path: Path) -> None:
    repo = _source_repo(tmp_path)
    (repo / "installer/component_install_lock.py").unlink()
    _git(repo, "rm", "--cached", "installer/component_install_lock.py")
    _git(repo, "commit", "--quiet", "-m", "missing required installer lock helper")
    artifact = tmp_path / "must-not-exist"
    with pytest.raises(recovery.RecoveryError, match="missing backend/ or required backend installer inputs"):
        recovery._tracked_source_files(repo)
    assert not artifact.exists()


def test_verify_rejects_tampered_archive(tmp_path: Path) -> None:
    _, artifact, receipt = _artifact(tmp_path)
    artifact.chmod(0o700)
    archive = artifact / recovery.ARCHIVE_NAME
    archive.chmod(0o600)
    archive.write_bytes(archive.read_bytes() + b"tamper")
    with pytest.raises(recovery.RecoveryError, match="archive hash mismatch"):
        recovery.verify(artifact, receipt)


def test_prepare_excludes_ignored_environment_cache_and_backup_files(tmp_path: Path) -> None:
    repo = _source_repo(tmp_path)
    (repo / "backend/secret.env").write_text("IGNORED_SECRET=do-not-archive\n")
    cache = repo / "backend/fwrouter_api/__pycache__"
    cache.mkdir()
    (cache / "module.pyc").write_bytes(b"ignored bytecode")
    (repo / "backend/local.bak").write_bytes(b"ignored backup")
    receipt = _receipt(repo)
    artifact = tmp_path / "recovery-artifact"
    recovery.prepare(repo, artifact, receipt)
    manifest, entries = recovery.verify(artifact, receipt)
    names = {entry["path"] for entry in entries}
    assert not any("secret.env" in name or "__pycache__" in name or name.endswith(".bak") for name in names)
    assert manifest["source_sha"] == json.loads(receipt.read_text())["source_sha"]


def test_verify_rejects_traversal_member_even_when_archive_hash_is_rewritten(tmp_path: Path) -> None:
    _, artifact, receipt = _artifact(tmp_path)
    artifact.chmod(0o700)
    archive_path = artifact / recovery.ARCHIVE_NAME
    archive_path.chmod(0o600)
    malicious = tmp_path / "malicious.tar"
    with tarfile.open(malicious, "w") as archive:
        info = tarfile.TarInfo("../outside")
        info.size = 1
        archive.addfile(info, io.BytesIO(b"x"))
    archive_path.write_bytes(malicious.read_bytes())
    manifest_path = artifact / recovery.MANIFEST_NAME
    manifest_path.chmod(0o600)
    manifest = json.loads(manifest_path.read_text())
    manifest["archive_sha256"] = recovery._sha256(archive_path.read_bytes())
    manifest_path.write_text(json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n")
    with pytest.raises(recovery.RecoveryError, match="unsafe artifact path"):
        recovery.verify(artifact, receipt)


def test_interrupted_stage_retains_partial_target_and_can_be_repeated(tmp_path: Path) -> None:
    _, artifact, receipt = _artifact(tmp_path)
    private_root, target = _private_target("interrupt")
    original_run = subprocess.run

    def interrupt(*args, **kwargs):
        partial = target / "opt/fwrouter-api/partial-stage-marker"
        partial.parent.mkdir(parents=True, exist_ok=True)
        partial.write_text("inspect or restage\n")
        raise subprocess.TimeoutExpired(args[0], kwargs.get("timeout"))

    try:
        with patch.object(recovery.subprocess, "run", side_effect=interrupt):
            with pytest.raises(recovery.RecoveryError, match="partial target retained"):
                recovery.stage(artifact, receipt, target)
        assert (target / "opt/fwrouter-api/partial-stage-marker").read_text() == "inspect or restage\n"
        with patch.object(recovery.subprocess, "run", wraps=original_run):
            stage_receipt = recovery.stage(artifact, receipt, target)
        assert (target / "opt/fwrouter-api/partial-stage-marker").read_text() == "inspect or restage\n"
        assert (target / "opt/fwrouter-api/fwrouter_api/__init__.py").is_file()
        assert "partial-stage-marker" in stage_receipt["retained_extra_files"]
    finally:
        private_root.chmod(0o700)
        shutil.rmtree(private_root)


def test_traffic_collector_defers_until_fake_api_reports_ready(tmp_path: Path) -> None:
    """Adapt only curl's absolute path; use real jq and a private fake API."""
    script = REPO / "host/libexec/fwrouter/traffic-collect-api.sh"
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    calls = tmp_path / "curl-calls"
    mode = tmp_path / "mode"
    mode.write_text("unavailable")
    curl = bin_dir / "curl"
    curl.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$*\" >> {str(calls)!r}\n"
        f"case \"$(cat {str(mode)!r})\" in\n"
        "  unavailable) exit 7 ;;\n"
        "  unhealthy) printf '%s' '{\"ok\":true,\"data\":{\"status\":\"healthy\",\"database\":{\"status\":\"degraded\"}}}' ;;\n"
        "  malformed) printf '%s' '{not-json' ;;\n"
        "  ready) printf '%s' '{\"ok\":true,\"data\":{\"status\":\"healthy\",\"database\":{\"status\":\"healthy\"}}}' ;;\n"
        "esac\n"
        "case \"$*\" in\n"
        "  *'/api/v2/traffic/collect'*)\n"
        "    while [ $# -gt 0 ]; do if [ \"$1\" = -o ]; then shift; printf '%s' '{\"ok\":true,\"data\":{\"job\":{\"status\":\"success\"}}}' > \"$1\"; fi; shift; done ;;\n"
        "esac\n"
    )
    curl.chmod(0o755)
    source = script.read_text()
    assert source.count("/usr/bin/curl") == 2
    adapted = tmp_path / "traffic-collect-api.sh"
    adapted.write_text(source.replace("/usr/bin/curl", str(curl)))
    adapted.chmod(0o755)

    for status, reason in (("unavailable", "API_UNAVAILABLE"),
                           ("unhealthy", "API_NOT_READY"),
                           ("malformed", "API_NOT_READY")):
        calls.unlink(missing_ok=True)
        mode.write_text(status)
        deferred = subprocess.run([str(adapted)], text=True, capture_output=True, check=True)
        assert json.loads(deferred.stdout) == {
            "ok": False, "deferred": True, "reason": reason
        }
        requests = calls.read_text().splitlines()
        assert len(requests) == 1
        assert "/api/v2/health" in requests[0]
        assert all("/api/v2/traffic/collect" not in request for request in requests)

    calls.unlink()
    no_jq_path = tmp_path / "without-jq"
    no_jq_path.mkdir()
    for utility in ("cat", "mktemp", "rm"):
        (no_jq_path / utility).symlink_to(Path("/bin") / utility)
    missing_jq_env = {**os.environ, "PATH": str(no_jq_path)}
    missing_jq = subprocess.run(
        [str(adapted)], env=missing_jq_env, text=True, capture_output=True, check=True
    )
    assert json.loads(missing_jq.stdout) == {
        "ok": False, "deferred": True, "reason": "READINESS_CHECK_UNAVAILABLE"
    }
    assert not calls.exists()

    mode.write_text("ready")
    collected = subprocess.run([str(adapted)], text=True, capture_output=True, check=True)
    assert collected.returncode == 0
    requests = calls.read_text().splitlines()
    assert len(requests) == 2
    assert "/api/v2/health" in requests[0]
    assert "/api/v2/traffic/collect" in requests[1]


def test_host_component_installs_exact_readiness_guard_into_disposable_target(tmp_path: Path) -> None:
    source = REPO / "host/libexec/fwrouter/traffic-collect-api.sh"
    target = tmp_path / "target"
    result = subprocess.run(
        [str(REPO / "installer/install.sh"), "--deploy", "--component", "host",
         "--target", str(target)],
        text=True, capture_output=True, check=True,
    )
    installed = target / "usr/local/libexec/fwrouter/traffic-collect-api.sh"
    assert installed.read_bytes() == source.read_bytes()
    assert "host" in result.stdout

    repeated = subprocess.run(
        [str(REPO / "installer/install.sh"), "--deploy", "--component", "host",
         "--target", str(target)],
        text=True, capture_output=True, check=True,
    )
    assert installed.read_bytes() == source.read_bytes()
    assert "host" in repeated.stdout


def test_stage_refuses_to_report_complete_with_stale_executable_module(tmp_path: Path) -> None:
    _, artifact, receipt = _artifact(tmp_path)
    private_root, target = _private_target("stale-module")
    stale = target / "opt/fwrouter-api/removed_module.py"
    stale.parent.mkdir(parents=True)
    stale.write_text("OLD_EXECUTABLE_MODULE = True\n")
    try:
        with pytest.raises(recovery.RecoveryError, match="unqualified executable/code files"):
            recovery.stage(artifact, receipt, target)
        assert stale.exists()
        assert (target / "opt/fwrouter-api/fwrouter_api/__init__.py").is_file()
    finally:
        private_root.chmod(0o700)
        shutil.rmtree(private_root)


def test_stage_rejects_symlink_target_and_target_outside_private_temp_root(tmp_path: Path) -> None:
    _, artifact, receipt = _artifact(tmp_path)
    private_root, target = _private_target("path")
    outside = tmp_path / "outside"
    outside.mkdir()
    link = private_root / "link"
    link.symlink_to(target, target_is_directory=True)
    try:
        with pytest.raises(recovery.RecoveryError, match="non-symlink"):
            recovery.stage(artifact, receipt, link)
        with pytest.raises(recovery.RecoveryError, match="private temporary directory"):
            recovery.stage(artifact, receipt, outside)
    finally:
        private_root.chmod(0o700)
        shutil.rmtree(private_root)


def test_archived_generator_is_not_a_safe_mandatory_vpn_rollback_target() -> None:
    # Execute the exact historical pure generator and the actual current one.
    # This is component evidence, not native packet or live rollback evidence.
    from fwrouter_api.services.dataplane_nft_chains import _build_classify_chain_lines
    fixture = TOOL.parent / "tests/fixtures/unsafe_archived_classify.py"
    namespace = {}
    exec(compile(fixture.read_text(), str(fixture), "exec"), namespace)
    options = dict(mode="selective", selective_default="vpn",
                   selective_vpn_policy_ready=False, selective_degraded=True,
                   scoped_steering_rules=[])
    old_options = {**options}
    old_options["selective_vpn_ready"] = old_options.pop("selective_vpn_policy_ready")
    old = namespace["_build_classify_chain_lines"](**old_options)
    new = _build_classify_chain_lines(**options)
    assert old[-2] == '        goto fwrouter_direct comment "selective degraded default direct"'
    assert new[-2] == '        drop comment "selective default vpn"'
    assert '        goto fwrouter_direct comment "selective degraded default direct"' not in new
    assert any('protected IPv4 always direct' in line for line in new)
    direct = _build_classify_chain_lines(**{**options, "selective_default": "direct"})
    assert direct[-2] == old[-2]


def test_real_installer_failure_after_copy_reports_failure_and_allows_safe_restage(tmp_path: Path) -> None:
    # A real child installer copies code and then fails its owned bootstrap.
    # This exercises file-restore failure, not native or production activation.
    repo = _source_repo(tmp_path)
    bootstrap = repo / 'backend/scripts/bootstrap-state.sh'
    bootstrap.write_text('#!/bin/sh\nexit 42\n')
    _git(repo, 'add', 'backend/scripts/bootstrap-state.sh')
    _git(repo, 'commit', '--quiet', '-m', 'owned failing bootstrap')
    bad_receipt = _receipt(repo)
    bad_artifact = tmp_path / 'failing-artifact'
    recovery.prepare(repo, bad_artifact, bad_receipt)
    private, target = _private_target('real-failure')
    try:
        env = target / 'opt/fwrouter-api/.env'
        env.parent.mkdir(parents=True)
        env.write_bytes(b'TEST_ONLY_CANARY=keep\n')
        with pytest.raises(recovery.RecoveryError, match='failed with status 42'):
            recovery.stage(bad_artifact, bad_receipt, target)
        assert (target / 'opt/fwrouter-api/fwrouter_api/__init__.py').is_file()
        assert env.read_bytes() == b'TEST_ONLY_CANARY=keep\n'
        bootstrap.write_text('#!/bin/sh\nset -eu\nmkdir -p "$1" "$2" "$3"\n')
        _git(repo, 'add', 'backend/scripts/bootstrap-state.sh')
        _git(repo, 'commit', '--quiet', '-m', 'owned recovered bootstrap')
        good_receipt = _receipt(repo)
        good_artifact = tmp_path / 'restage-artifact'
        recovery.prepare(repo, good_artifact, good_receipt)
        staged = recovery.stage(good_artifact, good_receipt, target)
        assert staged['installer_returncode'] == 0
        assert staged['runtime_qualification'] == 'not_established'
        assert env.read_bytes() == b'TEST_ONLY_CANARY=keep\n'
    finally:
        shutil.rmtree(private)


def test_stage_rejects_caller_temp_override_outside_canonical_tmp(tmp_path: Path) -> None:
    _, artifact, receipt = _artifact(tmp_path)
    private, target = _private_target("temp-override")
    try:
        with patch.object(recovery.tempfile, "gettempdir", return_value="/opt"):
            with pytest.raises(recovery.RecoveryError, match="canonical /tmp"):
                recovery.stage(artifact, receipt, target)
        assert not (target / "opt/fwrouter-api").exists()
    finally:
        shutil.rmtree(private)


def test_verify_rejects_compressed_tar_even_with_matching_archive_hash(tmp_path: Path) -> None:
    import gzip
    _, artifact, receipt = _artifact(tmp_path)
    artifact.chmod(0o700)
    archive = artifact / recovery.ARCHIVE_NAME
    archive.chmod(0o600)
    archive.write_bytes(gzip.compress(archive.read_bytes()))
    manifest_path = artifact / recovery.MANIFEST_NAME
    manifest_path.chmod(0o600)
    manifest = json.loads(manifest_path.read_text())
    manifest["archive_sha256"] = recovery._sha256(archive.read_bytes())
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(recovery.RecoveryError, match="archive is unreadable"):
        recovery.verify(artifact, receipt)
