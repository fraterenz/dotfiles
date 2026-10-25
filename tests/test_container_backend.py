"""Backend command/policy tests; Podman and systemd actions are mocked."""

from contextlib import ExitStack
import copy
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
import unittest
from unittest import mock

from test_containers import Fixture, NEW, UNIT
from dotfiles.container_backend import Backend
from dotfiles.container_support import Error, Runner, Unit


class BackendTests(Fixture):
    def setUp(self):
        super().setUp()
        self.runner = mock.Mock(spec=Runner)
        self.runner.run.return_value = subprocess.CompletedProcess([], 0, "", "")
        self.real_backend = Backend(self.paths, self.runner)

    def test_build_flags_numeric_identity_and_distinct_tags(self):
        commands = []
        def build(argv, **kwargs):
            commands.append(argv)
            Path(argv[argv.index("--iidfile") + 1]).write_text(NEW)
            self.assertIsNone(kwargs["timeout"])
            return subprocess.CompletedProcess(argv, 0, "", "")
        self.runner.run.side_effect = build
        self.runner.json.return_value = [{"Id": NEW}]
        with mock.patch("dotfiles.container_backend.os.getuid", return_value=1234), mock.patch("dotfiles.container_backend.os.getgid", return_value=5678):
            self.assertEqual(self.real_backend.build(self.context, False), NEW)
            self.assertEqual(self.real_backend.build(self.context, True), NEW)
        normal, update = commands
        self.assertIn("--pull=missing", normal)
        self.assertNotIn("--no-cache", normal)
        self.assertIn("--pull=always", update)
        self.assertIn("--no-cache", update)
        self.assertIn("USER_UID=1234", normal)
        self.assertIn("USER_GID=5678", normal)
        self.assertIn("USER_NAME=dev", normal)
        self.assertIn(str(self.context / "Containerfile.dev"), normal)
        self.assertNotEqual(normal[normal.index("--tag") + 1], update[update.index("--tag") + 1])
        self.assertFalse(any("localhost/devbox:latest" in argv for argv in commands))

    def test_build_image_identity_mismatch_fails(self):
        def build(argv, **kwargs):
            Path(argv[argv.index("--iidfile") + 1]).write_text(NEW)
        self.runner.run.side_effect = build
        self.runner.json.return_value = [{"Id": "sha256:" + "9" * 64}]
        with self.assertRaisesRegex(Error, "identity mismatch"):
            self.real_backend.build(self.context, False)

    def test_generator_validation_checks_real_exec_flags_not_comments(self):
        self.real_backend.generator = "/mock/quadlet"
        start = "/usr/bin/podman run --pull never --replace --replace=false " + NEW + " /usr/bin/sleep infinity"
        valid = "---dev.service---\n[Service]\nExecStart=" + start + "\n"
        def generate(argv, **kwargs):
            directory = Path(kwargs["env"]["QUADLET_UNIT_DIRS"])
            self.assertEqual((directory / "dev.container").read_text(), Unit(UNIT).pinned(NEW))
            self.assertEqual(argv, ["/mock/quadlet", "--user", "--dryrun"])
            return subprocess.CompletedProcess(argv, 0, valid, "")
        self.runner.run.side_effect = generate
        self.real_backend.validate_unit(Unit(UNIT).pinned(NEW), NEW)
        self.runner.run.side_effect = None
        for output in (
            valid.replace("--pull never", "--pull always"),
            valid.replace("--pull never", "--pull never --pull=always"),
            valid.replace("--replace=false", "--replace=false --replace"),
            valid.replace("--replace=false ", "").replace("infinity", "infinity --replace=false"),
            "---dev.service---\n# " + NEW + " --replace=false\n[Service]\nExecStart=/bin/true\n",
            "[Service]\nExecStart=" + start,
        ):
            with self.subTest(output=output):
                self.runner.run.return_value = subprocess.CompletedProcess([], 0, output, "")
                with self.assertRaises(Error):
                    self.real_backend.validate_unit(Unit(UNIT).pinned(NEW), NEW)
        self.runner.run.return_value = subprocess.CompletedProcess([], 0, valid, "unsupported key 'Example'")
        with self.assertRaisesRegex(Error, "does not support"):
            self.real_backend.validate_unit(Unit(UNIT).pinned(NEW), NEW)

    def test_precheck_never_uses_real_mounts_ports_or_network(self):
        self.real_backend.precheck(NEW, ["fish", "git"])
        command = self.runner.run.call_args[0][0]
        self.assertIn("--network=none", command)
        self.assertIn("--pull=never", command)
        self.assertIn("--entrypoint=/bin/sh", command)
        self.assertFalse(set(command) & {"--volume", "-v", "--mount", "-p", "--publish", "--name"})

    def runtime_fixture(self):
        unit = Unit(UNIT).pinned(NEW)
        policy = {"profile": "local", "required_tools": ["fish", "git"], "timeout_seconds": 300, "stability_seconds": 0}
        record = self.lifecycle.record(unit, NEW, policy, "a" * 64)
        mounts = Unit(unit).mounts(self.paths.home)
        data = {
            "Image": NEW, "State": {"Running": True},
            "Config": {"Cmd": ["/usr/bin/sleep", "infinity"], "WorkingDir": "/home/dev", "Env": ["HOME=/home/dev"]},
            "Mounts": [{"Type": "bind", "Source": m.source, "Destination": m.target, "RW": "ro" not in m.options} for m in mounts],
        }
        self.real_backend.service = mock.Mock(return_value={"LoadState": "loaded", "ActiveState": "active"})
        self.real_backend.container = mock.Mock(return_value=data)
        self.real_backend.dropins = mock.Mock(return_value=[])
        def execute(argv, **kwargs):
            output = ""
            if argv[3] == "/usr/bin/id":
                output = str(os.getuid() if argv[4] == "-u" else os.getgid())
            elif argv[3] == "/usr/bin/stat":
                source = next(m.source for m in mounts if m.target == argv[-1])
                output = str(Path(source).stat().st_ino)
            elif argv[-1] == "--version":
                output = argv[3] + " test-version\nextra details\n"
            return subprocess.CompletedProcess(argv, 0, output, "")
        self.runner.run.side_effect = execute
        return record, data

    def test_runtime_verifies_numeric_user_mount_inodes_and_tool_versions(self):
        record, data = self.runtime_fixture()
        versions = self.real_backend.verify(record)
        self.assertEqual(versions, {"fish": "fish test-version", "git": "git test-version"})
        self.assertTrue(any(row.args[0][3] == "python3" for row in self.runner.run.call_args_list))
        data["Mounts"][1]["RW"] = True
        with self.assertRaisesRegex(Error, "incorrect read-only mode"):
            self.real_backend.verify(record)

    def test_stale_mount_inode_is_detected(self):
        record, _ = self.runtime_fixture()
        run = self.runner.run.side_effect
        def stale(argv, **kwargs):
            if argv[3] == "/usr/bin/stat":
                return subprocess.CompletedProcess(argv, 0, "0", "")
            return run(argv, **kwargs)
        self.runner.run.side_effect = stale
        with self.assertRaisesRegex(Error, "replaced mount inode"):
            self.real_backend.verify(record)

    def test_adoption_audit_is_read_only_and_rejects_runtime_mismatch(self):
        record, data = self.runtime_fixture()
        self.real_backend.audit_adoption(record)
        self.runner.run.assert_not_called()
        data["Config"]["Cmd"] = ["/unreviewed/command"]
        with self.assertRaisesRegex(Error, "running command"):
            self.real_backend.audit_adoption(record)

    def test_adoption_image_change_during_review_is_rejected(self):
        record, data = self.runtime_fixture()
        data["Image"] = "sha256:" + "9" * 64
        with self.assertRaisesRegex(Error, "image changed during adoption"):
            self.real_backend.audit_adoption(record)

    def test_foreign_retention_tag_is_not_moved(self):
        self.runner.json.return_value = [{"Id": "sha256:" + "9" * 64}]
        with self.assertRaisesRegex(Error, "refusing to move"):
            self.real_backend.retain(NEW)
        self.assertFalse(any(row.args[0][:3] == ["podman", "image", "tag"] for row in self.runner.run.call_args_list))

    def policy_paths(self, loaded=False):
        root = self.paths.home / "systemd"
        root.mkdir()
        self.real_backend.service = mock.Mock(return_value={"LoadState": "loaded" if loaded else "not-found", "DropInPaths": ""})
        self.runner.run.return_value = subprocess.CompletedProcess([], 0, str(root) + "\n", "")
        return root

    def test_passive_resource_dropins_are_saved_without_rewriting(self):
        root = self.policy_paths()
        generic = root / "service.d/10-policy.conf"
        specific = root / "dev.service.d/20-resources.conf"
        generic.parent.mkdir()
        specific.parent.mkdir()
        generic.write_text("[Service]\nTimeoutStopFailureMode=abort\n")
        specific.write_text("[Service]\nCPUWeight=100\nIOWeight=100\n")
        policies = self.real_backend.dropins()
        self.assertEqual({row["path"] for row in policies}, {str(generic), str(specific)})
        self.assertEqual(specific.read_text(), "[Service]\nCPUWeight=100\nIOWeight=100\n")

    def test_new_unloaded_lifecycle_dropin_is_rejected_before_reload(self):
        root = self.policy_paths(loaded=True)
        path = root / "dev.service.d/override.conf"
        path.parent.mkdir()
        path.write_text("[Service]\nExecStart=\nExecStart=/bin/true\n")
        with self.assertRaisesRegex(Error, "Unreviewed service drop-in"):
            self.real_backend.dropins()
        self.assertIn("ExecStart=/bin/true", path.read_text())

    def test_new_unloaded_resource_policy_requires_separate_review(self):
        root = self.policy_paths(loaded=True)
        path = root / "dev.service.d/cpu.conf"
        path.parent.mkdir()
        path.write_text("[Service]\nCPUWeight=200\n")
        with self.assertRaisesRegex(Error, "not loaded"):
            self.real_backend.dropins()

    def test_specific_dropin_precedes_generic_same_basename(self):
        root = self.policy_paths()
        for name, value in (("service.d", 100), ("dev.service.d", 200)):
            path = root / name / "cpu.conf"
            path.parent.mkdir()
            path.write_text("[Service]\nCPUWeight=" + str(value) + "\n")
        policies = self.real_backend.dropins()
        self.assertEqual(len(policies), 1)
        self.assertIn("CPUWeight=200", policies[0]["text"])

    def test_unmanaged_name_is_rejected_before_service_stop(self):
        self.runner.json.return_value = [{"Config": {"Labels": {}}}]
        with self.assertRaisesRegex(Error, "unmanaged container"):
            self.real_backend.stop(300)
        self.assertEqual(self.runner.run.call_args_list, [mock.call(["podman", "container", "exists", "dev"], check=False)])

    def test_container_left_after_service_stop_is_not_forcibly_removed(self):
        self.real_backend.container = mock.Mock(return_value={"owned": True})
        self.real_backend.service = mock.Mock(return_value={"LoadState": "loaded"})
        with self.assertRaisesRegex(Error, "remains after stopping"):
            self.real_backend.stop(300)
        self.assertEqual(self.runner.run.call_args_list, [mock.call(["systemctl", "--user", "stop", "dev.service"], timeout=315)])

    def test_start_resets_failure_limit_and_never_replaces_existing_container(self):
        self.real_backend.container = mock.Mock(return_value={"owned": True})
        with self.assertRaisesRegex(Error, "already exists"):
            self.real_backend.start(300)
        self.runner.run.assert_not_called()
        self.real_backend.container.return_value = None
        self.real_backend.dropins = mock.Mock(return_value=[])
        self.real_backend.start(300)
        self.assertEqual([row.args[0] for row in self.runner.run.call_args_list], [
            ["systemctl", "--user", "daemon-reload"],
            ["systemctl", "--user", "reset-failed", "dev.service"],
            ["systemctl", "--user", "start", "dev.service"],
        ])

    def test_foreign_native_service_is_not_managed(self):
        self.runner.run.return_value = subprocess.CompletedProcess([], 0, "LoadState=loaded\nSourcePath=\nFragmentPath=/etc/systemd/user/dev.service\n", "")
        with self.assertRaisesRegex(Error, "not generated"):
            self.real_backend.service()

    def test_network_and_unknown_filesystems_are_rejected(self):
        for kind in ("nfs4", "cifs", ""):
            with self.subTest(kind=kind):
                self.runner.run.return_value = subprocess.CompletedProcess([], 0, kind, "")
                with self.assertRaises(Error):
                    self.real_backend.check_local_filesystem(self.paths.home, "workspace")

    def test_quadlet_dropins_and_other_units_using_dev_are_conflicts(self):
        root = self.paths.active.parent
        root.mkdir(parents=True)
        other = root / "another.container"
        other.write_text("[Container]\nContainerName=dev\n")
        with self.assertRaisesRegex(Error, "Conflicting Quadlet"):
            self.real_backend.check_quadlet_conflicts()
        other.unlink()
        dropin = root / "dev.container.d/override.conf"
        dropin.parent.mkdir()
        dropin.write_text("[Container]\nPodmanArgs=--privileged\n")
        with self.assertRaisesRegex(Error, "Quadlet drop-ins"):
            self.real_backend.check_quadlet_conflicts()

    def test_preflight_profiles_and_rootless_prerequisites(self):
        base = {"host": {"arch": "amd64", "security": {"rootless": True}, "cgroupVersion": "v2", "cgroupManager": "systemd", "idMappings": {"uidmap": [{"size": 65536}], "gidmap": [{"size": 65536}]}}, "store": {"graphRoot": str(self.paths.home / "storage")}}
        def preflight(profile, release, info):
            self.runner.json.return_value = info
            real_read = Path.read_text
            real_exists = Path.exists
            with ExitStack() as stack:
                stack.enter_context(mock.patch("dotfiles.container_backend.os.geteuid", return_value=1000))
                stack.enter_context(mock.patch("dotfiles.container_backend.pwd.getpwuid", return_value=SimpleNamespace(pw_dir=str(self.paths.home))))
                stack.enter_context(mock.patch("dotfiles.container_backend.shutil.which", return_value="/usr/bin/tool"))
                stack.enter_context(mock.patch("dotfiles.container_backend.os.access", return_value=True))
                stack.enter_context(mock.patch.dict("os.environ", {"QUADLET_UNIT_DIRS": ""}))
                stack.enter_context(mock.patch.object(Path, "read_text", lambda p, *a, **k: release if str(p) == "/etc/os-release" else real_read(p, *a, **k)))
                stack.enter_context(mock.patch.object(Path, "exists", lambda p: False if str(p) == "/run/ostree-booted" else real_exists(p)))
                stack.enter_context(mock.patch.object(self.real_backend, "check_local_filesystem"))
                stack.enter_context(mock.patch.object(self.real_backend, "check_quadlet_conflicts"))
                stack.enter_context(mock.patch.object(self.real_backend, "dropins", return_value=[]))
                return self.real_backend.preflight(profile, Unit(UNIT).mounts(self.paths.home))
        rocky = 'ID=rocky\nVERSION_ID="9.6"\n'
        self.assertEqual(preflight("work", rocky, base), "amd64")
        with self.assertRaisesRegex(Error, "does not match"):
            preflight("local", rocky, base)
        for key, value in (("cgroupVersion", "v1"), ("cgroupManager", "cgroupfs"), ("security", {"rootless": False}), ("idMappings", {}), ("serviceIsRemote", True)):
            with self.subTest(key=key):
                info = copy.deepcopy(base)
                info["host"][key] = value
                with self.assertRaises(Error):
                    preflight("work", rocky, info)


if __name__ == "__main__":
    unittest.main()
