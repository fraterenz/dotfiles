"""Explicit, transactional activation of one rootless Quadlet devbox."""

import argparse
import copy
from contextlib import nullcontext
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

from .container_backend import Backend
from .container_support import (
    DEFAULT_TOOLS, Error, Paths, Store, Unit, atomic_write, context_fingerprint,
    image_id, read_text, sync_directory, validate_policy,
)


class Settings:
    def __init__(self, path):
        self.path = path
        try:
            self.raw = path.read_bytes()
            data = json.loads(self.raw)
        except (OSError, ValueError) as exc:
            raise Error("Apply the staged devbox configuration first: " + str(path)) from exc
        if not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] != 1:
            raise Error("Unsupported devbox configuration version")
        if set(data) - {"version", "profile", "required_tools", "timeout_seconds", "stability_seconds"}:
            raise Error("Unknown devbox configuration field")
        self.profile = data.get("profile")
        self.tools = data.get("required_tools", list(DEFAULT_TOOLS))
        self.timeout = data.get("timeout_seconds", 300)
        self.stability = data.get("stability_seconds", 2)
        validate_policy(self.policy())
        self.context = path.parent / "build"
        self.unit_path = path.parent / "dev.container"
        if self.unit_path.is_symlink():
            raise Error("The staged Quadlet must be a regular file")
        try:
            self.unit = Unit(self.unit_path.read_text())
        except OSError as exc:
            raise Error("Apply the staged desired Quadlet before setup") from exc
        self.unit.validate()
        if not (self.context / "Containerfile.dev").is_file():
            raise Error("The staged build context needs Containerfile.dev")

    def policy(self):
        return {
            "profile": self.profile, "required_tools": self.tools,
            "timeout_seconds": self.timeout, "stability_seconds": self.stability,
        }


