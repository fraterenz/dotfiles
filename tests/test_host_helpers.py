"""Host provisioning tests: fake package managers, no network or host mutations."""

import ast
from contextlib import redirect_stderr
from copy import deepcopy
import http.client
import io
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


SOURCE = Path(__file__).resolve().parents[1]
LIBRARY = SOURCE / "private_dot_local/lib"
sys.path.insert(0, str(LIBRARY))

from dotfiles_host.core import CompatibilityError, ConfigurationError, Error, Host, RebootRequired, Runner, atomic_json, detect_host, host_lock, sha256
from dotfiles_host.downloads import GitHub, SafeRedirect, ToolUpdater, expected_digest, extract_executable, select_asset, validate_elf
from dotfiles_host.host import HostManager, enabled_repositories, flatpak_flags
from dotfiles_host.manifest import Manifest, work_policy
from dotfiles_host.cli import main


def scratch():
    return tempfile.TemporaryDirectory(prefix="dotfiles-host-", dir="/tmp/opencode" if Path("/tmp/opencode").is_dir() else None)


def manifest_data(profile="local"):
    # Only the profile is templated in the shared JSON manifest.
    text = (SOURCE / "dot_config/dotfiles/software.json.tmpl").read_text()
    return json.loads(text.replace("{{ .profile | toJson }}", json.dumps(profile)))


def result(output="", code=0, stderr=""):
    return SimpleNamespace(stdout=output, returncode=code, stderr=stderr)


class FakeRunner:
    def __init__(self, dry_run=False):
        self.dry_run = dry_run
        self.events, self.messages = [], []
        self.missing = set()
        self.pending = []
        self.flatpaks = {"com.google.Chrome", "io.mpv.Mpv", "org.gnome.Papers", "org.zotero.Zotero"}
        self.remotes = {"flathub"}
        self.fail_action = None
        self.bad_candidate = False

    def emit(self, message):
        self.messages.append(message)

    def capture(self, argv, check=True, timeout=60):
        self.events.append(("query", list(argv)))
        if argv[0] == "rpm":
            return result(code=1 if argv[-1] in self.missing else 0)
        if argv[:2] == ["rpm-ostree", "status"]:
            return result(json.dumps({"deployments": self.pending + [{"booted": True, "requested-packages": []}]}))
        if argv[:2] == ["flatpak", "list"]:
            return result("\n".join(sorted(self.flatpaks)))
        if argv[:2] == ["flatpak", "remotes"]:
            return result("\n".join(sorted(self.remotes)))
        if argv[-1] == "--version":
            path = Path(argv[0])
            if self.bad_candidate and path.name == "candidate":
                return result(code=1)
            payload = path.read_bytes()
            return result("demo " + ("2.0.0" if b"2.0.0" in payload else "1.0.0"))
        raise AssertionError("Unexpected test command: " + repr(argv))

    def action(self, argv):
        self.events.append(("action", list(argv)))
        if self.fail_action and self.fail_action(argv):
            raise Error("simulated host action failure")
        if self.dry_run:
            return
        if argv[:2] == ["rpm-ostree", "install"]:
            self.pending = [{"booted": False, "staged": True, "requested-packages": argv[3:]}]
        if argv[:2] == ["rpm-ostree", "upgrade"]:
            self.pending = [{"booted": False, "staged": True, "requested-packages": []}]


def elf(version="2.0.0", machine=62):
    header = bytearray(24)
    header[:6] = b"\x7fELF\x02\x01"
    struct.pack_into("<H", header, 18, machine)
    return bytes(header) + (" demo " + version).encode()


def archive_bytes(payload=None, member="demo", symlink=False, duplicate=False):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        info = tarfile.TarInfo(member)
        if symlink:
            info.type, info.linkname = tarfile.SYMTYPE, "elsewhere"
            archive.addfile(info)
        else:
            payload = payload if payload is not None else elf()
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
            if duplicate:
                other = tarfile.TarInfo("second/demo")
                other.size = len(payload)
                archive.addfile(other, io.BytesIO(payload))
    return stream.getvalue()


