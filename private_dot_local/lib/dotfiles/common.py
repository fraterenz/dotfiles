"""Shared commands, Linux host checks, and durable files (Python 3.9+)."""

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import json
import os
from pathlib import Path
import platform
import shlex
import stat
import subprocess
import tempfile


class Error(RuntimeError):
    """An operation failed without authorizing loss of working files or data."""


class ConfigurationError(Error):
    pass


class Runner:
    def run(self, argv, check=True, timeout=30, capture=True, env=None, stdin=None):
        try:
            result = subprocess.run(argv, capture_output=capture, text=True, timeout=timeout, env=env, stdin=stdin)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise Error("Command unavailable or timed out: " + shlex.join(map(str, argv))) from exc
        if check and result.returncode:
            detail = (result.stderr or "").strip()[-2000:]
            raise Error("Command failed: " + shlex.join(map(str, argv)) + ("\n" + detail if detail else ""))
        return result

    def json(self, argv):
        try:
            return json.loads(self.run(argv).stdout)
        except ValueError as exc:
            raise Error("Invalid JSON from " + str(argv[0])) from exc


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def private_directory(path):
    path = Path(path)
    if path.is_symlink():
        raise Error("Refusing a symlink for private state: " + str(path))
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.stat().st_uid != os.getuid():
        raise Error("Private state is owned by a different user: " + str(path))
    path.chmod(0o700)
    return path


def atomic_write(path, contents, mode=0o600):
    """Flush a complete sibling file, rename it, then flush the directory."""
    path = Path(path)
    if path.is_symlink():
        raise Error("Refusing to replace a symlink: " + str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix="." + path.name + "-", dir=str(path.parent))
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def file_lock(path, busy_message):
    path = Path(path)
    private_directory(path.parent)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise Error("Lock must be a regular file owned by this user: " + str(path))
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Error(busy_message) from exc
        yield
    finally:
        os.close(fd)


@dataclass(frozen=True)
class Host:
    profile: str
    os_id: str
    architecture: str


def detect_host(profile, release_path=Path("/etc/os-release"), atomic=None, machine=None):
    values = {}
    try:
        for line in Path(release_path).read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                key, value = line.split("=", 1)
                parsed = shlex.split(value)
                values[key] = parsed[0] if parsed else ""
    except (OSError, ValueError) as exc:
        raise ConfigurationError("Cannot identify the running host") from exc
    atomic = Path("/run/ostree-booted").exists() if atomic is None else atomic
    if profile == "local":
        valid = values.get("ID") == "fedora" and values.get("VARIANT_ID") == "sway-atomic" and atomic
    elif profile == "work":
        valid = values.get("ID") == "rocky" and values.get("VERSION_ID", "").split(".")[0] == "9" and not atomic
    else:
        raise ConfigurationError("Profile must be local or work")
    if not valid:
        raise ConfigurationError("Profile {} does not match the running OS".format(profile))
    architecture = machine or platform.machine()
    architecture = {"amd64": "x86_64", "arm64": "aarch64"}.get(architecture, architecture)
    if architecture not in ("x86_64", "aarch64"):
        raise ConfigurationError("Unsupported architecture: " + architecture)
    return Host(profile, values["ID"], architecture)
