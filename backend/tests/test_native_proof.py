from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
import sys
import subprocess
from pathlib import PurePosixPath
from types import SimpleNamespace

import pytest

from fwrouter_api.services import native_proof


REPO_ROOT = Path(__file__).resolve().parents[2]
XRAY_IMAGE_PIN = "sha256:" + "1" * 64
MIHOMO_IMAGE_PIN = "sha256:" + "6" * 64


def _fake_root_owned_sysctl_tree(monkeypatch: pytest.MonkeyPatch) -> None:
    """Model procfs ownership while exercising the reader on a test-owned tmp tree."""
    original_lstat = Path.lstat
    original_fstat = os.fstat

    def root_owned_lstat(path: Path):
        observed = original_lstat(path)
        return SimpleNamespace(st_mode=observed.st_mode, st_uid=0)

    def root_owned_fstat(descriptor: int):
        observed = original_fstat(descriptor)
        return SimpleNamespace(st_mode=observed.st_mode, st_uid=0, st_dev=observed.st_dev,
                               st_ino=observed.st_ino, st_mtime_ns=observed.st_mtime_ns)

    monkeypatch.setattr(Path, "lstat", root_owned_lstat)
    monkeypatch.setattr(os, "fstat", root_owned_fstat)


def _installer_manifest_module():
    path = REPO_ROOT / "installer" / "controlled_backend_lifecycle.py"
    spec = importlib.util.spec_from_file_location("controlled_backend_lifecycle_for_manifest_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_backend_manifest_matches_rollout_owner_and_exclusions(tmp_path: Path) -> None:
    root = tmp_path / "backend"
    root.mkdir()
    (root / "package.py").write_text("important", encoding="utf-8")
    (root / "tool").write_text("binary", encoding="utf-8")
    (root / "tool").chmod(0o755)

    lifecycle = _installer_manifest_module()
    expected = lifecycle._manifest_digest(lifecycle._backend_manifest(root))

    assert native_proof.backend_manifest(root) == lifecycle._backend_manifest(root)
    assert native_proof.backend_manifest_sha256(root) == expected
    excluded_names = (".env", ".env.production", ".venv/module.py", ".pytest_cache/state", "__pycache__/module.py", "cache.db")
    for name in excluded_names:
        relative = Path(name).as_posix()
        assert native_proof._excluded_backend_path(PurePosixPath(relative))
        assert lifecycle._excluded(PurePosixPath(relative))


def test_backend_manifest_rejects_symlink_directory(tmp_path: Path) -> None:
    root = tmp_path / "backend"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "module.py").write_text("x", encoding="utf-8")
    (root / "linked").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="BACKEND_SYMLINK_DIRECTORY"):
        native_proof.backend_manifest(root)


def test_core_applied_manifest_reader_accepts_bounded_streamed_artifact_over_16_mib(tmp_path: Path) -> None:
    path = tmp_path / "applied-manifest.json"
    prefix = b'{"padding":"'
    suffix = b'"}'
    payload = prefix + b"x" * (16 * 1024 * 1024 + 17) + suffix
    path.write_bytes(payload)

    manifest, digest = native_proof._read_core_applied_manifest(path)

    assert len(manifest["padding"]) == 16 * 1024 * 1024 + 17
    assert digest == __import__("hashlib").sha256(payload).hexdigest()
    assert native_proof._hash_core_applied_manifest(path) == digest


def test_core_applied_manifest_reader_rejects_oversized_and_non_object_artifacts(tmp_path: Path) -> None:
    oversized = tmp_path / "oversized-manifest.json"
    with oversized.open("wb") as stream:
        stream.truncate(native_proof._MAX_CORE_APPLIED_MANIFEST_BYTES + 1)
    with pytest.raises(ValueError):
        native_proof._read_core_applied_manifest(oversized)

    malformed = tmp_path / "non-object-manifest.json"
    malformed.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="CORE_NATIVE_READBACK_INVALID"):
        native_proof._read_core_applied_manifest(malformed)


class _SyntheticBoundedProcess:
    """Pipe-backed Popen double; routine tests never execute child processes."""

    def __init__(self, stdout_bytes: bytes, stderr_bytes: bytes = b"", returncode: int = 0,
                 *, hold_open: bool = False) -> None:
        import threading

        stdout_read, stdout_write = os.pipe()
        stderr_read, stderr_write = os.pipe()
        self.stdout = os.fdopen(stdout_read, "rb", buffering=0)
        self.stderr = os.fdopen(stderr_read, "rb", buffering=0)
        self._writers = [stdout_write, stderr_write]
        self._returncode = None if hold_open else returncode
        self.killed = False

        def write_payload() -> None:
            try:
                for fd, payload in ((stdout_write, stdout_bytes), (stderr_write, stderr_bytes)):
                    view = memoryview(payload)
                    while view:
                        written = os.write(fd, view[:64 * 1024])
                        view = view[written:]
            except OSError:
                pass
            finally:
                for fd in (stdout_write, stderr_write):
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                self._writers.clear()

        self._thread = None
        if not hold_open:
            self._thread = threading.Thread(target=write_payload, daemon=True)
            self._thread.start()

    def poll(self) -> int | None:
        return self._returncode

    def wait(self, timeout: float | None = None) -> int:
        if self._returncode is None:
            raise subprocess.TimeoutExpired("synthetic-ip", timeout)
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        return self._returncode

    def kill(self) -> None:
        self.killed = True
        self._returncode = -9
        for fd in self._writers:
            try:
                os.close(fd)
            except OSError:
                pass
        self._writers.clear()


@pytest.mark.parametrize("stream,limit", [("stdout", 4 * 1024 * 1024), ("stderr", 64 * 1024)])
def test_bounded_ip_json_reader_bounds_both_pipes_and_reaps(
    monkeypatch: pytest.MonkeyPatch, stream: str, limit: int,
) -> None:
    process = _SyntheticBoundedProcess(
        b"x" * (limit + 1) if stream == "stdout" else b"[]",
        b"x" * (limit + 1) if stream == "stderr" else b"",
    )
    monkeypatch.setattr(native_proof.subprocess, "Popen", lambda *_args, **_kwargs: process)
    with pytest.raises(ValueError, match="CORE_IP_READBACK_INVALID"):
        native_proof._run_bounded_ip_json(["ip", "-j"])
    assert process.killed and process.poll() is not None


def test_bounded_ip_json_reader_timeout_kills_and_reaps(monkeypatch: pytest.MonkeyPatch) -> None:
    process = _SyntheticBoundedProcess(b"", hold_open=True)
    monkeypatch.setattr(native_proof, "_IP_COMMAND_TIMEOUT_SECONDS", 0.02)
    monkeypatch.setattr(native_proof.subprocess, "Popen", lambda *_args, **_kwargs: process)
    with pytest.raises(ValueError, match="CORE_IP_READBACK_UNAVAILABLE"):
        native_proof._run_bounded_ip_json(["ip", "-j"])
    assert process.killed and process.poll() is not None


def test_bounded_ip_json_reader_requires_zero_exit_and_valid_json(monkeypatch: pytest.MonkeyPatch) -> None:
    for stdout, stderr, returncode, reason in (
        (b"[]", b"", 7, "CORE_IP_READBACK_UNAVAILABLE"),
        (b"not-json", b"", 0, "CORE_IP_READBACK_INVALID"),
    ):
        process = _SyntheticBoundedProcess(stdout, stderr, returncode)
        monkeypatch.setattr(native_proof.subprocess, "Popen", lambda *_args, **_kwargs: process)
        with pytest.raises(ValueError, match=reason):
            native_proof._run_bounded_ip_json(["ip", "-j"])
    process = _SyntheticBoundedProcess(b"[1]")
    monkeypatch.setattr(native_proof.subprocess, "Popen", lambda *_args, **_kwargs: process)
    assert native_proof._run_bounded_ip_json(["ip", "-j"]) == [1]