def demo_tool():
    return {"id": "demo", "executable": "demo", "repository": "example/demo", "version": {"policy": "latest"}, "aliases": [], "profiles": ["local", "work"], "assets": {"x86_64": ["demo-{tag}.tar.gz"]}, "compatibility": "Test fixture"}


class FakeGitHub:
    def __init__(self, payload=None):
        import hashlib
        self.payload = payload if payload is not None else archive_bytes()
        self.calls, self.downloads = 0, 0
        self.fail_download = False
        self.corrupt = False
        self.asset = {"name": "demo-v2.0.0.tar.gz", "size": len(self.payload), "digest": "sha256:" + hashlib.sha256(self.payload).hexdigest(), "browser_download_url": "https://github.com/example/demo/releases/download/v2.0.0/demo-v2.0.0.tar.gz"}
        self.metadata = {"tag_name": "v2.0.0", "assets": [self.asset]}

    def release(self, tool):
        self.calls += 1
        return self.metadata

    def download(self, asset, path):
        self.downloads += 1
        if self.fail_download:
            raise Error("simulated download failure")
        data = self.payload[:-1] + bytes([self.payload[-1] ^ 1]) if self.corrupt else self.payload
        Path(path).write_bytes(data)

    def checksum_text(self, asset):
        return self.asset["digest"].split(":")[1] + "  " + self.asset["name"] + "\n"


class ManifestTests(unittest.TestCase):
    def test_both_profiles_and_existing_downloads(self):
        for profile in ("local", "work"):
            m = Manifest(manifest_data(profile))
            self.assertEqual({t["executable"] for t in m.tools}, {"chezmoi", "bat", "fzf", "glow", "rg", "zoxide"})
            self.assertNotIn("fish", {t["executable"] for t in m.tools})
            self.assertNotIn("chezmoi", {p["name"] for p in m.packages})

    def test_conflicting_ownership_is_rejected(self):
        data = manifest_data()
        data["downloaded_tools"][0]["aliases"] = ["rg"]
        with self.assertRaises(ConfigurationError):
            Manifest(data)

    def test_malformed_pins_and_package_options_are_rejected(self):
        for edit in (lambda d: d.update(schema_version=True), lambda d: d["downloaded_tools"][0].update(version={"policy": "pinned"}), lambda d: d["profiles"]["local"]["native_packages"][0].update(name="--bad-option")):
            data = manifest_data()
            edit(data)
            with self.assertRaises(ConfigurationError):
                Manifest(data)

    def test_work_policy_is_explicit_and_restricted(self):
        with scratch() as tmp:
            path = Path(tmp) / "policy.json"
            policy = json.loads((SOURCE / "docs/host-policy.example.json").read_text())
            path.write_text(json.dumps(policy))
            with self.assertRaises(ConfigurationError):
                work_policy(path, Manifest(manifest_data("work")))
            policy["approved"] = True
            path.write_text(json.dumps(policy))
            self.assertTrue(work_policy(path, Manifest(manifest_data("work")))["approved"])
            for override in ("--enablerepo=unapproved", "--repo=unapproved", "--enablerep=unapproved", "-c/etc/other.conf", "--downloadonly", "--help"):
                with self.subTest(override=override):
                    policy["dnf"]["update_args"] = ["upgrade", override]
                    path.write_text(json.dumps(policy))
                    with self.assertRaises(ConfigurationError):
                        work_policy(path, Manifest(manifest_data("work")))

    def test_bad_checksum_templates_and_escaping_destinations_are_rejected(self):
        for values in ({"checksum_asset": "checksums-{unknown}.txt"}, {"destination": "../bin/chezmoi"}, {"destination": "/usr/bin/chezmoi"}):
            with self.subTest(values=values):
                data = manifest_data()
                data["downloaded_tools"][0].update(values)
                with self.assertRaises(ConfigurationError):
                    Manifest(data)

    def test_flatpak_policy_ids_cannot_be_remapped_to_another_scope(self):
        data = manifest_data()
        data["profiles"]["local"]["flatpak_installations"][0].update(scope="system", name="other")
        with self.assertRaisesRegex(ConfigurationError, "actual scope"):
            Manifest(data)


