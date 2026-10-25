"""Exercise profile gating and fish helpers without changing the live home."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


SOURCE = Path(__file__).resolve().parents[1]
FUNCTIONS = SOURCE / "dot_config/private_fish/functions"
KEPT_FUNCTIONS = {"fish_prompt", "clc", "clippy", "l", "la", "ll", "lla", "rm"}


class CleanupTests(unittest.TestCase):
    def test_only_selected_functions_are_managed(self):
        actual = {p.name.removeprefix("executable_").removesuffix(".fish") for p in FUNCTIONS.iterdir()}
        self.assertEqual(actual, KEPT_FUNCTIONS)

    def test_wallpaper_and_operational_notes_are_not_published(self):
        self.assertFalse((SOURCE / "Pictures/background.jpg").exists())
        self.assertFalse((SOURCE / "docs/migration.md").exists())

    def test_rm_keeps_interactive_deletion(self):
        self.assertIn("command rm -i $argv", (FUNCTIONS / "executable_rm.fish").read_text())


class ProfileTests(unittest.TestCase):
    def chezmoi(self, directory, profile, *args):
        binary = os.environ.get("CHEZMOI_BIN") or shutil.which("chezmoi")
        if not binary:
            self.skipTest("chezmoi is required for profile rendering tests")
        return subprocess.check_output(
            [
                binary, "--source", str(SOURCE), "--destination", directory,
                "--config", str(Path(directory) / "test.toml"),
                "--persistent-state", str(Path(directory) / "state.boltdb"),
                "--override-data", json.dumps({"gitName": "Example User", "gitEmail": "example@example.invalid", "profile": profile}),
                *args,
            ],
            text=True,
        )

    def scratch(self):
        parent = "/tmp/opencode" if Path("/tmp/opencode").is_dir() else None
        return tempfile.TemporaryDirectory(prefix="dotfiles-profile-", dir=parent)

    def test_sway_is_local_only(self):
        for profile in ("local", "work"):
            with self.subTest(profile=profile), self.scratch() as directory:
                paths = self.chezmoi(directory, profile, "managed", "--include=files,symlinks").splitlines()
                sway = [p for p in paths if p.startswith(".config/sway/")]
                self.assertEqual(bool(sway), profile == "local")
                self.assertNotIn("Pictures/background.jpg", paths)

    def test_local_wallpaper_is_optional(self):
        with self.scratch() as directory:
            target = str(Path(directory) / ".config/sway/config.d/40-output.conf")
            rendered = self.chezmoi(directory, "local", "cat", target)
            self.assertIn("#f6f2ee solid_color", rendered)
            wallpaper = Path(directory) / "Pictures/background.jpg"
            wallpaper.parent.mkdir()
            wallpaper.write_bytes(b"local unmanaged test wallpaper")
            rendered = self.chezmoi(directory, "local", "cat", target)
            self.assertIn("~/Pictures/background.jpg fill", rendered)
            self.assertNotIn("solid_color", rendered)


if __name__ == "__main__":
    unittest.main()
