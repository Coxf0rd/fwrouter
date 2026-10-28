from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "installer" / "host_publication.py"
spec = importlib.util.spec_from_file_location("host_publication_under_test", MODULE_PATH)
publisher = importlib.util.module_from_spec(spec)
assert spec.loader
spec.loader.exec_module(publisher)


def _tree(tmp_path: Path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    for entry in publisher.FILES:
        source_path = source / entry["source"]
        source_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / entry["source"], source_path)
        source_path.chmod(entry["mode"])
        target_path = target / entry["target"]
        target_path.parent.mkdir(parents=True, exist_ok=True)
    return source, target


def _plan(source: Path, target: Path, *, old: dict[str, str | None] | None = None, entries=None):
    rows = []
    old = old or {}
    for entry in entries if entries is not None else publisher.FILES:
        source_path = source / entry["source"]
        rows.append({
            "source": entry["source"],
            "target": entry["target"],
            "expected_old_sha256": old.get(entry["target"]),
            "source_sha256": hashlib.sha256(source_path.read_bytes()).hexdigest(),
            "mode": entry["mode"],
        })
    return {"schema": publisher.SCHEMA, "files": rows}


def _write_plan(path: Path, value: dict):
    path.write_text(json.dumps(value), encoding="utf-8")


def test_publish_cas_modes_and_no_unrelated_files(tmp_path):
    source, target = _tree(tmp_path)
    sentinel = target / "etc/systemd/system/operator.service"
    sentinel.write_text("keep", encoding="utf-8")
    prior_service = target / publisher.FILES[1]["target"]
    prior_service.write_bytes(b"old unit contents")
    old = {publisher.FILES[1]["target"]: hashlib.sha256(b"old unit contents").hexdigest()}
    plan = tmp_path / "plan.json"
    _write_plan(plan, _plan(source, target, old=old))

    result = publisher.publish(source, target, plan)

    assert [row["result"] for row in result] == ["published", "published"]
    for entry in publisher.FILES:
        installed = target / entry["target"]
        assert installed.read_bytes() == (source / entry["source"]).read_bytes()
        assert installed.stat().st_mode & 0o777 == entry["mode"]
    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert not (target / "etc/sysctl.d").exists()
    assert not (target / "usr/local/sbin").exists()


def test_stale_digest_and_target_symlink_fail_without_overwrite(tmp_path):
    source, target = _tree(tmp_path)
    target_file = target / publisher.FILES[0]["target"]
    target_file.write_text("operator edit", encoding="utf-8")
    stale_plan = tmp_path / "stale.json"
    _write_plan(stale_plan, _plan(source, target))
    with pytest.raises(publisher.PublicationError, match="compare_and_swap"):
        publisher.publish(source, target, stale_plan)
    assert target_file.read_text(encoding="utf-8") == "operator edit"

    outside = tmp_path / "outside"
    outside.write_text("do not follow", encoding="utf-8")
    target_file.unlink()
    target_file.symlink_to(outside)
    with pytest.raises(publisher.PublicationError, match="single_link_regular"):
        publisher.publish(source, target, stale_plan)
    assert outside.read_text(encoding="utf-8") == "do not follow"


def test_all_destinations_preflight_before_first_publication(tmp_path):
    source, target = _tree(tmp_path)
    second = target / publisher.FILES[1]["target"]
    second.write_text("stale unit", encoding="utf-8")
    plan = tmp_path / "stale-second.json"
    _write_plan(plan, _plan(source, target))
    with pytest.raises(publisher.PublicationError, match="compare_and_swap"):
        publisher.publish(source, target, plan)
    assert not (target / publisher.FILES[0]["target"]).exists()
    assert second.read_text(encoding="utf-8") == "stale unit"

    second.unlink()
    second.parent.chmod(0o770)
    with pytest.raises(publisher.PublicationError, match="ancestor_directory_unsafe"):
        publisher.publish(source, target, plan)
    assert not (target / publisher.FILES[0]["target"]).exists()