class HostDetectionTests(unittest.TestCase):
    def test_actual_os_not_profile_alone_selects_backend(self):
        with scratch() as tmp:
            release = Path(tmp) / "os-release"
            release.write_text('ID=fedora\nVARIANT_ID="sway-atomic"\nVERSION_ID=44\n')
            self.assertEqual(detect_host("local", release, True, "x86_64").os_id, "fedora")
            with self.assertRaises(ConfigurationError):
                detect_host("local", release, False, "x86_64")
            with self.assertRaises(ConfigurationError):
                detect_host("work", release, True, "x86_64")
            release.write_text('ID=rocky\nVERSION_ID="9.7"\n')
            self.assertEqual(detect_host("work", release, False, "arm64").architecture, "aarch64")
            with self.assertRaises(ConfigurationError):
                detect_host("work", release, False, "unsupported")

    def test_lock_excludes_concurrent_replacement_and_dry_run_does_not_create_state(self):
        with scratch() as tmp:
            state = Path(tmp) / "state"
            with host_lock(state, dry_run=True):
                self.assertFalse(state.exists())
            with host_lock(state):
                with self.assertRaises(Error):
                    with host_lock(state):
                        pass


class HostPhaseTests(unittest.TestCase):
    def setUp(self):
        self.flatpak = patch("dotfiles_host.host.shutil.which", return_value="flatpak")
        self.flatpak.start()
        self.addCleanup(self.flatpak.stop)

    def make_manager(self, tmp, runner, profile="local"):
        home = Path(tmp) / "home"
        home.mkdir()
        manager = HostManager(Manifest(manifest_data(profile)), Host(profile, "fedora" if profile == "local" else "rocky", "x86_64"), runner, home, home / ".local/state/dotfiles", home / "host-policy.json", repo_directory=Path(tmp) / "repos")
        manager.updater = SimpleNamespace(reconcile=lambda tool, update=False: runner.events.append(("tool", tool["id"])))
        return manager

    def test_native_and_all_flatpaks_precede_user_tools(self):
        with scratch() as tmp:
            runner = FakeRunner()
            manager = self.make_manager(tmp, runner)
            manager.run(update=True)
            events = [e for e in runner.events if e[0] in ("action", "tool")]
            self.assertEqual(events[0], ("action", ["rpm-ostree", "upgrade"]))
            self.assertEqual([e[1][2] for e in events[1:3]], ["--user", "--system"])
            self.assertTrue(all(e[0] == "tool" for e in events[3:]))

    def test_failed_host_step_blocks_all_user_tool_activity(self):
        for fail in (lambda a: a[0] == "rpm-ostree", lambda a: a[0] == "flatpak" and "--system" in a):
            with self.subTest(fail=fail), scratch() as tmp:
                runner = FakeRunner()
                runner.fail_action = fail
                manager = self.make_manager(tmp, runner)
                with self.assertRaises(Error):
                    manager.run(update=True)
                self.assertFalse(any(e[0] == "tool" for e in runner.events))

    def test_atomic_reboot_checkpoint_and_pending_rerun(self):
        with scratch() as tmp:
            runner = FakeRunner()
            runner.missing = {"fish"}
            manager = self.make_manager(tmp, runner)
            with self.assertRaises(RebootRequired):
                manager.run()
            actions = [e for e in runner.events if e[0] == "action"]
            self.assertEqual(actions, [("action", ["rpm-ostree", "install", "--idempotent", "fish"])])
            runner.events.clear()
            with self.assertRaises(RebootRequired):
                manager.run()
            self.assertFalse(any(e[0] in ("action", "tool") for e in runner.events))
            runner.missing.clear()
            runner.pending.clear()
            manager.run()
            self.assertTrue(any(e[0] == "tool" for e in runner.events))

    def test_missing_repository_is_not_silently_added(self):
        with scratch() as tmp:
            runner = FakeRunner()
            runner.missing = {"ghostty"}
            manager = self.make_manager(tmp, runner)
            with self.assertRaisesRegex(Error, "repositories"):
                manager.run()
            self.assertFalse(any(e[0] == "action" for e in runner.events))

    def test_missing_flatpak_remote_blocks_user_phase(self):
        with scratch() as tmp:
            runner = FakeRunner()
            runner.flatpaks.clear()
            runner.remotes.clear()
            manager = self.make_manager(tmp, runner)
            with patch("dotfiles_host.host.shutil.which", return_value="flatpak"), self.assertRaisesRegex(Error, "remote"):
                manager.run()
            self.assertFalse(any(e[0] == "tool" for e in runner.events))

    def test_unknown_bin_entries_are_reported_and_untouched(self):
        with scratch() as tmp:
            runner = FakeRunner()
            manager = self.make_manager(tmp, runner)
            unknown = manager.home / ".local/bin/unregistered"
            unknown.parent.mkdir(parents=True)
            unknown.write_text("do not execute or replace")
            with self.assertRaisesRegex(Error, "incomplete"):
                manager.user_tools(update=True)
            self.assertEqual(unknown.read_text(), "do not execute or replace")
            self.assertTrue(any("Unregistered" in m for m in runner.messages))

    def test_work_is_blocked_until_policy_exists(self):
        with scratch() as tmp:
            runner = FakeRunner()
            manager = self.make_manager(tmp, runner, "work")
            with self.assertRaises(ConfigurationError):
                manager.run(update=True)
            self.assertEqual(runner.events, [])

    def test_work_uses_only_explicit_approved_repositories(self):
        with scratch() as tmp:
            runner = FakeRunner()
            manager = self.make_manager(tmp, runner, "work")
            manager.repo_directory.mkdir()
            (manager.repo_directory / "site.repo").write_text('[approved]\nenabled=1\n[other]\nenabled=0\n')
            manager.policy_path.write_text(json.dumps({"approved": True, "dnf": {"repositories": ["approved"], "install_args": ["install", "-y"], "update_args": ["upgrade", "--security", "-y"]}, "flatpak_installations": []}))
            manager.policy_path.chmod(0o600)
            manager.run(update=True)
            action = next(e[1] for e in runner.events if e[0] == "action")
            self.assertEqual(action, ["sudo", "/usr/bin/dnf", "--disablerepo=*", "--enablerepo=approved", "upgrade", "--security", "-y"])

    def test_manager_is_delegated_once_after_host_steps(self):
        with scratch() as tmp:
            runner = FakeRunner()
            manager = self.make_manager(tmp, runner)
            manager.manifest.managers = [{"id": "example-manager", "entries": ["managed-tool", "managed-alias"], "install_argv": ["example-manager", "install"], "update_argv": ["example-manager", "update"]}]
            directory = manager.home / ".local/bin"
            directory.mkdir(parents=True)
            (directory / "managed-tool").write_text("owned by manager")
            (directory / "managed-alias").symlink_to("managed-tool")
            manager.run(update=True)
            self.assertEqual(sum(e == ("action", ["example-manager", "update"]) for e in runner.events), 1)

    def test_named_flatpak_installations_are_all_updated_before_user_tools(self):
        with scratch() as tmp:
            runner = FakeRunner()
            manager = self.make_manager(tmp, runner)
            manager.manifest.installations.append({"id": "extra", "scope": "system", "name": "extra"})
            manager.run(update=True)
            events = [e for e in runner.events if e[0] in ("action", "tool")]
            self.assertEqual(events[3], ("action", ["flatpak", "update", "--installation=extra", "--noninteractive", "--assumeyes"]))
            self.assertEqual(events[4][0], "tool")

    def test_unapproved_work_flatpak_scope_blocks_before_any_action(self):
        with scratch() as tmp:
            runner = FakeRunner()
            manager = self.make_manager(tmp, runner, "work")
            manager.manifest.installations.append({"id": "user", "scope": "user"})
            manager.policy_path.write_text(json.dumps({"approved": True, "dnf": {"repositories": ["approved"], "install_args": ["install", "-y"], "update_args": ["upgrade", "-y"]}, "flatpak_installations": []}))
            with self.assertRaisesRegex(ConfigurationError, "Flatpak"):
                manager.run(update=True)
            self.assertEqual(runner.events, [])


