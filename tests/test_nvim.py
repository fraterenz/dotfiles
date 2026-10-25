"""Editor dependencies and opt-in headless tests, never the live configuration."""

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import unittest


SOURCE = Path(__file__).resolve().parents[1]
NVIM = SOURCE / "dot_config/nvim"


class EditorDependencyTests(unittest.TestCase):
    def test_image_installs_and_activation_requires_editor_tools(self):
        containerfile = (SOURCE / "dot_config/dotfiles/devbox/build/Containerfile.dev").read_text()
        base_packages = re.search(r"RUN dnf -y install \\\n(.*?)&& dnf clean all", containerfile, re.S).group(1).split()
        self.assertTrue({"neovim", "ripgrep", "par"}.issubset(base_packages))
        config = (SOURCE / "dot_config/dotfiles/devbox/config.json.tmpl").read_text().replace("{{ .profile | toJson }}", '"local"')
        self.assertTrue({"nvim", "rg", "par"}.issubset(json.loads(config)["required_tools"]))

    def test_search_command_is_loaded_without_external_plugins(self):
        self.assertIn("require('config.search')", (NVIM / "init.lua").read_text())
        search = (NVIM / "lua/config/search.lua").read_text()
        self.assertIn("nvim_create_user_command('Rg'", search)
        self.assertNotIn("fzf", search)


class HeadlessEditorTests(unittest.TestCase):
    def test_search_and_formatter_workflows(self):
        override = os.environ.get("DOTFILES_NVIM_TEST_COMMAND")
        if override:
            command = json.loads(override)
            self.assertIsInstance(command, list)
            self.assertTrue(command and all(isinstance(arg, str) for arg in command))
        else:
            if any(not shutil.which(tool) for tool in ("nvim", "rg", "par")):
                self.skipTest("Needs nvim/rg/par, or an explicit DOTFILES_NVIM_TEST_COMMAND JSON argv for a disposable environment")
            command = [shutil.which("nvim"), "--headless", "--noplugin", "-u", "NONE", "-i", "NONE", "-n", "-l", "/dev/stdin"]
        sources = {name: (NVIM / "lua/config" / (name + ".lua")).read_text() for name in ("search", "keybinds")}
        payload = json.dumps(sources)
        self.assertNotIn("]====]", payload)
        script = "local sources = vim.json.decode([====[" + payload + "]====])\n" + (SOURCE / "tests/nvim/editor.lua").read_text()
        result = subprocess.run(command, input=script, capture_output=True, text=True, timeout=300)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Editor search/formatter checks passed", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