def test_destination_ancestor_symlink_is_rejected(tmp_path):
    source, target = _tree(tmp_path)
    outside = tmp_path / "outside-tree"
    outside.mkdir()
    shutil.move(target / "usr/local/libexec/fwrouter", tmp_path / "saved-fwrouter")
    (target / "usr/local/libexec").rmdir()
    (target / "usr/local/libexec").symlink_to(outside, target_is_directory=True)
    plan = tmp_path / "plan.json"
    _write_plan(plan, _plan(source, target))
    with pytest.raises(publisher.PublicationError, match="ancestor_directory_unsafe"):
        publisher.publish(source, target, plan)
    assert list(outside.iterdir()) == []


def test_plan_symlink_and_wrong_fixed_mode_are_rejected(tmp_path):
    source, target = _tree(tmp_path)
    real_plan = tmp_path / "real-plan.json"
    _write_plan(real_plan, _plan(source, target))
    linked_plan = tmp_path / "linked-plan.json"
    linked_plan.symlink_to(real_plan)
    with pytest.raises(publisher.PublicationError, match="plan_unavailable_or_symlink"):
        publisher.publish(source, target, linked_plan)
    value = _plan(source, target)
    value["files"][0]["mode"] = 0o644
    with pytest.raises(publisher.PublicationError, match="plan_mode_invalid"):
        publisher.validate_plan(value)


def test_plan_accepts_nonempty_allowlist_subsets_in_canonical_order(tmp_path):
    source, target = _tree(tmp_path)
    for entry in publisher.FILES:
        plan = tmp_path / f"{entry['target'].replace('/', '-')}.json"
        _write_plan(plan, _plan(source, target, entries=[entry]))
        result = publisher.publish(source, target, plan)
        assert result == [{"target": entry["target"], "result": "published"}]
    value = _plan(source, target)
    value["files"].reverse()
    with pytest.raises(publisher.PublicationError, match="order_or_duplicate"):
        publisher.validate_plan(value)
    value = _plan(source, target)
    value["files"] = [value["files"][0], value["files"][0]]
    with pytest.raises(publisher.PublicationError, match="order_or_duplicate"):
        publisher.validate_plan(value)


def test_same_digest_mode_correction_is_cas_and_idempotent(tmp_path):
    source, target = _tree(tmp_path)
    entry = publisher.FILES[0]
    target_file = target / entry["target"]
    source_bytes = (source / entry["source"]).read_bytes()
    target_file.write_bytes(source_bytes)
    target_file.chmod(0o750)
    digest = hashlib.sha256(source_bytes).hexdigest()
    plan = tmp_path / "mode-correction.json"
    _write_plan(plan, _plan(source, target, old={entry["target"]: digest}, entries=[entry]))

    result = publisher.publish(source, target, plan)
    assert result == [{"target": entry["target"], "result": "published"}]
    assert target_file.stat().st_mode & 0o777 == entry["mode"]
    assert publisher.publish(source, target, plan) == [
        {"target": entry["target"], "result": "already_desired"}]

    target_file.chmod(0o664)
    with pytest.raises(publisher.PublicationError, match="single_link_regular"):
        publisher.publish(source, target, plan)


def test_plan_permissions_and_writable_absolute_ancestor_are_rejected(tmp_path):
    source, target = _tree(tmp_path)
    plan = tmp_path / "plan.json"
    _write_plan(plan, _plan(source, target))
    plan.chmod(0o660)
    with pytest.raises(publisher.PublicationError, match="plan_type_or_size_invalid"):
        publisher.publish(source, target, plan)

    plan.chmod(0o600)
    target.chmod(0o770)
    with pytest.raises(publisher.PublicationError, match="absolute_ancestor_permissions_unsafe"):
        publisher.publish(source, target, plan)