class DownloadTests(unittest.TestCase):
    def prepare(self, tmp, payload=None, dry_run=False):
        home = Path(tmp) / "home"
        target = home / ".local/bin/demo"
        target.parent.mkdir(parents=True)
        target.write_bytes(elf("1.0.0"))
        target.chmod(0o755)
        runner, client = FakeRunner(dry_run), FakeGitHub(payload)
        updater = ToolUpdater(home, home / ".local/state/dotfiles", Host("local", "fedora", "x86_64"), runner, client)
        return target, updater, runner, client

    def test_setup_skips_existing_and_updates_back_up_previous_binary(self):
        with scratch() as tmp:
            target, updater, runner, client = self.prepare(tmp)
            old = target.read_bytes()
            updater.reconcile(demo_tool())
            self.assertEqual(client.calls, 0)
            updater.reconcile(demo_tool(), update=True)
            self.assertIn(b"2.0.0", target.read_bytes())
            record = json.loads((updater.state / "tools/demo/installed.json").read_text())
            self.assertEqual(Path(record["previous_binary"]).read_bytes(), old)
            self.assertEqual(target.stat().st_mode & 0o777, 0o755)

    def test_download_checksum_and_compatibility_failures_keep_original(self):
        for failure in ("download", "checksum", "probe", "architecture"):
            with self.subTest(failure=failure), scratch() as tmp:
                payload = archive_bytes(elf(machine=183)) if failure == "architecture" else None
                target, updater, runner, client = self.prepare(tmp, payload)
                original = target.read_bytes()
                client.fail_download = failure == "download"
                client.corrupt = failure == "checksum"
                runner.bad_candidate = failure == "probe"
                with self.assertRaises(Error):
                    updater.reconcile(demo_tool(), update=True)
                self.assertEqual(target.read_bytes(), original)

    def test_unsafe_archive_paths_symlinks_and_duplicates_are_rejected(self):
        for kwargs in ({"member": "../demo"}, {"symlink": True}, {"duplicate": True}):
            with self.subTest(kwargs=kwargs), scratch() as tmp:
                target, updater, runner, client = self.prepare(tmp, archive_bytes(**kwargs))
                old = target.read_bytes()
                with self.assertRaises(Error):
                    updater.reconcile(demo_tool(), update=True)
                self.assertEqual(target.read_bytes(), old)

    def test_no_digest_is_fail_closed_and_checksum_fallback_is_supported(self):
        client = FakeGitHub()
        asset = deepcopy(client.asset)
        asset.pop("digest")
        release = {"tag_name": "v2.0.0", "assets": [asset]}
        with self.assertRaises(Error):
            expected_digest(demo_tool(), release, asset, client)
        tool = demo_tool()
        tool["checksum_asset"] = "checksums.txt"
        checksum_asset = dict(asset, name="checksums.txt")
        release["assets"].append(checksum_asset)
        self.assertEqual(expected_digest(tool, release, asset, client), client.asset["digest"].split(":")[1])

    def test_dry_run_does_not_download_archives_create_state_or_replace_files(self):
        with scratch() as tmp:
            target, updater, runner, client = self.prepare(tmp, dry_run=True)
            before = {str(p.relative_to(tmp)) for p in Path(tmp).rglob("*")}
            old = target.read_bytes()
            updater.reconcile(demo_tool(), update=True)
            self.assertEqual(client.downloads, 0)
            self.assertEqual(target.read_bytes(), old)
            self.assertEqual(before, {str(p.relative_to(tmp)) for p in Path(tmp).rglob("*")})

    def test_foreign_alias_is_not_overwritten_and_blocks_replacement(self):
        with scratch() as tmp:
            target, updater, runner, client = self.prepare(tmp)
            alias = target.parent / "alias"
            alias.write_text("user work")
            tool = demo_tool()
            tool["aliases"] = ["alias"]
            with self.assertRaises(Error):
                updater.reconcile(tool, update=True)
            self.assertEqual(alias.read_text(), "user work")
            self.assertIn(b"1.0.0", target.read_bytes())

    def test_record_failure_restores_old_binary(self):
        with scratch() as tmp:
            target, updater, runner, client = self.prepare(tmp)
            old = target.read_bytes()
            from dotfiles_host import downloads
            original = downloads.atomic_json
            def fail_installed(path, record):
                if Path(path).name == "installed.json":
                    raise OSError("simulated state write failure")
                return original(path, record)
            with patch("dotfiles_host.downloads.atomic_json", side_effect=fail_installed), self.assertRaises(OSError):
                updater.reconcile(demo_tool(), update=True)
            self.assertEqual(target.read_bytes(), old)

    def test_first_install_and_record_failure_leave_no_half_installation(self):
        for fail_record in (False, True):
            with self.subTest(fail_record=fail_record), scratch() as tmp:
                target, updater, runner, client = self.prepare(tmp)
                target.unlink()
                if fail_record:
                    with patch("dotfiles_host.downloads.atomic_json", side_effect=OSError("simulated record failure")), self.assertRaises(OSError):
                        updater.reconcile(demo_tool())
                    self.assertFalse(target.exists())
                else:
                    updater.reconcile(demo_tool())
                    self.assertIn(b"2.0.0", target.read_bytes())
                    record = json.loads((updater.state / "tools/demo/installed.json").read_text())
                    self.assertIsNone(record["previous_binary"])
                self.assertEqual(list(target.parent.glob(".dotfiles-*")), [])

    def test_interruption_during_record_write_restores_previous_binary(self):
        with scratch() as tmp:
            target, updater, runner, client = self.prepare(tmp)
            old = target.read_bytes()
            from dotfiles_host import downloads
            original = downloads.atomic_json
            def interrupt_installed(path, record):
                if Path(path).name == "installed.json":
                    raise KeyboardInterrupt()
                return original(path, record)
            with patch("dotfiles_host.downloads.atomic_json", side_effect=interrupt_installed), self.assertRaises(KeyboardInterrupt):
                updater.reconcile(demo_tool(), update=True)
            self.assertEqual(target.read_bytes(), old)

    def test_late_record_write_failure_also_restores_previous_record(self):
        for had_record in (False, True):
            with self.subTest(had_record=had_record), scratch() as tmp:
                target, updater, runner, client = self.prepare(tmp)
                old = target.read_bytes()
                record_path = updater.state / "tools/demo/installed.json"
                previous = {"version": "1.0.0", "sha256": sha256(target)}
                if had_record:
                    atomic_json(record_path, previous)
                from dotfiles_host import downloads
                original = downloads.atomic_json
                def fail_after_write(path, record):
                    original(path, record)
                    if Path(path).name == "installed.json":
                        raise OSError("simulated fsync failure after record replacement")
                with patch("dotfiles_host.downloads.atomic_json", side_effect=fail_after_write), self.assertRaises(OSError):
                    updater.reconcile(demo_tool(), update=True)
                self.assertEqual(target.read_bytes(), old)
                if had_record:
                    self.assertEqual(json.loads(record_path.read_text()), previous)
                else:
                    self.assertFalse(record_path.exists())

    def test_concurrent_destination_change_is_not_overwritten(self):
        with scratch() as tmp:
            target, updater, runner, client = self.prepare(tmp)
            original_download = client.download
            def change_destination(asset, path):
                original_download(asset, path)
                target.write_bytes(elf("3.0.0"))
            client.download = change_destination
            with self.assertRaisesRegex(Error, "concurrently"):
                updater.reconcile(demo_tool(), update=True)
            self.assertIn(b"3.0.0", target.read_bytes())

    def test_symlink_destinations_are_not_replaced(self):
        with scratch() as tmp:
            target, updater, runner, client = self.prepare(tmp)
            original = target.parent / "original"
            target.rename(original)
            target.symlink_to(original.name)
            with self.assertRaisesRegex(Error, "symlink"):
                updater.reconcile(demo_tool(), update=True)
            self.assertEqual(os.readlink(target), "original")
            self.assertIn(b"1.0.0", original.read_bytes())

    def test_pins_and_aliases_have_one_owner(self):
        with scratch() as tmp:
            target, updater, runner, client = self.prepare(tmp)
            tool = demo_tool()
            tool["aliases"] = ["alias"]
            tool["version"] = {"policy": "pinned", "tag": "v1.0.0"}
            updater.reconcile(tool, update=True)
            self.assertEqual(client.calls, 0)
            self.assertEqual(os.readlink(target.parent / "alias"), "demo")
            tool["version"]["tag"] = "v2.0.0"
            with self.assertRaises(Error):
                updater.reconcile(tool)


