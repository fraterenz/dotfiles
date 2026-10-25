"""Safe file/state primitives and the deliberately narrow Quadlet input model.

Compatible with the Python 3.9 standard library. No package installation,
uninstallation, pruning, or host OpenCode operations belong in this module.
"""

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat

from .common import Error, Runner, atomic_write, file_lock, sync_directory


RESOURCE_KEYS = {
    "CPUWeight", "StartupCPUWeight", "CPUQuota", "CPUQuotaPeriodSec", "IOWeight",
    "StartupIOWeight", "MemoryHigh", "MemoryMax", "MemorySwapMax", "TasksMax",
    "Nice", "OOMPolicy", "TimeoutStopFailureMode", "TimeoutAbortSec",
    "TimeoutStopSec", "TimeoutStartSec",
}
DEFAULT_TOOLS = ["fish", "git", "nvim", "python3", "uv", "opencode", "rg", "par", "bat", "zoxide"]


def validate_policy(policy):
    if policy["profile"] not in ("local", "work"):
        raise Error("Initialize the local/work profile before container setup")
    tools = policy["required_tools"]
    if not isinstance(tools, list) or any(not isinstance(t, str) or not re.fullmatch(r"[A-Za-z0-9_.+-]+", t) for t in tools):
        raise Error("required_tools must be a list of executable names, not commands")
    for key, minimum, maximum in (("timeout_seconds", 30, 1800), ("stability_seconds", 0, 30)):
        if type(policy[key]) is not int or not minimum <= policy[key] <= maximum:
            raise Error("{} must be between {} and {}".format(key, minimum, maximum))


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def image_id(value):
    if not isinstance(value, str):
        raise Error("Podman returned an invalid immutable image ID")
    value = value.removeprefix("sha256:")
    if not re.fullmatch(r"[a-f0-9]{64}", value):
        raise Error("Podman returned an invalid immutable image ID")
    return "sha256:" + value


def read_text(path):
    return path.read_text() if path.exists() else None


@dataclass
class Paths:
    home: Path
    config_home: Path
    state_home: Path
    runtime_dir: Path

    @classmethod
    def environment(cls):
        home = Path.home()
        return cls(
            home,
            Path(os.environ.get("XDG_CONFIG_HOME", home / ".config")),
            Path(os.environ.get("XDG_STATE_HOME", home / ".local/state")),
            Path(os.environ.get("XDG_RUNTIME_DIR", "/run/user/" + str(os.getuid()))),
        )

    @property
    def desired(self):
        return self.config_home / "dotfiles/devbox"

    @property
    def active(self):
        return self.config_home / "containers/systemd/dev.container"

    @property
    def state(self):
        return self.state_home / "dotfiles/devbox"


