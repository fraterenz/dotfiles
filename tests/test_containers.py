"""Lifecycle fault injection. Mutable paths and service operations are isolated."""

import ast
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE / "private_dot_local/lib"))
from dotfiles.container_support import Error, Paths, Store, Unit, atomic_write, context_fingerprint, image_id
from dotfiles.containers import Lifecycle, Settings, main


OLD = "sha256:" + "1" * 64
NEW = "sha256:" + "2" * 64
NEXT = "sha256:" + "3" * 64
UNIT = """[Unit]
Description=Development container
[Container]
Image=localhost/devbox:managed
ContainerName=dev
UserNS=keep-id
DropCapability=all
NoNewPrivileges=true
Volume=%h/dev:/home/dev:Z
Volume=%h/.gitconfig:/home/dev/.gitconfig:ro,Z
Environment=HOME=/home/dev
WorkingDir=/home/dev
Exec=/usr/bin/sleep infinity
[Service]
Restart=on-failure
[Install]
WantedBy=default.target
"""


class FakeBackend:
    def __init__(self, paths):
        self.paths = paths
        self.events = []
        self.running = False
        self.image = None
        self.images = {OLD}
        self.next_image = NEW
        self.fail_build = False
        self.fail_precheck = False
        self.fail_start = set()
        self.fail_verify = set()
        self.resource_policy = []

    def preflight(self, profile, mounts):
        self.events.append(("preflight", profile))
        return "amd64"

    def service(self):
        return {"LoadState": "loaded" if self.paths.active.exists() else "not-found", "ActiveState": "active" if self.running else "inactive"}

    def container(self):
        return {"Image": self.image, "State": {"Running": True}} if self.running else None

    def dropins(self):
        return copy.deepcopy(self.resource_policy)

    def image_exists(self, image):
        return image in self.images

    def retain(self, image):
        self.events.append(("retain", image))
        if image not in self.images:
            raise Error("missing image")
        return "retained-" + image.split(":")[1]

    def build(self, context, update):
        self.events.append(("build", update))
        if self.fail_build:
            raise Error("build failed")
        self.images.add(self.next_image)
        return self.next_image

    def validate_unit(self, text, image):
        self.events.append(("validate", image))
        assert "Image=" + image in text and "Pull=never" in text and "--replace=false" in text

    def precheck(self, image, tools):
        self.events.append(("precheck", image))
        if self.fail_precheck:
            raise Error("precheck failed")

    def writable_diff(self):
        return "C /etc"

    def audit_adoption(self, record):
        self.events.append(("audit", record["image"]))

    def stop(self, timeout):
        self.events.append(("stop", self.image))
        self.running = False

    def start(self, timeout):
        self.image = image_id(Unit(self.paths.active.read_text()).one("Container", "Image"))
        self.events.append(("start", self.image))
        self.running = True
        if self.image in self.fail_start:
            raise Error("start failed")

    def unload(self):
        self.events.append(("unload", None))

    def verify(self, record):
        self.events.append(("verify", record["image"]))
        if not self.running or self.image != record["image"] or self.image in self.fail_verify:
            raise Error("smoke test failed")
        return {}

    def source_revision(self):
        return "test-revision"


class Fixture(unittest.TestCase):
    def setUp(self):
        parent = "/tmp/opencode" if Path("/tmp/opencode").is_dir() else None
        temporary = tempfile.TemporaryDirectory(prefix="dotfiles-lifecycle-", dir=parent)
        self.addCleanup(temporary.cleanup)
        home = Path(temporary.name) / "home"
        self.paths = Paths(home, home / ".config", home / ".local/state", home / "runtime")
        self.paths.desired.mkdir(parents=True)
        (home / "dev").mkdir()
        (home / ".gitconfig").write_text("[core]\n    editor = vi\n")
        (self.paths.desired / "dev.container").write_text(UNIT)
        self.context = self.paths.desired / "build"
        self.context.mkdir()
        (self.context / "Containerfile.dev").write_text("FROM scratch\n")
        self.config = self.paths.desired / "config.json"
        self.config.write_text(json.dumps({"version": 1, "profile": "local", "required_tools": ["sh"], "stability_seconds": 0}))
        self.backend = FakeBackend(self.paths)
        self.messages = []
        self.lifecycle = Lifecycle(self.paths, self.backend, output=self.messages.append)

    def existing(self):
        self.paths.active.parent.mkdir(parents=True)
        self.paths.active.write_text(UNIT.replace("localhost/devbox:managed", "localhost/devbox:latest"))
        self.backend.running = True
        self.backend.image = OLD
        return self.paths.active.read_text()

    def state(self):
        return self.lifecycle.store.read()


