"""Host setup and its native-package/reboot prerequisites."""

import configparser
from pathlib import Path
import shutil

from .core import ConfigurationError, Error, RebootRequired
from .downloads import ToolUpdater
from .manifest import work_policy
from .updates.operations import atomic_status, flatpak_flags, reconcile_user_tools, unknown_entries


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

    def flatpaks(self):
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
        return unknown_entries(self.manifest, self.home)

    def user_tools(self):
        reconcile_user_tools(
            manifest=self.manifest, home=self.home, runner=self.runner,
            updater=self.updater, update=False, pending_reboot=self.pending_reboot,
        )

    def run(self):
        self.authorize()
        self.setup_native()
        # Setup retains its prerequisites; updates use the independent executor.
        self.flatpaks()
        self.user_tools()
        self.runner.emit("Dry run complete; no changes made." if self.runner.dry_run else "Host setup complete.")
