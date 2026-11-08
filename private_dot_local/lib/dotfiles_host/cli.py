"""Explicit user-facing entry points; no chezmoi apply hooks."""

import argparse
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
import os
from pathlib import Path
import signal
import sys
from threading import Event
from typing import Literal

from .core import ConfigurationError, Error, RebootRequired, Runner, detect_host, host_lock
from .host import HostManager
from .manifest import Manifest
from .updates import UpdateReport, UpdateTarget, run_updates
from .updates.policy import Profile, REGISTRIES, build_updates, resolve_phases


def add_update_arguments(parser: argparse.ArgumentParser) -> None:
    descriptions = {
        UpdateTarget.ALL: "Update everything permitted by this machine's profile (the default)",
        UpdateTarget.FLATPAK: "Update configured Flatpak installations (local profile only)",
        UpdateTarget.USER_TOOLS: "Update registered user tools and tool managers",
        UpdateTarget.RPM_OSTREE: "Stage an OS update last, without rebooting (local profile only)",
    }
    for target, help_text in descriptions.items():
        parser.add_argument("--" + target.value, dest="targets", action="append_const", const=target, default=[], help=help_text)


@contextmanager
def interrupt_updates(cancelled: Event) -> Iterator[None]:
    previous = signal.getsignal(signal.SIGINT)

    def interrupt(signum, frame):
        cancelled.set()
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, interrupt)
    try:
        yield
    except BaseException:
        cancelled.set()
        raise
    finally:
        signal.signal(signal.SIGINT, previous)


def print_update_report(report: UpdateReport, *, output: Callable[[str], None] = print) -> None:
    for result in report.results:
        status = "OK" if result.succeeded else "FAILED"
        detail = (": " + result.detail) if result.detail else ""
        output("{}: {}{}".format(result.phase.name.value, status, detail))


def main(operation: Literal["setup", "update"], argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install missing host software" if operation == "setup" else "Update selected software; Flatpaks and user tools run concurrently, then rpm-ostree")
    parser.add_argument("--dry-run", action="store_true", help="Query and print actions without installing, replacing, downloading archives, or creating state")
    parser.add_argument("--manifest", type=Path, default=Path.home() / ".config/dotfiles/software.json")
    parser.add_argument("--policy", type=Path, help="Machine-local approved Rocky policy (default: host-policy.json beside manifest)")
    if operation == "update":
        add_update_arguments(parser)
    args = parser.parse_args(argv)
    try:
        if os.geteuid() == 0:
            raise ConfigurationError("Run this command as your normal user, not sudo; only package operations are elevated")
        manifest = Manifest.load(args.manifest)
        host = detect_host(manifest.profile)
        home = Path.home()
        state = home / ".local/state/dotfiles"
        policy_path = args.policy or args.manifest.parent / "host-policy.json"
        if operation == "setup":
            manager = HostManager(manifest, host, Runner(args.dry_run), home, state, policy_path)
            with host_lock(state, dry_run=args.dry_run):
                manager.run()
            return 0

        registry = REGISTRIES[Profile(manifest.profile)]
        phases = resolve_phases(registry, requested=args.targets or [UpdateTarget.ALL])
        cancelled = Event()
        updates = build_updates(
            phases, manifest=manifest, host=host, home=home, state=state,
            policy_path=policy_path, dry_run=args.dry_run, cancelled=cancelled,
        )
        with interrupt_updates(cancelled), host_lock(state, dry_run=args.dry_run):
            report = run_updates(updates)
        print_update_report(report)
        if report.exit_code:
            print("Dry run incomplete." if args.dry_run else "Host update incomplete.")
        else:
            print("Dry run complete; no operational changes made." if args.dry_run else "Host update complete.")
        return report.exit_code
    except RebootRequired as exc:
        print("Reboot checkpoint: " + str(exc), file=sys.stderr)
        return 75
    except ConfigurationError as exc:
        print("Configuration/policy error: " + str(exc), file=sys.stderr)
        return 2
    except (Error, OSError) as exc:
        print("Failed: " + str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted; verify completed package operations before retrying.", file=sys.stderr)
        return 130