class Lifecycle:
    def __init__(self, paths, backend=None, config=None, output=print):
        self.paths = paths
        self.backend = backend or Backend(paths)
        self.store = Store(paths)
        self.config_path = config or paths.desired / "config.json"
        self.output = output

    def active_text(self):
        if self.paths.active.is_symlink():
            raise Error("The active Quadlet is a symlink; review it before adoption")
        return read_text(self.paths.active)

    def assert_active(self, *records):
        text = self.active_text()
        allowed = set()
        for record in records:
            if record is None:
                allowed.add(None)
            else:
                allowed.update((record["unit"], record.get("observed_unit", record["unit"])))
        if text not in allowed:
            raise Error("The active Quadlet changed outside setup-containers; preserve and review it before proceeding")

    def signatures(self, text):
        return [mount.identity() for mount in Unit(text).mounts(self.paths.home)]

    def record(self, unit, image, policy, fingerprint=None):
        return {
            **policy,
            "unit": unit, "observed_unit": unit, "image": image_id(image),
            "active_path": str(self.paths.active),
            "build_fingerprint": fingerprint, "mounts": self.signatures(unit),
            "dropins": self.backend.dropins(),
            "source_revision": self.backend.source_revision(),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

    def adopt(self, settings, dry_run, acknowledged):
        text = self.active_text()
        service = self.backend.service()
        container = self.backend.container()
        if text is None:
            if container or service.get("LoadState") != "not-found":
                raise Error("An existing dev service/container has no matching active Quadlet")
            return None
        if service.get("ActiveState") != "active" or not container or not container.get("State", {}).get("Running"):
            raise Error("First adoption requires the existing generated dev.service to be running; review inactive definitions manually")
        if not dry_run and not acknowledged:
            raise Error("Existing devbox needs review: run --dry-run, back up its data, then use --adopt")
        old = Unit(text)
        old.validate()
        self.backend.preflight(settings.profile, old.mounts(self.paths.home))
        image = image_id(container["Image"])
        record = self.record(old.pinned(image), image, dict(settings.policy(), required_tools=[]))
        record["observed_unit"] = text
        self.backend.validate_unit(record["unit"], image)
        self.backend.audit_adoption(record)
        record["writable_layer_diff"] = self.backend.writable_diff()
        self.output("Existing devbox detected; adoption preserves its actual image and runtime definition.")
        self.output("Review/back up persistent work and writable-layer changes before --adopt.")
        if record["writable_layer_diff"]:
            self.output("Writable-layer changes:\n" + record["writable_layer_diff"])
        if not dry_run:
            record["retained_tag"] = self.backend.retain(image)
        return record

    def run(self, update=False, rollback=False, adopt=False, dry_run=False):
        if os.geteuid() == 0:
            raise Error("Run setup-containers as the rootless user, never with sudo")
        with (nullcontext() if dry_run else self.store.lock()):
            return self.reconcile(update, rollback, adopt, dry_run)

    def reconcile(self, update, rollback, adopt, dry_run):
        state = self.store.read()
        if state["pending"]:
            pending = state["pending"]
            target = pending["before"] or pending["after"]
            mounts = Unit(pending["before"]["unit"]).mounts(self.paths.home) if pending["before"] else []
            self.backend.preflight(target["profile"], mounts)
            if dry_run:
                self.check_recovery(state)
                self.output("Would recover the interrupted activation before accepting any new deployment.")
                return
            self.recover(state)
            raise Error("Interrupted activation recovered; review desired inputs before rerunning setup")
        if rollback:
            return self.rollback(state, dry_run)
        return self.setup(state, update, adopt, dry_run)

    def rollback(self, state, dry_run):
        target = state["previous"]
        if not target:
            raise Error("No retained previous working deployment is available")
        self.backend.preflight(target["profile"], Unit(target["unit"]).mounts(self.paths.home))
        self.assert_active(state["current"])
        if not self.backend.image_exists(target["image"]):
            raise Error("The retained rollback image is missing; rollback never builds or pulls")
        self.backend.validate_unit(target["unit"], target["image"])
        if self.backend.dropins() != target["dropins"]:
            raise Error("External service resource policy changed; review it before rollback")
        if dry_run:
            self.output("Would restore the retained image and active Quadlet without building or pulling.")
            return
        target = copy.deepcopy(target)
        target["mounts"] = self.signatures(target["unit"])
        self.activate(state, target)
        self.output("Previous runtime restored. Desired files and persistent data were not reverted.")
        self.output("Review desired inputs before setup; restored source revision: " + target["source_revision"])

    def setup(self, state, update, adopt, dry_run):
        settings = Settings(self.config_path)
        mounts = settings.unit.mounts(self.paths.home)
        architecture = self.backend.preflight(settings.profile, mounts)
        if not state["current"]:
            state["current"] = self.adopt(settings, dry_run, adopt)
            if state["current"] and not dry_run:
                self.store.write(state)
        self.assert_active(state["current"])
        current = state["current"]
        if current and current["profile"] != settings.profile:
            raise Error("The saved deployment belongs to a different machine profile")
        if current and self.backend.dropins() != current["dropins"]:
            raise Error("External service resource policy changed; review it before deployment")
        if current:
            self.check_persistent_mounts(current, mounts)
        fingerprint = context_fingerprint(settings.context, os.getuid(), os.getgid(), architecture)
        build = update or not current or current["build_fingerprint"] != fingerprint or not self.backend.image_exists(current["image"])
        image = current["image"] if current else "sha256:" + "0" * 64
        unit = settings.unit.pinned(image)
        self.backend.validate_unit(unit, image)
        if dry_run:
            self.output("Would build a distinct candidate image." if build else "Would reuse the recorded immutable image.")
            if update:
                self.output("Update policy: --pull=always and --no-cache; version pins remain in Containerfile.dev.")
            elif build:
                self.output("Setup uses the build cache; floating dependencies may still change on cache misses.")
            if not build and self.unchanged(current, unit, fingerprint):
                self.output("Runtime and mount identities are unchanged; no restart is needed.")
            else:
                self.output("Would validate, promote, start, and verify dev.service; failure restores the previous runtime.")
            if self.active_text() is not None and not adopt and current and current["build_fingerprint"] is None:
                self.output("First real activation requires --adopt after reviewing backups.")
            self.output("Dry run: no builds, tags, state writes, service changes, or host OpenCode changes.")
            return

        if build:
            self.output("Building a candidate; the running image/tag and active Quadlet remain unchanged.")
            if not update:
                self.output("Floating dependencies may resolve newer versions if build layers miss the cache.")
            image = self.backend.build(settings.context, update)
            unit = settings.unit.pinned(image)
            self.backend.validate_unit(unit, image)
        if self.unchanged(current, unit, fingerprint):
            checked = self.verified(dict(current, **settings.policy()))
            self.check_inputs(settings, architecture, fingerprint)
            if checked != current:
                state["current"] = checked
                self.store.write(state)
            self.output("Devbox image and runtime are current; no restart was needed.")
            return
        self.backend.precheck(image, settings.tools)
        self.check_inputs(settings, architecture, fingerprint)
        for mount in mounts:
            if mount.target == "/home/dev" and not Path(mount.source).exists():
                Path(mount.source).mkdir(parents=True, mode=0o700)
        candidate = self.record(unit, image, settings.policy(), fingerprint)
        candidate["retained_tag"] = self.backend.retain(image)
        self.activate(state, candidate)
        self.output("Devbox activated and verified. Previous runtime retained; no persistent data was removed.")

    def check_inputs(self, settings, architecture, fingerprint):
        if settings.path.read_bytes() != settings.raw or settings.unit_path.read_text() != settings.unit.text or context_fingerprint(settings.context, os.getuid(), os.getgid(), architecture) != fingerprint:
            raise Error("Desired inputs changed during setup; the active deployment was not replaced")

    def verified(self, record):
        record = copy.deepcopy(record)
        record["tool_versions"] = self.backend.verify(record)
        return record

    def check_persistent_mounts(self, current, desired):
        proposed = {mount.target: mount for mount in desired}
        saved = {row["target"]: row for row in current["mounts"]}
        for old in Unit(current["unit"]).mounts(self.paths.home):
            if "ro" in old.options:
                continue
            new = proposed.get(old.target)
            if not new or "ro" in new.options or new.identity() != saved.get(old.target):
                raise Error("A writable persistent mount changed; restore its path or perform a separately reviewed data migration: " + old.target)

    def unchanged(self, current, unit, fingerprint):
        if not current or current["build_fingerprint"] != fingerprint or current["unit"] != unit:
            return False
        if self.active_text() != unit or current["mounts"] != self.signatures(unit):
            return False
        service = self.backend.service()
        container = self.backend.container()
        return bool(service.get("ActiveState") == "active" and container and container.get("State", {}).get("Running") and image_id(container["Image"]) == current["image"])

    def activate(self, state, candidate):
        candidate = copy.deepcopy(candidate)
        candidate["observed_unit"] = candidate["unit"]
        before = copy.deepcopy(state["current"])
        self.assert_active(before)
        if before and self.backend.dropins() != before["dropins"]:
            raise Error("External resource policy changed before activation")
        if before:
            self.check_persistent_mounts(before, Unit(candidate["unit"]).mounts(self.paths.home))
            self.backend.validate_unit(before["unit"], before["image"])
            before["retained_tag"] = self.backend.retain(before["image"])
        pending_state = copy.deepcopy(state)
        pending_state["pending"] = {"before": before, "after": candidate, "previous_before": state["previous"]}
        self.store.write(pending_state)  # Must be durable before stopping the working service.
        try:
            self.backend.stop(candidate["timeout_seconds"])
            self.assert_active(before)
            atomic_write(self.paths.active, candidate["unit"].encode())
            self.backend.start(candidate["timeout_seconds"])
            candidate = self.verified(candidate)
            committed = {"version": 1, "current": candidate, "previous": before, "pending": None}
            self.store.write(committed)
        except BaseException as failure:
            try:
                self.recover(pending_state)
            except BaseException as recovery:
                raise Error("Activation failed and automatic recovery failed. Preserve pending state; use manual recovery.\nActivation: " + str(failure) + "\nRecovery: " + str(recovery)) from failure
            message = "the previous runtime was recovered" if before else "the failed first deployment was stopped and its active unit removed"
            raise Error("Activation failed; " + message + ".\n" + str(failure)) from failure

    def check_recovery(self, state):
        pending = state["pending"]
        before, after = pending["before"], pending["after"]
        self.assert_active(before, after)
        if before:
            if not self.backend.image_exists(before["image"]):
                raise Error("Previous local image is missing; recovery will not pull or rebuild")
            if self.backend.dropins() != before["dropins"]:
                raise Error("External resource-policy edits require manual review; they will not be overwritten")
            self.backend.validate_unit(before["unit"], before["image"])

    def recover(self, state):
        self.check_recovery(state)
        pending = state["pending"]
        before, after = pending["before"], pending["after"]
        # Deliberately use the service lifecycle, never podman rm --force or volume deletion.
        self.backend.stop(after["timeout_seconds"])
        self.assert_active(before, after)
        if before:
            atomic_write(self.paths.active, before["unit"].encode())
            self.backend.start(before["timeout_seconds"])
            before = self.verified(before)
            before["observed_unit"] = before["unit"]
            before["mounts"] = self.signatures(before["unit"])
        else:
            if self.paths.active.exists():
                self.paths.active.unlink()
                sync_directory(self.paths.active.parent)
            self.backend.unload()
        self.store.write({"version": 1, "current": before, "previous": pending["previous_before"], "pending": None})


def main(argv=None):
    parser = argparse.ArgumentParser(description="Explicit rootless devbox build, activation, update, and rollback")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--update", action="store_true", help="Pull the base and rebuild without cached layers, respecting source pins")
    mode.add_argument("--rollback", action="store_true", help="Restore the saved previous runtime; never build or pull")
    parser.add_argument("--adopt", action="store_true", help="Acknowledge review/backups before replacing the existing unmanaged devbox")
    parser.add_argument("--dry-run", action="store_true", help="Inspect/validate and explain actions without persistent changes")
    parser.add_argument("--config", type=Path, help="Staged config.json path; active/state paths remain fixed to this user")
    options = parser.parse_args(argv)
    if options.rollback and options.adopt:
        parser.error("--adopt is not applicable to --rollback")
    try:
        Lifecycle(Paths.environment(), config=options.config).run(
            update=options.update, rollback=options.rollback,
            adopt=options.adopt, dry_run=options.dry_run,
        )
    except Error as exc:
        print("setup-containers: " + str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("setup-containers: interrupted before activation", file=sys.stderr)
        return 130
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print("setup-containers: invalid input or system error: " + str(exc), file=sys.stderr)
        return 1
    return 0
