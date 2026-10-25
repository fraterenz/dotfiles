"""Real chezmoi deployment and Quadlet generation in disposable directories."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from test_containers import NEW, SOURCE
from dotfiles.container_backend import Backend
from dotfiles.container_support import Paths, Unit
from dotfiles.containers import Settings


class ContainerIntegrationTests(unittest.TestCase):
    def test_both_profiles_deploy_an_inert_definition_and_working_wrapper(self):
        chezmoi = os.environ.get("CHEZMOI_BIN") or shutil.which("chezmoi")
        if not chezmoi:
            self.skipTest("chezmoi is required for sandbox deployment")
        generators = ("/usr/lib/systemd/system-generators/podman-system-generator", "/usr/libexec/podman/quadlet")
        generator = next((p for p in generators if os.access(p, os.X_OK)), None)
        parent = "/tmp/opencode" if Path("/tmp/opencode").is_dir() else None
        for profile in ("local", "work"):
            with self.subTest(profile=profile), tempfile.TemporaryDirectory(prefix="dotfiles-container-apply-", dir=parent) as directory:
                temporary = Path(directory)
                home = temporary / "home"
                home.mkdir()
                data = {"gitName": "Example User", "gitEmail": "example@example.invalid", "profile": profile}
                applied = subprocess.run([
                    chezmoi, "--source", str(SOURCE), "--destination", str(home),
                    "--config", str(temporary / "test.toml"),
                    "--persistent-state", str(temporary / "chezmoi-state"),
                    "--override-data", json.dumps(data), "apply", "--force",
                ], capture_output=True, text=True)
                self.assertEqual(applied.returncode, 0, applied.stderr)
                paths = Paths(home, home / ".config", home / ".local/state", temporary / "runtime")
                settings = Settings(paths.desired / "config.json")
                self.assertEqual(settings.profile, profile)
                self.assertFalse(paths.active.exists())
                self.assertFalse(paths.state.exists())
                self.assertFalse((home / ".local/bin/chezmoi").exists())
                self.assertFalse((home / ".config/fish/functions/lab.fish").exists())
                self.assertEqual((home / ".config/sway").exists(), profile == "local")
                targets = {mount.target for mount in settings.unit.mounts(home)}
                self.assertNotIn("/home/dev/.config/fish", targets)
                self.assertNotIn("/home/dev/.config/fish/fish_variables", targets)
                wrapper = home / ".local/bin/setup-containers"
                self.assertTrue(os.access(wrapper, os.X_OK))
                result = subprocess.run([str(wrapper), "--help"], check=True, capture_output=True, text=True)
                self.assertIn("--rollback", result.stdout)
                self.assertFalse(list((home / ".local/lib").rglob("__pycache__")))
                if generator:
                    backend = Backend(paths)
                    backend.generator = generator
                    backend.validate_unit(settings.unit.pinned(NEW), NEW)

    def test_private_workspace_override_is_rendered_without_source_changes(self):
        chezmoi = os.environ.get("CHEZMOI_BIN") or shutil.which("chezmoi")
        if not chezmoi:
            self.skipTest("chezmoi is required for template checks")
        parent = "/tmp/opencode" if Path("/tmp/opencode").is_dir() else None
        with tempfile.TemporaryDirectory(prefix="dotfiles-workspace-", dir=parent) as directory:
            temporary = Path(directory)
            data = {"gitName": "Example User", "gitEmail": "example@example.invalid", "profile": "work", "devWorkspace": "/srv/development"}
            command = [
                chezmoi, "--source", str(SOURCE), "--destination", str(temporary / "home"),
                "--config", str(temporary / "test.toml"),
                "--persistent-state", str(temporary / "chezmoi-state"),
                "--override-data", json.dumps(data),
            ]
            target = temporary / "home/.config/dotfiles/devbox/dev.container"
            rendered = subprocess.check_output(command + ["cat", str(target)], text=True)
            self.assertIn("Volume=/srv/development:/home/dev:Z", rendered)
            # Render the actual initialization template too, without writing any
            # real chezmoi configuration or invoking init against the source.
            initialized = subprocess.run(command + ["execute-template", "--init"], input=(SOURCE / ".chezmoi.toml.tmpl").read_text(), capture_output=True, text=True)
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            self.assertIn('devWorkspace = "/srv/development"', initialized.stdout)


if __name__ == "__main__":
    unittest.main()