def test_duplicate_json_keys_and_non_allowlisted_path_rejected(tmp_path):
    source, target = _tree(tmp_path)
    plan = tmp_path / "plan.json"
    plan.write_text('{"schema":"x","schema":"y"}', encoding="utf-8")
    with pytest.raises(publisher.PublicationError, match="duplicate_json_key"):
        publisher.publish(source, target, plan)
    value = _plan(source, target)
    value["files"][0]["target"] = "etc/other.conf"
    with pytest.raises(publisher.PublicationError, match="path_not_allowlisted"):
        publisher.validate_plan(value)


def test_source_symlink_ancestor_is_rejected(tmp_path):
    source, target = _tree(tmp_path)
    real = tmp_path / "real-libexec"
    shutil.move(source / "host/libexec/fwrouter", real)
    (source / "host/libexec").rmdir()
    (source / "host/libexec").symlink_to(tmp_path, target_is_directory=True)
    plan = tmp_path / "plan.json"
    _write_plan(plan, _plan_from_repo(source))
    with pytest.raises(publisher.PublicationError, match="ancestor_directory_unsafe"):
        publisher.publish(source, target, plan)


def _plan_from_repo(source: Path):
    rows = []
    for entry in publisher.FILES:
        source_path = ROOT / entry["source"]
        rows.append({"source": entry["source"], "target": entry["target"],
                     "expected_old_sha256": None,
                     "source_sha256": hashlib.sha256(source_path.read_bytes()).hexdigest(),
                     "mode": entry["mode"]})
    return {"schema": publisher.SCHEMA, "files": rows}


def test_partial_publication_retries_same_exact_plan(tmp_path, monkeypatch):
    source, target = _tree(tmp_path)
    plan = tmp_path / "plan.json"
    value = _plan(source, target)
    _write_plan(plan, value)
    original_replace = publisher.os.replace
    replace_calls = 0

    def fail_second_replace(*args, **kwargs):
        nonlocal replace_calls
        replace_calls += 1
        if replace_calls == 2:
            raise OSError("simulated interruption before second file publication")
        return original_replace(*args, **kwargs)

    monkeypatch.setattr(publisher.os, "replace", fail_second_replace)
    with pytest.raises(publisher.PublicationError, match="publication_io_failed"):
        publisher.publish(source, target, plan)
    first = target / publisher.FILES[0]["target"]
    second = target / publisher.FILES[1]["target"]
    assert first.read_bytes() == (source / publisher.FILES[0]["source"]).read_bytes()
    assert not second.exists()

    monkeypatch.setattr(publisher.os, "replace", original_replace)
    retried = publisher.publish(source, target, plan)
    assert [row["result"] for row in retried] == ["already_desired", "published"]
    assert second.read_bytes() == (source / publisher.FILES[1]["source"]).read_bytes()


