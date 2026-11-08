"""Process execution, host identification, and private operational state."""

from contextlib import nullcontext
import codecs
import hashlib
import json
import locale
import os
from pathlib import Path
import selectors
import shlex
import signal
import subprocess
import time

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
    def __init__(self, dry_run=False, emit=print, *, cancelled=None):
        self.dry_run = dry_run
        self.emit = emit
        self.cancelled = cancelled

    def check_cancelled(self):
        if self.cancelled is not None and self.cancelled.is_set():
            raise KeyboardInterrupt

    @staticmethod
    def _stop_process(process):
        # Update commands get their own process group, so cancelling a wrapper
        # also stops its children instead of leaving an updater in the background.
        try:
            for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(process.pid, sig)
                except ProcessLookupError:
                    break
                try:
                    process.wait(timeout=2)
                    break
                except subprocess.TimeoutExpired:
                    continue
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()

    def run(self, argv, check=True, timeout=30, capture=True, env=None, stdin=None):
        self.check_cancelled()
        if self.cancelled is None:
            # Preserve the existing setup command/terminal behaviour.
            return super().run(argv, check=check, timeout=timeout, capture=capture, env=env, stdin=stdin)

        command = shlex.join(map(str, argv))
        captured = {"stdout": bytearray(), "stderr": bytearray()}
        encoding = locale.getpreferredencoding(False)
        decoder = codecs.getincrementaldecoder(encoding)(errors="replace")
        pending = ""
        tail = ""
        deadline = time.monotonic() + timeout if timeout is not None else None
        selector = selectors.DefaultSelector()
        try:
            process = subprocess.Popen(
                argv, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE if capture else subprocess.STDOUT,
                env=env, stdin=stdin, start_new_session=True,
            )
        except OSError as exc:
            selector.close()
            raise Error("Command unavailable or timed out: " + command) from exc

        streams = [process.stdout] + ([process.stderr] if capture else [])
        try:
            for name, stream in zip(("stdout", "stderr"), streams):
                selector.register(stream, selectors.EVENT_READ, name)
            while selector.get_map() or process.poll() is None:
                self.check_cancelled()
                if deadline is not None and time.monotonic() >= deadline:
                    raise Error("Command unavailable or timed out: " + command)
                for key, _ in selector.select(timeout=0.1):
                    block = os.read(key.fd, 65536)
                    if not block:
                        selector.unregister(key.fileobj)
                        continue
                    if capture:
                        captured[key.data].extend(block)
                    else:
                        text = decoder.decode(block)
                        tail = (tail + text)[-2000:]
                        pending += text
                        while "\n" in pending:
                            line, pending = pending.split("\n", 1)
                            self.emit(line.rstrip("\r"))
                        if len(pending) >= 65536:
                            self.emit(pending)
                            pending = ""
            self.check_cancelled()
            if not capture:
                pending += decoder.decode(b"", final=True)
                if pending:
                    self.emit(pending.rstrip("\r"))
            stdout = captured["stdout"].decode(encoding, errors="replace") if capture else None
            stderr = captured["stderr"].decode(encoding, errors="replace") if capture else None
            result = subprocess.CompletedProcess(argv, process.wait(), stdout, stderr)
        except BaseException:
            self._stop_process(process)
            raise
        finally:
            selector.close()
            for stream in streams:
                stream.close()
        if check and result.returncode:
            detail = (stderr if capture else tail).strip()[-2000:]
            raise Error("Command failed: " + command + ("\n" + detail if detail else ""))
        return result

    def capture(self, argv, check=True, timeout=60):
        return self.run(argv, check=check, timeout=timeout, stdin=subprocess.DEVNULL)

    def action(self, argv):
        self.check_cancelled()
        self.emit(("Would run: " if self.dry_run else "Running: ") + shlex.join(argv))
        if not self.dry_run:
            if self.cancelled is None:
                self.run(argv, timeout=None, capture=False)
            else:
                # Parallel operations must not compete for terminal input.
                self.run(argv, timeout=None, capture=False, stdin=subprocess.DEVNULL)