class HTTPTests(unittest.TestCase):
    def test_interrupted_and_truncated_downloads_retry_without_live_installation(self):
        for interrupted in (False, True):
            with self.subTest(interrupted=interrupted), scratch() as tmp:
                client = GitHub(token="")
                asset = {"size": 4, "browser_download_url": "https://github.com/example/demo/releases/download/v2.0.0/demo.tar.gz"}
                target = Path(tmp) / "release.tar.gz"
                with patch.object(client, "open") as request, patch("dotfiles_host.downloads.time.sleep") as delay:
                    if interrupted:
                        request.side_effect = http.client.IncompleteRead(b"partial")
                    else:
                        request.side_effect = lambda url: io.BytesIO(b"bad")
                    with self.assertRaisesRegex(Error, "three attempts"):
                        client.download(asset, target)
                    self.assertEqual(request.call_count, 3)
                    self.assertEqual(delay.call_count, 2)

    def test_successful_retry_discards_partial_archive(self):
        with scratch() as tmp:
            client = GitHub(token="")
            asset = {"size": 4, "browser_download_url": "https://github.com/example/demo/releases/download/v2.0.0/demo.tar.gz"}
            target = Path(tmp) / "release.tar.gz"
            with patch.object(client, "open", side_effect=[io.BytesIO(b"bad"), io.BytesIO(b"good")]), patch("dotfiles_host.downloads.time.sleep"):
                client.download(asset, target)
            self.assertEqual(target.read_bytes(), b"good")

    def test_metadata_and_checksum_interruptions_are_reported(self):
        client = GitHub(token="")
        with patch.object(client, "open", side_effect=http.client.IncompleteRead(b"partial")):
            with self.assertRaisesRegex(Error, "interrupted"):
                client.release(demo_tool())
            with self.assertRaisesRegex(Error, "interrupted"):
                client.checksum_text({"size": 4, "browser_download_url": "https://github.com/checksums"})

    def test_release_policy_rejects_prerelease_and_pin_mismatch(self):
        client, fixture = GitHub(token=""), FakeGitHub()
        for metadata, version in ((dict(fixture.metadata, prerelease=True), {"policy": "latest"}), (fixture.metadata, {"policy": "pinned", "tag": "v1.0.0"})):
            with self.subTest(version=version), patch.object(client, "open", return_value=io.BytesIO(json.dumps(metadata).encode())):
                tool = demo_tool()
                tool["version"] = version
                with self.assertRaises(Error):
                    client.release(tool)

    def test_cross_host_redirects_do_not_forward_github_credentials(self):
        import urllib.request
        request = urllib.request.Request("https://api.github.com/releases", headers={"Authorization": "Bearer test-only"})
        handler = SafeRedirect()
        redirected = handler.redirect_request(request, None, 302, "Found", {}, "https://release-assets.githubusercontent.com/archive")
        self.assertIsNone(redirected.get_header("Authorization"))
        with self.assertRaisesRegex(Error, "insecure"):
            handler.redirect_request(request, None, 302, "Found", {}, "http://example.invalid/archive")
        with self.assertRaisesRegex(Error, "credential-bearing"):
            handler.redirect_request(request, None, 302, "Found", {}, "https://user:password@example.invalid/archive")