class Store:
    def __init__(self, paths):
        self.paths = paths
        self.path = paths.state / "deployments.json"

    def lock(self):
        return file_lock(self.paths.state / "lock", "Another container setup/update/rollback is running")

    def read(self):
        if self.paths.state.is_symlink() or self.path.is_symlink():
            raise Error("State directory/file must not be a symlink")
        if not self.path.exists():
            return {"version": 1, "current": None, "previous": None, "pending": None}
        info = self.path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise Error("State file must be owned by this user with mode 0600")
        try:
            state = json.loads(self.path.read_text())
            self.validate(state)
        except (ValueError, KeyError, TypeError) as exc:
            raise Error("Invalid deployment state; preserve it and use documented recovery") from exc
        return state

    def validate(self, state):
        if not isinstance(state, dict) or set(state) != {"version", "current", "previous", "pending"} or type(state["version"]) is not int or state["version"] != 1:
            raise ValueError("unsupported state schema/version")
        for key in ("current", "previous"):
            if state[key] is not None:
                self.validate_record(state[key])
        if state["previous"] is not None and state["current"] is None:
            raise ValueError("previous deployment without current deployment")
        pending = state["pending"]
        if pending is not None:
            if not isinstance(pending, dict) or set(pending) != {"before", "after", "previous_before"}:
                raise ValueError("invalid pending activation")
            self.validate_record(pending["after"])
            for key in ("before", "previous_before"):
                if pending[key] is not None:
                    self.validate_record(pending[key])
            before, current = pending["before"], state["current"]
            if (before is None) != (current is None) or (before is not None and any(before[key] != current[key] for key in ("unit", "image"))):
                raise ValueError("pending predecessor does not match current deployment")
            if pending["previous_before"] != state["previous"]:
                raise ValueError("pending history does not match previous deployment")

    def validate_record(self, record):
        try:
            required = {
                "image", "unit", "observed_unit", "active_path", "profile", "build_fingerprint",
                "mounts", "required_tools", "timeout_seconds", "stability_seconds", "dropins",
                "source_revision", "created_at",
            }
            if not isinstance(record, dict) or not required.issubset(record):
                raise ValueError("incomplete deployment")
            if image_id(record["image"]) != record["image"]:
                raise ValueError("noncanonical image ID")
            if not isinstance(record["unit"], str) or not isinstance(record["observed_unit"], str):
                raise ValueError("invalid unit")
            unit = Unit(record["unit"])
            unit.validate()
            mounts = unit.mounts(self.paths.home)
            observed = Unit(record["observed_unit"])
            observed.validate()
            # One canonical-rendering check covers image ID, no-pull policy,
            # non-replacing arguments, and all preserved runtime settings.
            if observed.pinned(record["image"]) != record["unit"]:
                raise ValueError("observed unit differs from pinned runtime")
            if not isinstance(record["active_path"], str) or Path(record["active_path"]).resolve() != self.paths.active.resolve():
                raise ValueError("active path changed")
            validate_policy(record)
            if record["build_fingerprint"] is not None and not re.fullmatch(r"[a-f0-9]{64}", record["build_fingerprint"]):
                raise ValueError("invalid build fingerprint")
            if not isinstance(record["mounts"], list) or any(not isinstance(m, dict) for m in record["mounts"]):
                raise ValueError("invalid mount identities")
            if any(not isinstance(m.get("target"), str) or not isinstance(m.get("source"), str) for m in record["mounts"]):
                raise ValueError("invalid mount source/target")
            if sorted(m["target"] for m in record["mounts"]) != sorted(m.target for m in mounts):
                raise ValueError("mount identities do not match unit")
            if not isinstance(record["source_revision"], str) or not isinstance(record["created_at"], str):
                raise ValueError("invalid source/timestamp metadata")
            if "tool_versions" in record and (not isinstance(record["tool_versions"], dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in record["tool_versions"].items())):
                raise ValueError("invalid tool version diagnostics")
            if not isinstance(record["dropins"], list):
                raise ValueError("invalid resource policies")
            for row in record["dropins"]:
                if not isinstance(row, dict) or set(row) != {"path", "text", "sha256"} or any(not isinstance(v, str) for v in row.values()) or row["sha256"] != sha256(row["text"].encode()):
                    raise ValueError("invalid resource-policy snapshot")
        except (Error, KeyError, TypeError, ValueError) as exc:
            raise Error("Invalid deployment record; preserve state and use documented recovery") from exc

    def write(self, state):
        self.validate(state)
        atomic_write(self.path, (json.dumps(state, indent=2, sort_keys=True) + "\n").encode())


@dataclass
class Mount:
    source: str
    target: str
    options: tuple
    named: bool = False

    def identity(self):
        if self.named:
            return {"source": self.source, "target": self.target, "named": True}
        path = Path(self.source)
        if not path.exists():
            return {"source": str(path), "target": self.target, "missing": True}
        info = path.stat()
        return {
            "source": str(path.resolve()), "target": self.target,
            "device": info.st_dev, "inode": info.st_ino,
        }


