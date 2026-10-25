"""Shared primitives: real scratch files, mocked child processes, no host actions."""

import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE / "private_dot_local/lib"))

from dotfiles.common import Error, Runner, atomic_write, file_lock, private_directory
from dotfiles_host.core import Runner as HostRunner


class FileTests(unittest.TestCase):
    def setUp(self):
        parent = "/tmp/opencode" if Path("/tmp/opencode").is_dir() else None
        temporary = tempfile.TemporaryDirectory(prefix="dotfiles-common-", dir=parent)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_atomic_file_permissions_are_flushed_before_replacement(self):
        path = self.root / "record"
        path.write_bytes(b"old")
        fsync = os.fsync
        flushed_files = []
        def flush(fd):
            info = os.fstat(fd)
            if stat.S_ISREG(info.st_mode):
                flushed_files.append(stat.S_IMODE(info.st_mode))
                self.assertEqual(path.read_bytes(), b"old")
            fsync(fd)
        with mock.patch("dotfiles.common.os.fsync", side_effect=flush):
            atomic_write(path, b"new")
        self.assertEqual(flushed_files, [0o600])
        self.assertEqual(path.read_bytes(), b"new")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(list(self.root.iterdir()), [path])

    def test_failed_atomic_replace_preserves_original_and_cleans_temporary(self):
        path = self.root / "record"
        path.write_bytes(b"old")
        with mock.patch("dotfiles.common.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                atomic_write(path, b"new")
        self.assertEqual(path.read_bytes(), b"old")
        self.assertEqual(list(self.root.iterdir()), [path])

    def test_symlink_file_and_private_directory_are_not_followed(self):
        original = self.root / "original"
        original.write_bytes(b"keep")
        link = self.root / "link"
        link.symlink_to(original)
        with self.assertRaisesRegex(Error, "symlink"):
            atomic_write(link, b"new")
        self.assertTrue(link.is_symlink())
        self.assertEqual(original.read_bytes(), b"keep")
        directory = self.root / "directory"
        directory.mkdir(mode=0o755)
        link.unlink()
        link.symlink_to(directory)
        with self.assertRaisesRegex(Error, "symlink"):
            private_directory(link)
        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o755)

    def test_lock_is_exclusive_and_uses_a_private_directory(self):
        path = self.root / "state/lock"
        with file_lock(path, "busy"):
            self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            with self.assertRaisesRegex(Error, "busy"):
                with file_lock(path, "busy"):
                    self.fail("Concurrent lock acquired")

    def test_nonregular_lock_is_rejected(self):
        path = self.root / "lock"
        os.mkfifo(path)
        with self.assertRaisesRegex(Error, "regular file"):
            with file_lock(path, "busy"):
                self.fail("FIFO accepted as lock")


class RunnerTests(unittest.TestCase):
    def test_command_failure_reports_diagnostics_but_check_false_returns_status(self):
        failure = subprocess.CompletedProcess(["demo"], 1, "", "specific failure")
        with mock.patch("dotfiles.common.subprocess.run", return_value=failure) as run:
            with self.assertRaisesRegex(Error, "specific failure"):
                Runner().run(["demo", "literal;argument"])
            self.assertIs(Runner().run(["demo"], check=False), failure)
            self.assertFalse(run.call_args.kwargs.get("shell", False))

    def test_timeouts_and_missing_commands_are_normalized(self):
        for failure in (OSError("missing"), subprocess.TimeoutExpired(["demo"], 1)):
            with self.subTest(failure=failure):
                with mock.patch("dotfiles.common.subprocess.run", side_effect=failure):
                    with self.assertRaisesRegex(Error, "unavailable or timed out"):
                        Runner().run(["demo"])

    def test_host_queries_are_noninteractive_but_actions_keep_the_terminal(self):
        success = subprocess.CompletedProcess(["demo"], 0, "", "")
        with mock.patch("dotfiles.common.subprocess.run", return_value=success) as run:
            runner = HostRunner(emit=lambda message: None)
            runner.capture(["demo"])
            self.assertEqual(run.call_args.kwargs["stdin"], subprocess.DEVNULL)
            self.assertEqual(run.call_args.kwargs["timeout"], 60)
            runner.action(["demo"])
            self.assertIsNone(run.call_args.kwargs["stdin"])
            self.assertIsNone(run.call_args.kwargs["timeout"])
            self.assertFalse(run.call_args.kwargs["capture_output"])

    def test_host_dry_run_does_not_execute_actions(self):
        messages = []
        with mock.patch("dotfiles.common.subprocess.run") as run:
            HostRunner(dry_run=True, emit=messages.append).action(["demo"])
            run.assert_not_called()
        self.assertEqual(messages, ["Would run: demo"])

    def test_invalid_json_is_reported(self):
        success = subprocess.CompletedProcess(["demo"], 0, "invalid JSON", "")
        with mock.patch("dotfiles.common.subprocess.run", return_value=success):
            with self.assertRaisesRegex(Error, "Invalid JSON"):
                Runner().json(["demo"])


if __name__ == "__main__":
    unittest.main()