class ChezmoiHostIntegrationTests(unittest.TestCase):
    def test_sandbox_apply_deploys_executables_not_bytecode_and_preserves_local_mode(self):
        binary = os.environ.get("CHEZMOI_BIN") or shutil.which("chezmoi")
        if not binary:
            self.skipTest("chezmoi is required for the sandbox deployment check")
        with scratch() as tmp:
            root = Path(tmp)
            source, home = root / "source", root / "home"
            source.mkdir()
            home.mkdir()
            (home / ".local/bin").mkdir(parents=True)
            (home / ".local").chmod(0o700)
            shutil.copy2(SOURCE / ".chezmoiignore", source / ".chezmoiignore")
            files = [SOURCE / "dot_config/dotfiles/software.json.tmpl"]
            files += list((SOURCE / "private_dot_local/lib/dotfiles_host").glob("*.py"))
            files += [SOURCE / "private_dot_local/lib/dotfiles" / name for name in ("__init__.py", "common.py")]
            files += [SOURCE / "private_dot_local/bin" / ("executable_" + name) for name in ("setup-host", "update-host")]
            for path in files:
                target = source / path.relative_to(SOURCE)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
            cache = source / "private_dot_local/lib/dotfiles_host/__pycache__"
            cache.mkdir()
            (cache / "core.cpython-39.pyc").write_bytes(b"not distributable")
            base = [binary, "--source", str(source), "--destination", str(home), "--config", str(root / "test.toml"), "--persistent-state", str(root / "state.boltdb"), "--override-data", '{"profile":"work"}']
            applied = subprocess.run(base + ["apply"], capture_output=True, text=True)
            self.assertEqual(applied.returncode, 0, applied.stderr)
            self.assertEqual((home / ".local").stat().st_mode & 0o777, 0o700)
            self.assertFalse(list(home.rglob("*.pyc")))
            self.assertFalse(list(home.rglob("__pycache__")))
            manifest = Manifest.load(home / ".config/dotfiles/software.json")
            self.assertEqual(manifest.profile, "work")
            for name in ("setup-host", "update-host"):
                executable = home / ".local/bin" / name
                self.assertTrue(os.access(executable, os.X_OK))
                helped = subprocess.run([str(executable), "--help"], capture_output=True, text=True)
                self.assertEqual(helped.returncode, 0, helped.stderr)
            self.assertFalse(list(home.rglob("__pycache__")))


