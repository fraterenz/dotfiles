"""Package/Flatpak phases and strictly ordered user-tool reconciliation."""

import configparser
import json
import os
from pathlib import Path
import shutil

from .core import CompatibilityError, ConfigurationError, Error, RebootRequired
from .downloads import ToolUpdater
from .manifest import destination, work_policy


def enabled_repositories(directory=Path("/etc/yum.repos.d")):
    enabled = set()
    for path in sorted(Path(directory).glob("*.repo")):
        parser = configparser.ConfigParser(interpolation=None, strict=False)
        try:
            with path.open() as stream:
                parser.read_file(stream)
            for section in parser.sections():
                if parser.getboolean(section, "enabled", fallback=True):
                    enabled.add(section)
        except (OSError, configparser.Error, ValueError) as exc:
            raise Error("Cannot inspect repository configuration: {}".format(path.name)) from exc
    return enabled


def atomic_status(runner):
    try:
        status = json.loads(runner.capture(["rpm-ostree", "status", "--json"]).stdout)
        deployments = status["deployments"]
        booted = [i for i, d in enumerate(deployments) if d.get("booted")]
        if len(booted) != 1:
            raise ValueError("Missing/ambiguous booted deployment")
        pending = [d for i, d in enumerate(deployments) if not d.get("booted") and (d.get("staged") or i < booted[0])]
        requested = {name for d in pending for name in d.get("requested-packages", [])}
        return bool(pending), requested
    except (ValueError, KeyError, TypeError) as exc:
        raise Error("Cannot interpret rpm-ostree deployment status") from exc


def flatpak_flags(installation):
    if installation.get("name"):
        return ["--installation=" + installation["name"]]
    return ["--" + installation["scope"]]