def test_core_apply_receipt_hashes_large_manifest_and_rejects_cross_fence_replacement(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    candidate = tmp_path / "candidate.nft"
    applied = tmp_path / "applied.nft"
    manifest_path = tmp_path / "applied-manifest.json"
    candidate.write_text("table inet fwrouter_v2 {}\n", encoding="utf-8")
    applied.write_text(candidate.read_text(encoding="utf-8"), encoding="utf-8")
    original = b'{"plan_id":"fixture-apply","padding":"' + b"x" * (16 * 1024 * 1024 + 17) + b'"}'
    manifest_path.write_bytes(original)
    proof_path = tmp_path / "receipt.json"
    paths = {"candidate_nft_path": str(candidate), "applied_nft_path": str(applied),
             "applied_manifest_path": str(manifest_path)}
    monkeypatch.setattr(native_proof, "core_native_proof_path", lambda: proof_path)
    monkeypatch.setattr(native_proof, "read_live_nft_semantic_sha256", lambda: "a" * 64)
    monkeypatch.setattr(native_proof, "_core_ip_facts", lambda: {"semantic_sha256": "b" * 64})

    assert native_proof.write_core_native_apply_receipt(
        manifest={"plan_id": "fixture-apply"}, artifact_paths=paths, core_intent_revision=12,
    ) is True
    stored = json.loads(proof_path.read_text(encoding="utf-8"))
    assert stored["applied_manifest_sha256"] == __import__("hashlib").sha256(original).hexdigest()

    proof_path.unlink()
    replacement = original.replace(b"fixture-apply", b"fixture-change")
    calls = 0

    def replace_during_native_capture() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            manifest_path.write_bytes(replacement)
        return "a" * 64

    monkeypatch.setattr(native_proof, "read_live_nft_semantic_sha256", replace_during_native_capture)
    assert native_proof.write_core_native_apply_receipt(
        manifest={"plan_id": "fixture-apply"}, artifact_paths=paths, core_intent_revision=12,
    ) is False
    assert not proof_path.exists()

    with manifest_path.open("wb") as stream:
        stream.truncate(native_proof._MAX_CORE_APPLIED_MANIFEST_BYTES + 1)
    assert native_proof.write_core_native_apply_receipt(
        manifest={"plan_id": "fixture-apply"}, artifact_paths=paths, core_intent_revision=12,
    ) is False
    assert not proof_path.exists()


def test_core_readback_binds_large_manifest_bytes_and_rejects_cross_fence_replacement(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from fwrouter_api.services import dataplane_global, dataplane_live, dataplane_status

    generated = tmp_path / "generated"
    dataplane = generated / "dataplane"
    dataplane.mkdir(parents=True)
    manifest_path = dataplane / "applied-manifest.json"
    nft_path = dataplane / "applied.nft"
    padding = "x" * (16 * 1024 * 1024 + 17)
    routing = {"desired_mode": "direct", "applied_mode": "direct", "selective_default": "direct",
               "server_mode": "auto", "desired_fixed_server_id": None, "applied_fixed_server_id": None}

    def write_manifest(marker: str) -> bytes:
        raw = json.dumps({"plan_id": "fixture-apply", "routing_global_state": routing,
                          "padding": marker + padding[1:]}, sort_keys=True).encode()
        manifest_path.write_bytes(raw)
        return raw

    applied_bytes = write_manifest("x")
    nft_path.write_text("table inet fwrouter_v2 {}\n", encoding="utf-8")
    receipt_path = dataplane / native_proof._CORE_NATIVE_PROOF_NAME
    monkeypatch.setattr(dataplane_global, "get_settings", lambda: SimpleNamespace(
        paths=SimpleNamespace(generated_dir=generated)))
    monkeypatch.setattr("fwrouter_api.services.core_intent.read_core_runtime_context", lambda: {"context": 1})
    monkeypatch.setattr(dataplane_status, "_read_live_dataplane_payload", lambda: {"required_chains": {"input": True}})
    monkeypatch.setattr(dataplane_status, "_live_owned_table_ready", lambda _payload: True)
    monkeypatch.setattr(dataplane_live, "_probe_live_global_mode_uncached", lambda: {"ok": True, "mode": "direct"})
    ip_facts = {"semantic_sha256": "b" * 64, "checks": {"ipv6": True}}
    monkeypatch.setattr(native_proof, "_core_ip_facts", lambda: ip_facts)
    monkeypatch.setattr(native_proof, "read_live_nft_semantic_sha256", lambda: "a" * 64)
    receipt = {
        "schema": 1, "apply_id": "fixture-apply", "core_intent_revision": 5,
        "applied_manifest_sha256": __import__("hashlib").sha256(applied_bytes).hexdigest(),
        "candidate_nft_sha256": native_proof._hash_regular_file(nft_path),
        "native_semantics_sha256": "a" * 64, "native_ip_semantics_sha256": "b" * 64,
        "normalization": "nft-table-v1-counter-values-and-dns-timeout-elements-only",
    }
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    monkeypatch.setattr(native_proof, "core_native_proof_path", lambda: receipt_path)
    db = {"latest_apply": {"apply_id": "fixture-apply", "status": "applied", "core_intent_revision": 5},
          "core_revision": 5, "routing": {**routing, "apply_state": "clean"}, "selection": {"revision": 7}}

    result = native_proof._core_readback(db)
    assert result["applied_manifest_stable"] is True
    assert result["applied_manifest_sha256"] == __import__("hashlib").sha256(applied_bytes).hexdigest()

    applied_bytes = write_manifest("y")
    receipt["applied_manifest_sha256"] = __import__("hashlib").sha256(applied_bytes).hexdigest()
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    changed = False

    def mutate_during_readback() -> str:
        nonlocal changed
        if not changed:
            changed = True
            write_manifest("z")
        return "a" * 64

    monkeypatch.setattr(native_proof, "read_live_nft_semantic_sha256", mutate_during_readback)
    with pytest.raises(RuntimeError, match="CORE_RUNTIME_CONTEXT_CHANGED"):
        native_proof._core_readback(db)


def test_core_readback_only_allows_explicit_exact_receipt_pin_for_stale_ip_binding(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from fwrouter_api.services import dataplane_global, dataplane_live, dataplane_status
    import hashlib

    generated = tmp_path / "generated"
    dataplane = generated / "dataplane"
    dataplane.mkdir(parents=True)
    manifest_path = dataplane / "applied-manifest.json"
    nft_path = dataplane / "applied.nft"
    receipt_path = dataplane / native_proof._CORE_NATIVE_PROOF_NAME
    routing = {"desired_mode": "direct", "applied_mode": "direct", "selective_default": "direct",
               "server_mode": "auto", "desired_fixed_server_id": None, "applied_fixed_server_id": None}
    manifest = {"plan_id": "apply-1", "routing_global_state": routing}
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    nft_path.write_text("owned nft", encoding="utf-8")
    receipt = {
        "schema": 1, "apply_id": "apply-1", "core_intent_revision": 3,
        "candidate_nft_sha256": native_proof._hash_regular_file(nft_path),
        "applied_manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "native_semantics_sha256": "a" * 64, "native_ip_semantics_sha256": "b" * 64,
        "normalization": "nft-table-v1-counter-values-and-dns-timeout-elements-only",
    }
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    receipt_sha = native_proof._hash_regular_file(receipt_path)
    monkeypatch.setattr(native_proof, "core_native_proof_path", lambda: receipt_path)
    monkeypatch.setattr(dataplane_global, "get_settings", lambda: SimpleNamespace(
        paths=SimpleNamespace(generated_dir=generated)))
    monkeypatch.setattr("fwrouter_api.services.core_intent.read_core_runtime_context", lambda: {"context": 1})
    monkeypatch.setattr(dataplane_status, "_read_live_dataplane_payload", lambda: {"required_chains": {"input": True}})
    monkeypatch.setattr(dataplane_status, "_live_owned_table_ready", lambda _payload: True)
    monkeypatch.setattr(dataplane_live, "_probe_live_global_mode_uncached", lambda: {"ok": True, "mode": "direct"})
    monkeypatch.setattr(native_proof, "_routing_manifest_matches", lambda *_args: True)
    monkeypatch.setattr(native_proof, "_core_ip_facts", lambda: {"semantic_sha256": "c" * 64, "checks": {"required": True}})
    monkeypatch.setattr(native_proof, "read_live_nft_semantic_sha256", lambda: "a" * 64)
    db = {"latest_apply": {"apply_id": "apply-1", "status": "applied", "core_intent_revision": 3},
          "core_revision": 3, "routing": {**routing, "apply_state": "clean"}, "selection": {"revision": 9}}

    with pytest.raises(ValueError, match="CORE_IP_POLICY_MISMATCH"):
        native_proof._core_readback(db)
    proof = native_proof._core_readback(db, _expected_stale_receipt_sha256=receipt_sha)
    assert proof["native_ip_semantics_sha256"] == "c" * 64

    # The narrow exception does not bypass nft owner validation or accept a
    # different receipt byte sequence.
    monkeypatch.setattr(native_proof, "read_live_nft_semantic_sha256", lambda: "d" * 64)
    with pytest.raises(ValueError, match="CORE_NATIVE_RULES_MISMATCH"):
        native_proof._core_readback(db, _expected_stale_receipt_sha256=receipt_sha)


def test_core_ip_receipt_reattest_is_plan_bound_and_changes_only_ip_digest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from contextlib import nullcontext
    import hashlib

    receipt_path = tmp_path / "applied-native-proof.json"
    old = {
        "schema": 1, "apply_id": "apply-1", "core_intent_revision": 3,
        "candidate_nft_sha256": "a" * 64, "applied_manifest_sha256": "b" * 64,
        "native_semantics_sha256": "c" * 64, "native_ip_semantics_sha256": "d" * 64,
        "normalization": "nft-table-v1-counter-values-and-dns-timeout-elements-only",
    }
    receipt_path.write_text(json.dumps(old), encoding="utf-8")
    monkeypatch.setattr(native_proof, "core_native_proof_path", lambda: receipt_path)
    monkeypatch.setattr("fwrouter_api.adapters.xray_common.xray_writer_guard",
                        lambda **_kwargs: nullcontext())
    facts = {
        "tool_manifest_sha256": "1" * 64, "installed_backend_manifest_sha256": "2" * 64,
        "checkpoint_sha256": "3" * 64, "operation_id": "4" * 32,
        "source_fingerprint_sha256": "5" * 64, "generation_digest": "6" * 64,
        "generation_revision": 61, "selection_revision": 9, "core_intent_revision": 3,
        "latest_apply_id_sha256": "7" * 64, "database_fence_sha256": "8" * 64,
        "receipt_sha256": hashlib.sha256(receipt_path.read_bytes()).hexdigest(),
        "old_ip_semantics_sha256": "d" * 64, "current_ip_semantics_sha256": "e" * 64,
        "core_native_semantics_sha256": "c" * 64, "applied_manifest_sha256": "b" * 64,
        "applied_nft_sha256": "a" * 64,
    }
    core = {"native_ip_semantics_sha256": "e" * 64, "native_rule_semantics_sha256": "c" * 64}
    sequence = iter([
        (facts, old, core, "3" * 64),
        ({**facts, "receipt_sha256": hashlib.sha256(
            (json.dumps({**old, "native_ip_semantics_sha256": "e" * 64}, ensure_ascii=True,
                        indent=2, sort_keys=True) + "\n").encode()).hexdigest()},
         {**old, "native_ip_semantics_sha256": "e" * 64}, core, "3" * 64),
    ])
    monkeypatch.setattr(native_proof, "_core_ip_receipt_reattest_facts", lambda **_kwargs: next(sequence))
    planned = native_proof.reattest_core_ip_receipt(
        expected_tool_manifest_sha256="1" * 64,
        expected_installed_backend_manifest_sha256="2" * 64,
    )
    assert planned["status"] == "READY"
    assert planned["action"] == "REATTEST"
    assert planned["receipt_sha256"] == facts["receipt_sha256"]
    sequence = iter([
        (facts, old, core, "3" * 64),
        ({**facts, "receipt_sha256": hashlib.sha256(
            (json.dumps({**old, "native_ip_semantics_sha256": "e" * 64}, ensure_ascii=True,
                        indent=2, sort_keys=True) + "\n").encode()).hexdigest()},
         {**old, "native_ip_semantics_sha256": "e" * 64}, core, "3" * 64),
    ])
    result = native_proof.reattest_core_ip_receipt(
        expected_tool_manifest_sha256="1" * 64,
        expected_installed_backend_manifest_sha256="2" * 64,
        apply=True, expected_plan_sha256=planned["plan_sha256"],
        confirmation=native_proof._CORE_IP_REATTEST_CONFIRMATION,
    )
    assert result["status"] == "PASS"
    updated = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert updated == {**old, "native_ip_semantics_sha256": "e" * 64}
    assert {key: value for key, value in updated.items() if key != "native_ip_semantics_sha256"} == {
        key: value for key, value in old.items() if key != "native_ip_semantics_sha256"
    }


def test_core_ip_receipt_reattest_projection_sessions_cannot_write_database(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    import sqlite3
    from contextlib import nullcontext

    from fwrouter_api.db import connection

    database = tmp_path / "readonly-projection.sqlite3"
    with sqlite3.connect(database) as db:
        db.execute("CREATE TABLE guard_probe (value INTEGER NOT NULL)")
    monkeypatch.setattr(connection, "get_db_path", lambda: database)
    monkeypatch.setattr("fwrouter_api.adapters.xray_common.xray_writer_guard",
                        lambda **_kwargs: nullcontext())

    def attempted_projection_write(**_kwargs):
        with connection.connect() as db:
            db.execute("INSERT INTO guard_probe(value) VALUES (1)")
        raise AssertionError("read-only projection unexpectedly accepted a write")

    monkeypatch.setattr(native_proof, "_core_ip_receipt_reattest_facts", attempted_projection_write)
    result = native_proof.reattest_core_ip_receipt(
        expected_tool_manifest_sha256="1" * 64,
        expected_installed_backend_manifest_sha256="2" * 64,
    )
    assert result["status"] == "BLOCKED"
    with sqlite3.connect(database) as db:
        assert db.execute("SELECT COUNT(*) FROM guard_probe").fetchone()[0] == 0


def test_core_ip_receipt_reattest_rejects_stale_plan_without_writing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from contextlib import nullcontext

    receipt_path = tmp_path / "proof.json"
    original = {
        "schema": 1, "apply_id": "apply-1", "core_intent_revision": 3,
        "candidate_nft_sha256": "a" * 64, "applied_manifest_sha256": "b" * 64,
        "native_semantics_sha256": "c" * 64, "native_ip_semantics_sha256": "d" * 64,
        "normalization": "nft-table-v1-counter-values-and-dns-timeout-elements-only",
    }
    receipt_path.write_text(json.dumps(original), encoding="utf-8")
    monkeypatch.setattr(native_proof, "core_native_proof_path", lambda: receipt_path)
    monkeypatch.setattr("fwrouter_api.adapters.xray_common.xray_writer_guard",
                        lambda **_kwargs: nullcontext())
    facts = {"old_ip_semantics_sha256": "d" * 64, "current_ip_semantics_sha256": "e" * 64}
    monkeypatch.setattr(native_proof, "_core_ip_receipt_reattest_facts",
                        lambda **_kwargs: (facts, original, {}, "3" * 64))
    result = native_proof.reattest_core_ip_receipt(
        expected_tool_manifest_sha256="1" * 64,
        expected_installed_backend_manifest_sha256="2" * 64,
        apply=True, expected_plan_sha256="0" * 64,
        confirmation=native_proof._CORE_IP_REATTEST_CONFIRMATION,
    )
    assert result["status"] == "BLOCKED"
    assert result["reason"] == "CORE_IP_RECEIPT_REATTEST_PLAN_CHANGED"
    assert json.loads(receipt_path.read_text(encoding="utf-8")) == original


def test_core_ip_receipt_reattest_noop_does_not_rewrite_receipt(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from contextlib import nullcontext

    receipt_path = tmp_path / "proof.json"
    original = {"native_ip_semantics_sha256": "d" * 64, "other": "preserved"}
    receipt_path.write_text(json.dumps(original), encoding="utf-8")
    before = receipt_path.read_bytes()
    monkeypatch.setattr(native_proof, "core_native_proof_path", lambda: receipt_path)
    monkeypatch.setattr("fwrouter_api.adapters.xray_common.xray_writer_guard",
                        lambda **_kwargs: nullcontext())
    facts = {"old_ip_semantics_sha256": "d" * 64, "current_ip_semantics_sha256": "d" * 64}
    monkeypatch.setattr(native_proof, "_core_ip_receipt_reattest_facts",
                        lambda **_kwargs: (facts, original, {}, "3" * 64))
    monkeypatch.setattr("fwrouter_api.services.artifacts.atomic_write_json",
                        lambda *_args: pytest.fail("no-op must not publish"))
    plan = native_proof.reattest_core_ip_receipt(
        expected_tool_manifest_sha256="1" * 64,
        expected_installed_backend_manifest_sha256="2" * 64,
    )
    result = native_proof.reattest_core_ip_receipt(
        expected_tool_manifest_sha256="1" * 64,
        expected_installed_backend_manifest_sha256="2" * 64,
        apply=True, expected_plan_sha256=plan["plan_sha256"],
        confirmation=native_proof._CORE_IP_REATTEST_CONFIRMATION,
    )
    assert plan["action"] == "NOOP"
    assert result["status"] == "NOOP"
    assert receipt_path.read_bytes() == before


def test_core_ip_receipt_reattest_postwrite_fence_failure_preserves_new_receipt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from contextlib import nullcontext
    import hashlib

    receipt_path = tmp_path / "proof.json"
    original = {
        "schema": 1, "apply_id": "apply-1", "core_intent_revision": 3,
        "candidate_nft_sha256": "a" * 64, "applied_manifest_sha256": "b" * 64,
        "native_semantics_sha256": "c" * 64, "native_ip_semantics_sha256": "d" * 64,
        "normalization": "nft-table-v1-counter-values-and-dns-timeout-elements-only",
    }
    receipt_path.write_text(json.dumps(original), encoding="utf-8")
    monkeypatch.setattr(native_proof, "core_native_proof_path", lambda: receipt_path)
    monkeypatch.setattr("fwrouter_api.adapters.xray_common.xray_writer_guard",
                        lambda **_kwargs: nullcontext())
    new_receipt = {**original, "native_ip_semantics_sha256": "e" * 64}
    new_sha = hashlib.sha256((json.dumps(new_receipt, ensure_ascii=True, indent=2, sort_keys=True) + "\n").encode()).hexdigest()
    before_facts = {"old_ip_semantics_sha256": "d" * 64, "current_ip_semantics_sha256": "e" * 64,
                    "receipt_sha256": "1" * 64, "database_fence_sha256": "2" * 64,
                    "tool_manifest_sha256": "3" * 64, "installed_backend_manifest_sha256": "4" * 64}
    after_facts = {**before_facts, "receipt_sha256": new_sha, "database_fence_sha256": "f" * 64}
    values = iter([(before_facts, original, {"native_ip_semantics_sha256": "e" * 64}, "5" * 64),
                   (after_facts, new_receipt, {"native_ip_semantics_sha256": "e" * 64}, "5" * 64)])
    monkeypatch.setattr(native_proof, "_core_ip_receipt_reattest_facts", lambda **_kwargs: next(values))
    plan = native_proof.reattest_core_ip_receipt(
        expected_tool_manifest_sha256="3" * 64,
        expected_installed_backend_manifest_sha256="4" * 64,
    )
    values = iter([(before_facts, original, {"native_ip_semantics_sha256": "e" * 64}, "5" * 64),
                   (after_facts, new_receipt, {"native_ip_semantics_sha256": "e" * 64}, "5" * 64)])
    result = native_proof.reattest_core_ip_receipt(
        expected_tool_manifest_sha256="3" * 64,
        expected_installed_backend_manifest_sha256="4" * 64,
        apply=True, expected_plan_sha256=plan["plan_sha256"],
        confirmation=native_proof._CORE_IP_REATTEST_CONFIRMATION,
    )
    assert result["status"] == "INCOMPLETE"
    assert result["reason"] == "CORE_IP_RECEIPT_REATTEST_POSTWRITE_FENCE_FAILED"
    assert json.loads(receipt_path.read_text(encoding="utf-8")) == new_receipt


def test_core_ip_receipt_reattest_facts_bind_source_installed_tree_and_checkpoint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    import hashlib

    tool_sha = native_proof.backend_manifest_sha256(native_proof._backend_root())
    installed_root = tmp_path / "installed"
    installed_root.mkdir()
    (installed_root / "api.py").write_text("installed-old", encoding="utf-8")
    installed_sha = native_proof.backend_manifest_sha256(installed_root)
    monkeypatch.setattr(native_proof, "_INSTALLED_BACKEND_ROOT", installed_root)
    monkeypatch.setattr(native_proof.os, "geteuid", lambda: 1000)
    with pytest.raises(ValueError, match="CORE_IP_RECEIPT_REATTEST_ROOT_REQUIRED"):
        native_proof._core_ip_receipt_reattest_facts(
            expected_tool_manifest_sha256=tool_sha,
            expected_installed_backend_manifest_sha256=installed_sha,
        )
    # Continue the positive branch under the explicit test-only root identity;
    # production still checks the actual effective UID against literal root.
    monkeypatch.setattr(native_proof.os, "geteuid", lambda: 0)
    checkpoint_path = tmp_path / "generation-checkpoint.json"
    checkpoint_path.write_text("proof-only-checkpoint", encoding="utf-8")
    checkpoint_sha = hashlib.sha256(checkpoint_path.read_bytes()).hexdigest()
    source_fingerprint = "5" * 64
    digest = "6" * 64
    checkpoint = {
        "operation_mode": "proof_only_legacy_admission", "operation_id": "4" * 32,
        "phase": "proof_complete", "require_native_proof": True,
        "candidate_digest": digest, "candidate_revision": 61,
        "source_fingerprint": source_fingerprint, "selection_revision": 9,
        "selection_decision_id": "selection-9",
    }
    monkeypatch.setattr(
        "fwrouter_api.services.xray_subscription_service._xray_generation_checkpoint_path",
        lambda _adapter: checkpoint_path,
    )
    monkeypatch.setattr(
        "fwrouter_api.services.xray_subscription_service._legacy_admission_checkpoint_payload",
        lambda _path: checkpoint.copy(),
    )
    db = {
        "schema": {"ok": True}, "integrity": ["ok"], "foreign_key_violations": 0,
        "active_jobs": 0, "inventory_digest": digest, "source_fingerprint": source_fingerprint,
        "core_revision": 3, "selection": {"revision": 9, "decision_id": "selection-9"},
        "generation": {"status": "verified", "candidate_digest": digest,
                        "verified_digest": digest, "candidate_revision": 61, "verified_revision": 61},
    }
    monkeypatch.setattr(native_proof, "_db_snapshot", lambda _path: db.copy())
    monkeypatch.setattr("fwrouter_api.db.connection.get_db_path", lambda: tmp_path / "unused.db")
    receipt_path = tmp_path / "core-receipt.json"
    receipt = {"schema": 1, "apply_id": "apply-1", "core_intent_revision": 3,
               "candidate_nft_sha256": "a" * 64, "applied_manifest_sha256": "b" * 64,
               "native_semantics_sha256": "c" * 64, "native_ip_semantics_sha256": "d" * 64,
               "normalization": "nft-table-v1-counter-values-and-dns-timeout-elements-only"}
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    monkeypatch.setattr(native_proof, "core_native_proof_path", lambda: receipt_path)
    core = {"native_ip_semantics_sha256": "e" * 64, "native_rule_semantics_sha256": "c" * 64}
    monkeypatch.setattr(native_proof, "_core_readback", lambda *_args, **_kwargs: core.copy())

    facts, existing_receipt, _core, observed_checkpoint_sha = native_proof._core_ip_receipt_reattest_facts(
        expected_tool_manifest_sha256=tool_sha,
        expected_installed_backend_manifest_sha256=installed_sha,
    )
    assert observed_checkpoint_sha == checkpoint_sha
    assert existing_receipt == receipt
    assert facts["tool_manifest_sha256"] == tool_sha
    assert facts["installed_backend_manifest_sha256"] == installed_sha
    assert facts["operation_id"] == "4" * 32
    assert facts["old_ip_semantics_sha256"] == "d" * 64
    assert facts["current_ip_semantics_sha256"] == "e" * 64

    for mutation, expected_error in (
        ({"active_jobs": 1}, "CORE_IP_RECEIPT_REATTEST_DATABASE_NOT_READY"),
        ({"source_fingerprint": "f" * 64}, "CORE_IP_RECEIPT_REATTEST_GENERATION_FENCE_CHANGED"),
        ({"generation": {**db["generation"], "verified_revision": 60}},
         "CORE_IP_RECEIPT_REATTEST_GENERATION_FENCE_CHANGED"),
    ):
        changed = {**db, **mutation}
        monkeypatch.setattr(native_proof, "_db_snapshot", lambda _path, state=changed: state.copy())
        with pytest.raises(ValueError, match=expected_error):
            native_proof._core_ip_receipt_reattest_facts(
                expected_tool_manifest_sha256=tool_sha,
                expected_installed_backend_manifest_sha256=installed_sha,
            )
    monkeypatch.setattr(native_proof, "_db_snapshot", lambda _path: db.copy())
    with pytest.raises(ValueError, match="CORE_IP_RECEIPT_REATTEST_INSTALLED_BACKEND_CHANGED"):
        native_proof._core_ip_receipt_reattest_facts(
            expected_tool_manifest_sha256=tool_sha,
            expected_installed_backend_manifest_sha256="0" * 64,
        )

    for invalid_checkpoint in (
        {**checkpoint, "phase": "proof_started"},
        {**checkpoint, "require_native_proof": False},
        {**checkpoint, "selection_revision": 8},
    ):
        monkeypatch.setattr(
            "fwrouter_api.services.xray_subscription_service._legacy_admission_checkpoint_payload",
            lambda _path, value=invalid_checkpoint: value,
        )
        with pytest.raises(ValueError):
            native_proof._core_ip_receipt_reattest_facts(
                expected_tool_manifest_sha256=tool_sha,
                expected_installed_backend_manifest_sha256=installed_sha,
            )


def test_readonly_database_sets_query_only_and_does_not_change_database(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    creator = sqlite3.connect(path)
    creator.execute("CREATE TABLE proof (value TEXT)")
    creator.execute("INSERT INTO proof VALUES ('before')")
    creator.commit()
    creator.close()
    before = path.read_bytes()

    connection = native_proof._open_readonly_database(path)
    try:
        assert connection.in_transaction
        assert connection.execute("PRAGMA query_only").fetchone()[0] == 1
        assert connection.execute("SELECT value FROM proof").fetchone()[0] == "before"
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("UPDATE proof SET value='after'")
    finally:
        connection.rollback()
        connection.close()

    assert path.read_bytes() == before


def test_nested_projection_uses_readonly_connection_and_blocks_expired_ttl_write(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    import stat
    from contextlib import nullcontext

    from fwrouter_api.db import connection as db_connection
    from fwrouter_api.services import server_state

    path = tmp_path / "state.db"
    creator = sqlite3.connect(path)
    creator.executescript("""
        CREATE TABLE routing_global_state (
            id INTEGER PRIMARY KEY, desired_mode TEXT, applied_mode TEXT, selective_default TEXT,
            server_mode TEXT, desired_fixed_server_id TEXT, applied_fixed_server_id TEXT,
            fixed_server_until TEXT, active_auto_server_id TEXT, apply_state TEXT,
            error_code TEXT, error_message TEXT, updated_at TEXT
        );
        CREATE TABLE settings (key TEXT PRIMARY KEY, value_json TEXT NOT NULL);
        INSERT INTO routing_global_state VALUES
            (1, 'vpn', 'vpn', 'direct', 'fixed', 'server-a', 'server-a',
             '2000-01-01 00:00:00', NULL, 'clean', NULL, NULL, '2000-01-01 00:00:00');
        INSERT INTO settings VALUES ('routing.auto_selection_revision', '4');
    """)
    creator.commit()
    creator.close()
    path.chmod(0o640)
    tmp_path.chmod(0o750)
    before_bytes = path.read_bytes()
    before_db_mode = stat.S_IMODE(path.stat().st_mode)
    before_parent_mode = stat.S_IMODE(tmp_path.stat().st_mode)
    before_db_mtime = path.stat().st_mtime_ns
    before_parent_mtime = tmp_path.stat().st_mtime_ns
    monkeypatch.setattr(db_connection, "get_db_path", lambda: path)
    monkeypatch.setattr(server_state, "xray_writer_guard", lambda **_kwargs: nullcontext())

    with db_connection.readonly_db_session_scope():
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            server_state.get_routing_global_state(expire_ttl=True)

    assert path.read_bytes() == before_bytes
    assert stat.S_IMODE(path.stat().st_mode) == before_db_mode
    assert stat.S_IMODE(tmp_path.stat().st_mode) == before_parent_mode
    assert path.stat().st_mtime_ns == before_db_mtime
    assert tmp_path.stat().st_mtime_ns == before_parent_mtime
    assert not Path(f"{path}-wal").exists()
    assert not Path(f"{path}-shm").exists()


def test_readonly_database_scope_resets_after_exception_and_is_context_local() -> None:
    from threading import Thread

    from fwrouter_api.db.connection import _READ_ONLY_DB_SESSIONS, readonly_db_session_scope

    observed_in_thread: list[bool] = []
    with pytest.raises(RuntimeError, match="fixture failure"):
        with readonly_db_session_scope():
            assert _READ_ONLY_DB_SESSIONS.get() is True
            thread = Thread(target=lambda: observed_in_thread.append(_READ_ONLY_DB_SESSIONS.get()))
            thread.start()
            thread.join(timeout=2)
            assert not thread.is_alive()
            raise RuntimeError("fixture failure")

    assert observed_in_thread == [False]
    assert _READ_ONLY_DB_SESSIONS.get() is False


def _proof_ready_db() -> dict[str, object]:
    recorded = _recorded_native_proof()
    return {
        "schema": {"ok": True, "expected_schema_version": 27, "actual_schema_version": 27},
        "integrity": ["ok"], "foreign_key_violations": 0, "inventory_digest": "internal",
        "inventory_count": 2,
        "generation": {"status": "verified", "candidate_digest": "internal", "verified_digest": "internal",
                        "candidate_revision": 4, "verified_revision": 4, "native_proof": recorded},
        "selection": {"revision": 8, "decision_id": "private", "active_server_id": "private"},
        "core_revision": 5, "source_fingerprint": "internal-secret-bearing-digest",
        "latest_apply": {"apply_id": "private-apply-id", "status": "applied", "core_intent_revision": 5},
        "routing": {"apply_state": "clean"},
        "modules": [
            {"module_name": name, "desired_state": "enabled", "lifecycle_mode": "managed", "apply_state": "clean"}
            for name in ("core", "vpn", "xray")
        ],
        "active_jobs": 0,
        "xray_identities": [],
}


def _recorded_native_proof(*, inventory_digest: str = "internal", generation_revision: int = 4) -> dict[str, object]:
    return {
        "schema": 1, "inventory_digest": inventory_digest, "generation_revision": generation_revision,
        "backend_manifest_sha256": "a" * 64,
        "xray": {key: value for key, value in {
            "config_sha256": "1" * 64, "runtime_config_sha256": "2" * 64,
            "runtime_incarnation_sha256": "3" * 64, "durable_bindings_sha256": "4" * 64,
            "durable_modes_sha256": "5" * 64, "runtime_image_digest": XRAY_IMAGE_PIN,
            "runtime_image_reference_sha256": "a" * 64, "runtime_version_sha256": "b" * 64,
        }.items()},
        "mihomo": {
            "config_sha256": "6" * 64, "semantic_config_sha256": "7" * 64,
            "durable_semantic_config_sha256": "8" * 64, "input_fingerprint_sha256": "9" * 64,
            "runtime_incarnation_sha256": "a" * 64, "runtime_receipt_config_sha256": "b" * 64,
            "runtime_receipt_fingerprint_sha256": "c" * 64, "runtime_image_digest": MIHOMO_IMAGE_PIN,
            "runtime_image_reference_sha256": "d" * 64, "runtime_version_sha256": "e" * 64,
        },
        "core": {
            "applied_manifest_sha256": "d" * 64, "applied_nft_sha256": "e" * 64,
            "native_rule_semantics_sha256": "f" * 64, "native_ip_semantics_sha256": "0" * 64,
            "latest_apply_id_sha256": __import__("hashlib").sha256(b"private-apply-id").hexdigest(),
            "core_intent_revision": 5, "selection_revision": 8,
        },
    }


def _prepare_mihomo_readback_dependencies(monkeypatch: pytest.MonkeyPatch, config_path: Path):
    from fwrouter_api.adapters.mihomo import MihomoHttpAdapter
    from fwrouter_api.services import mihomo_config, mihomo_config_status, servers
    from fwrouter_api.services import mihomo_reconcile_fingerprint as fingerprint
    from fwrouter_api.services import mihomo_config_yaml
    from fwrouter_api.services import xray_bindings, xray_handoff

    class Adapter:
        def __init__(self) -> None:
            self.config_path = config_path

        def _config_text(self) -> str:
            # This fixture injects alternate stale text; production adapter
            # currently uses its own strict file reader.
            return "mode: global\n"

        def _proxies(self, **_kwargs):
            return {}

        def _get_json(self, *_args, **_kwargs):
            return {"rules": []}

    adapter = Adapter()
    adapter.receipt_state = {
        "version": 3, "fingerprint_hash": "fixture", "active_config_hash": "fixture",
        "runtime_incarnation": "runtime-fixture", "udp_policy_version": 1,
    }
    parsed = MihomoHttpAdapter._parse_vpn_udp_policy_config(
        mihomo_config_yaml.read_mihomo_yaml_text(config_path)
    )
    monkeypatch.setattr(servers, "get_routing_global_state", lambda **_kwargs: {})
    monkeypatch.setattr(xray_bindings, "collect_xray_runtime_bindings", lambda: [])
    monkeypatch.setattr(xray_handoff, "build_xray_handoff_assignments", lambda _bindings: [])
    monkeypatch.setattr(mihomo_config, "build_mihomo_config", lambda *_args, **_kwargs: parsed)
    monkeypatch.setattr(mihomo_config_status, "_configs_equal", lambda _left, _right: True)
    monkeypatch.setattr(fingerprint, "mihomo_input_fingerprint_profiles",
                        lambda _routing: [("ordinary_full", {"hash": "fixture", "payload": {"routing": None}})])
    monkeypatch.setattr(fingerprint, "matching_mihomo_input_fingerprint",
                        lambda _values: ({"hash": "fixture"}, adapter.receipt_state))
    monkeypatch.setattr(fingerprint, "mihomo_udp_policy_receipt_matches",
                        lambda state=None: state == adapter.receipt_state)
    monkeypatch.setattr(fingerprint, "mihomo_input_receipt_snapshot_matches",
                        lambda state: state == adapter.receipt_state)
    monkeypatch.setattr(fingerprint, "_load_state", lambda: dict(adapter.receipt_state))
    monkeypatch.setattr(MihomoHttpAdapter, "_vpn_udp_policy_readback", staticmethod(
        lambda *_args, **_kwargs: {
            "native_top_level_rules_match": True,
            "selector_membership_matches": True,
            "guarded_rules_match": True,
        }
    ))
    monkeypatch.setattr(native_proof, "_mihomo_selection_target_matches", lambda *_args: True)
    return adapter, {"image_digest": MIHOMO_IMAGE_PIN, "image_reference_sha256": "a" * 64,
                     "version_sha256": "b" * 64, "incarnation_sha256": "c" * 64}


def test_mihomo_receipt_profiles_match_existing_full_none_and_owner_projections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fwrouter_api.services import mihomo_reconcile_fingerprint as fingerprint
    from fwrouter_api.services import vpn_auto_selection_state, xray_subscription_service

    def current(_routing):
        payload = {"version": 3, "routing": None, "tables": {"routes": ["durable"]},
                   "files": {"rules": "a" * 64}, "source": {"module": "b" * 64}}
        if isinstance(_routing, dict):
            payload["routing"] = {key: value for key, value in _routing.items()
                                   if key != "active_auto_server_id"}
        return {"version": 3, "hash": fingerprint._json_hash(payload), "payload": payload}

    monkeypatch.setattr(fingerprint, "current_mihomo_input_fingerprint", current)
    routing = {
        "server_mode": "auto", "desired_fixed_server_id": None, "applied_fixed_server_id": None,
        "fixed_server_until": None, "active_auto_server_id": "logical-1", "desired_mode": "vpn",
        "applied_mode": "vpn", "selective_default": "direct", "apply_state": "clean",
        "updated_at": "2026-10-26T00:00:00Z", "error_code": None, "error_message": None,
    }

    # Bind the profile definitions to the actual owner projection functions.
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        "CREATE TABLE routing_global_state (id INTEGER, server_mode TEXT, desired_fixed_server_id TEXT, "
        "applied_fixed_server_id TEXT, fixed_server_until TEXT, active_auto_server_id TEXT, "
        "desired_mode TEXT, applied_mode TEXT, selective_default TEXT, apply_state TEXT, updated_at TEXT)"
    )
    connection.execute(
        "INSERT INTO routing_global_state VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        tuple(routing[key] for key in (
            "server_mode", "desired_fixed_server_id", "applied_fixed_server_id", "fixed_server_until",
            "active_auto_server_id", "desired_mode", "applied_mode", "selective_default", "apply_state", "updated_at",
        )),
    )

    class SettingsConnection:
        def execute(self, sql, *args):
            if "FROM settings" in sql:
                return SimpleNamespace(fetchone=lambda: None)
            return connection.execute(sql, *args)

    @contextmanager
    def readonly_session():
        yield SettingsConnection()

    monkeypatch.setattr(xray_subscription_service, "db_session", readonly_session)
    monkeypatch.setattr(vpn_auto_selection_state, "read_selection_fence",
                        lambda _connection: {"revision": 7, "decision_id": "decision-7"})
    captured = xray_subscription_service._capture_generation_auto_selection()
    assert captured["routing"] == {key: routing[key] for key in (
        "server_mode", "desired_fixed_server_id", "applied_fixed_server_id", "fixed_server_until",
        "active_auto_server_id", "desired_mode", "applied_mode", "selective_default", "apply_state", "updated_at",
    )}
    fixed = xray_subscription_service._fixed_xray_routing_projection(routing)
    assert fixed == {key: routing[key] for key in (
        "desired_mode", "applied_mode", "selective_default", "server_mode",
        "desired_fixed_server_id", "applied_fixed_server_id", "fixed_server_until", "apply_state",
    )}

    profiles = fingerprint.mihomo_input_fingerprint_profiles(routing)
    by_name = dict(profiles)
    assert set(by_name) == {"ordinary_full", "proof_only_generation", "fixed_generation", "ordinary_none"}
    assert by_name["ordinary_full"]["payload"]["routing"] == {
        key: value for key, value in routing.items() if key != "active_auto_server_id"
    }
    assert "error_code" not in by_name["proof_only_generation"]["payload"]["routing"]
    assert "active_auto_server_id" not in by_name["proof_only_generation"]["payload"]["routing"]
    assert set(by_name["proof_only_generation"]["payload"]["routing"]) == {
        "server_mode", "desired_fixed_server_id", "applied_fixed_server_id", "fixed_server_until",
        "desired_mode", "applied_mode", "selective_default", "apply_state", "updated_at",
    }
    assert by_name["proof_only_generation"]["payload"]["routing"] == {
        key: value for key, value in captured["routing"].items()
        if key not in {"active_auto_server_id"}
    }
    assert by_name["fixed_generation"]["payload"]["routing"] == fixed
    assert by_name["ordinary_none"]["payload"]["routing"] is None

    receipt = {"version": 3, "fingerprint_hash": by_name["proof_only_generation"]["hash"],
               "active_config_hash": "c" * 64}
    monkeypatch.setattr(fingerprint, "_load_state", lambda: receipt.copy())
    monkeypatch.setattr(fingerprint, "active_config_hash", lambda: "c" * 64)
    monkeypatch.setattr(fingerprint, "_receipt_runtime_matches", lambda _state: True)
    monkeypatch.setattr(fingerprint, "mihomo_input_receipt_snapshot_matches", lambda state: state == receipt)
    matched = fingerprint.matching_mihomo_input_fingerprint([item for _, item in profiles])
    assert matched == (by_name["proof_only_generation"], receipt)


def test_mihomo_receipt_profiles_reject_real_drift_unknown_state_and_ambiguity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fwrouter_api.services import mihomo_reconcile_fingerprint as fingerprint

    def current(_routing):
        payload = {"version": 3, "routing": None, "tables": {"intent": "old"},
                   "files": {}, "source": {}}
        if isinstance(_routing, dict):
            payload["routing"] = {key: value for key, value in _routing.items()
                                   if key != "active_auto_server_id"}
        return {"version": 3, "hash": fingerprint._json_hash(payload), "payload": payload}

    monkeypatch.setattr(fingerprint, "current_mihomo_input_fingerprint", current)
    original = {
        "server_mode": "auto", "desired_fixed_server_id": None, "applied_fixed_server_id": None,
        "fixed_server_until": None, "active_auto_server_id": "auto-1", "desired_mode": "vpn",
        "applied_mode": "vpn", "selective_default": "direct", "apply_state": "clean",
        "updated_at": "2026-10-26T00:00:00Z", "error_code": None,
    }
    before = fingerprint.mihomo_input_fingerprint_profiles(original)
    drifted = fingerprint.mihomo_input_fingerprint_profiles(original | {"desired_mode": "direct"})
    real_snapshot_match = fingerprint.mihomo_input_receipt_snapshot_matches
    receipt = {"version": 3, "fingerprint_hash": dict(before)["proof_only_generation"]["hash"],
               "active_config_hash": "d" * 64}
    monkeypatch.setattr(fingerprint, "_load_state", lambda: receipt.copy())
    monkeypatch.setattr(fingerprint, "active_config_hash", lambda: "d" * 64)
    monkeypatch.setattr(fingerprint, "_receipt_runtime_matches", lambda _state: True)
    monkeypatch.setattr(fingerprint, "mihomo_input_receipt_snapshot_matches", lambda state: state == receipt)
    assert fingerprint.matching_mihomo_input_fingerprint([item for _, item in drifted]) is None
    monkeypatch.setattr(fingerprint, "_load_state", lambda: None)
    assert fingerprint.matching_mihomo_input_fingerprint([item for _, item in before]) is None

    monkeypatch.setattr(fingerprint, "_load_state", lambda: receipt.copy())
    receipt["version"] = 2
    assert fingerprint.matching_mihomo_input_fingerprint([item for _, item in before]) is None
    receipt["version"] = 3
    matching = dict(before)["proof_only_generation"]
    ambiguous = matching | {"payload": matching["payload"] | {"different": True}}
    real_json_hash = fingerprint._json_hash
    monkeypatch.setattr(fingerprint, "_json_hash", lambda _value: matching["hash"])
    assert fingerprint.matching_mihomo_input_fingerprint([matching, ambiguous]) is None

    receipt["version"] = 3
    states = iter((receipt.copy(), receipt.copy(), receipt | {"fingerprint_hash": "e" * 64}))
    monkeypatch.setattr(fingerprint, "_load_state", lambda: next(states))
    monkeypatch.setattr(fingerprint, "_json_hash", real_json_hash)
    monkeypatch.setattr(fingerprint, "_current_runtime_incarnation", lambda: "runtime")
    monkeypatch.setattr(fingerprint, "mihomo_input_receipt_snapshot_matches",
                        real_snapshot_match)
    assert fingerprint.matching_mihomo_input_fingerprint([matching]) is None


@pytest.mark.parametrize("profile_name", ["ordinary_full", "proof_only_generation", "fixed_generation", "ordinary_none"])
def test_existing_receipt_writer_output_is_consumable_for_each_supported_owner_profile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, profile_name: str,
) -> None:
    from fwrouter_api.services import mihomo_reconcile_fingerprint as fingerprint

    route = {
        "server_mode": "fixed", "desired_fixed_server_id": "fixed-1", "applied_fixed_server_id": "fixed-1",
        "fixed_server_until": "2026-10-27T00:00:00Z", "active_auto_server_id": "auto-1",
        "desired_mode": "vpn", "applied_mode": "vpn", "selective_default": "direct",
        "apply_state": "clean", "updated_at": "2026-10-26T00:00:00Z", "error_message": None,
    }
    payload = {"version": 3, "routing": None, "tables": {"intent": "bound"},
               "files": {"input": "a" * 64}, "source": {"service": "b" * 64}}

    def current(routing):
        value = dict(payload)
        if isinstance(routing, dict):
            value["routing"] = {key: item for key, item in routing.items() if key != "active_auto_server_id"}
        return {"version": 3, "hash": fingerprint._json_hash(value), "payload": value}

    monkeypatch.setattr(fingerprint, "current_mihomo_input_fingerprint", current)
    profiles = dict(fingerprint.mihomo_input_fingerprint_profiles(route))
    stored: dict[str, object] = {}
    monkeypatch.setattr(fingerprint, "active_config_hash", lambda: "c" * 64)
    monkeypatch.setattr(fingerprint, "_current_runtime_incarnation", lambda: "runtime-incarnation")
    monkeypatch.setattr(fingerprint, "_resolved_candidate_config_path", lambda: tmp_path / "candidate.yaml")
    monkeypatch.setattr(fingerprint, "_file_hash", lambda _path: "d" * 64)
    monkeypatch.setattr(fingerprint, "atomic_write_json", lambda _path, value: stored.update(value))
    monkeypatch.setattr("fwrouter_api.services.live_probe_cache.clear_live_probe_cache", lambda: None)
    monkeypatch.setattr(fingerprint, "_receipt_runtime_matches", lambda state: state.get("runtime_incarnation") == "runtime-incarnation")
    monkeypatch.setattr(fingerprint, "_load_state", lambda: dict(stored) if stored else None)

    assert fingerprint.write_mihomo_reconcile_fingerprint_state(
        fingerprint=profiles[profile_name],
        result={"reconcile_reason": "proof_only_legacy_admission" if profile_name == "proof_only_generation" else "normal"},
        native_proof={"native_top_level_rules_match": True, "selector_membership_matches": True,
                      "guarded_rules_match": True},
        expected_active_hash="c" * 64,
        expected_runtime_incarnation="runtime-incarnation",
    )
    matched = fingerprint.matching_mihomo_input_fingerprint(list(profiles.values()))
    assert matched == (profiles[profile_name], stored)
    assert stored["fingerprint_hash"] == profiles[profile_name]["hash"]


@pytest.mark.parametrize("replace_at", ["after_match", "during_native_reads"])
def test_mihomo_readback_rejects_receipt_replacement_after_match(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, replace_at: str,
) -> None:
    from fwrouter_api.adapters.mihomo import MihomoHttpAdapter
    from fwrouter_api.services import mihomo_reconcile_fingerprint as fingerprint

    config = tmp_path / "config.yaml"
    config.write_text("mode: rule\n", encoding="utf-8")
    adapter, runtime = _prepare_mihomo_readback_dependencies(monkeypatch, config)
    state_a = dict(adapter.receipt_state)
    state_b = state_a | {"fingerprint_hash": "b" * 64, "updated_at": "replacement"}

    def udp_receipt_matches(state=None):
        if replace_at == "after_match":
            adapter.receipt_state = state_b
            return True
        return state == adapter.receipt_state

    monkeypatch.setattr(fingerprint, "mihomo_udp_policy_receipt_matches", udp_receipt_matches)
    if replace_at == "during_native_reads":
        original_get_json = adapter._get_json

        def replace_during_native_read(*args, **kwargs):
            adapter.receipt_state = state_b
            return original_get_json(*args, **kwargs)

        adapter._get_json = replace_during_native_read
    monkeypatch.setattr(MihomoHttpAdapter, "_vpn_udp_policy_readback", staticmethod(
        lambda *_args, **_kwargs: {
            "native_top_level_rules_match": True,
            "selector_membership_matches": True,
            "guarded_rules_match": True,
        }
    ))

    with pytest.raises(RuntimeError, match="MIHOMO_READBACK_FENCE_CHANGED"):
        native_proof._mihomo_readback({}, adapter=adapter, runtime_identity_reader=lambda: runtime)
    assert state_a["fingerprint_hash"] == "fixture"
    assert adapter.receipt_state == state_b


def test_mihomo_receipt_snapshot_fence_accepts_stable_and_rejects_changed_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fwrouter_api.services import mihomo_reconcile_fingerprint as fingerprint

    state = {"version": 3, "fingerprint_hash": "a" * 64, "active_config_hash": "b" * 64,
             "runtime_incarnation": "runtime", "udp_policy_version": 1}
    monkeypatch.setattr(fingerprint, "active_config_hash", lambda: "b" * 64)
    monkeypatch.setattr(fingerprint, "_current_runtime_incarnation", lambda: "runtime")
    monkeypatch.setattr(fingerprint, "_receipt_runtime_matches", lambda _state: True)
    monkeypatch.setattr(fingerprint, "_load_state", lambda: state.copy())
    assert fingerprint.mihomo_input_receipt_snapshot_matches(state)

    reads = iter((state.copy(), state | {"fingerprint_hash": "c" * 64}))
    monkeypatch.setattr(fingerprint, "_load_state", lambda: next(reads))
    assert not fingerprint.mihomo_input_receipt_snapshot_matches(state)


def test_mihomo_generation_owner_snapshot_requires_revision_and_decision_identity() -> None:
    routing = {
        "server_mode": "auto", "desired_fixed_server_id": None, "applied_fixed_server_id": None,
        "fixed_server_until": None, "active_auto_server_id": "auto-1", "desired_mode": "vpn",
        "applied_mode": "vpn", "selective_default": "direct", "apply_state": "clean",
        "updated_at": "2026-10-26T00:00:00Z",
    }
    db = {
        "selection": {"revision": 9, "decision_id": "decision-a"},
        "routing": {key: routing[key] for key in (
            "desired_mode", "applied_mode", "selective_default", "server_mode",
            "desired_fixed_server_id", "applied_fixed_server_id", "active_auto_server_id", "apply_state",
        )},
    }
    owner = {"routing": dict(routing), "selection_revision": 9, "selection_decision_id": "decision-a"}
    assert native_proof._mihomo_generation_owner_snapshot_matches(owner, db, routing)
    assert not native_proof._mihomo_generation_owner_snapshot_matches(
        owner | {"selection_decision_id": "decision-b"}, db, routing,
    )
    assert not native_proof._mihomo_generation_owner_snapshot_matches(
        owner | {"selection_revision": 10}, db, routing,
    )


def test_mihomo_readback_accepts_20mb_config_and_binds_parsed_bytes_to_hash(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    config = tmp_path / "config.yaml"
    content = b"mode: rule\n#" + b"x" * (20 * 1024 * 1024)
    config.write_bytes(content)
    adapter, runtime = _prepare_mihomo_readback_dependencies(monkeypatch, config)

    result = native_proof._mihomo_readback({}, adapter=adapter, runtime_identity_reader=lambda: runtime)

    assert result["config_sha256"] == __import__("hashlib").sha256(content).hexdigest()
    assert result["semantic_config_sha256"] == result["durable_semantic_config_sha256"]
    assert result["config_stable"] is True


@pytest.mark.parametrize("kind", ["oversized", "symlink"])
def test_mihomo_readback_rejects_oversized_or_symlinked_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, kind: str,
) -> None:
    config = tmp_path / "config.yaml"
    if kind == "oversized":
        with config.open("wb") as stream:
            stream.truncate(32 * 1024 * 1024 + 1)
    else:
        target = tmp_path / "target.yaml"
        target.write_text("mode: rule\n", encoding="utf-8")
        config.symlink_to(target)
    adapter = SimpleNamespace(config_path=config)
    with pytest.raises(ValueError):
        native_proof._mihomo_readback({}, adapter=adapter, runtime_identity_reader=lambda: {})


def test_mihomo_readback_rejects_config_replacement_after_initial_parse(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from fwrouter_api.services import mihomo_config_yaml

    config = tmp_path / "config.yaml"
    config.write_text("mode: rule\n", encoding="utf-8")
    replacement = tmp_path / "replacement.yaml"
    replacement.write_text("mode: global\n", encoding="utf-8")
    adapter, runtime = _prepare_mihomo_readback_dependencies(monkeypatch, config)
    original_reader = mihomo_config_yaml.read_mihomo_yaml_text
    replaced = False

    def replace_after_read(path, **kwargs):
        nonlocal replaced
        text = original_reader(path, **kwargs)
        if not replaced:
            replacement.replace(config)
            replaced = True
        return text

    monkeypatch.setattr(mihomo_config_yaml, "read_mihomo_yaml_text", replace_after_read)
    with pytest.raises(RuntimeError, match="MIHOMO_READBACK_FENCE_CHANGED"):
        native_proof._mihomo_readback({}, adapter=adapter, runtime_identity_reader=lambda: runtime)


def _patch_pass_dependencies(monkeypatch: pytest.MonkeyPatch, *, after_db: dict[str, object] | None = None) -> str:
    expected = "a" * 64
    monkeypatch.setattr(native_proof, "backend_manifest_sha256", lambda _root: expected)
    monkeypatch.setattr(native_proof, "_db_snapshot", lambda _path: _proof_ready_db())
    monkeypatch.setattr(native_proof, "_checkpoint_snapshot", lambda: {"present": False, "valid": True, "sha256": None})
    monkeypatch.setattr(native_proof, "_xray_readback", lambda _db: {
        "config_sha256": "1" * 64, "runtime_config_sha256": "2" * 64,
        "runtime_incarnation_sha256": "3" * 64, "durable_bindings_sha256": "4" * 64,
        "durable_modes_sha256": "5" * 64, "runtime_image_digest": XRAY_IMAGE_PIN,
        "runtime_image_reference_sha256": "a" * 64, "runtime_version_sha256": "b" * 64,
    })
    monkeypatch.setattr(native_proof, "_mihomo_readback", lambda _db: {
        "config_sha256": "6" * 64, "semantic_config_sha256": "7" * 64,
        "durable_semantic_config_sha256": "8" * 64, "input_fingerprint_sha256": "9" * 64,
        "runtime_incarnation_sha256": "a" * 64, "runtime_receipt_config_sha256": "b" * 64,
        "runtime_receipt_fingerprint_sha256": "c" * 64, "runtime_image_digest": MIHOMO_IMAGE_PIN,
        "runtime_image_reference_sha256": "d" * 64, "runtime_version_sha256": "e" * 64,
    })
    monkeypatch.setattr(native_proof, "_core_readback", lambda _db: {
        "applied_manifest_sha256": "d" * 64, "applied_nft_sha256": "e" * 64,
        "native_rule_semantics_sha256": "f" * 64,
        "native_ip_semantics_sha256": "0" * 64,
        "latest_apply_id_sha256": __import__("hashlib").sha256(b"private-apply-id").hexdigest(),
        "core_intent_revision": 5, "selection_revision": 8,
    })
    monkeypatch.setattr("fwrouter_api.db.connection.get_db_path", lambda: Path("/readonly/test.db"))

    from contextlib import contextmanager

    @contextmanager
    def read_guard():
        yield True

    monkeypatch.setattr("fwrouter_api.adapters.xray_common.xray_writer_read_guard", read_guard)
    if after_db is not None:
        calls = iter((_proof_ready_db(), after_db))
        monkeypatch.setattr(native_proof, "_db_snapshot", lambda _path: next(calls))
    return expected


def test_collect_native_proof_reports_pass_without_private_identifiers(monkeypatch: pytest.MonkeyPatch) -> None:
    expected = _patch_pass_dependencies(monkeypatch)

    result = native_proof.collect_native_proof(scope="ui", expected_backend_manifest_sha256=expected,
                                               expected_xray_image_digest=XRAY_IMAGE_PIN,
                                               expected_mihomo_image_digest=MIHOMO_IMAGE_PIN)

    assert result["status"] == "PASS", result
    assert result["proof_schema_version"] == 1
    assert "mihomo_config_rules_selectors_runtime" in result["checks_executed"]
    encoded = json.dumps(result, sort_keys=True)
    for private in ("private-apply-id", "internal-secret-bearing-digest", '"active_server_id"', '"decision_id"'):
        assert private not in encoded


def test_collect_native_proof_returns_stale_on_changed_durable_fence(monkeypatch: pytest.MonkeyPatch) -> None:
    changed = _proof_ready_db()
    changed["selection"] = {"revision": 9, "decision_id": "private", "active_server_id": "private"}
    expected = _patch_pass_dependencies(monkeypatch, after_db=changed)

    result = native_proof.collect_native_proof(scope="ui", expected_backend_manifest_sha256=expected,
                                               expected_xray_image_digest=XRAY_IMAGE_PIN,
                                               expected_mihomo_image_digest=MIHOMO_IMAGE_PIN)

    assert result["status"] == "STALE"
    assert result["reason"] == "durable_fence_changed"


def test_collect_native_proof_rejects_wrong_expected_manifest_before_native_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(native_proof, "backend_manifest_sha256", lambda _root: "b" * 64)
    monkeypatch.setattr(native_proof, "_db_snapshot", lambda _path: pytest.fail("DB read must not run"))

    result = native_proof.collect_native_proof(scope="ui", expected_backend_manifest_sha256="a" * 64,
                                               expected_xray_image_digest=XRAY_IMAGE_PIN,
                                               expected_mihomo_image_digest=MIHOMO_IMAGE_PIN)

    assert result["status"] == "STALE"
    assert result["reason"] == "backend_manifest_changed"


def test_native_proof_cli_cannot_attest_environment_selected_backend(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    from argparse import Namespace

    from fwrouter_api.cli import _run_native_proof

    decoy = tmp_path / "approved-backend"
    decoy.mkdir()
    (decoy / "native_proof.py").write_text("different executable source\n", encoding="utf-8")
    decoy_sha = native_proof.backend_manifest_sha256(decoy)
    monkeypatch.setenv("FWROUTER_BACKEND_ROOT", str(decoy))
    monkeypatch.setattr(native_proof, "_db_snapshot", lambda _path: pytest.fail("DB read must not run"))

    assert native_proof._backend_root() == REPO_ROOT / "backend"
    status = _run_native_proof(Namespace(
        scope="ui",
        expected_backend_manifest_sha256=decoy_sha,
        expected_xray_image_digest=XRAY_IMAGE_PIN,
        expected_mihomo_image_digest=MIHOMO_IMAGE_PIN,
    ))

    output = json.loads(capsys.readouterr().out)
    executing_sha = native_proof.backend_manifest_sha256(REPO_ROOT / "backend")
    assert status == 1
    assert output["status"] == "STALE"
    assert output["reason"] == "backend_manifest_changed"
    assert output["source_identity"]["sha256"] == executing_sha
    assert output["source_identity"]["sha256"] != decoy_sha


def test_legacy_generation_without_native_tuple_is_incomplete(monkeypatch: pytest.MonkeyPatch) -> None:
    db = _proof_ready_db()
    db["generation"] = {key: value for key, value in db["generation"].items() if key != "native_proof"}
    expected = _patch_pass_dependencies(monkeypatch)
    monkeypatch.setattr(native_proof, "_db_snapshot", lambda _path: db)

    result = native_proof.collect_native_proof(scope="ui", expected_backend_manifest_sha256=expected,
                                               expected_xray_image_digest=XRAY_IMAGE_PIN,
                                               expected_mihomo_image_digest=MIHOMO_IMAGE_PIN)

    assert result["status"] == "INCOMPLETE"
    assert result["reason"] == "native_generation_receipt_missing_or_invalid"


def test_recorded_native_generation_tuple_mismatch_is_stale(monkeypatch: pytest.MonkeyPatch) -> None:
    db = _proof_ready_db()
    db["generation"]["native_proof"]["mihomo"]["semantic_config_sha256"] = "0" * 64
    expected = _patch_pass_dependencies(monkeypatch)
    monkeypatch.setattr(native_proof, "_db_snapshot", lambda _path: db)

    result = native_proof.collect_native_proof(scope="ui", expected_backend_manifest_sha256=expected,
                                               expected_xray_image_digest=XRAY_IMAGE_PIN,
                                               expected_mihomo_image_digest=MIHOMO_IMAGE_PIN)

    assert result["status"] == "STALE"
    assert result["reason"] == "native_generation_receipt_mismatch"


def test_runtime_change_between_component_reads_is_stale(monkeypatch: pytest.MonkeyPatch) -> None:
    expected = _patch_pass_dependencies(monkeypatch)
    original = native_proof._mihomo_readback
    calls = 0

    def changes_after_first_read(db):
        nonlocal calls
        calls += 1
        observed = original(db)
        if calls > 1:
            observed = dict(observed, runtime_incarnation_sha256="0" * 64)
        return observed

    monkeypatch.setattr(native_proof, "_mihomo_readback", changes_after_first_read)
    result = native_proof.collect_native_proof(
        scope="ui", expected_backend_manifest_sha256=expected,
        expected_xray_image_digest=XRAY_IMAGE_PIN, expected_mihomo_image_digest=MIHOMO_IMAGE_PIN,
    )
    assert result["status"] == "STALE"
    assert result["reason"] == "native_runtime_fence_changed"


def test_core_manifest_comparison_ignores_selector_owned_auto_target_but_checks_policy() -> None:
    core_manifest = {
        "desired_mode": "selective", "applied_mode": "selective", "selective_default": "direct",
        "server_mode": "auto", "desired_fixed_server_id": "", "applied_fixed_server_id": "",
        "active_auto_server_id": "stale-from-apply",
    }
    current_routing = {**core_manifest, "active_auto_server_id": "currently-selected"}

    assert native_proof._routing_manifest_matches(core_manifest, current_routing)
    assert not native_proof._routing_manifest_matches(core_manifest, {**current_routing, "desired_mode": "vpn"})


def test_mihomo_native_selector_must_match_persistent_auto_and_fixed_targets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fwrouter_api.services import mihomo_config_proxies, mihomo_config_status

    monkeypatch.setattr(mihomo_config_status, "_resolve_active_auto_runtime_target",
                        lambda _server_id, *, expected_targets: "server-a" if "server-a" in expected_targets else None)
    monkeypatch.setattr(mihomo_config_status, "_resolve_active_fixed_runtime_target",
                        lambda _server_id, *, expected_targets: "server-b" if "server-b" in expected_targets else None)
    monkeypatch.setattr(
        mihomo_config_proxies, "_logical_profile_groups",
        lambda **_kwargs: pytest.fail("concrete target readback must not query logical profile storage"),
    )
    config = {"proxies": [{"name": "server-a"}, {"name": "server-b"}]}

    assert native_proof._mihomo_selection_target_matches(
        {"routing": {"server_mode": "auto"}, "selection": {"active_server_id": "durable-a"}},
        config, {"vpn-auto": {"now": "server-a"}, "vpn-global": {"now": "vpn-auto"}},
    )
    assert not native_proof._mihomo_selection_target_matches(
        {"routing": {"server_mode": "auto"}, "selection": {"active_server_id": "durable-a"}},
        config, {"vpn-auto": {"now": "server-b"}, "vpn-global": {"now": "vpn-auto"}},
    )
    assert not native_proof._mihomo_selection_target_matches(
        {"routing": {"server_mode": "auto"}, "selection": {"active_server_id": "durable-a"}},
        config, {"vpn-auto": {"now": "server-a"}, "vpn-global": {"now": "REJECT"}},
    )
    assert native_proof._mihomo_selection_target_matches(
        {"routing": {"server_mode": "fixed", "applied_fixed_server_id": "durable-b"}},
        config, {"vpn-global": {"now": "server-b"}},
    )
    assert not native_proof._mihomo_selection_target_matches(
        {"routing": {"server_mode": "fixed", "applied_fixed_server_id": "durable-b"}},
        config, {"vpn-global": {"now": "server-a"}},
    )


def test_mihomo_native_selector_accepts_only_proven_logical_profile_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fwrouter_api.services import custom_servers, mihomo_config_proxies, mihomo_config_status

    group_name = "Logical Profile [logical-id]"
    fixed_group_name = "Fixed Logical Profile [logical-id]"
    rows = [
        {"server_id": "logical-auto", "raw": {"name": "display", "_fwrouter_runtime_name": group_name}},
        {"server_id": "logical-fixed", "raw": {"name": "fixed display", "_fwrouter_runtime_name": fixed_group_name}},
    ]

    def runtime_rows(*, explicit_server_ids=None, **_kwargs):
        if explicit_server_ids:
            return [row for row in rows if row["server_id"] in explicit_server_ids]
        return rows

    monkeypatch.setattr(custom_servers, "resolve_mihomo_runtime_proxy_rows", runtime_rows)
    monkeypatch.setattr(mihomo_config_status, "resolve_mihomo_runtime_proxy_rows", runtime_rows)
    config = {
        "proxies": [{"name": "member-a"}, {"name": "member-b"}],
        "proxy-groups": [
            {"name": group_name, "type": "fallback", "proxies": ["member-a", "member-b"]},
            {"name": fixed_group_name, "type": "fallback", "proxies": ["member-a", "member-b"]},
        ],
    }
    native_group = {"type": "fallback", "all": ["member-a", "member-b"], "now": "member-a"}
    generated_groups = [
        {"name": name, "type": "fallback", "proxies": ["member-a", "member-b"]}
        for name in (group_name, fixed_group_name)
    ]
    monkeypatch.setattr(mihomo_config_proxies, "_logical_profile_groups", lambda **_kwargs: generated_groups)

    assert native_proof._mihomo_selection_target_matches(
        {"routing": {"server_mode": "auto"}, "selection": {"active_server_id": "logical-auto"}},
        config,
        {"vpn-auto": {"now": group_name}, "vpn-global": {"now": "vpn-auto"}, group_name: native_group},
    )
    assert native_proof._mihomo_selection_target_matches(
        {"routing": {"server_mode": "fixed", "applied_fixed_server_id": "logical-fixed"}},
        config,
        {"vpn-global": {"now": fixed_group_name}, fixed_group_name: native_group},
    )
    # Missing runtime group, incomplete membership, or an untrusted group shape
    # must not turn a durable logical ID into a native PASS.
    assert not native_proof._mihomo_selection_target_matches(
        {"routing": {"server_mode": "auto"}, "selection": {"active_server_id": "logical-auto"}},
        config, {"vpn-auto": {"now": group_name}, "vpn-global": {"now": "vpn-auto"}},
    )
    assert not native_proof._mihomo_selection_target_matches(
        {"routing": {"server_mode": "auto"}, "selection": {"active_server_id": "logical-auto"}},
        config, {"vpn-auto": {"now": group_name}, group_name: {"type": "fallback", "all": ["member-a"], "now": "member-a"}},
    )
    assert not native_proof._mihomo_selection_target_matches(
        {"routing": {"server_mode": "auto"}, "selection": {"active_server_id": "logical-auto"}},
        config,
        {"vpn-auto": {"now": group_name}, group_name: {**native_group, "now": "REJECT"}},
    )
    assert not native_proof._mihomo_selection_target_matches(
        {"routing": {"server_mode": "auto"}, "selection": {"active_server_id": "logical-auto"}},
        {**config, "proxy-groups": [{"name": group_name, "type": "fallback", "proxies": ["member-a", "missing"]}]},
        {"vpn-auto": {"now": group_name}, group_name: native_group},
    )
    assert not native_proof._mihomo_selection_target_matches(
        {"routing": {"server_mode": "auto"}, "selection": {"active_server_id": "logical-auto"}},
        {**config, "proxy-groups": [{"name": group_name, "type": "select", "proxies": ["member-a", "member-b"]}]},
        {"vpn-auto": {"now": group_name}, group_name: {"type": "select", "all": ["member-a", "member-b"], "now": "member-a"}},
    )
    rows.append({"server_id": "ambiguous-alias", "raw": {"name": group_name, "_fwrouter_runtime_name": group_name}})
    assert not native_proof._mihomo_selection_target_matches(
        {"routing": {"server_mode": "auto"}, "selection": {"active_server_id": "logical-auto"}},
        config,
        {"vpn-auto": {"now": group_name}, group_name: native_group},
    )


def test_mihomo_fixed_logical_target_rejects_colliding_runtime_group_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fwrouter_api.services import mihomo_config_proxies, mihomo_config_status

    group_name = "Shared Logical Profile [collision]"
    fixed_id = "durable-profile-a"
    fixed_row = {
        "server_id": fixed_id,
        "raw": {"name": "profile-a", "_fwrouter_runtime_name": group_name},
    }
    resolver_calls: list[set[str] | None] = []

    def explicit_target_rows(*, explicit_server_ids=None, **_kwargs):
        resolver_calls.append(set(explicit_server_ids) if explicit_server_ids is not None else None)
        return [fixed_row] if explicit_server_ids == {fixed_id} else []

    monkeypatch.setattr(mihomo_config_status, "resolve_mihomo_runtime_proxy_rows", explicit_target_rows)
    generated_profile_a = {
        "name": group_name, "type": "fallback", "proxies": ["member-a"],
    }
    generated_profile_b = {
        "name": group_name, "type": "fallback", "proxies": ["member-b"],
    }
    monkeypatch.setattr(
        mihomo_config_proxies, "_logical_profile_groups",
        lambda **_kwargs: [generated_profile_a, generated_profile_b],
    )
    config_for_profile_b = {
        "proxies": [{"name": "member-a"}, {"name": "member-b"}],
        "proxy-groups": [generated_profile_b],
    }
    native = {
        "vpn-global": {"now": group_name},
        group_name: {"type": "fallback", "all": ["member-b"], "now": "member-b"},
    }
    fixed_db = {"routing": {"server_mode": "fixed", "applied_fixed_server_id": fixed_id}}

    # The real fixed-target resolver only fetches the explicit durable ID, so
    # its single-row result cannot reveal another profile's aliased group name.
    assert not native_proof._mihomo_selection_target_matches(fixed_db, config_for_profile_b, native)
    assert resolver_calls == [{fixed_id}]

    # Duplicate names in candidate config are independently ambiguous even
    # when one generated durable profile exactly matches the winning config.
    monkeypatch.setattr(
        mihomo_config_proxies, "_logical_profile_groups", lambda **_kwargs: [generated_profile_b],
    )
    duplicate_config = {
        **config_for_profile_b,
        "proxy-groups": [generated_profile_a, generated_profile_b],
    }
    assert not native_proof._mihomo_selection_target_matches(fixed_db, duplicate_config, native)
    malformed_duplicate_config = {
        **config_for_profile_b,
        "proxy-groups": [
            generated_profile_b,
            {"name": group_name, "type": "fallback", "proxies": ["member-a", "missing"]},
        ],
    }
    assert not native_proof._mihomo_selection_target_matches(fixed_db, malformed_duplicate_config, native)

    # A logical group name colliding with a concrete proxy name is ambiguous;
    # it must not take the early concrete-target success path.
    collision_group = {"name": "member-a", "type": "fallback", "proxies": ["member-b"]}
    monkeypatch.setattr(
        mihomo_config_proxies, "_logical_profile_groups", lambda **_kwargs: [collision_group],
    )
    collision_config = {
        "proxies": [{"name": "member-a"}, {"name": "member-b"}],
        "proxy-groups": [collision_group],
    }
    collision_native = {
        "vpn-global": {"now": "member-a"},
        "member-a": {"type": "fallback", "all": ["member-b"], "now": "member-b"},
    }
    assert not native_proof._mihomo_selection_target_matches(fixed_db, collision_config, collision_native)
    assert resolver_calls == [{fixed_id}, {fixed_id}, {fixed_id}, {fixed_id}]


def test_unknown_exception_text_is_never_returned_as_public_reason() -> None:
    assert native_proof._safe_failure_reason(RuntimeError("TOKEN_SECRET_123")) == "native_readback_unavailable"
    assert native_proof._safe_failure_reason(ValueError("CORE_NATIVE_RULES_MISMATCH")) == "CORE_NATIVE_RULES_MISMATCH"


def _nft_payload(*, extra_rule: bool = False, counter: int = 1, static_element: str = "198.51.100.1",
                 dynamic_element: str = "203.0.113.1", timeout: int = 30) -> dict[str, object]:
    table = {"family": "inet", "name": "fwrouter_v2", "handle": 1}
    chains = [
        {"family": "inet", "table": "fwrouter_v2", "name": "output", "type": "filter", "hook": "output",
         "prio": 0, "policy": "drop", "handle": 2},
    ]
    sets = [
        {"family": "inet", "table": "fwrouter_v2", "name": "static_allow", "type": "ipv4_addr", "handle": 3},
        {"family": "inet", "table": "fwrouter_v2", "name": "dns_direct_ipv4", "type": "ipv4_addr",
         "flags": ["timeout"], "timeout": timeout, "handle": 4},
    ]
    elements = [
        {"family": "inet", "table": "fwrouter_v2", "name": "static_allow", "elem": [static_element]},
        {"family": "inet", "table": "fwrouter_v2", "name": "dns_direct_ipv4", "elem": [dynamic_element]},
    ]
    rules = [{"family": "inet", "table": "fwrouter_v2", "chain": "output", "handle": 5,
              "expr": [{"counter": {"packets": counter, "bytes": counter * 10}}, {"accept": None}]}]
    if extra_rule:
        rules.append({"family": "inet", "table": "fwrouter_v2", "chain": "output", "handle": 6,
                      "expr": [{"accept": None}]})
    return {"nftables": [{"metainfo": {"version": "1.0"}}, {"table": table},
                         *[{"chain": row} for row in chains], *[{"set": row} for row in sets],
                         *[{"element": row} for row in elements], *[{"rule": row} for row in rules]]}


def test_core_native_semantic_digest_covers_rules_and_static_objects() -> None:
    baseline = native_proof.canonical_nft_table_sha256(_nft_payload())
    assert native_proof.canonical_nft_table_sha256(_nft_payload(counter=99, dynamic_element="192.0.2.5")) == baseline
    assert native_proof.canonical_nft_table_sha256(_nft_payload(extra_rule=True)) != baseline
    assert native_proof.canonical_nft_table_sha256(_nft_payload(static_element="198.51.100.2")) != baseline
    assert native_proof.canonical_nft_table_sha256(_nft_payload(timeout=31)) != baseline


def test_core_native_semantic_digest_preserves_rule_order_and_rejects_unknown_objects() -> None:
    payload = _nft_payload(extra_rule=True)
    rules = [entry["rule"] for entry in payload["nftables"] if "rule" in entry]
    payload["nftables"] = [entry for entry in payload["nftables"] if "rule" not in entry]
    payload["nftables"].extend({"rule": row} for row in reversed(rules))
    assert native_proof.canonical_nft_table_sha256(payload) != native_proof.canonical_nft_table_sha256(_nft_payload(extra_rule=True))
    with pytest.raises(ValueError, match="CORE_NFT_READBACK_INVALID"):
        native_proof.canonical_nft_table({"nftables": [{"counter": {}}]})


def test_core_native_named_counter_semantics_ignore_only_volatile_fields() -> None:
    payload = _nft_payload()
    counter = {"family": "inet", "table": "fwrouter_v2", "name": "traffic_tx",
               "packets": 12, "bytes": 345, "handle": 99}
    payload["nftables"].append({"counter": counter})
    baseline = native_proof.canonical_nft_table_sha256(payload)

    changed_volatile_values = _nft_payload()
    changed_volatile_values["nftables"].append({"counter": {
        **counter, "packets": 999, "bytes": 123456, "handle": 101,
    }})
    assert native_proof.canonical_nft_table_sha256(changed_volatile_values) == baseline

    changed_name = _nft_payload()
    changed_name["nftables"].append({"counter": {**counter, "name": "traffic_rx"}})
    assert native_proof.canonical_nft_table_sha256(changed_name) != baseline

    changed_stable_field = _nft_payload()
    changed_stable_field["nftables"].append({"counter": {**counter, "comment": "owned"}})
    assert native_proof.canonical_nft_table_sha256(changed_stable_field) != baseline


def test_core_native_receipt_readback_uses_named_counter_canonicalizer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _nft_payload()
    payload["nftables"].append({"counter": {
        "family": "inet", "table": "fwrouter_v2", "name": "traffic_tx",
        "packets": 12, "bytes": 345, "handle": 99,
    }})
    monkeypatch.setattr(native_proof.subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(
        returncode=0, stdout=json.dumps(payload),
    ))
    assert native_proof.read_live_nft_semantic_sha256() == native_proof.canonical_nft_table_sha256(payload)


@pytest.mark.parametrize("counter", [
    {"family": "inet", "table": "fwrouter_v2", "packets": 0, "bytes": 0},
    {"family": "inet", "table": "fwrouter_v2", "name": "bad", "packets": True, "bytes": 0},
    {"family": "inet", "table": "fwrouter_v2", "name": "bad", "packets": -1, "bytes": 0},
    {"family": "inet", "table": "fwrouter_v2", "name": "bad", "packets": 0, "bytes": "0"},
])
def test_core_native_named_counter_rejects_malformed_objects(counter: dict[str, object]) -> None:
    payload = _nft_payload()
    payload["nftables"].append({"counter": counter})
    with pytest.raises(ValueError, match="CORE_NFT_READBACK_INVALID"):
        native_proof.canonical_nft_table(payload)


def test_core_native_named_counter_rejects_duplicates_and_wrong_owner() -> None:
    counter = {"family": "inet", "table": "fwrouter_v2", "name": "traffic_tx",
               "packets": 0, "bytes": 0}
    duplicate = _nft_payload()
    duplicate["nftables"].extend(({"counter": counter}, {"counter": dict(counter)}))
    with pytest.raises(ValueError, match="CORE_NFT_READBACK_DUPLICATE_COUNTER"):
        native_proof.canonical_nft_table(duplicate)

    for wrong_counter in (
        {**counter, "family": "ip"},
        {key: value for key, value in counter.items() if key != "family"},
        {key: value for key, value in counter.items() if key != "table"},
    ):
        wrong_owner = _nft_payload()
        wrong_owner["nftables"].append({"counter": wrong_counter})
        with pytest.raises(ValueError, match="CORE_NFT_READBACK_UNEXPECTED_TABLE"):
            native_proof.canonical_nft_table(wrong_owner)

    ambiguous = _nft_payload()
    ambiguous["nftables"].append({"counter": counter, "rule": {}})
    with pytest.raises(ValueError, match="CORE_NFT_READBACK_UNSUPPORTED_OBJECT"):
        native_proof.canonical_nft_table(ambiguous)

    unknown_companion = _nft_payload()
    unknown_companion["nftables"].append({"counter": counter, "flowtable": {}})
    with pytest.raises(ValueError, match="CORE_NFT_READBACK_UNSUPPORTED_OBJECT"):
        native_proof.canonical_nft_table(unknown_companion)

    counter_metainfo = _nft_payload()
    counter_metainfo["nftables"].append({"counter": counter, "metainfo": {}})
    with pytest.raises(ValueError, match="CORE_NFT_READBACK_UNSUPPORTED_OBJECT"):
        native_proof.canonical_nft_table(counter_metainfo)


def test_core_ip_receipt_hash_covers_priority_rules_and_full_table_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    fixtures = {
        "-4": {
            "rules": [
                {"priority": 100, "fwmark": "0x100", "table": 100},
                {"priority": 101, "fwmark": "0x102", "table": 100},
            ],
            "routes": [{"type": "local", "dst": "default", "dev": "lo", "table": 100}],
        },
        "-6": {
            "rules": [
                {"priority": 100, "fwmark": "0x100", "table": 100},
                {"priority": 101, "fwmark": "0x102", "table": 100},
            ],
            "routes": [{"type": "local", "dst": "::/0", "dev": "lo", "table": 100}],
        },
    }

    def install(monkeypatch, facts):
        monkeypatch.setattr(native_proof, "_ipv6_host_facts", lambda: {
            "mode": "active", "interfaces": ["lo", "eth0"],
            "disable_ipv6": {"all": 0, "default": 0, "lo": 0, "eth0": 0},
            "addresses": [], "routes": [],
        })

        def run(command, **_kwargs):
            assert command[1] == "-N", "iproute2 table aliases must be disabled for semantic comparisons"
            family = command[3]
            payload = facts[family]["rules"] if "rule" in command else facts[family]["routes"]
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload), stderr="")
        monkeypatch.setattr(native_proof.subprocess, "run", run)

    install(monkeypatch, fixtures)
    baseline = native_proof._core_ip_facts()["semantic_sha256"]
    for local_type in ("local", "2"):
        local_route_facts = json.loads(json.dumps(fixtures))
        local_route_facts["-4"]["routes"][0]["type"] = local_type
        local_route_facts["-6"]["routes"][0]["type"] = local_type
        install(monkeypatch, local_route_facts)
        assert native_proof._core_ip_facts()["checks"]["-4_local_default_route"] is True
        assert native_proof._core_ip_facts()["checks"]["-6_local_default_route"] is True
    unicast_route_facts = json.loads(json.dumps(fixtures))
    unicast_route_facts["-4"]["routes"][0]["type"] = "1"
    unicast_route_facts["-6"]["routes"][0]["type"] = "unicast"
    install(monkeypatch, unicast_route_facts)
    with pytest.raises(ValueError, match="CORE_IP_POLICY_MISMATCH"):
        native_proof._core_ip_facts()
    install(monkeypatch, fixtures)
    default_spelling = json.loads(json.dumps(fixtures))
    default_spelling["-6"]["routes"][0]["dst"] = "default"
    install(monkeypatch, default_spelling)
    assert native_proof._core_ip_facts()["checks"]["-6_local_default_route"] is True
    assert native_proof._core_ip_facts()["semantic_sha256"] != baseline
    higher_priority = json.loads(json.dumps(fixtures))
    higher_priority["-4"]["rules"].insert(0, {"priority": 5, "to": "0.0.0.0/0", "table": "main"})
    install(monkeypatch, higher_priority)
    assert native_proof._core_ip_facts()["semantic_sha256"] != baseline
    changed_route = json.loads(json.dumps(fixtures))
    changed_route["-6"]["routes"][0]["dev"] = "eth0"
    install(monkeypatch, changed_route)
    with pytest.raises(ValueError, match="CORE_IP_POLICY_MISMATCH"):
        native_proof._core_ip_facts()


def test_core_ip_facts_accepts_proven_disabled_ipv6_only_without_relaxing_mark_rules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixtures = {
        "-4": {
            "rules": [{"priority": 100, "fwmark": "0x100", "table": 100},
                      {"priority": 101, "fwmark": "0x102", "table": 100}],
            "routes": [{"type": "local", "dst": "default", "dev": "lo", "table": 100}],
        },
        "-6": {
            "rules": [{"priority": 100, "fwmark": "0x100", "table": 100},
                      {"priority": 101, "fwmark": "0x102", "table": 100}],
            "routes": [],
        },
    }
    monkeypatch.setattr(native_proof, "_ipv6_host_facts", lambda: {
        "mode": "disabled", "interfaces": ["lo"], "interface_indexes": {"lo": 1},
        "disable_ipv6": {"all": 1, "default": 1, "lo": 1},
        "addresses": [], "routes": [],
    })

    def run(command, **_kwargs):
        family = command[3]
        payload = fixtures[family]["rules"] if "rule" in command else fixtures[family]["routes"]
        return subprocess.CompletedProcess(command, 0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(native_proof.subprocess, "run", run)
    result = native_proof._core_ip_facts()
    assert result["checks"]["-6_mark_rule"] is True
    assert result["checks"]["-6_full_vpn_mark_rule"] is True
    assert result["checks"]["-6_local_default_route"] is False
    assert result["checks"]["-6_disabled_mode_proven"] is True
    assert result["checks"]["-6_policy_enforced"] is True
    baseline_semantics = result["semantic_sha256"]

    monkeypatch.setattr(native_proof, "_ipv6_host_facts", lambda: {
        "mode": "disabled", "interfaces": ["eth0", "lo"],
        "interface_indexes": {"eth0": 2, "lo": 1},
        "disable_ipv6": {"all": 1, "default": 1, "eth0": 1, "lo": 1},
        "addresses": [], "routes": [],
    })
    assert native_proof._core_ip_facts()["semantic_sha256"] != baseline_semantics

    fixtures["-6"]["rules"] = fixtures["-6"]["rules"][:1]
    with pytest.raises(ValueError, match="CORE_IP_POLICY_MISMATCH"):
        native_proof._core_ip_facts()


def test_ipv6_host_disabled_mode_requires_exact_stable_inventory_and_empty_network_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    conf = tmp_path / "conf"
    for name in ("all", "default", "lo", "eth0"):
        directory = conf / name
        directory.mkdir(parents=True)
        (directory / "disable_ipv6").write_text("1\n", encoding="ascii")
    monkeypatch.setattr(native_proof, "_IPV6_SYSCTL_CONF_PATH", conf)
    _fake_root_owned_sysctl_tree(monkeypatch)
    current = {"addresses": [], "routes": [],
               "links": [{"ifname": "lo", "ifindex": 1}, {"ifname": "eth0", "ifindex": 2}]}

    def run(command):
        if command[1:4] == ["-j", "link", "show"]:
            payload = current["links"]
        elif "address" in command:
            payload = current["addresses"]
        elif "route" in command:
            payload = current["routes"]
        else:
            raise AssertionError(command)
        return payload

    monkeypatch.setattr(native_proof, "_run_bounded_ip_json", run)
    baseline = native_proof._ipv6_host_facts()
    assert baseline["mode"] == "disabled"
    assert baseline["interfaces"] == ["eth0", "lo"]

    # Any live address or route contradicts an all-interfaces-disabled claim.
    current["addresses"] = [{"ifname": "eth0", "addr_info": [{"family": "inet6", "local": "fe80::1"}]}]
    with pytest.raises(ValueError, match="CORE_IP_POLICY_MISMATCH"):
        native_proof._ipv6_host_facts()
    current["addresses"] = []
    current["routes"] = [{"dst": "default", "type": "unicast", "dev": "eth0"}]
    with pytest.raises(ValueError, match="CORE_IP_POLICY_MISMATCH"):
        native_proof._ipv6_host_facts()
    current["routes"] = []

    (conf / "eth0" / "disable_ipv6").write_text("0\n", encoding="ascii")
    active = native_proof._ipv6_host_facts()
    assert active["mode"] == "active"
    assert active != baseline
    current["links"] = [{"ifname": "lo", "ifindex": 1}, {"ifname": "eth0", "ifindex": 3}]
    replacement = native_proof._ipv6_host_facts()
    assert replacement["interface_indexes"]["eth0"] == 3
    assert replacement != active
    current["links"] = [{"ifname": "lo", "ifindex": 1}, {"ifname": "eth0", "ifindex": 2}]

    current["addresses"] = [{"ifname": "eth0", "addr_info": [{
        "family": "inet6", "local": "2001:db8::1", "valid_life_time": 100,
        "preferred_life_time": 80,
    }]}]
    current["routes"] = [{"dst": "default", "type": "unicast", "dev": "eth0", "expires": 80}]
    first_timed = native_proof._ipv6_host_facts()
    current["addresses"][0]["addr_info"][0]["valid_life_time"] = 90
    current["addresses"][0]["addr_info"][0]["preferred_life_time"] = 70
    current["routes"][0]["expires"] = 70
    second_timed = native_proof._ipv6_host_facts()
    assert first_timed == second_timed

    (conf / "eth0" / "disable_ipv6").write_text("2\n", encoding="ascii")
    with pytest.raises(ValueError, match="CORE_IP_READBACK_INVALID"):
        native_proof._ipv6_host_facts()


@pytest.mark.parametrize("bad_inventory", [
    [{"ifname": "eth0", "ifindex": 2}],
    [{"ifname": "lo", "ifindex": 1}],
    [{"ifname": "lo", "ifindex": 1}, {"ifname": "eth0", "ifindex": 2},
     {"ifname": "eth0", "ifindex": 3}],
    [{"ifname": "lo", "ifindex": 1}, {"ifname": "bad/name", "ifindex": 2}],
    [{"ifname": "lo", "ifindex": 1}, {"ifname": "eth0", "ifindex": 1}],
])
def test_ipv6_host_facts_reject_incomplete_duplicate_or_malformed_interfaces(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, bad_inventory: list[dict[str, str]],
) -> None:
    conf = tmp_path / "conf"
    for name in ("all", "default", "lo", "eth0"):
        directory = conf / name
        directory.mkdir(parents=True)
        (directory / "disable_ipv6").write_text("1\n", encoding="ascii")
    monkeypatch.setattr(native_proof, "_IPV6_SYSCTL_CONF_PATH", conf)
    _fake_root_owned_sysctl_tree(monkeypatch)

    def run(command):
        if "link" in command:
            payload = bad_inventory
        else:
            payload = []
        return payload

    monkeypatch.setattr(native_proof, "_run_bounded_ip_json", run)
    with pytest.raises(ValueError, match="CORE_IP_READBACK_INVALID"):
        native_proof._ipv6_host_facts()


def test_ipv6_host_facts_rejects_symlinked_sysctl_tree(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(target, target_is_directory=True)
    monkeypatch.setattr(native_proof, "_IPV6_SYSCTL_CONF_PATH", linked)
    _fake_root_owned_sysctl_tree(monkeypatch)

    def run(command):
        payload = [{"ifname": "lo"}] if "link" in command else []
        return payload

    monkeypatch.setattr(native_proof, "_run_bounded_ip_json", run)
    with pytest.raises(ValueError, match="CORE_IP_READBACK_INVALID"):
        native_proof._ipv6_host_facts()


def test_optional_core_receipt_runtime_error_does_not_fail_existing_promotion(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from fwrouter_api.services.dataplane_nft_artifacts import promote_last_good

    candidate = tmp_path / "candidate.nft"
    candidate.write_text("table inet fwrouter_v2 {}\n", encoding="utf-8")
    paths = {
        "candidate_nft_path": str(candidate),
        "current_nft_path": str(tmp_path / "current.nft"),
        "applied_nft_path": str(tmp_path / "applied.nft"),
        "last_good_nft_path": str(tmp_path / "last-good.nft"),
        "current_manifest_path": str(tmp_path / "current-manifest.json"),
        "applied_manifest_path": str(tmp_path / "applied-manifest.json"),
        "last_good_manifest_path": str(tmp_path / "last-good-manifest.json"),
    }

    def fail_with_runtime_error(**_kwargs: object) -> bool:
        raise RuntimeError("optional receipt failure")

    monkeypatch.setitem(
        sys.modules,
        "fwrouter_api.services.native_proof",
        SimpleNamespace(write_core_native_apply_receipt=fail_with_runtime_error),
    )
    promote_last_good(
        manifest={"plan_id": "fixture-apply", "routing_global_state": {"applied_mode": "direct"}},
        artifact_paths=paths,
        core_intent_revision=1,
    )

    assert Path(paths["applied_nft_path"]).read_text(encoding="utf-8") == candidate.read_text(encoding="utf-8")
    assert Path(paths["last_good_manifest_path"]).is_file()

def test_sqlite_generation_reader_keeps_legacy_marker_but_has_no_proof() -> None:
    from fwrouter_api.services.subscription import _read_subscription_native_generation

    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript("""
        CREATE TABLE settings (key TEXT PRIMARY KEY, value_json TEXT NOT NULL);
        CREATE TABLE servers (server_id TEXT, server_name TEXT, provider_name TEXT, country_code TEXT,
                              region TEXT, raw_json TEXT, inventory_state TEXT);
        CREATE TABLE subscription_server_memberships (source_id TEXT, server_id TEXT,
                              entry_identity_hash TEXT, parser_format TEXT, is_active INTEGER);
    """)
    from fwrouter_api.services.subscription import _subscription_inventory_digest, SUBSCRIPTION_NATIVE_GENERATION_KEY

    digest, _ = _subscription_inventory_digest(connection)
    marker = {"schema": 1, "status": "verified", "candidate_digest": digest, "candidate_revision": 4,
              "verified_digest": digest, "verified_revision": 4}
    connection.execute("INSERT INTO settings VALUES (?, ?)", (SUBSCRIPTION_NATIVE_GENERATION_KEY, json.dumps(marker)))
    state = _read_subscription_native_generation(connection)
    assert state["status"] == "verified"
    assert state["native_proof"] is None


@pytest.mark.parametrize("capture_status", ["INCOMPLETE", "PASS"])
def test_generation_promotion_preserves_contract_and_persists_only_complete_proof(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capture_status: str,
) -> None:
    from fwrouter_api.services import subscription

    db_path = tmp_path / "generation.db"
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    connection.executescript("""
        CREATE TABLE settings (key TEXT PRIMARY KEY, value_json TEXT NOT NULL, updated_at TEXT);
        CREATE TABLE servers (server_id TEXT, server_name TEXT, provider_name TEXT, country_code TEXT,
                              region TEXT, raw_json TEXT, inventory_state TEXT);
        CREATE TABLE subscription_server_memberships (source_id TEXT, server_id TEXT,
                              entry_identity_hash TEXT, parser_format TEXT, is_active INTEGER);
        CREATE TABLE apply_versions (apply_id TEXT, status TEXT, core_intent_revision INTEGER, created_at TEXT);
    """)
    digest, _ = subscription._subscription_inventory_digest(connection)
    connection.execute("INSERT INTO settings(key, value_json) VALUES (?, ?)", (subscription.SUBSCRIPTION_NATIVE_GENERATION_KEY,
        json.dumps({"schema": 1, "status": "pending", "candidate_digest": digest, "candidate_revision": 4,
                    "verified_digest": None, "verified_revision": None})))
    connection.execute("INSERT INTO apply_versions VALUES (?, 'applied', 5, '2026-10-23T00:00:00Z')",
                       ("private-apply-id",))
    connection.commit()

    @contextmanager
    def session():
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise

    monkeypatch.setattr(subscription, "db_session", session)
    monkeypatch.setattr(subscription, "xray_writer_guard_is_held", lambda: True)
    from fwrouter_api.services import mihomo_reconcile_fingerprint
    monkeypatch.setattr(mihomo_reconcile_fingerprint, "current_mihomo_input_fingerprint", lambda _routing: {"hash": "a" * 64})
    monkeypatch.setattr(mihomo_reconcile_fingerprint, "mihomo_udp_policy_receipt_matches", lambda: True)
    monkeypatch.setattr(mihomo_reconcile_fingerprint, "mihomo_input_unchanged", lambda _fingerprint: True)
    proof = _recorded_native_proof(inventory_digest=digest)
    monkeypatch.setattr(native_proof, "capture_native_generation_proof", lambda **_kw: (
        {"status": "PASS", "proof": proof} if capture_status == "PASS" else {"status": "INCOMPLETE"}
    ))
    monkeypatch.setattr("fwrouter_api.services.core_intent.read_core_intent_revision", lambda _connection: 5)
    monkeypatch.setattr("fwrouter_api.services.vpn_auto_selection_state.read_selection_fence",
                        lambda _connection: {"revision": 8})
    admission = subscription._SubscriptionInventoryAdmission(digest, 4, subscription._SUBSCRIPTION_ADMISSION_ISSUER)

    result = subscription._promote_subscription_inventory_generation(admission)

    marker = json.loads(connection.execute("SELECT value_json FROM settings WHERE key=?",
                                           (subscription.SUBSCRIPTION_NATIVE_GENERATION_KEY,)).fetchone()[0])
    assert result["ok"] is True
    assert marker["status"] == "verified"
    if capture_status == "PASS":
        assert marker["native_proof"] == proof
    else:
        assert "native_proof" not in marker
    connection.close()


def test_owner_capture_promotion_marker_and_readonly_collector_share_exact_proof(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from fwrouter_api.services import subscription

    db_path = tmp_path / "generation-proof.db"
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    connection.executescript("""
        CREATE TABLE settings (key TEXT PRIMARY KEY, value_json TEXT NOT NULL, updated_at TEXT);
        CREATE TABLE servers (server_id TEXT, server_name TEXT, provider_name TEXT, country_code TEXT,
                              region TEXT, raw_json TEXT, inventory_state TEXT);
        CREATE TABLE subscription_server_memberships (source_id TEXT, server_id TEXT,
                              entry_identity_hash TEXT, parser_format TEXT, is_active INTEGER);
        CREATE TABLE apply_versions (apply_id TEXT, status TEXT, core_intent_revision INTEGER, created_at TEXT);
    """)
    digest, count = subscription._subscription_inventory_digest(connection)
    connection.execute("INSERT INTO settings(key, value_json) VALUES (?, ?)", (subscription.SUBSCRIPTION_NATIVE_GENERATION_KEY,
        json.dumps({"schema": 1, "status": "pending", "candidate_digest": digest, "candidate_revision": 4,
                    "verified_digest": None, "verified_revision": None})))
    connection.execute("INSERT INTO apply_versions VALUES (?, 'applied', 5, '2026-10-23T00:00:00Z')",
                       ("private-apply-id",))
    connection.commit()

    @contextmanager
    def session():
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise

    monkeypatch.setattr(subscription, "db_session", session)
    monkeypatch.setattr(subscription, "xray_writer_guard_is_held", lambda: True)
    from fwrouter_api.services import mihomo_reconcile_fingerprint
    monkeypatch.setattr(mihomo_reconcile_fingerprint, "current_mihomo_input_fingerprint", lambda _routing: {"hash": "a" * 64})
    monkeypatch.setattr(mihomo_reconcile_fingerprint, "mihomo_udp_policy_receipt_matches", lambda: True)
    monkeypatch.setattr(mihomo_reconcile_fingerprint, "mihomo_input_unchanged", lambda _fingerprint: True)
    monkeypatch.setattr("fwrouter_api.services.core_intent.read_core_intent_revision", lambda _connection: 5)
    monkeypatch.setattr("fwrouter_api.services.vpn_auto_selection_state.read_selection_fence",
                        lambda _connection: {"revision": 8})

    recorded = _recorded_native_proof(inventory_digest=digest)
    xray_evidence = dict(recorded["xray"], config_matches_mounted=True, runtime_incarnation_stable=True,
                         identity_count=0, bindings_match=True, modes_match=True)
    mihomo_evidence = dict(recorded["mihomo"], config_stable=True, runtime_incarnation_stable=True,
                           selector_count=0, native_rule_count=0, native_top_level_rules_match=True,
                           selector_membership_matches=True, guarded_rules_match=True,
                           selection_target_matches_durable_state=True)
    core_evidence = dict(recorded["core"], apply_owned_by_latest_revision=True, applied_manifest_stable=True,
                         applied_nft_stable=True, native_rule_semantics_match_owner_receipt=True)
    owner_snapshot = {
        "schema": {"ok": True}, "integrity": ["ok"], "foreign_key_violations": 0,
        "inventory_digest": digest, "inventory_count": count,
        "generation": {"status": "pending", "inventory_digest": digest,
                       "candidate_digest": digest, "candidate_revision": 4},
    }
    monkeypatch.setattr(native_proof, "backend_manifest_sha256", lambda _root: "a" * 64)
    monkeypatch.setattr(native_proof, "_db_snapshot", lambda _path: dict(owner_snapshot))
    monkeypatch.setattr(native_proof, "_checkpoint_snapshot", lambda: {"present": False, "valid": True, "sha256": None})
    from fwrouter_api.db import connection as db_connection

    def readonly_reader(evidence):
        def read(_db):
            assert db_connection._READ_ONLY_DB_SESSIONS.get() is True
            return dict(evidence)
        return read

    monkeypatch.setattr(native_proof, "_xray_readback", readonly_reader(xray_evidence))
    monkeypatch.setattr(native_proof, "_mihomo_readback", readonly_reader(mihomo_evidence))
    monkeypatch.setattr(native_proof, "_core_readback", readonly_reader(core_evidence))
    monkeypatch.setattr("fwrouter_api.db.connection.get_db_path", lambda: db_path)
    from fwrouter_api.adapters import xray_common
    monkeypatch.setattr(xray_common, "xray_writer_guard_is_held", lambda: True)
    admission = subscription._SubscriptionInventoryAdmission(digest, 4, subscription._SUBSCRIPTION_ADMISSION_ISSUER)

    promoted = subscription._promote_subscription_inventory_generation(admission)
    assert db_connection._READ_ONLY_DB_SESSIONS.get() is False
    marker = json.loads(connection.execute("SELECT value_json FROM settings WHERE key=?",
                                           (subscription.SUBSCRIPTION_NATIVE_GENERATION_KEY,)).fetchone()[0])
    assert promoted["ok"] is True
    assert marker["status"] == "verified"
    assert marker["native_proof"]["inventory_digest"] == digest
    assert marker["native_proof"]["generation_revision"] == 4

    verified_db = _proof_ready_db()
    verified_db["inventory_digest"] = digest
    verified_db["inventory_count"] = count
    verified_db["generation"].update({
        "candidate_digest": digest, "verified_digest": digest,
        "native_proof": marker["native_proof"],
    })
    monkeypatch.setattr(native_proof, "_db_snapshot", lambda _path: dict(verified_db))
    expected = "a" * 64
    from contextlib import contextmanager as contextmanager_factory

    @contextmanager_factory
    def read_guard():
        yield True

    monkeypatch.setattr(xray_common, "xray_writer_read_guard", read_guard)
    monkeypatch.setattr(native_proof, "_xray_readback", readonly_reader(xray_evidence))
    monkeypatch.setattr(native_proof, "_mihomo_readback", readonly_reader(mihomo_evidence))
    monkeypatch.setattr(native_proof, "_core_readback", readonly_reader(core_evidence))
    result = native_proof.collect_native_proof(
        scope="ui", expected_backend_manifest_sha256=expected,
        expected_xray_image_digest=XRAY_IMAGE_PIN, expected_mihomo_image_digest=MIHOMO_IMAGE_PIN,
    )
    assert result["status"] == "PASS", result
    assert db_connection._READ_ONLY_DB_SESSIONS.get() is False
    connection.close()


def test_xray_read_guard_fails_closed_without_creating_lock_or_directory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from fwrouter_api.adapters import xray_common

    run_dir = tmp_path / "missing"
    monkeypatch.setattr(xray_common, "get_settings", lambda: SimpleNamespace(paths=SimpleNamespace(run_dir=run_dir)))

    with xray_common.xray_writer_read_guard() as acquired:
        assert acquired is False

    assert not run_dir.exists()


def test_xray_read_guard_is_nonblocking_for_writer_and_rejects_same_thread(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from fwrouter_api.adapters import xray_common

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    lock_path = run_dir / "xray-writer.lock"
    lock_path.touch()
    monkeypatch.setattr(xray_common, "get_settings", lambda: SimpleNamespace(paths=SimpleNamespace(run_dir=run_dir)))

    writer_fd = os.open(lock_path, os.O_RDWR)
    import fcntl
    fcntl.flock(writer_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with xray_common.xray_writer_read_guard() as acquired:
            assert acquired is False
    finally:
        fcntl.flock(writer_fd, fcntl.LOCK_UN)
        os.close(writer_fd)

    with xray_common.xray_writer_guard():
        with xray_common.xray_writer_read_guard() as acquired:
            assert acquired is False


def test_xray_read_guard_rejects_lock_inode_replacement_and_permission_drift(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    from fwrouter_api.adapters import xray_common

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    lock_path = run_dir / "xray-writer.lock"
    lock_path.touch(mode=0o600)
    lock_path.chmod(0o600)
    monkeypatch.setattr(xray_common, "get_settings", lambda: SimpleNamespace(paths=SimpleNamespace(run_dir=run_dir)))

    with pytest.raises(RuntimeError, match="XRAY_READ_LOCK_CHANGED"):
        with xray_common.xray_writer_read_guard() as acquired:
            assert acquired is True
            lock_path.unlink()
            lock_path.touch(mode=0o600)
            lock_path.chmod(0o600)

    with pytest.raises(RuntimeError, match="XRAY_READ_LOCK_CHANGED"):
        with xray_common.xray_writer_read_guard() as acquired:
            assert acquired is True
            lock_path.chmod(0o644)


def test_default_xray_read_commands_do_not_create_docker_cli_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from fwrouter_api.adapters.xray_real import RealXrayAdapter

    state_dir = tmp_path / "docker-state"
    monkeypatch.setattr("fwrouter_api.adapters.xray_real.DOCKER_CLI_STATE_DIR", state_dir)
    seen: dict[str, object] = {}

    def run(command, **kwargs):
        seen["env"] = kwargs["env"]
        return subprocess.CompletedProcess(command, 0, stdout="container-id\n", stderr="")

    monkeypatch.setattr("fwrouter_api.adapters.xray_real.subprocess.run", run)
    adapter = RealXrayAdapter()
    response = adapter._default_runner("runtime_container_id", {})

    assert response.ok
    assert not state_dir.exists()
    assert seen["env"]["HOME"] == "/proc"
    assert "DOCKER_CONFIG" not in seen["env"]


def test_mihomo_runtime_identity_checks_container_image_pin_and_version(monkeypatch: pytest.MonkeyPatch) -> None:
    from fwrouter_api.services import subscription_pipeline

    container_id = "a" * 64
    image_digest = "sha256:" + "b" * 64
    image_reference = subscription_pipeline.MIHOMO_IMAGE
    calls: list[list[str]] = []

    def docker_readonly(arguments: list[str], timeout: float = 4.0) -> str:
        del timeout
        calls.append(arguments)
        if arguments[0:2] == ["compose", "-f"] and "ps" in arguments:
            return container_id
        if arguments[0] == "inspect":
            return f"{container_id}|{image_digest}|{image_reference}|true|2026-10-23T00:00:00Z"
        if arguments[0:2] == ["compose", "-f"] and arguments[-2:] == ["config", "--images"]:
            return image_reference
        raise AssertionError(f"unexpected read-only Docker command shape: {arguments[:2]}")

    class Adapter:
        def __init__(self, version: str) -> None:
            self.version = version

        def _get_json(self, path: str, *, timeout_seconds: float) -> dict[str, str]:
            assert path == "/version"
            assert timeout_seconds == 2.0
            return {"version": self.version}

    monkeypatch.setattr(native_proof, "_docker_readonly", docker_readonly)
    observed = native_proof._mihomo_runtime_identity(Adapter("v1.19.31"))
    assert observed["image_digest"] == image_digest
    assert observed["image_reference_sha256"] == __import__("hashlib").sha256(image_reference.encode()).hexdigest()
    assert observed["version_sha256"] == __import__("hashlib").sha256(b"v1.19.31").hexdigest()
    assert len(calls) == 3

    with pytest.raises(ValueError, match="MIHOMO_RUNTIME_VERSION_MISMATCH"):
        native_proof._mihomo_runtime_identity(Adapter("v1.19.32"))

    def wrong_image(arguments: list[str], timeout: float = 4.0) -> str:
        if arguments[0] == "inspect":
            return f"{container_id}|{image_digest}|untrusted:mutable|true|2026-10-23T00:00:00Z"
        return docker_readonly(arguments, timeout)

    monkeypatch.setattr(native_proof, "_docker_readonly", wrong_image)
    with pytest.raises(ValueError, match="MIHOMO_RUNTIME_IMAGE_REFERENCE_MISMATCH"):
        native_proof._mihomo_runtime_identity(Adapter("v1.19.31"))
