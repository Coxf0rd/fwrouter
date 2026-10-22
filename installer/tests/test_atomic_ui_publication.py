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

    def publish(self) -> str:
        return ui_publication.publish(self.ui, self.active, self.backend_source, self.backend_installed)

    def _actual_installer_fixture(self) -> tuple[Path, Path, Path]:
        source = self.root / "disposable-source"
        (source / "installer").mkdir(parents=True)
        for name in ("install.sh", "ui_publication.py", "controlled_backend_lifecycle.py"):
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
        self.assertEqual(len(list((self.active.parent / ".fwrouter-ui-releases").glob("release-initial-*"))), 1)

    def test_installer_all_installs_backend_before_compatible_ui(self) -> None:
        shutil.rmtree(self.target_root)
        result = subprocess.run(
            [str(ROOT / "installer/install.sh"), "--deploy", "--all", "--target", str(self.target_root)],
            cwd=ROOT, text=True, capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.active.is_symlink())
        self.assertTrue((self.target_root / "opt/fwrouter-api/fwrouter_api").is_dir())

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
            count += 1
            if count == 6:
                raise OSError("injected postflight failure")
            return original(root, **kwargs)

        with mock.patch.object(ui_publication, "_walk_tree", side_effect=fail_postflight):
            with self.assertRaises(OSError):
                self.publish()
        self.assertTrue(self.active.is_symlink())
        self.assertEqual((self.active / "index.html").read_text(), "candidate-v1")
        preserved = list((self.active.parent / ".fwrouter-ui-releases").glob("release-initial-*"))
        self.assertEqual(len(preserved), 1)
        self.assertEqual((preserved[0] / "old.txt").read_text(), "complete prior tree")

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
        self.assertEqual(len(releases), 1)

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
        ] + list((active.parent / ".fwrouter-ui-releases").glob("release-initial-*"))
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
        archived = list((self.active.parent / ".fwrouter-ui-releases").glob("release-initial-*"))
        self.assertEqual(len(archived), 1)
        self.assertEqual((archived[0] / "old.txt").read_text(), "complete prior tree")

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
        archived = list((self.active.parent / ".fwrouter-ui-releases").glob("release-initial-*"))
        self.assertEqual(len(archived), 1)
        self.assertEqual((archived[0] / "old.txt").read_text(), "complete prior tree")

    def test_empty_ui_source_fails_without_touching_active_tree(self) -> None:
        (self.ui / "index.html").unlink()
        with self.assertRaises(ui_publication.PublicationError):
            self.publish()
        self.assertFalse(self.active.is_symlink())
        self.assertEqual((self.active / "old.txt").read_text(), "complete prior tree")

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
