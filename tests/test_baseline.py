"""Safety checks for the migration baseline; never apply to the real home."""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


SOURCE = Path(__file__).resolve().parents[1]


class BaselineSafetyTests(unittest.TestCase):
    def test_no_automatic_setup_scripts(self):
        scripts = [
            str(path.relative_to(SOURCE))
            for path in SOURCE.rglob("run_*")
            if ".git" not in path.relative_to(SOURCE).parts
        ]
        self.assertEqual(scripts, [])

    def test_no_exact_directories(self):
        self.assertEqual(list(SOURCE.rglob("exact_*")), [])

    def test_container_inputs_are_staged(self):
        desired = SOURCE / "dot_config/dotfiles/devbox"
        self.assertTrue((desired / "dev.container.tmpl").is_file())
        self.assertTrue(any((desired / "build").glob("Containerfile*")))
        self.assertFalse((SOURCE / "dot_config/containers/systemd/dev.container").exists())

    def test_no_host_opencode_path_in_fish(self):
        fish = SOURCE / "dot_config/private_fish/executable_config.fish"
        self.assertNotIn(".opencode/bin", fish.read_text())

    def test_deployed_paths_exclude_runtime_and_scaffolding(self):
        binary = os.environ.get("CHEZMOI_BIN") or shutil.which("chezmoi")
        if not binary:
            self.skipTest("Set CHEZMOI_BIN or install chezmoi to check modeled paths")
        scratch = "/tmp/opencode" if Path("/tmp/opencode").is_dir() else None
        with tempfile.TemporaryDirectory(prefix="dotfiles-test-", dir=scratch) as tmp:
            result = subprocess.run(
                [
                    binary,
                    "--source", str(SOURCE),
                    "--destination", tmp,
                    "--config", str(Path(tmp) / "test.toml"),
                    "--persistent-state", str(Path(tmp) / "state.boltdb"),
                    "--override-data", '{"gitName":"Example User","gitEmail":"example@example.invalid","profile":"local"}',
                    "managed", "--include=files,symlinks",
                ],
                check=True, capture_output=True, text=True,
            )
        paths = set(result.stdout.splitlines())
        forbidden = {
            "README.md", ".gitconfig.bak", ".netrc",
            ".config/containers/systemd/dev.container",
            ".config/fish/fish_variables",
            ".config/fish/functions/config.fish",
            ".config/fish/functions/fish_prompt.fish.bak",
            ".config/fish/functions/lab.fish",
            ".config/chezmoi/chezmoi.toml",
            ".local/bin/chezmoi",
            ".opencode/bin/opencode",
        }
        self.assertFalse(paths & forbidden)
        self.assertFalse(any(path.startswith((".github/", "docs/", "tests/", ".ssh/", ".local/state/", ".cache/")) for path in paths))
        self.assertIn(".config/dotfiles/devbox/dev.container", paths)
        self.assertIn(".config/nvim/init.lua", paths)


if __name__ == "__main__":
    unittest.main()