class HostManager:
    def __init__(self, manifest, host, runner, home, state, policy_path, client=None, repo_directory=Path("/etc/yum.repos.d")):
        self.manifest, self.host, self.runner = manifest, host, runner
        self.home, self.state = Path(home), Path(state)
        self.policy_path = Path(policy_path)
        self.repo_directory = Path(repo_directory)
        self.policy = None
        self.pending_reboot = False
        self.updater = ToolUpdater(self.home, self.state, host, runner, client)

    def authorize(self):
        if self.host.profile != "work":
            return
        if not self.policy_path.is_file():
            raise ConfigurationError("Work operations are blocked: create machine-local host-policy.json after site approval")
        info = self.policy_path.stat()
        if info.st_uid not in (0, os.getuid()) or info.st_mode & 0o022:
            raise ConfigurationError("Work policy must be owned by this user/root and not group/world writable")
        self.policy = work_policy(self.policy_path, self.manifest)
        absent = set(self.policy["dnf"]["repositories"]) - enabled_repositories(self.repo_directory)
        if absent:
            raise ConfigurationError("Approved repositories are not enabled/configured: " + ", ".join(sorted(absent)))

    def dnf_command(self, operation):
        if self.policy is None:
            raise ConfigurationError("Site dnf policy has not been approved")
        dnf = self.policy["dnf"]
        return ["sudo", "/usr/bin/dnf", "--disablerepo=*"] + ["--enablerepo=" + repo for repo in dnf["repositories"]] + dnf[operation + "_args"]

    def package_installed(self, name):
        result = self.runner.capture(["rpm", "-q", "--quiet", name], check=False)
        if result.returncode not in (0, 1) or (result.returncode and result.stderr.strip()):
            raise Error("Cannot query installed RPM packages")
        return result.returncode == 0

    def setup_native(self):
        missing = [p for p in self.manifest.packages if not self.package_installed(p["name"])]
        if self.host.profile == "local":
            self.pending_reboot, requested = atomic_status(self.runner)
            new = [p for p in missing if p["name"] not in requested]
            repositories = enabled_repositories(self.repo_directory) if new else set()
            absent = {p["required_repository"] for p in new if p.get("required_repository") and p["required_repository"] not in repositories}
            if absent:
                raise Error("Required package repositories are not configured; no remotes will be added: " + ", ".join(sorted(absent)))
            if new:
                self.runner.action(["rpm-ostree", "install", "--idempotent"] + [p["name"] for p in new])
            if missing and self.runner.dry_run:
                self.runner.emit("Native requirements would be staged; reboot and rerun setup-host before continuing.")
                self.runner.emit("Subsequent dry-run actions describe that post-reboot rerun, not pre-reboot installation.")
                return
            if missing:
                self.pending_reboot, _ = atomic_status(self.runner)
                still_missing = [p["name"] for p in missing if not self.package_installed(p["name"])]
                if still_missing:
                    raise RebootRequired("Native requirements are not active: {}. Reboot manually, then rerun setup-host; no user tools were changed.".format(", ".join(still_missing)))
            if self.pending_reboot:
                self.runner.emit("A system deployment is pending reboot; currently required native packages are usable.")
        elif missing:
            self.runner.action(self.dnf_command("install") + [p["name"] for p in missing])
            if not self.runner.dry_run and any(not self.package_installed(p["name"]) for p in missing):
                raise Error("Native installation did not provide every required RPM")

    def update_native(self):
        if self.host.profile == "local":
            self.runner.action(["rpm-ostree", "upgrade"])
            self.pending_reboot, _ = atomic_status(self.runner)
            if self.pending_reboot:
                self.runner.emit("System update is staged for a manual reboot; user binaries must run on the currently booted host.")
        else:
            self.runner.action(self.dnf_command("update"))
            self.runner.emit("Follow the site's service-restart/reboot procedure; no reboot is performed automatically.")

    def flatpaks(self, update=False):
        if not self.manifest.installations:
            return
        available = bool(shutil.which("flatpak"))
        if not available:
            if self.runner.dry_run:
                self.runner.emit("Flatpak queries await the planned native installation.")
            else:
                raise Error("Flatpak is not usable after native setup")
        for installation in self.manifest.installations:
            flags = flatpak_flags(installation)
            if update:
                self.runner.action(["flatpak", "update"] + flags + ["--noninteractive", "--assumeyes"])
                continue
            apps = [a for a in self.manifest.apps if a["installation"] == installation["id"]]
            if not apps:
                continue
            if available:
                installed = set(self.runner.capture(["flatpak", "list", "--app", "--columns=application"] + flags).stdout.splitlines())
                remotes = set(self.runner.capture(["flatpak", "remotes", "--columns=name"] + flags).stdout.splitlines())
            else:
                installed, remotes = set(), set()
            for app in apps:
                if app["id"] in installed:
                    continue
                if app["remote"] not in remotes and available:
                    raise Error("Flatpak remote {} is missing from {}; configure it explicitly after approval".format(app["remote"], installation["id"]))
                self.runner.action(["flatpak", "install"] + flags + ["--noninteractive", "--assumeyes", app["remote"], app["id"]])

    def unknown_entries(self):
        known = set(self.manifest.managed)
        for tool in self.manifest.tools:
            relative = Path(destination(tool))
            if relative.parent == Path(".local/bin"):
                known.update([tool["executable"]] + tool["aliases"])
        for manager in self.manifest.managers:
            known.update(manager["entries"])
        directory = self.home / ".local/bin"
        return sorted(p.name for p in directory.iterdir() if p.name not in known) if directory.is_dir() else []

    def user_tools(self, update=False):
        failures = []
        for entry in self.unknown_entries():
            self.runner.emit("Unregistered local-bin entry (left untouched): {!r}".format(entry))
            failures.append("unregistered entry")
        for tool in self.manifest.tools:
            try:
                self.updater.reconcile(tool, update=update)
            except (Error, OSError) as exc:
                self.runner.emit("{}: {}".format(tool["id"], exc))
                if isinstance(exc, CompatibilityError) and self.pending_reboot:
                    self.runner.emit("Replacement deferred: reboot into the pending deployment and rerun update-host.")
                failures.append(tool["id"])
        for manager in self.manifest.managers:
            missing = any(not os.path.lexists(self.home / ".local/bin" / entry) for entry in manager["entries"])
            if not update and not missing:
                continue
            try:
                self.runner.action(manager["update_argv"] if update else manager["install_argv"])
                if not self.runner.dry_run and any(not os.path.lexists(self.home / ".local/bin" / entry) for entry in manager["entries"]):
                    raise Error("Manager did not provide its declared entries")
            except Error as exc:
                self.runner.emit("{}: {}".format(manager["id"], exc))
                failures.append(manager["id"])
        if failures:
            raise Error("User-tool phase incomplete ({}); working installations were retained where replacement failed".format(", ".join(failures)))

    def run(self, update=False):
        self.authorize()
        if update:
            self.update_native()
        else:
            self.setup_native()
        # Exceptions in either required host phase stop all user-tool updates.
        self.flatpaks(update=update)
        self.user_tools(update=update)
        self.runner.emit("Dry run complete; no changes made." if self.runner.dry_run else "Host update complete." if update else "Host setup complete.")