class CLITests(unittest.TestCase):
    def test_failure_exit_codes(self):
        for exception, code in ((ConfigurationError("policy"), 2), (Error("failure"), 1), (RebootRequired("checkpoint"), 75), (KeyboardInterrupt(), 130)):
            with self.subTest(code=code), scratch() as tmp, redirect_stderr(io.StringIO()):
                with patch("dotfiles_host.cli.os.geteuid", return_value=1000), patch("dotfiles_host.cli.Manifest.load", return_value=Manifest(manifest_data())), patch("dotfiles_host.cli.detect_host", return_value=Host("local", "fedora", "x86_64")), patch("dotfiles_host.cli.HostManager") as manager, patch("dotfiles_host.cli.Path.home", return_value=Path(tmp)):
                    manager.return_value.run.side_effect = exception
                    self.assertEqual(main("setup", ["--dry-run"]), code)

    def test_running_as_root_is_rejected_before_loading_desired_state(self):
        with patch("dotfiles_host.cli.os.geteuid", return_value=0), patch("dotfiles_host.cli.Manifest.load") as load, redirect_stderr(io.StringIO()):
            self.assertEqual(main("setup", []), 2)
            load.assert_not_called()


class CompatibilityTests(unittest.TestCase):
    def test_all_helper_sources_parse_as_python_39(self):
        paths = list((LIBRARY / "dotfiles_host").glob("*.py")) + list((SOURCE / "private_dot_local/bin").glob("executable_*-host"))
        for path in paths:
            with self.subTest(path=path.name):
                ast.parse(path.read_text(), feature_version=(3, 9))

    def test_wrappers_can_show_help_without_deployment_or_privilege(self):
        for name in ("setup-host", "update-host"):
            result = subprocess.run([sys.executable, str(SOURCE / "private_dot_local/bin" / ("executable_" + name)), "--help"], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("--dry-run", result.stdout)


if __name__ == "__main__":
    unittest.main()
