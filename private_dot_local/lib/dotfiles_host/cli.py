"""Explicit user-facing entry points; no chezmoi apply hooks."""

import argparse
import os
from pathlib import Path
import sys

from .core import ConfigurationError, Error, RebootRequired, Runner, detect_host, host_lock
from .host import HostManager
from .manifest import Manifest


def main(operation, argv=None):
    parser = argparse.ArgumentParser(description="Install missing host software" if operation == "setup" else "Update host packages/Flatpaks before registered user tools")
    parser.add_argument("--dry-run", action="store_true", help="Query and print actions without installing, replacing, downloading archives, or creating state")
    parser.add_argument("--manifest", type=Path, default=Path.home() / ".config/dotfiles/software.json")
    parser.add_argument("--policy", type=Path, help="Machine-local approved Rocky policy (default: host-policy.json beside manifest)")
    args = parser.parse_args(argv)
    try:
        if os.geteuid() == 0:
            raise ConfigurationError("Run this command as your normal user, not sudo; only package operations are elevated")
        manifest = Manifest.load(args.manifest)
        host = detect_host(manifest.profile)
        home = Path.home()
        state = home / ".local/state/dotfiles"
        runner = Runner(args.dry_run)
        manager = HostManager(manifest, host, runner, home, state, args.policy or args.manifest.parent / "host-policy.json")
        with host_lock(state, dry_run=args.dry_run):
            manager.run(update=operation == "update")
        return 0
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
