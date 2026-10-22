from __future__ import annotations

import os
from pathlib import Path
import shutil
import signal
import subprocess
import stat
import sys
import tempfile
import textwrap
import time
import unittest
from unittest import mock

def stat_mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)

ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "installer" / "ui_publication.py"

sys.path.insert(0, str(HELPER.parent))
import ui_publication


class AtomicUiPublicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="fwrouter-ui-publication-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ui = self.root / "source-ui"
        self.ui.mkdir()
        (self.ui / "index.html").write_text("candidate-v1")
        self.backend_source = self.root / "backend-source"
        self.backend_source.mkdir()
        (self.backend_source / "app.py").write_text("compatible")
        self.target_root = self.root / "target"
        self.backend_installed = self.target_root / "opt/fwrouter-api"
        self.backend_installed.mkdir(parents=True)
        shutil.copy2(self.backend_source / "app.py", self.backend_installed / "app.py")
        self.active = self.target_root / "opt/fwrouter-ui"
        self.active.mkdir()
        (self.active / "old.txt").write_text("complete prior tree")
        (self.active / "index.html").write_text("complete prior index")
        self.initial_digest, _ = ui_publication._walk_tree(self.active, owner_uid=os.geteuid(), require_index=True)

    def publish(self) -> str:
        return ui_publication.publish(self.ui, self.active, self.backend_source, self.backend_installed)

    def _actual_installer_fixture(self) -> tuple[Path, Path, Path]:
        source = self.root / "disposable-source"
        (source / "installer").mkdir(parents=True)
        for name in ("install.sh", "ui_publication.py", "controlled_backend_lifecycle.py",
                     "component_install_lock.py", "obsolete-backend-paths.txt"):
            shutil.copy2(ROOT / "installer" / name, source / "installer" / name)
        (source / "backend").mkdir()
        (source / "backend" / "source.py").write_text("compatible backend source\n")
        ui = source / "ui"
        ui.mkdir()
        (ui / "index.html").write_text("new immutable ui\n")
        with (ui / "large-asset.bin").open("wb") as stream:
            stream.truncate(64 * 1024 * 1024)
        target = self.root / "actual-installer-target"
        installed_backend = target / "opt/fwrouter-api"
        installed_backend.parent.mkdir(parents=True)
        shutil.copytree(source / "backend", installed_backend)
        active = target / "opt/fwrouter-ui"
        active.mkdir()
        (active / "index.html").write_text("old complete ui\n")
        return source, target, active

    def _run_real_installer_until(self, predicate, signum: int) -> tuple[subprocess.Popen, bytes, bytes]:
        source, target, _active = self._actual_installer_fixture()
        proc = subprocess.Popen(
            [str(source / "installer/install.sh"), "--deploy", "--component", "ui", "--target", str(target)],
            cwd=source, text=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
        )
        deadline = __import__("time").monotonic() + 30
        observed = False
        while proc.poll() is None and __import__("time").monotonic() < deadline:
            if predicate(target):
                observed = True
                os.killpg(proc.pid, signum)
                break
            __import__("time").sleep(0.001)
        if not observed and proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
        stdout, stderr = proc.communicate(timeout=20)
        self.assertTrue(observed, f"installer did not reach interruption barrier: {stderr.decode(errors='replace')}")
        return proc, stdout, stderr

    def test_installer_component_ui_publishes_atomically(self) -> None:
        shutil.rmtree(self.backend_installed)
        seed = subprocess.run(
            [str(ROOT / "installer/install.sh"), "--deploy", "--component", "backend", "--target", str(self.target_root)],
            cwd=ROOT, text=True, capture_output=True, check=False,
        )
        self.assertEqual(seed.returncode, 0, seed.stderr)
        result = subprocess.run(
            [str(ROOT / "installer/install.sh"), "--deploy", "--component", "ui", "--target", str(self.target_root)],
            cwd=ROOT, text=True, capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.active.is_symlink())
        self.assertEqual((self.active / "index.html").read_bytes(), (ROOT / "ui/index.html").read_bytes())
        self.assertTrue((self.active.parent / ".fwrouter-ui-releases" / f"release-{self.initial_digest}").is_dir())

    def test_installer_all_installs_backend_before_compatible_ui(self) -> None:
        shutil.rmtree(self.target_root)
        result = subprocess.run(
            [str(ROOT / "installer/install.sh"), "--deploy", "--all", "--target", str(self.target_root)],
            cwd=ROOT, text=True, capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.active.is_symlink())
        self.assertTrue((self.target_root / "opt/fwrouter-api/fwrouter_api").is_dir())

    def test_ui_release_switch_does_not_change_native_proof_backend_identity(self) -> None:
        """The UI publisher owns UI integrity; missing native owner proof stays INCOMPLETE."""
        backend_root = ROOT / "backend"
        sys.path.insert(0, str(backend_root))
        try:
            from fwrouter_api.db import connection as db_connection
            from fwrouter_api.services import native_proof
        finally:
            sys.path.remove(str(backend_root))

        from contextlib import contextmanager

        @contextmanager
        def read_guard():
            yield True

        proof_db = {
            "schema": {"ok": True, "expected_schema_version": 27, "actual_schema_version": 27},
            "integrity": ["ok"],
            "foreign_key_violations": 0,
            "inventory_digest": "synthetic-inventory-digest",
            "inventory_count": 1,
            "generation": {
                "status": "verified",
                "candidate_digest": "synthetic-inventory-digest",
                "verified_digest": "synthetic-inventory-digest",
                "candidate_revision": 1,
                "verified_revision": 1,
            },
            "selection": {"revision": 1},
            "core_revision": 1,
            "latest_apply": {"status": "applied"},
            "routing": {"apply_state": "clean"},
            "modules": [],
            "active_jobs": 0,
        }
        expected_backend_sha = native_proof.backend_manifest_sha256(native_proof._backend_root())
        self.assertEqual(native_proof._backend_root(), backend_root)
        self.assertNotEqual(native_proof._backend_root(), self.active)
        before_backend_sha = native_proof.backend_manifest_sha256(backend_root)

        with mock.patch.object(native_proof, "_db_snapshot", return_value=proof_db), \
                mock.patch.object(native_proof, "_checkpoint_snapshot",
                                  return_value={"present": False, "valid": True, "sha256": None}), \
                mock.patch.object(db_connection, "get_db_path", return_value=self.root / "unused.db"), \
                mock.patch("fwrouter_api.adapters.xray_common.xray_writer_read_guard", read_guard), \
                mock.patch.object(native_proof, "_xray_readback", side_effect=AssertionError("receipt gate must stop before runtime readback")), \
                mock.patch.object(native_proof, "_mihomo_readback", side_effect=AssertionError("receipt gate must stop before runtime readback")), \
                mock.patch.object(native_proof, "_core_readback", side_effect=AssertionError("receipt gate must stop before runtime readback")):
            before = native_proof.collect_native_proof(
                scope="ui", expected_backend_manifest_sha256=expected_backend_sha,
                expected_xray_image_digest="sha256:" + "1" * 64,
                expected_mihomo_image_digest="sha256:" + "2" * 64,
            )
            initial_ui_digest, _ = ui_publication._walk_tree(self.active, owner_uid=os.geteuid(), require_index=True)
            first_publication = self.publish()
            first_release = ui_publication._managed_release(
                self.active, self.active.parent / ".fwrouter-ui-releases", os.geteuid(),
            )
            self.assertIsNotNone(first_release)
            first_ui_digest, _ = ui_publication._walk_tree(first_release, owner_uid=os.geteuid(), require_index=True)
            after_initial_pointer = native_proof.collect_native_proof(
                scope="ui", expected_backend_manifest_sha256=expected_backend_sha,
                expected_xray_image_digest="sha256:" + "1" * 64,
                expected_mihomo_image_digest="sha256:" + "2" * 64,
            )

            (self.ui / "index.html").write_text("candidate-v2\n", encoding="utf-8")
            second_publication = self.publish()
            active_release = ui_publication._managed_release(
                self.active, self.active.parent / ".fwrouter-ui-releases", os.geteuid(),
            )
            self.assertIsNotNone(active_release)
            second_ui_digest, _ = ui_publication._walk_tree(active_release, owner_uid=os.geteuid(), require_index=True)
            after = native_proof.collect_native_proof(
                scope="ui", expected_backend_manifest_sha256=expected_backend_sha,
                expected_xray_image_digest="sha256:" + "1" * 64,
                expected_mihomo_image_digest="sha256:" + "2" * 64,
            )

        after_backend_sha = native_proof.backend_manifest_sha256(backend_root)
        self.assertNotEqual(initial_ui_digest, first_ui_digest)
        self.assertNotEqual(first_ui_digest, second_ui_digest)
        self.assertEqual((self.active / "index.html").read_text(encoding="utf-8"), "candidate-v2\n")
        self.assertTrue(first_publication.startswith("Published UI release "))
        self.assertTrue(second_publication.startswith("Published UI release "))
        releases = self.active.parent / ".fwrouter-ui-releases"
        for retained_digest in (initial_ui_digest, first_ui_digest):
            retained = releases / f"release-{retained_digest}"
            self.assertEqual(ui_publication._release_digest(retained, os.geteuid()), retained_digest)
        self.assertEqual(before_backend_sha, after_backend_sha)
        self.assertEqual(before["source_identity"], after["source_identity"])
        self.assertEqual(before["source_identity"], after_initial_pointer["source_identity"])
        self.assertEqual(before["source_identity"]["sha256"], expected_backend_sha)
        self.assertEqual(before["status"], "INCOMPLETE")
        self.assertEqual(after_initial_pointer["status"], "INCOMPLETE")
        self.assertEqual(after["status"], "INCOMPLETE")
        self.assertEqual(before["reason"], "native_generation_receipt_missing_or_invalid")
        self.assertEqual(after_initial_pointer["reason"], "native_generation_receipt_missing_or_invalid")
        self.assertEqual(after["reason"], "native_generation_receipt_missing_or_invalid")
        self.assertNotIn("xray", before)
        self.assertNotIn("mihomo", before)
        self.assertNotIn("core", before)
        self.assertNotIn("xray", after_initial_pointer)
        self.assertNotIn("mihomo", after_initial_pointer)
        self.assertNotIn("core", after_initial_pointer)
        self.assertNotIn("xray", after)
        self.assertNotIn("mihomo", after)
        self.assertNotIn("core", after)

    def test_synthetic_native_proof_fingerprints_stay_stable_across_ui_pointer_switches(self) -> None:
        """Exercise the complete proof contract with synthetic readers, never live runtimes."""
        backend_root = ROOT / "backend"
        sys.path.insert(0, str(backend_root))
        try:
            from fwrouter_api.db import connection as db_connection
            from fwrouter_api.services import native_proof
        finally:
            sys.path.remove(str(backend_root))

        from contextlib import contextmanager

        @contextmanager
        def read_guard():
            yield True

        xray_pin = "sha256:" + "1" * 64
        mihomo_pin = "sha256:" + "2" * 64
        inventory_digest = "f" * 64
        apply_id = "synthetic-apply-id"
        xray = {
            "config_sha256": "1" * 64,
            "runtime_config_sha256": "2" * 64,
            "runtime_incarnation_sha256": "3" * 64,
            "durable_bindings_sha256": "4" * 64,
            "durable_modes_sha256": "5" * 64,
            "runtime_image_digest": xray_pin,
            "runtime_image_reference_sha256": "6" * 64,
            "runtime_version_sha256": "7" * 64,
        }
        mihomo = {
            "config_sha256": "8" * 64,
            "semantic_config_sha256": "9" * 64,
            "durable_semantic_config_sha256": "a" * 64,
            "input_fingerprint_sha256": "b" * 64,
            "runtime_incarnation_sha256": "c" * 64,
            "runtime_receipt_config_sha256": "d" * 64,
            "runtime_receipt_fingerprint_sha256": "e" * 64,
            "runtime_image_digest": mihomo_pin,
            "runtime_image_reference_sha256": "f" * 64,
            "runtime_version_sha256": "0" * 64,
        }
        core = {
            "applied_manifest_sha256": "1" * 64,
            "applied_nft_sha256": "2" * 64,
            "native_rule_semantics_sha256": "3" * 64,
            "native_ip_semantics_sha256": "4" * 64,
            "latest_apply_id_sha256": __import__("hashlib").sha256(apply_id.encode()).hexdigest(),
            "core_intent_revision": 7,
            "selection_revision": 11,
        }
        executing_backend_sha = native_proof.backend_manifest_sha256(native_proof._backend_root())
        proof_db = {
            "schema": {"ok": True, "expected_schema_version": 27, "actual_schema_version": 27},
            "integrity": ["ok"],
            "foreign_key_violations": 0,
            "inventory_digest": inventory_digest,
            "inventory_count": 1,
            "generation": {
                "status": "verified",
                "candidate_digest": inventory_digest,
                "verified_digest": inventory_digest,
                "candidate_revision": 3,
                "verified_revision": 3,
            },
            "selection": {"revision": 11},
            "core_revision": 7,
            "latest_apply": {"apply_id": apply_id, "status": "applied", "core_intent_revision": 7},
            "routing": {"apply_state": "clean"},
            "modules": [
                {"module_name": name, "desired_state": "enabled", "lifecycle_mode": "managed"}
                for name in ("core", "vpn", "xray")
            ],
            "active_jobs": 0,
        }
        proof_db["generation"]["native_proof"] = native_proof._make_native_generation_proof(
            executing_backend_sha, xray=xray, mihomo=mihomo, core=core,
            inventory_digest=inventory_digest, generation_revision=3,
        )

        with mock.patch.object(native_proof, "_db_snapshot", return_value=proof_db) as db_snapshot, \
                mock.patch.object(native_proof, "_checkpoint_snapshot",
                                  return_value={"present": False, "valid": True, "sha256": None}) as checkpoint_snapshot, \
                mock.patch.object(db_connection, "get_db_path", return_value=self.root / "unused.db"), \
                mock.patch("fwrouter_api.adapters.xray_common.xray_writer_read_guard", read_guard), \
                mock.patch.object(native_proof, "_xray_readback", side_effect=lambda _db: dict(xray)) as xray_read, \
                mock.patch.object(native_proof, "_mihomo_readback", side_effect=lambda _db: dict(mihomo)) as mihomo_read, \
                mock.patch.object(native_proof, "_core_readback", side_effect=lambda _db: dict(core)) as core_read:
            before = native_proof.collect_native_proof(
                scope="ui", expected_backend_manifest_sha256=executing_backend_sha,
                expected_xray_image_digest=xray_pin, expected_mihomo_image_digest=mihomo_pin,
            )
            first_publication = self.publish()
            after_initial_pointer = native_proof.collect_native_proof(
                scope="ui", expected_backend_manifest_sha256=executing_backend_sha,
                expected_xray_image_digest=xray_pin, expected_mihomo_image_digest=mihomo_pin,
            )
            (self.ui / "index.html").write_text("candidate-v2\n", encoding="utf-8")
            second_publication = self.publish()
            after_managed_switch = native_proof.collect_native_proof(
                scope="ui", expected_backend_manifest_sha256=executing_backend_sha,
                expected_xray_image_digest=xray_pin, expected_mihomo_image_digest=mihomo_pin,
            )

        # PASS here validates the collector against explicit synthetic component
        # readers and an owner-shaped fixture. It is not evidence from real engines.
        proofs = (before, after_initial_pointer, after_managed_switch)
        self.assertTrue(first_publication.startswith("Published UI release "))
        self.assertTrue(second_publication.startswith("Published UI release "))
        self.assertEqual((self.active / "index.html").read_text(encoding="utf-8"), "candidate-v2\n")
        self.assertEqual(db_snapshot.call_count, 6)
        self.assertEqual(checkpoint_snapshot.call_count, 6)
        self.assertEqual(xray_read.call_count, 6)
        self.assertEqual(mihomo_read.call_count, 6)
        self.assertEqual(core_read.call_count, 6)
        for proof in proofs:
            self.assertEqual(proof["status"], "PASS", proof)
            self.assertEqual(proof["source_identity"]["sha256"], executing_backend_sha)
            self.assertEqual(proof["database"], before["database"])
            self.assertEqual(proof["native_proof_sha256"], before["native_proof_sha256"])
            self.assertIn("final_native_runtime_fence_recheck", proof["checks_executed"])
            self.assertIn("generation_native_receipt_exact_match", proof["checks_executed"])
            self.assertEqual(proof["xray"], {**xray, "runtime_image_digest": xray_pin})
            self.assertEqual(proof["mihomo"], {**mihomo, "runtime_image_digest": mihomo_pin})
            self.assertEqual(proof["core"], core)

    def test_backend_installer_waits_for_ui_compatibility_check_and_exchange(self) -> None:
        source = self.root / "concurrent-ui-source"
        (source / "installer").mkdir(parents=True)
        for name in ("install.sh", "ui_publication.py", "controlled_backend_lifecycle.py",
                     "component_install_lock.py", "obsolete-backend-paths.txt"):
            shutil.copy2(ROOT / "installer" / name, source / "installer" / name)
        publisher = source / "installer/ui_publication.py"
        code = publisher.read_text()
        exchange = "def _exchange(left: Path, right: Path) -> None:\n"
        self.assertIn(exchange, code)
        code = code.replace(exchange, "def _exchange_unpaused(left: Path, right: Path) -> None:\n", 1)
        code = code.replace("import sys\n", "import sys\nimport time\n", 1)
        barrier = '''
def _exchange(left: Path, right: Path) -> None:
    reached = Path(os.environ["FWROUTER_TEST_EXCHANGE_REACHED"])
    release = Path(os.environ["FWROUTER_TEST_EXCHANGE_RELEASE"])
    reached.write_text("ready")
    deadline = time.monotonic() + 20
    while not release.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    if not release.exists():
        raise PublicationError("test exchange barrier timed out")
    _exchange_unpaused(left, right)

'''
        code = code.replace("\ndef _fsync_dir(path: Path) -> None:\n",
                            "\n" + barrier + "def _fsync_dir(path: Path) -> None:\n", 1)
        publisher.write_text(code)

        backend_a = source / "backend"
        (backend_a / "scripts").mkdir(parents=True)
        (backend_a / "source.py").write_text("same compatible backend\n")
        (backend_a / "scripts/bootstrap-state.sh").write_text("#!/bin/sh\nexit 0\n")
        os.chmod(backend_a / "scripts/bootstrap-state.sh", 0o755)
        ui = source / "ui"
        ui.mkdir()
        (ui / "index.html").write_text("compatible UI\n")

        target = self.root / "concurrent-target"
        installed_backend = target / "opt/fwrouter-api"
        installed_backend.mkdir(parents=True)
        shutil.copytree(backend_a, installed_backend, dirs_exist_ok=True)
        active_ui = target / "opt/fwrouter-ui"
        active_ui.mkdir()
        (active_ui / "index.html").write_text("old UI\n")

        backend_b_source = self.root / "concurrent-backend-source"
        (backend_b_source / "installer").mkdir(parents=True)
        for name in ("install.sh", "component_install_lock.py", "obsolete-backend-paths.txt",
                     "controlled_backend_lifecycle.py"):
            shutil.copy2(ROOT / "installer" / name, backend_b_source / "installer" / name)
        backend_b = backend_b_source / "backend"
        shutil.copytree(backend_a, backend_b)
        backend_file_b = backend_b / "source.py"
        os.utime(backend_file_b, ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000))

        reached = self.root / "exchange-reached"
        release = self.root / "exchange-release"
        ui_env = os.environ.copy()
        ui_env.update({"FWROUTER_TEST_EXCHANGE_REACHED": str(reached),
                       "FWROUTER_TEST_EXCHANGE_RELEASE": str(release)})
        ui_proc = subprocess.Popen(
            [str(source / "installer/install.sh"), "--deploy", "--component", "ui", "--target", str(target)],
            cwd=source, env=ui_env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        deadline = time.monotonic() + 15
        while not reached.exists() and ui_proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(reached.exists(), "UI installer did not reach the post-validation exchange barrier")

        backend_file = installed_backend / "source.py"
        before_mtime = backend_file.stat().st_mtime_ns
        backend_proc = subprocess.Popen(
            [str(backend_b_source / "installer/install.sh"), "--deploy", "--component", "backend",
             "--target", str(target)],
            cwd=backend_b_source, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        time.sleep(0.2)
        backend_waited = backend_proc.poll() is None
        backend_unchanged = backend_file.stat().st_mtime_ns == before_mtime
        release.write_text("continue")
        _, ui_stderr = ui_proc.communicate(timeout=20)
        self.assertEqual(ui_proc.returncode, 0, ui_stderr.decode(errors="replace"))
        self.assertTrue(active_ui.is_symlink())
        self.assertEqual((active_ui / "index.html").read_text(), "compatible UI\n")
        _, backend_stderr = backend_proc.communicate(timeout=20)
        self.assertTrue(backend_waited, "competing backend installer was not serialized")
        self.assertTrue(backend_unchanged, "backend changed before the UI pointer switch")
        self.assertEqual(backend_proc.returncode, 0, backend_stderr.decode(errors="replace"))
        self.assertEqual(backend_file.read_text(), "same compatible backend\n")
        self.assertEqual(backend_file.stat().st_mtime_ns, backend_file_b.stat().st_mtime_ns,
                         "competing backend installer did not run after UI publication")

    def test_backend_mismatch_refuses_without_mutating_active_tree(self) -> None:
        (self.backend_installed / "app.py").write_text("different")
        with self.assertRaises(ui_publication.PublicationError):
            self.publish()
        self.assertFalse(self.active.is_symlink())
        self.assertEqual((self.active / "old.txt").read_text(), "complete prior tree")

    def test_symlink_source_is_rejected(self) -> None:
        (self.ui / "unsafe").symlink_to("/etc/passwd")
        with self.assertRaises(ui_publication.PublicationError):
            self.publish()
        self.assertFalse(self.active.is_symlink())

    def test_source_file_swapped_to_symlink_between_stat_and_open_is_rejected(self) -> None:
        secret = self.root / "external-secret"
        secret.write_text("must never be copied into public UI")
        index = self.ui / "index.html"
        original_open = os.open
        swapped = False

        def swap_before_open(path, flags, *args, **kwargs):
            nonlocal swapped
            if path == "index.html" and kwargs.get("dir_fd") is not None and not swapped:
                index.unlink()
                index.symlink_to(secret)
                swapped = True
            return original_open(path, flags, *args, **kwargs)

        with mock.patch.object(ui_publication.os, "open", side_effect=swap_before_open):
            with self.assertRaises(OSError):
                self.publish()
        self.assertTrue(swapped)
        self.assertFalse(self.active.is_symlink())
        self.assertEqual((self.active / "old.txt").read_text(), "complete prior tree")
        self.assertFalse(any(self.active.parent.glob(".fwrouter-ui-releases/release-*")))

    def test_source_file_mutation_during_pinned_read_blocks_publication(self) -> None:
        index = self.ui / "index.html"
        original_read = os.read
        mutated = False

        def mutate_after_read(fd, size):
            nonlocal mutated
            chunk = original_read(fd, size)
            if chunk and not mutated:
                index.write_text("candidate-v2")
                mutated = True
            return chunk

        with mock.patch.object(ui_publication.os, "read", side_effect=mutate_after_read):
            with self.assertRaises(ui_publication.PublicationError):
                self.publish()
        self.assertTrue(mutated)
        self.assertFalse(self.active.is_symlink())
        self.assertEqual((self.active / "old.txt").read_text(), "complete prior tree")

    def test_copy_failure_leaves_existing_tree_untouched(self) -> None:
        with mock.patch.object(ui_publication.os, "write", side_effect=OSError("ENOSPC")):
            with self.assertRaises(OSError):
                self.publish()
        self.assertFalse(self.active.is_symlink())
        self.assertEqual((self.active / "old.txt").read_text(), "complete prior tree")
        releases = self.active.parent / ".fwrouter-ui-releases"
        self.assertFalse(any(releases.glob("release-*")))

    def test_post_exchange_failure_keeps_new_active_and_whole_previous_tree(self) -> None:
        original = ui_publication._walk_tree
        count = 0

        def fail_postflight(root: Path, **kwargs):
            nonlocal count
            if root.name.startswith("release-") and kwargs.get("require_index"):
                raise OSError("injected postflight failure")
            return original(root, **kwargs)

        with mock.patch.object(ui_publication, "_walk_tree", side_effect=fail_postflight):
            with self.assertRaises(OSError):
                self.publish()
        self.assertTrue(self.active.is_symlink())
        self.assertEqual((self.active / "index.html").read_text(), "candidate-v1")
        preserved = self.active.parent / ".fwrouter-ui-releases" / f"release-{self.initial_digest}"
        self.assertEqual((preserved / "old.txt").read_text(), "complete prior tree")

    def test_sigkill_immediately_after_exchange_preserves_old_tree(self) -> None:
        code = textwrap.dedent(f"""
            import os, signal, sys
            from pathlib import Path
            sys.path.insert(0, {str(HELPER.parent)!r})
            import ui_publication as u
            original = u._exchange
            def exchange_then_kill(left, right):
                original(left, right)
                os.kill(os.getpid(), signal.SIGKILL)
            u._exchange = exchange_then_kill
            u.publish(Path({str(self.ui)!r}), Path({str(self.active)!r}), Path({str(self.backend_source)!r}), Path({str(self.backend_installed)!r}))
        """)
        result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True)
        self.assertEqual(result.returncode, -signal.SIGKILL)
        self.assertTrue(self.active.is_symlink())
        self.assertEqual((self.active / "index.html").read_text(), "candidate-v1")
        preserved = [p for p in self.active.parent.iterdir() if p.name.startswith(".fwrouter-ui-next-")]
        self.assertEqual(len(preserved), 1)
        self.assertEqual((preserved[0] / "old.txt").read_text(), "complete prior tree")

    def test_same_version_is_verified_noop(self) -> None:
        self.publish()
        before = os.lstat(self.active).st_ino
        result = self.publish()
        self.assertIn("already current", result)
        self.assertEqual(os.lstat(self.active).st_ino, before)


    def test_concurrent_same_version_deployments_serialize(self) -> None:
        command = [sys.executable, str(HELPER), "--source", str(self.ui), "--target", str(self.active),
                   "--backend-source", str(self.backend_source), "--backend-installed", str(self.backend_installed)]
        first = subprocess.Popen(command, cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        second = subprocess.Popen(command, cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out1, err1 = first.communicate(timeout=20)
        out2, err2 = second.communicate(timeout=20)
        self.assertEqual(first.returncode, 0, err1)
        self.assertEqual(second.returncode, 0, err2)
        self.assertTrue(self.active.is_symlink())
        self.assertEqual((self.active / "index.html").read_text(), "candidate-v1")
        releases = list((self.active.parent / ".fwrouter-ui-releases").glob("release-[0-9a-f]*"))
        self.assertEqual(len(releases), 2)  # The initial complete tree is retained as a managed release.

    def test_unsupported_exchange_fails_closed(self) -> None:
        with mock.patch.object(ui_publication, "_exchange", side_effect=ui_publication.PublicationError("unsupported")):
            with self.assertRaises(ui_publication.PublicationError):
                self.publish()
        self.assertFalse(self.active.is_symlink())
        self.assertEqual((self.active / "old.txt").read_text(), "complete prior tree")


    def _run_injected_process(self, inject: str, *, term: bool = False):
        code = (
            "import os, signal, sys\n"
            "from pathlib import Path\n"
            f"sys.path.insert(0, {str(HELPER.parent)!r})\n"
            "import ui_publication as u\n"
            f"{inject}\n"
            "try:\n"
            f"    u.publish(Path({str(self.ui)!r}), Path({str(self.active)!r}), "
            f"Path({str(self.backend_source)!r}), Path({str(self.backend_installed)!r}))\n"
            "except InterruptedError:\n"
            "    raise SystemExit(1)\n"
        )
        return subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True)

    def test_sigterm_during_preparation_keeps_old_tree_and_cleans_stage(self) -> None:
        inject = textwrap.dedent("""
            original = u._walk_tree
            def walk_then_term(*args, **kwargs):
                result = original(*args, **kwargs)
                if kwargs.get("copy_to") is not None:
                    os.kill(os.getpid(), signal.SIGTERM)
                return result
            u._walk_tree = walk_then_term
        """)
        result = self._run_injected_process(inject)
        self.assertEqual(result.returncode, 1)
        self.assertFalse(self.active.is_symlink())
        self.assertEqual((self.active / "old.txt").read_text(), "complete prior tree")
        releases = self.active.parent / ".fwrouter-ui-releases"
        self.assertFalse(any(releases.glob(".staging-*")))

    def test_real_installer_sigterm_during_copy_preserves_active_ui(self) -> None:
        proc, _stdout, stderr = self._run_real_installer_until(
            lambda target: any(
                (stage / "large-asset.bin").exists() and (stage / "large-asset.bin").stat().st_size > 0
                for stage in (target / "opt/.fwrouter-ui-releases").glob(".staging-*")
            ),
            signal.SIGTERM,
        )
        active = self.root / "actual-installer-target/opt/fwrouter-ui"
        self.assertNotEqual(proc.returncode, 0, stderr.decode(errors="replace"))
        self.assertFalse(active.is_symlink())
        self.assertEqual((active / "index.html").read_text(), "old complete ui\n")

    def test_real_installer_sigkill_during_copy_preserves_active_ui(self) -> None:
        proc, _stdout, stderr = self._run_real_installer_until(
            lambda target: any(
                (stage / "large-asset.bin").exists() and (stage / "large-asset.bin").stat().st_size > 0
                for stage in (target / "opt/.fwrouter-ui-releases").glob(".staging-*")
            ),
            signal.SIGKILL,
        )
        active = self.root / "actual-installer-target/opt/fwrouter-ui"
        self.assertEqual(proc.returncode, -signal.SIGKILL, stderr.decode(errors="replace"))
        self.assertFalse(active.is_symlink())
        self.assertEqual((active / "index.html").read_text(), "old complete ui\n")

    def test_real_installer_sigterm_after_pointer_switch_keeps_both_whole(self) -> None:
        proc, _stdout, stderr = self._run_real_installer_until(
            lambda target: (target / "opt/fwrouter-ui").is_symlink(),
            signal.SIGTERM,
        )
        active = self.root / "actual-installer-target/opt/fwrouter-ui"
        self.assertNotEqual(proc.returncode, 0, stderr.decode(errors="replace"))
        self.assertTrue(active.is_symlink())
        self.assertEqual((active / "index.html").read_text(), "new immutable ui\n")
        preserved = [
            path for path in active.parent.iterdir()
            if path.name.startswith(".fwrouter-ui-next-")
        ] + list((active.parent / ".fwrouter-ui-releases").glob("release-[0-9a-f]*"))
        self.assertTrue(any((path / "index.html").read_text() == "old complete ui\n" for path in preserved))

    def test_sigkill_after_archive_keeps_new_and_previous_complete(self) -> None:
        inject = textwrap.dedent("""
            original = u._fsync_dir
            seen = 0
            def fsync_then_kill(path):
                global seen
                original(path)
                if path.name == ".fwrouter-ui-releases":
                    seen += 1
                    if seen == 2:
                        os.kill(os.getpid(), signal.SIGKILL)
            u._fsync_dir = fsync_then_kill
        """)
        result = self._run_injected_process(inject)
        self.assertEqual(result.returncode, -signal.SIGKILL)
        self.assertTrue(self.active.is_symlink())
        self.assertEqual((self.active / "index.html").read_text(), "candidate-v1")
        archived = self.active.parent / ".fwrouter-ui-releases" / f"release-{self.initial_digest}"
        self.assertEqual((archived / "old.txt").read_text(), "complete prior tree")

    def test_sigkill_during_postflight_keeps_both_versions(self) -> None:
        inject = textwrap.dedent("""
            original = u._walk_tree
            def walk_then_kill(root, **kwargs):
                result = original(root, **kwargs)
                if root.name.startswith("release-") and kwargs.get("require_index"):
                    os.kill(os.getpid(), signal.SIGKILL)
                return result
            u._walk_tree = walk_then_kill
        """)
        result = self._run_injected_process(inject)
        self.assertEqual(result.returncode, -signal.SIGKILL)
        self.assertTrue(self.active.is_symlink())
        self.assertEqual((self.active / "index.html").read_text(), "candidate-v1")
        archived = self.active.parent / ".fwrouter-ui-releases" / f"release-{self.initial_digest}"
        self.assertEqual((archived / "old.txt").read_text(), "complete prior tree")

    def test_empty_ui_source_fails_without_touching_active_tree(self) -> None:
        (self.ui / "index.html").unlink()
        with self.assertRaises(ui_publication.PublicationError):
            self.publish()
        self.assertFalse(self.active.is_symlink())
        self.assertEqual((self.active / "old.txt").read_text(), "complete prior tree")

    def test_restore_release_requires_trusted_source_digest_and_preserves_current(self) -> None:
        self.publish()
        old_digest, _ = ui_publication._walk_tree(self.ui, owner_uid=os.geteuid(), require_index=True)
        (self.ui / "index.html").write_text("candidate-v2")
        self.publish()
        current_digest, _ = ui_publication._walk_tree(self.ui, owner_uid=os.geteuid(), require_index=True)
        self.assertNotEqual(old_digest, current_digest)

        (self.ui / "index.html").write_text("candidate-v1")
        result = ui_publication.publish(
            self.ui, self.active, self.backend_source, self.backend_installed,
            restore_release_sha256=old_digest,
        )
        self.assertIn("Restored UI release", result)
        self.assertEqual((self.active / "index.html").read_text(), "candidate-v1")
        releases = self.active.parent / ".fwrouter-ui-releases"
        self.assertEqual(ui_publication._release_digest(releases / f"release-{current_digest}", os.geteuid()), current_digest)

    def test_restore_rejects_source_mismatch_corrupt_or_missing_release_without_switch(self) -> None:
        self.publish()
        old_digest, _ = ui_publication._walk_tree(self.ui, owner_uid=os.geteuid(), require_index=True)
        (self.ui / "index.html").write_text("candidate-v2")
        self.publish()
        releases = self.active.parent / ".fwrouter-ui-releases"
        active_name = ui_publication._managed_release(self.active, releases, os.geteuid()).name
        with self.assertRaisesRegex(ui_publication.PublicationError, "source UI digest"):
            ui_publication.publish(self.ui, self.active, self.backend_source, self.backend_installed, restore_release_sha256=old_digest)
        self.assertEqual(ui_publication._managed_release(self.active, releases, os.geteuid()).name, active_name)

        old_release = releases / f"release-{old_digest}"
        (old_release / "index.html").write_text("corrupted")
        (self.ui / "index.html").write_text("candidate-v1")
        with self.assertRaises(ui_publication.PublicationError):
            ui_publication.publish(self.ui, self.active, self.backend_source, self.backend_installed, restore_release_sha256=old_digest)
        self.assertEqual(ui_publication._managed_release(self.active, releases, os.geteuid()).name, active_name)

        shutil.rmtree(old_release)
        with self.assertRaisesRegex(ui_publication.PublicationError, "does not exist"):
            ui_publication.publish(self.ui, self.active, self.backend_source, self.backend_installed, restore_release_sha256=old_digest)
        self.assertEqual(ui_publication._managed_release(self.active, releases, os.geteuid()).name, active_name)

    def test_restore_rejects_backend_mismatch_without_switch(self) -> None:
        self.publish()
        old_digest, _ = ui_publication._walk_tree(self.ui, owner_uid=os.geteuid(), require_index=True)
        (self.ui / "index.html").write_text("candidate-v2")
        self.publish()
        active_name = ui_publication._managed_release(
            self.active, self.active.parent / ".fwrouter-ui-releases", os.geteuid()
        ).name
        (self.ui / "index.html").write_text("candidate-v1")
        (self.backend_installed / "app.py").write_text("incompatible installed backend")
        with self.assertRaisesRegex(ui_publication.PublicationError, "backend"):
            ui_publication.publish(self.ui, self.active, self.backend_source, self.backend_installed,
                                   restore_release_sha256=old_digest)
        self.assertEqual(ui_publication._managed_release(
            self.active, self.active.parent / ".fwrouter-ui-releases", os.geteuid()
        ).name, active_name)

    def test_interrupted_initial_exchange_is_archived_and_restorable(self) -> None:
        old_digest, _ = ui_publication._walk_tree(self.active, owner_uid=os.geteuid(), require_index=True)
        code = textwrap.dedent(f"""
            import os, signal, sys
            from pathlib import Path
            sys.path.insert(0, {str(HELPER.parent)!r})
            import ui_publication as u
            original = u._exchange
            def exchange_then_kill(left, right):
                original(left, right)
                os.kill(os.getpid(), signal.SIGKILL)
            u._exchange = exchange_then_kill
            u.publish(Path({str(self.ui)!r}), Path({str(self.active)!r}), Path({str(self.backend_source)!r}), Path({str(self.backend_installed)!r}))
        """)
        result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True)
        self.assertEqual(result.returncode, -signal.SIGKILL)
        self.assertTrue(any(p.name.startswith(".fwrouter-ui-next-") for p in self.active.parent.iterdir()))

        (self.ui / "index.html").write_text("complete prior index")
        (self.ui / "old.txt").write_text("complete prior tree")
        command = [sys.executable, str(HELPER), "--source", str(self.ui), "--target", str(self.active),
                   "--backend-source", str(self.backend_source), "--backend-installed", str(self.backend_installed),
                   "--restore-release", old_digest]
        restored = subprocess.run(command, cwd=ROOT, text=True, capture_output=True)
        self.assertEqual(restored.returncode, 0, restored.stderr)
        self.assertEqual((self.active / "index.html").read_text(), "complete prior index")
        self.assertEqual(ui_publication._managed_release(self.active, self.active.parent / ".fwrouter-ui-releases", os.geteuid()).name, f"release-{old_digest}")

    def test_identical_existing_archive_is_never_overwritten_or_lost(self) -> None:
        releases = self.active.parent / ".fwrouter-ui-releases"
        releases.mkdir()
        existing = releases / f"release-{self.initial_digest}"
        shutil.copytree(self.active, existing)
        self.publish()
        self.assertEqual((existing / "index.html").read_text(), "complete prior index")
        orphans = [path for path in self.active.parent.glob(".fwrouter-ui-next-*") if path.is_dir()]
        self.assertEqual(len(orphans), 1)
        self.assertEqual((orphans[0] / "old.txt").read_text(), "complete prior tree")

    def test_corrupt_archive_collision_refuses_before_pointer_switch(self) -> None:
        releases = self.active.parent / ".fwrouter-ui-releases"
        releases.mkdir()
        conflict = releases / f"release-{self.initial_digest}"
        conflict.mkdir()
        (conflict / "index.html").write_text("different content")
        with self.assertRaisesRegex(ui_publication.PublicationError, "conflicts"):
            self.publish()
        self.assertFalse(self.active.is_symlink())
        self.assertEqual((self.active / "index.html").read_text(), "complete prior index")

    def test_writable_source_and_target_ancestor_are_rejected_without_chmod(self) -> None:
        os.chmod(self.ui, 0o777)
        with self.assertRaises(ui_publication.PublicationError):
            self.publish()
        self.assertEqual(stat_mode(self.ui), 0o777)
        os.chmod(self.ui, 0o755)
        parent = self.active.parent
        os.chmod(parent, 0o777)
        with self.assertRaises(ui_publication.PublicationError):
            self.publish()
        self.assertEqual(stat_mode(parent), 0o777)

    def test_source_size_bounds_fail_before_target_mutation(self) -> None:
        (self.ui / "large.bin").write_bytes(b"12345")
        with mock.patch.object(ui_publication, "MAX_UI_TOTAL_BYTES", 4):
            with self.assertRaisesRegex(ui_publication.PublicationError, "size limit"):
                self.publish()
        self.assertFalse(self.active.is_symlink())
        self.assertEqual((self.active / "old.txt").read_text(), "complete prior tree")

    def test_symlinked_source_or_target_ancestor_is_rejected(self) -> None:
        source_alias = self.root / "source-alias"
        source_alias.symlink_to(self.ui, target_is_directory=True)
        with self.assertRaises(ui_publication.PublicationError):
            ui_publication.publish(source_alias, self.active, self.backend_source, self.backend_installed)
        target_parent_alias = self.root / "target-parent-alias"
        target_parent_alias.symlink_to(self.active.parent, target_is_directory=True)
        aliased_target = target_parent_alias / self.active.name
        with self.assertRaises(ui_publication.PublicationError):
            ui_publication.publish(self.ui, aliased_target, self.backend_source, self.backend_installed)
        self.assertEqual((self.active / "old.txt").read_text(), "complete prior tree")

    def test_fifo_lock_is_rejected_without_blocking(self) -> None:
        lock = self.active.parent / ".fwrouter-ui.lock"
        os.mkfifo(lock, 0o600)
        with self.assertRaises(ui_publication.PublicationError):
            self.publish()
        self.assertEqual((self.active / "old.txt").read_text(), "complete prior tree")

    def test_component_lock_symlink_is_rejected_without_touching_target(self) -> None:
        lock = self.active.parent / ".fwrouter-component-install.lock"
        outside = self.root / "outside-lock-target"
        outside.write_text("untouched")
        lock.symlink_to(outside)
        with self.assertRaises(ui_publication.PublicationError):
            self.publish()
        self.assertEqual(outside.read_text(), "untouched")
        self.assertEqual((self.active / "old.txt").read_text(), "complete prior tree")

    def test_directories_and_files_must_be_readable_by_static_server(self) -> None:
        os.chmod(self.ui / "index.html", 0o600)
        with self.assertRaisesRegex(ui_publication.PublicationError, "not readable"):
            self.publish()
        os.chmod(self.ui / "index.html", 0o644)
        os.chmod(self.ui, 0o700)
        with self.assertRaisesRegex(ui_publication.PublicationError, "not traversable"):
            self.publish()

    def test_rootless_target_uses_effective_uid_and_live_root_guard_is_explicit(self) -> None:
        if os.geteuid() == 0:
            self.assertTrue(self.publish())
            return
        self.assertTrue(self.publish())
        with self.assertRaises(ui_publication.PublicationError):
            ui_publication.publish(self.ui, self.active, self.backend_source, self.backend_installed, require_root=True)

    def test_unmanaged_symlink_is_rejected(self) -> None:
        self.active.rename(self.active.with_name("old-ui"))
        self.active.symlink_to("/tmp/foreign-ui")
        with self.assertRaises(ui_publication.PublicationError):
            self.publish()
        self.assertEqual(os.readlink(self.active), "/tmp/foreign-ui")


if __name__ == "__main__":
    unittest.main()