def test_installer_selective_mode_uses_synthetic_target_and_rejects_bad_scope(tmp_path):
    source, target = _tree(tmp_path)
    plan = tmp_path / "plan.json"
    _write_plan(plan, _plan(source, target))
    env = {**os.environ, "FWROUTER_INSTALL_HOST_DEPS": "0",
           "FWROUTER_SETUP_PYTHON_ENV": "0", "FWROUTER_ENABLE_UNITS": "0",
           "FWROUTER_APPLY_SYSCTL": "0"}
    direct = subprocess.run(
        [sys.executable, str(MODULE_PATH), "--source-root", str(ROOT), "--target-root", str(target),
         "--plan", str(plan), "--component-lock-fd", "9"],
        cwd=ROOT, env=env, text=True, capture_output=True, check=False)
    assert direct.returncode != 0
    assert not (target / publisher.FILES[0]["target"]).exists()

    command = ["sh", str(ROOT / "installer/install.sh"), "--deploy", "--component", "host",
               "--host-plan", str(plan), "--target", str(target)]
    completed = subprocess.run(command, cwd=ROOT, env=env, text=True, capture_output=True, check=False)
    assert completed.returncode == 0, completed.stderr
    for entry in publisher.FILES:
        installed = target / entry["target"]
        assert installed.read_bytes() == (ROOT / entry["source"]).read_bytes()
        assert installed.stat().st_mode & 0o777 == entry["mode"]
    assert not (target / "etc/systemd/system/fwrouter-maintenance.timer").exists()
    assert not (target / "etc/systemd/system/fwrouter-mihomo.service").exists()
    assert not (target / "etc/sysctl.d").exists()
    assert not (target / "opt/fwrouter-api").exists()
    assert not (target / "var/lib/fwrouter-v2").exists()

    bad_scope = subprocess.run(
        ["sh", str(ROOT / "installer/install.sh"), "--deploy", "--all", "--host-plan", str(plan),
         "--target", str(target)], cwd=ROOT, env=env, text=True, capture_output=True, check=False)
    assert bad_scope.returncode != 0
    assert "Installed FWRouter components" not in bad_scope.stdout

    empty_plan = subprocess.run(
        ["sh", str(ROOT / "installer/install.sh"), "--deploy", "--component", "host",
         "--host-plan", "", "--target", str(target)],
        cwd=ROOT, env=env, text=True, capture_output=True, check=False)
    assert empty_plan.returncode != 0


@pytest.mark.parametrize("boundary", ["before_replace", "after_replace"])
def test_fsync_failure_is_not_success_and_exact_retry_completes(tmp_path, monkeypatch, boundary):
    source, target = _tree(tmp_path)
    plan = tmp_path / "plan.json"
    _write_plan(plan, _plan(source, target))
    original_fsync = publisher.os.fsync
    calls = 0

    def fail_selected_fsync(fd):
        nonlocal calls
        calls += 1
        if calls == (1 if boundary == "before_replace" else 2):
            raise OSError("synthetic fsync failure")
        return original_fsync(fd)

    monkeypatch.setattr(publisher.os, "fsync", fail_selected_fsync)
    with pytest.raises(publisher.PublicationError, match="publication_io_failed"):
        publisher.publish(source, target, plan)
    assert not (target / publisher.FILES[1]["target"]).exists()
    assert not list(target.rglob(".fwrouter-publish-*.tmp"))
    first = target / publisher.FILES[0]["target"]
    assert first.exists() is (boundary == "after_replace")
    monkeypatch.setattr(publisher.os, "fsync", original_fsync)
    result = publisher.publish(source, target, plan)
    assert result[0]["result"] == ("already_desired" if first.exists() and boundary == "after_replace" else "published")
    assert result[1]["result"] == "published"


def test_real_process_death_after_first_publication_restarts_forward(tmp_path):
    source, target = _tree(tmp_path)
    plan = tmp_path / "plan.json"
    _write_plan(plan, _plan(source, target))
    runner = tmp_path / "crash.py"
    runner.write_text(
        "import importlib.util, os, signal, sys\nfrom pathlib import Path\n"
        "spec=importlib.util.spec_from_file_location('publisher', sys.argv[1])\n"
        "p=importlib.util.module_from_spec(spec)\nspec.loader.exec_module(p)\n"
        "original=p._publish_one\n"
        "def publish_then_die(*args):\n"
        " result=original(*args)\n os.kill(os.getpid(), signal.SIGKILL)\n return result\n"
        "p._publish_one=publish_then_die\n"
        "p.publish(Path(sys.argv[2]), Path(sys.argv[3]), Path(sys.argv[4]))\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, "-B", str(runner), str(MODULE_PATH), str(source), str(target), str(plan)],
        capture_output=True, check=False, timeout=10,
    )
    assert result.returncode == -9
    assert (target / publisher.FILES[0]["target"]).read_bytes() == (source / publisher.FILES[0]["source"]).read_bytes()
    assert not (target / publisher.FILES[1]["target"]).exists()
    result = publisher.publish(source, target, plan)
    assert [row["result"] for row in result] == ["already_desired", "published"]