class Unit:
    """Keep repeated keys and source text; do not round-trip through ConfigParser."""

    def __init__(self, text):
        self.text = text
        self.lines = text.splitlines(keepends=True)
        self.entries = []
        section = None
        for index, line in enumerate(self.lines):
            stripped = line.strip()
            if not stripped or stripped.startswith(("#", ";")):
                continue
            if stripped.endswith("\\"):
                raise Error("Use single-line Quadlet entries; continuations are not supported")
            if stripped.startswith("[") and stripped.endswith("]"):
                section = stripped[1:-1]
                continue
            if section is None or "=" not in stripped:
                raise Error("Invalid Quadlet entry on line " + str(index + 1))
            key, value = stripped.split("=", 1)
            self.entries.append((index, section, key.strip(), value.strip()))

    def values(self, section, key):
        return [value for _, s, k, value in self.entries if (s, k) == (section, key)]

    def one(self, section, key, default=None):
        values = self.values(section, key)
        if len(values) > 1:
            raise Error("Ambiguous repeated setting: " + section + "." + key)
        return values[0] if values else default

    def validate(self):
        if sum(line.strip() == "[Container]" for line in self.lines) != 1:
            raise Error("Declare exactly one Container section")
        expected = {"ContainerName": "dev", "UserNS": "keep-id", "DropCapability": "all", "NoNewPrivileges": "true"}
        for key, value in expected.items():
            if self.one("Container", key) != value:
                raise Error("Required devbox setting: " + key + "=" + value)
        if not self.one("Container", "Image"):
            raise Error("The desired Quadlet needs an Image entry")
        for key in ("Rootfs", "Mount", "AutoUpdate", "Pod", "User", "Group", "AddCapability", "AddDevice"):
            if self.values("Container", key):
                raise Error("Unsupported devbox setting; review it explicitly: " + key)
        if self.one("Container", "SecurityLabelDisable", "false") != "false":
            raise Error("Do not disable SELinux separation")
        if self.one("Container", "ServiceName", "dev") != "dev":
            raise Error("Only dev.service is managed")
        if any(value != "--replace=false" for value in self.values("Container", "PodmanArgs")):
            raise Error("Use dedicated Quadlet keys instead of unreviewed PodmanArgs")
        if any(section == "Service" and key.startswith("Exec") for _, section, key, _ in self.entries):
            raise Error("Do not override the generated service lifecycle")
        service_keys = RESOURCE_KEYS | {"Restart", "RestartSec"}
        if any(section == "Service" and key not in service_keys for _, section, key, _ in self.entries):
            raise Error("Unreviewed service lifecycle setting in the desired Quadlet")
        if self.one("Container", "WorkingDir") != "/home/dev":
            raise Error("The devbox working directory must remain /home/dev")
        environment = [word for value in self.values("Container", "Environment") for word in shlex.split(value)]
        if [word for word in environment if word.startswith("HOME=")] != ["HOME=/home/dev"]:
            raise Error("Declare exactly one HOME=/home/dev environment setting")

    def pinned(self, image):
        replacements = {"Image": image_id(image), "Pull": "never"}
        self.one("Container", "Pull")
        skip = {i for i, s, k, _ in self.entries if s == "Container" and k in ("Image", "Pull", "PodmanArgs")}
        output = []
        for index, line in enumerate(self.lines):
            if index in skip:
                continue
            output.append(line)
            if line.strip() == "[Container]":
                output.extend(key + "=" + value + "\n" for key, value in replacements.items())
                output.append("PodmanArgs=--replace=false\n")
        return "".join(output)

    def mounts(self, home):
        result = []
        for value in self.values("Container", "Volume"):
            pieces = value.split(":")
            if len(pieces) not in (2, 3):
                raise Error("Use explicit source:target[:options] volumes")
            source, target = pieces[:2]
            options = tuple(pieces[2].split(",")) if len(pieces) == 3 else ()
            if not set(options).issubset({"ro", "rw", "z", "Z", "nodev", "nosuid", "noexec"}):
                raise Error("Unsupported mount option; ownership-changing :U is never automatic")
            if {"ro", "rw"}.issubset(options) or {"z", "Z"}.issubset(options):
                raise Error("Conflicting mount options")
            if any(c.isspace() or c in "'\"\\" for c in source + target) or "%" in target:
                raise Error("Mount paths must not contain whitespace, quotes, escapes, or target specifiers")
            if not target.startswith("/") or ".." in Path(target).parts:
                raise Error("Container mount targets must be absolute")
            source = source.replace("%h", str(home))
            if "%" in source or "\n" in source or not source:
                raise Error("Only %h and absolute host mount paths are supported")
            named = not source.startswith("/")
            if named and (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", source) or source.endswith(".volume")):
                raise Error("Use absolute bind mounts or simple named volumes, not relative/Quadlet references")
            if not named and Path(source).resolve() in (Path("/"), home.resolve(), (home / ".local/bin").resolve()):
                raise Error("Do not mount the whole host home, filesystem, or local bin")
            result.append(Mount(source, target, options, named))
        if len({mount.target for mount in result}) != len(result):
            raise Error("Duplicate container mount target")
        workspace = [mount for mount in result if mount.target == "/home/dev"]
        if len(workspace) != 1 or workspace[0].named or "ro" in workspace[0].options:
            raise Error("Declare one writable persistent host workspace at /home/dev")
        return result


def context_fingerprint(context, uid, gid, architecture):
    """Conservatively hash all inputs, even excluded ones, instead of guessing ignore semantics."""
    if not context.is_dir() or context.is_symlink():
        raise Error("The staged build context is missing or is a symlink")
    digest = hashlib.sha256(json.dumps([uid, gid, architecture, "dev"], separators=(",", ":")).encode())
    for path in sorted(context.rglob("*")):
        relative = str(path.relative_to(context))
        info = path.lstat()
        digest.update(json.dumps([relative, stat.S_IFMT(info.st_mode), stat.S_IMODE(info.st_mode), info.st_size if path.is_file() and not path.is_symlink() else 0], separators=(",", ":")).encode())
        if path.is_symlink():
            if not path.resolve().is_relative_to(context.resolve()):
                raise Error("Build-context symlinks must not escape the context")
            digest.update(b"link:" + os.readlink(path).encode())
        elif path.is_file():
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
        elif not path.is_dir():
            raise Error("Special files are not allowed in the build context")
    return digest.hexdigest()