class LifecycleTests(Fixture):
    def test_dry_run_has_no_persistent_or_service_changes(self):
        original = self.existing()
        self.lifecycle.run(dry_run=True)
        self.assertEqual(self.paths.active.read_text(), original)
        self.assertFalse(self.paths.state.exists())
        self.assertFalse(any(e[0] in {"build", "retain", "start", "stop"} for e in self.backend.events))

    def test_existing_devbox_requires_explicit_adoption(self):
        original = self.existing()
        with self.assertRaisesRegex(Error, "--adopt"):
            self.lifecycle.run()
        self.assertEqual(self.paths.active.read_text(), original)
        self.assertFalse(any(e[0] == "stop" for e in self.backend.events))

    def test_build_failure_preserves_working_container(self):
        original = self.existing()
        self.backend.fail_build = True
        with self.assertRaisesRegex(Error, "build failed"):
            self.lifecycle.run(adopt=True)
        self.assertEqual(self.paths.active.read_text(), original)
        self.assertEqual(self.backend.image, OLD)
        self.assertTrue(self.backend.running)
        self.assertIsNone(self.state()["pending"])

    def test_precheck_failure_does_not_stop_working_service(self):
        original = self.existing()
        self.backend.fail_precheck = True
        with self.assertRaisesRegex(Error, "precheck failed"):
            self.lifecycle.run(adopt=True)
        self.assertEqual(self.paths.active.read_text(), original)
        self.assertFalse(any(e[0] == "stop" for e in self.backend.events))

    def test_adoption_pins_actual_predecessor_before_promotion(self):
        self.existing()
        self.lifecycle.run(adopt=True)
        state = self.state()
        self.assertEqual(state["current"]["image"], NEW)
        self.assertEqual(state["previous"]["image"], OLD)
        self.assertIn("Image=" + NEW, self.paths.active.read_text())
        self.assertIn("Image=" + OLD, state["previous"]["unit"])
        self.assertLess(self.backend.events.index(("precheck", NEW)), self.backend.events.index(("stop", OLD)))
        self.assertEqual(self.lifecycle.store.path.stat().st_mode & 0o777, 0o600)

    def test_unchanged_setup_does_not_build_or_restart(self):
        self.lifecycle.run()
        self.backend.events.clear()
        self.lifecycle.run()
        self.assertFalse(any(e[0] in {"build", "start", "stop"} for e in self.backend.events))

    def test_replaced_bind_file_restarts_without_building(self):
        self.lifecycle.run()
        self.backend.events.clear()
        replacement = self.paths.home / "replacement"
        replacement.write_text("updated config")
        os.replace(replacement, self.paths.home / ".gitconfig")
        self.lifecycle.run()
        self.assertIn(("start", NEW), self.backend.events)
        self.assertFalse(any(e[0] == "build" for e in self.backend.events))

    def test_definition_only_change_reuses_image(self):
        self.lifecycle.run()
        self.backend.events.clear()
        path = self.paths.desired / "dev.container"
        path.write_text(path.read_text().replace("Description=Development", "Description=Updated development"))
        self.lifecycle.run()
        self.assertIn(("start", NEW), self.backend.events)
        self.assertFalse(any(e[0] == "build" for e in self.backend.events))

    def test_update_builds_fresh_and_retains_previous_runtime(self):
        self.lifecycle.run()
        self.backend.next_image = NEXT
        self.lifecycle.run(update=True)
        self.assertIn(("build", True), self.backend.events)
        self.assertEqual(self.state()["current"]["image"], NEXT)
        self.assertEqual(self.state()["previous"]["image"], NEW)

    def test_start_failure_recovers_and_returns_nonzero(self):
        self.existing()
        self.backend.fail_start.add(NEW)
        with self.assertRaisesRegex(Error, "previous runtime was recovered"):
            self.lifecycle.run(adopt=True)
        self.assertEqual(self.backend.image, OLD)
        self.assertTrue(self.backend.running)
        self.assertEqual(self.state()["current"]["image"], OLD)
        self.assertIsNone(self.state()["pending"])

    def test_smoke_failure_restores_exact_image_not_latest_tag(self):
        self.existing()
        self.backend.fail_verify.add(NEW)
        with self.assertRaisesRegex(Error, "previous runtime was recovered"):
            self.lifecycle.run(adopt=True)
        self.assertIn("Image=" + OLD, self.paths.active.read_text())
        self.assertNotIn("Image=localhost/devbox:latest", self.paths.active.read_text())

    def test_failed_first_activation_removes_only_its_own_unit(self):
        self.backend.fail_verify.add(NEW)
        with self.assertRaisesRegex(Error, "failed first deployment"):
            self.lifecycle.run()
        self.assertFalse(self.paths.active.exists())
        self.assertTrue((self.paths.home / ".gitconfig").exists())
        self.assertFalse(self.backend.running)
        self.assertIsNone(self.state()["current"])

    def test_rollback_never_reads_broken_desired_config_or_builds(self):
        self.lifecycle.run()
        self.backend.next_image = NEXT
        self.lifecycle.run(update=True)
        self.backend.events.clear()
        self.config.write_text("invalid JSON")
        self.lifecycle.run(rollback=True)
        self.assertEqual(self.backend.image, NEW)
        self.assertFalse(any(e[0] == "build" for e in self.backend.events))

    def test_missing_rollback_image_fails_before_stopping(self):
        self.lifecycle.run()
        self.backend.next_image = NEXT
        self.lifecycle.run(update=True)
        self.backend.images.remove(NEW)
        self.backend.events.clear()
        with self.assertRaisesRegex(Error, "rollback image is missing"):
            self.lifecycle.run(rollback=True)
        self.assertFalse(any(e[0] == "stop" for e in self.backend.events))
        self.assertEqual(self.backend.image, NEXT)

    def test_interrupted_activation_recovers_before_any_new_build(self):
        self.lifecycle.run()
        before = self.state()["current"]
        after = copy.deepcopy(before)
        after["image"] = NEXT
        after["unit"] = Unit(before["unit"]).pinned(NEXT)
        after["observed_unit"] = after["unit"]
        self.backend.images.add(NEXT)
        state = self.state()
        state["pending"] = {"before": before, "after": after, "previous_before": state["previous"]}
        self.lifecycle.store.write(state)
        atomic_write(self.paths.active, after["unit"].encode())
        self.backend.running, self.backend.image = True, NEXT
        self.backend.events.clear()
        with self.assertRaisesRegex(Error, "Interrupted activation recovered"):
            self.lifecycle.run()
        self.assertEqual(self.backend.image, NEW)
        self.assertIsNone(self.state()["pending"])
        self.assertFalse(any(e[0] == "build" for e in self.backend.events))

    def test_manual_active_edits_are_not_discarded(self):
        self.lifecycle.run()
        self.paths.active.write_text("user changed this file\n")
        with self.assertRaisesRegex(Error, "changed outside"):
            self.lifecycle.run()
        self.assertEqual(self.paths.active.read_text(), "user changed this file\n")

    def test_external_resource_policy_is_not_reverted(self):
        self.lifecycle.run()
        self.backend.resource_policy = [{"path": "policy", "sha256": "changed", "text": "CPUWeight=200"}]
        with self.assertRaisesRegex(Error, "resource policy changed"):
            self.lifecycle.run()
        self.assertEqual(self.backend.resource_policy[0]["text"], "CPUWeight=200")

    def test_failed_recovery_keeps_pending_evidence(self):
        self.existing()
        self.backend.fail_verify.update((OLD, NEW))
        with self.assertRaisesRegex(Error, "automatic recovery failed"):
            self.lifecycle.run(adopt=True)
        self.assertIsNotNone(self.state()["pending"])

    def test_missing_active_file_is_not_silently_recreated(self):
        self.lifecycle.run()
        self.paths.active.unlink()
        self.backend.events.clear()
        with self.assertRaisesRegex(Error, "changed outside"):
            self.lifecycle.run()
        self.assertFalse(self.paths.active.exists())
        self.assertFalse(any(e[0] == "stop" for e in self.backend.events))

    def test_changed_verification_policy_is_checked_without_restart(self):
        self.lifecycle.run()
        self.backend.events.clear()
        policy = json.loads(self.config.read_text())
        policy["required_tools"] = ["sh", "git"]
        self.config.write_text(json.dumps(policy))
        with mock.patch.object(self.backend, "verify", wraps=self.backend.verify) as verify:
            self.lifecycle.run()
        self.assertEqual(verify.call_args[0][0]["required_tools"], ["sh", "git"])
        self.assertEqual(self.state()["current"]["required_tools"], ["sh", "git"])
        self.assertFalse(any(e[0] in {"build", "stop", "start"} for e in self.backend.events))

    def test_inputs_changed_during_build_do_not_replace_runtime(self):
        original = self.existing()
        build = self.backend.build
        def changing_build(context, update):
            result = build(context, update)
            (context / "Containerfile.dev").write_text("FROM changed\n")
            return result
        with mock.patch.object(self.backend, "build", side_effect=changing_build):
            with self.assertRaisesRegex(Error, "inputs changed"):
                self.lifecycle.run(adopt=True)
        self.assertEqual(self.paths.active.read_text(), original)
        self.assertFalse(any(e[0] == "stop" for e in self.backend.events))

    def test_workspace_replaced_during_build_is_detected_before_stopping(self):
        self.lifecycle.run()
        self.backend.next_image = NEXT
        self.backend.events.clear()
        build = self.backend.build
        workspace = self.paths.home / "dev"
        saved = self.paths.home / "saved-dev"
        def moving_workspace(context, update):
            image = build(context, update)
            workspace.rename(saved)
            workspace.mkdir()
            return image
        with mock.patch.object(self.backend, "build", side_effect=moving_workspace):
            with self.assertRaisesRegex(Error, "persistent mount changed"):
                self.lifecycle.run(update=True)
        self.assertTrue(saved.exists())
        self.assertEqual(self.backend.image, NEW)
        self.assertFalse(any(e[0] == "stop" for e in self.backend.events))

    def test_failure_to_write_pending_state_does_not_stop_service(self):
        self.lifecycle.run()
        original = self.paths.active.read_text()
        self.backend.events.clear()
        self.backend.next_image = NEXT
        with mock.patch.object(self.lifecycle.store, "write", side_effect=OSError("state disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                self.lifecycle.run(update=True)
        self.assertEqual(self.paths.active.read_text(), original)
        self.assertEqual(self.backend.image, NEW)
        self.assertFalse(any(e[0] == "stop" for e in self.backend.events))

    def test_commit_state_failure_recovers_old_runtime(self):
        self.lifecycle.run()
        self.backend.next_image = NEXT
        write = self.lifecycle.store.write
        def failing_commit(state):
            if state["pending"] is None and state["current"]["image"] == NEXT:
                raise OSError("commit failed")
            return write(state)
        with mock.patch.object(self.lifecycle.store, "write", side_effect=failing_commit):
            with self.assertRaisesRegex(Error, "previous runtime was recovered"):
                self.lifecycle.run(update=True)
        self.assertEqual(self.backend.image, NEW)
        self.assertIsNone(self.state()["pending"])

    def test_atomic_active_write_failure_recovers_old_runtime(self):
        self.lifecycle.run()
        self.backend.next_image = NEXT
        def failing_promotion(path, data):
            if NEXT.encode() in data:
                raise OSError("unit disk full")
            return atomic_write(path, data)
        with mock.patch("dotfiles.containers.atomic_write", side_effect=failing_promotion):
            with self.assertRaisesRegex(Error, "previous runtime was recovered"):
                self.lifecycle.run(update=True)
        self.assertEqual(self.backend.image, NEW)
        self.assertIn("Image=" + NEW, self.paths.active.read_text())

    def test_keyboard_interrupt_during_activation_recovers(self):
        self.lifecycle.run()
        self.backend.next_image = NEXT
        verify = self.backend.verify
        def interrupted(record):
            if record["image"] == NEXT:
                raise KeyboardInterrupt()
            return verify(record)
        with mock.patch.object(self.backend, "verify", side_effect=interrupted):
            with self.assertRaisesRegex(Error, "previous runtime was recovered"):
                self.lifecycle.run(update=True)
        self.assertEqual(self.backend.image, NEW)

    def test_unreviewed_adoption_source_does_not_save_or_retain_it(self):
        original = self.existing()
        with mock.patch.object(self.backend, "audit_adoption", side_effect=Error("source differs from live runtime")):
            with self.assertRaisesRegex(Error, "source differs"):
                self.lifecycle.run(adopt=True)
        self.assertEqual(self.paths.active.read_text(), original)
        self.assertFalse(self.lifecycle.store.path.exists())
        self.assertFalse(any(e[0] in {"retain", "stop", "build"} for e in self.backend.events))

    def test_rollback_dry_run_does_not_write_or_restart(self):
        self.lifecycle.run()
        self.backend.next_image = NEXT
        self.lifecycle.run(update=True)
        original = self.lifecycle.store.path.read_bytes()
        self.backend.events.clear()
        self.lifecycle.run(rollback=True, dry_run=True)
        self.assertEqual(self.lifecycle.store.path.read_bytes(), original)
        self.assertEqual(self.backend.image, NEXT)
        self.assertFalse(any(e[0] in {"retain", "stop", "start", "build"} for e in self.backend.events))

    def test_root_is_rejected_before_creating_lock_or_state(self):
        with mock.patch("dotfiles.containers.os.geteuid", return_value=0):
            with self.assertRaisesRegex(Error, "never with sudo"):
                self.lifecycle.run()
        self.assertFalse(self.paths.state.exists())

    def test_writable_workspace_change_requires_separate_migration(self):
        self.lifecycle.run()
        unit = self.paths.desired / "dev.container"
        unit.write_text(UNIT.replace("%h/dev:/home/dev:Z", "%h/elsewhere:/home/dev:Z"))
        self.backend.events.clear()
        with self.assertRaisesRegex(Error, "persistent mount changed"):
            self.lifecycle.run()
        self.assertFalse((self.paths.home / "elsewhere").exists())
        self.assertFalse(any(e[0] in {"build", "stop", "start"} for e in self.backend.events))

    def test_cli_errors_are_nonzero(self):
        with mock.patch("dotfiles.containers.Lifecycle.run", side_effect=Error("test failure")):
            with mock.patch("sys.stderr"):
                self.assertEqual(main([]), 1)


class SupportTests(Fixture):
    def test_pinning_preserves_repeated_mount_keys(self):
        parsed = Unit(UNIT)
        parsed.validate()
        pinned = Unit(parsed.pinned(NEW))
        self.assertEqual(pinned.values("Container", "Volume"), parsed.values("Container", "Volume"))
        self.assertEqual(pinned.one("Container", "Pull"), "never")
        self.assertEqual(pinned.values("Container", "PodmanArgs"), ["--replace=false"])

    def test_ambiguous_images_and_unsafe_overrides_fail_closed(self):
        for extra in ("Image=another", "PodmanArgs=--privileged", "Mount=type=bind,source=/,target=/host"):
            with self.subTest(extra=extra), self.assertRaises(Error):
                Unit(UNIT.replace("ContainerName=dev", "ContainerName=dev\n" + extra)).validate()

    def test_full_home_and_chowning_mounts_are_rejected(self):
        for value in ("%h:/home/dev:Z", "%h/dev:/home/dev:U"):
            with self.subTest(volume=value), self.assertRaises(Error):
                Unit(UNIT.replace("%h/dev:/home/dev:Z", value)).mounts(self.paths.home)

    def test_file_modes_and_numeric_user_affect_build_fingerprint(self):
        first = context_fingerprint(self.context, 1000, 1000, "amd64")
        path = self.context / "installer"
        path.write_text("install")
        second = context_fingerprint(self.context, 1000, 1000, "amd64")
        path.chmod(0o755)
        third = context_fingerprint(self.context, 1000, 1000, "amd64")
        self.assertNotEqual(first, second)
        self.assertNotEqual(second, third)
        self.assertNotEqual(third, context_fingerprint(self.context, 2000, 2000, "amd64"))

    def test_context_symlink_cannot_escape(self):
        (self.context / "outside").symlink_to(self.paths.home / ".gitconfig")
        with self.assertRaisesRegex(Error, "escape"):
            context_fingerprint(self.context, 1000, 1000, "amd64")

    def test_setup_update_rollback_share_one_lock(self):
        with self.lifecycle.store.lock():
            with self.assertRaisesRegex(Error, "Another container"):
                with Store(self.paths).lock():
                    self.fail("Concurrent lock acquired")

    def test_corrupt_pending_state_is_rejected_before_backend_operations(self):
        self.paths.state.mkdir(parents=True)
        path = self.lifecycle.store.path
        for pending in ([], {}, {"before": None, "after": None, "previous_before": None}, {"before": 1}):
            with self.subTest(pending=pending):
                atomic_write(path, json.dumps({"version": 1, "current": None, "previous": None, "pending": pending}).encode())
                with self.assertRaises(Error):
                    self.lifecycle.run(dry_run=True)
                self.assertEqual(self.backend.events, [])

    def test_record_image_unit_mismatch_is_rejected(self):
        self.lifecycle.run()
        state = self.state()
        state["current"]["image"] = OLD
        atomic_write(self.lifecycle.store.path, json.dumps(state).encode())
        self.backend.events.clear()
        with self.assertRaisesRegex(Error, "Invalid deployment record"):
            self.lifecycle.run(dry_run=True)
        self.assertEqual(self.backend.events, [])

    def test_recovery_records_require_canonical_no_pull_non_replacing_units(self):
        self.lifecycle.run()
        original = self.state()
        for old, new in (("Pull=never", "Pull=always"), ("PodmanArgs=--replace=false\n", "")):
            with self.subTest(setting=old):
                state = copy.deepcopy(original)
                unit = state["current"]["unit"].replace(old, new)
                state["current"]["unit"] = unit
                state["current"]["observed_unit"] = unit
                atomic_write(self.lifecycle.store.path, json.dumps(state).encode())
                self.backend.events.clear()
                with self.assertRaisesRegex(Error, "Invalid deployment record"):
                    self.lifecycle.run(dry_run=True)
                self.assertEqual(self.backend.events, [])

    def test_unsafe_state_permissions_are_rejected(self):
        self.lifecycle.run()
        self.lifecycle.store.path.chmod(0o644)
        with self.assertRaisesRegex(Error, "mode 0600"):
            self.lifecycle.run(dry_run=True)

    def test_lifecycle_overrides_and_conflicting_mount_options_fail(self):
        for extra in ("ExecStartPre=/bin/false", "KillMode=none", "User=another"):
            with self.subTest(extra=extra), self.assertRaises(Error):
                Unit(UNIT.replace("Restart=on-failure", "Restart=on-failure\n" + extra)).validate()
        for value in ("%h/dev:/home/dev:ro,rw", "%h/dev:/home/dev:z,Z", "%h/space path:/home/dev:Z"):
            with self.subTest(value=value), self.assertRaises(Error):
                Unit(UNIT.replace("%h/dev:/home/dev:Z", value)).mounts(self.paths.home)

    def test_configuration_rejects_invalid_profile_tools_and_timeouts(self):
        original = json.loads(self.config.read_text())
        for key, value in (("version", True), ("profile", "unknown"), ("required_tools", ["sh; exit"]), ("timeout_seconds", True), ("stability_seconds", -1), ("unknown", 1)):
            with self.subTest(key=key):
                policy = dict(original, **{key: value})
                self.config.write_text(json.dumps(policy))
                with self.assertRaises(Error):
                    Settings(self.config)

    def test_default_required_tools_match_the_deployed_template(self):
        self.config.write_text(json.dumps({"version": 1, "profile": "local"}))
        template = (SOURCE / "dot_config/dotfiles/devbox/config.json.tmpl").read_text()
        deployed = json.loads(template.replace("{{ .profile | toJson }}", '"local"'))
        self.assertEqual(Settings(self.config).tools, deployed["required_tools"])

    def test_python_39_syntax(self):
        for path in (SOURCE / "private_dot_local/lib/dotfiles").glob("*.py"):
            ast.parse(path.read_text(), filename=str(path), feature_version=(3, 9))


if __name__ == "__main__":
    unittest.main()
