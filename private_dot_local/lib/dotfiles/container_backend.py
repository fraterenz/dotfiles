"""Podman/systemd operations. Only the lifecycle controller may activate units."""

import os
from pathlib import Path
import pwd
import re
import shlex
import shutil
import tempfile
import time
import uuid

from .common import detect_host
from .container_support import Error, RESOURCE_KEYS, Runner, Unit, image_id, sha256


NETWORK_FILESYSTEMS = {"nfs", "nfs4", "cifs", "smb3", "lustre", "gpfs", "fuse.sshfs"}


class Backend:
    def __init__(self, paths, runner=None):
        self.paths = paths
        self.runner = runner or Runner()
        self.generator = None

    def preflight(self, profile, mounts):
        if os.geteuid() == 0:
            raise Error("Run setup-containers as the rootless user, never with sudo")
        if self.paths.home.resolve() != Path(pwd.getpwuid(os.getuid()).pw_dir).resolve():
            raise Error("HOME must match this user's actual home")
        if any(not path.is_absolute() for path in (self.paths.config_home, self.paths.state_home, self.paths.runtime_dir)):
            raise Error("XDG configuration, state, and runtime paths must be absolute")
        if os.environ.get("QUADLET_UNIT_DIRS"):
            raise Error("Unset the custom QUADLET_UNIT_DIRS override before managing the real devbox")
        detect_host(profile)
        for name in ("podman", "systemctl", "systemd-analyze", "findmnt"):
            if not shutil.which(name):
                raise Error("Missing host prerequisite: " + name)
        candidates = (
            "/usr/lib/systemd/system-generators/podman-system-generator",
            "/usr/libexec/podman/quadlet",
        )
        self.generator = next((path for path in candidates if os.access(path, os.X_OK)), None)
        if not self.generator:
            raise Error("A usable Podman Quadlet generator is required")
        self.runner.run(["systemctl", "--user", "show", "--property=Version", "--value"])
        info = self.runner.json(["podman", "info", "--format=json"])
        host = info["host"]
        if host.get("serviceIsRemote") or not host.get("security", {}).get("rootless") or host.get("cgroupVersion") != "v2" or host.get("cgroupManager") != "systemd":
            raise Error("Rootless Podman with cgroup v2 and the systemd cgroup manager is required")
        for mapping in ("uidmap", "gidmap"):
            if sum(row.get("size", 0) for row in host.get("idMappings", {}).get(mapping, [])) <= 1:
                raise Error("Usable subordinate UID/GID ranges are required")
        self.check_local_filesystem(Path(info["store"]["graphRoot"]), "container graphroot")
        for mount in mounts:
            if mount.named:
                continue
            path = Path(mount.source)
            self.check_local_filesystem(path, "bind mount")
            if not path.exists() and mount.target != "/home/dev":
                raise Error("Apply the missing configuration before setup: " + str(path))
            if mount.target == "/home/dev" and path.exists() and (not path.is_dir() or not os.access(path, os.W_OK | os.X_OK)):
                raise Error("The persistent workspace must be a writable directory: " + str(path))
        self.check_quadlet_conflicts()
        self.dropins()
        return host.get("arch", "unknown")

    def check_local_filesystem(self, path, purpose):
        while not path.exists() and path != path.parent:
            path = path.parent
        kind = self.runner.run(["findmnt", "--noheadings", "--output", "FSTYPE", "--target", str(path)]).stdout.strip()
        if not kind or len(kind.splitlines()) != 1:
            raise Error("Cannot determine the filesystem for " + purpose)
        if kind in NETWORK_FILESYSTEMS:
            raise Error(purpose + " is on " + kind + "; configure approved persistent local storage/configuration copies first")

    def check_quadlet_conflicts(self):
        roots = [
            self.paths.runtime_dir / "containers/systemd", self.paths.active.parent,
            Path("/etc/containers/systemd/users") / str(os.getuid()),
            Path("/etc/containers/systemd/users"),
            Path("/usr/share/containers/systemd/users") / str(os.getuid()),
            Path("/usr/share/containers/systemd/users"),
        ]
        for root in roots:
            if not root.exists():
                continue
            for path in root.rglob("*.container"):
                relative = path.relative_to(root).parts
                if root.name == "users" and relative[0].isdigit() and relative[0] != str(os.getuid()):
                    continue
                if path.resolve() == self.paths.active.resolve():
                    continue
                text = path.read_text()
                if path.name == "dev.container" or re.search(r"(?m)^\s*(?:ContainerName|ServiceName)\s*=\s*dev\s*$", text):
                    raise Error("Conflicting Quadlet requires review: " + str(path))
            for name in ("dev.container.d", "container.d"):
                for directory in root.rglob(name):
                    relative = directory.relative_to(root).parts
                    if root.name == "users" and relative[0].isdigit() and relative[0] != str(os.getuid()):
                        continue
                    if any(directory.glob("*.conf")):
                        raise Error("Merge reviewed Quadlet drop-ins into the desired unit first: " + str(directory))

    def service(self):
        result = self.runner.run([
            "systemctl", "--user", "show", "dev.service",
            "--property=LoadState,ActiveState,SubState,FragmentPath,SourcePath,DropInPaths",
        ], check=False)
        values = dict(line.split("=", 1) for line in (result.stdout or "").splitlines() if "=" in line)
        if "LoadState" not in values:
            raise Error("Cannot inspect the user service manager")
        if values["LoadState"] != "not-found":
            source = Path(values.get("SourcePath") or "/")
            fragment = Path(values.get("FragmentPath") or "/")
            if source.resolve() != self.paths.active.resolve() or fragment.parent.name not in ("generator", "generator.early", "generator.late"):
                raise Error("dev.service is not generated from the expected active Quadlet")
        return values

    def dropins(self):
        result = []
        service = self.service()
        # Inspect disk even for a loaded service: a not-yet-reloaded ExecStart
        # override must be caught before our daemon-reload can activate it.
        roots = self.runner.run(["systemd-analyze", "--user", "unit-paths"]).stdout.splitlines()
        if not roots or any(not Path(root).is_absolute() for root in roots):
            raise Error("Cannot inspect the user service drop-in search paths")
        selected = {}
        for directory in ("service.d", "dev.service.d"):
            for root in reversed(roots):
                for path in sorted((Path(root) / directory).glob("*.conf")):
                    selected[path.name] = path
        names = [str(path) for path in selected.values() if path.resolve() != Path("/dev/null")]
        for name in names:
            path = Path(name)
            text = path.read_text()
            parsed = Unit(text)
            if any(section != "Service" or key not in RESOURCE_KEYS for _, section, key, _ in parsed.entries):
                raise Error("Unreviewed service drop-in can override the lifecycle: " + name)
            result.append({"path": name, "sha256": sha256(text.encode()), "text": text})
        if service["LoadState"] != "not-found" and set(names) != set(shlex.split(service.get("DropInPaths", ""))):
            raise Error("Service drop-ins changed on disk but are not loaded; review/reload them separately before setup")
        return sorted(result, key=lambda row: row["path"])

    def container(self):
        result = self.runner.run(["podman", "container", "exists", "dev"], check=False)
        if result.returncode == 1:
            return None
        if result.returncode:
            raise Error("Cannot inspect the existing dev container")
        data = self.runner.json(["podman", "inspect", "dev"])[0]
        if data.get("Config", {}).get("Labels", {}).get("PODMAN_SYSTEMD_UNIT") != "dev.service":
            raise Error("Container name dev belongs to an unmanaged container; nothing will be replaced")
        return data

    def image_exists(self, image):
        result = self.runner.run(["podman", "image", "exists", image], check=False)
        if result.returncode not in (0, 1):
            raise Error("Cannot inspect the local image store")
        return result.returncode == 0

    def retain(self, image):
        image = image_id(image)
        tag = "localhost/devbox:dotfiles-retained-" + image.split(":")[1]
        if self.image_exists(tag):
            actual = image_id(self.runner.json(["podman", "image", "inspect", tag])[0]["Id"])
            if actual != image:
                raise Error("A retained tag belongs to another image; refusing to move it")
        else:
            self.runner.run(["podman", "image", "tag", image, tag])
        return tag

    def build(self, context, update):
        tag = "localhost/devbox:dotfiles-build-" + uuid.uuid4().hex
        with tempfile.TemporaryDirectory(prefix="dotfiles-build-") as directory:
            iid = Path(directory) / "image-id"
            command = [
                "podman", "build", "--tag", tag, "--iidfile", str(iid),
                "--pull=" + ("always" if update else "missing"),
                "--build-arg", "USER_UID=" + str(os.getuid()),
                "--build-arg", "USER_GID=" + str(os.getgid()),
                "--build-arg", "USER_NAME=dev",
                "--file", str(context / "Containerfile.dev"),
            ]
            if update:
                command.append("--no-cache")
            command.append(str(context))
            self.runner.run(command, timeout=None, capture=False)
            built = image_id(iid.read_text().strip())
            actual = image_id(self.runner.json(["podman", "image", "inspect", tag])[0]["Id"])
            if actual != built:
                raise Error("Built image/tag identity mismatch")
        return built

    def validate_unit(self, text, image):
        with tempfile.TemporaryDirectory(prefix="dotfiles-quadlet-") as directory:
            (Path(directory) / "dev.container").write_text(text)
            result = self.runner.run(
                [self.generator, "--user", "--dryrun"],
                env=dict(os.environ, QUADLET_UNIT_DIRS=directory),
            )
        output = (result.stdout or "") + (result.stderr or "")
        if re.search(r"(?:unsupported|unknown) (?:key|section)", output, re.I):
            raise Error("Installed Quadlet does not support this definition")
        generated = re.search(r"(?ms)^---dev\.service---\n(.*?)(?=^---[^\n]+---$|\Z)", result.stdout or "")
        if not generated:
            raise Error("Quadlet did not generate dev.service")
        command = shlex.split(Unit(generated.group(1)).one("Service", "ExecStart", ""))
        image = image_id(image)
        flags = command[:command.index(image)] if image in command else []
        replacements = [flag for flag in flags if flag == "--replace" or flag.startswith("--replace=")]
        pulls = []
        for index, flag in enumerate(flags):
            if flag.startswith("--pull="):
                pulls.append(flag.split("=", 1)[1])
            elif flag == "--pull":
                pulls.append(flags[index + 1] if index + 1 < len(flags) else None)
        if (len(flags) < 2 or Path(flags[0]).name != "podman" or flags[1] != "run"
                or pulls[-1:] != ["never"] or replacements[-1:] != ["--replace=false"]):
            raise Error("Quadlet did not generate the expected pinned, non-replacing service")

    def precheck(self, image, tools):
        # No real work/config mounts, ports, host credentials, or private :Z relabels.
        self.runner.run([
            "podman", "run", "--rm", "--pull=never", "--network=none",
            "--userns=keep-id", "--cap-drop=all", "--security-opt=no-new-privileges",
            "--entrypoint=/bin/sh", image, "-c",
            'test "$(id -u)" = "$1" && test "$(id -g)" = "$2" && shift 2 && for tool do command -v "$tool" >/dev/null || exit 1; done',
            "precheck", str(os.getuid()), str(os.getgid()), *tools,
        ], timeout=120)

    def writable_diff(self):
        return self.runner.run(["podman", "diff", "dev"]).stdout.strip()

    def audit_adoption(self, record):
        """Read-only check that the saved source describes the running runtime."""
        data = self.container()
        unit = Unit(record["unit"])
        if not data or not data.get("State", {}).get("Running"):
            raise Error("The existing devbox stopped during adoption review")
        if image_id(data["Image"]) != record["image"]:
            raise Error("The existing devbox image changed during adoption review")
        config = data.get("Config", {})
        if config.get("Cmd") != shlex.split(unit.one("Container", "Exec", "")) or config.get("WorkingDir") != "/home/dev":
            raise Error("The existing Quadlet does not describe the running command/working directory")
        if [value for value in config.get("Env", []) if value.startswith("HOME=")] != ["HOME=/home/dev"]:
            raise Error("The running devbox has an unexpected HOME")
        self.verify_mounts(data, record["unit"], probe=False)

    def stop(self, timeout):
        # Generated ExecStop/ExecStopPost may remove by name. Inspect ownership
        # before systemctl stop, not only after it has already performed cleanup.
        self.container()
        service = self.service()
        if service["LoadState"] != "not-found":
            self.runner.run(["systemctl", "--user", "stop", "dev.service"], timeout=timeout + 15)
        if self.container() is not None:
            raise Error("Container dev remains after stopping its service; refusing to replace/remove it")

    def start(self, timeout):
        if self.container() is not None:
            raise Error("Container dev already exists; refusing to replace it")
        self.runner.run(["systemctl", "--user", "daemon-reload"])
        self.dropins()
        self.runner.run(["systemctl", "--user", "reset-failed", "dev.service"])
        self.runner.run(["systemctl", "--user", "start", "dev.service"], timeout=timeout + 15)

    def unload(self):
        self.runner.run(["systemctl", "--user", "daemon-reload"])

    def verify(self, record):
        time.sleep(record.get("stability_seconds", 2))
        service = self.service()
        data = self.container()
        if service.get("ActiveState") != "active" or not data or not data.get("State", {}).get("Running"):
            raise Error("The dev service did not remain active")
        if image_id(data["Image"]) != record["image"]:
            raise Error("The running container uses an unexpected image")
        for flag, value in (("-u", os.getuid()), ("-g", os.getgid())):
            actual = self.runner.run(["podman", "exec", "dev", "/usr/bin/id", flag]).stdout.strip()
            if actual != str(value):
                raise Error("The container numeric user/group does not match the host")
        self.verify_mounts(data, record["unit"], probe=True)
        self.runner.run([
            "podman", "exec", "dev", "/bin/sh", "-c",
            'for tool do command -v "$tool" >/dev/null || exit 1; done',
            "check-tools", *record.get("required_tools", []),
        ])
        self.runner.run([
            "podman", "exec", "dev", "python3", "-c",
            'import os,tempfile; f=tempfile.TemporaryFile(dir=os.environ["HOME"]); f.write(b"ok"); f.close()',
        ])
        if self.dropins() != record["dropins"]:
            raise Error("External service resource policy changed during activation")
        versions = {}
        versioned = {"fish", "git", "nvim", "python3", "uv", "opencode", "rg", "bat", "zoxide", "rustc", "cargo", "ruff", "ty", "gopls", "tokei"}
        for tool in record.get("required_tools", []):
            if tool not in versioned:
                continue
            try:
                result = self.runner.run(["podman", "exec", "dev", tool, "--version"], check=False)
                lines = (result.stdout or result.stderr or "").splitlines()
                versions[tool] = lines[0][:300] if result.returncode == 0 and lines else "unavailable"
            except Error:
                # A nonstandard --version is diagnostic, not a failed startup.
                versions[tool] = "unavailable"
        return versions

    def verify_mounts(self, data, text, probe):
        actual = {row["Destination"]: row for row in data.get("Mounts", [])}
        mounts = Unit(text).mounts(self.paths.home)
        if set(actual) != {mount.target for mount in mounts}:
            raise Error("The running devbox mounts do not match the saved Quadlet")
        for mount in mounts:
            row = actual[mount.target]
            if bool(row.get("RW")) != ("ro" not in mount.options):
                raise Error("Missing mount or incorrect read-only mode: " + mount.target)
            if not mount.named:
                if Path(row["Source"]).resolve() != Path(mount.source).resolve():
                    raise Error("Incorrect host mount source: " + mount.target)
                if probe:
                    inode = self.runner.run(["podman", "exec", "dev", "/usr/bin/stat", "--format=%i", mount.target]).stdout.strip()
                    if inode != str(Path(mount.source).stat().st_ino):
                        raise Error("The container still sees a replaced mount inode: " + mount.target)
            elif row.get("Type") != "volume" or row.get("Name") != mount.source:
                raise Error("Incorrect persistent named volume: " + mount.target)

    def source_revision(self):
        source = self.paths.home / ".local/share/chezmoi"
        if not shutil.which("git") or not (source / ".git").exists():
            return "unavailable"
        result = self.runner.run(["git", "-C", str(source), "rev-parse", "--verify", "HEAD"], check=False)
        if result.returncode:
            return "uncommitted"
        revision = result.stdout.strip()
        dirty = self.runner.run(["git", "-C", str(source), "status", "--porcelain"], check=False)
        return revision + ("+dirty" if dirty.returncode or dirty.stdout.strip() else "")
