"""Process execution, host identification, and private operational state."""

from contextlib import nullcontext
import hashlib
import json
from pathlib import Path
import shlex
import subprocess

# Keep the host backend's import surface while sharing the actual primitives.
from dotfiles.common import (
    ConfigurationError, Error, Host, Runner as CommandRunner, atomic_write,
    detect_host, file_lock, private_directory, sync_directory,
)


class RebootRequired(Error):
    pass


class CompatibilityError(Error):
    pass


def read_json(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError) as exc:
        raise ConfigurationError("Cannot read JSON from {}: {}".format(path, exc)) from exc


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_bytes(path, contents):
    path = Path(path)
    private_directory(path.parent)
    atomic_write(path, contents)


def atomic_json(path, value):
    atomic_bytes(path, (json.dumps(value, indent=2) + "\n").encode("utf-8"))


def host_lock(state, dry_run=False):
    if dry_run:
        return nullcontext()
    return file_lock(Path(state) / "host.lock", "Another setup-host/update-host process is running")


class Runner(CommandRunner):
    def __init__(self, dry_run=False, emit=print):
        self.dry_run = dry_run
        self.emit = emit

    def capture(self, argv, check=True, timeout=60):
        return self.run(argv, check=check, timeout=timeout, stdin=subprocess.DEVNULL)

    def action(self, argv):
        self.emit(("Would run: " if self.dry_run else "Running: ") + shlex.join(argv))
        if not self.dry_run:
            self.run(argv, timeout=None, capture=False)
