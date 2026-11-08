"""Update metadata, a generic staged executor, and independent operations."""

from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from enum import StrEnum, auto
import json
import os
from pathlib import Path

from ..core import CompatibilityError, ConfigurationError, Error, Host, Runner
from ..downloads import ToolUpdater
from ..manifest import Manifest, destination


class UpdateTarget(StrEnum):
    @staticmethod
    def _generate_next_value_(name, start, count, last_values):
        return name.lower().replace("_", "-")

    ALL = auto()
    FLATPAK = auto()
    USER_TOOLS = auto()
    RPM_OSTREE = auto()


type UpdateFunction = Callable[[], None]


@dataclass(frozen=True)
class Phase:
    name: UpdateTarget
    stage: int

    def __post_init__(self):
        if not isinstance(self.name, UpdateTarget) or self.name is UpdateTarget.ALL:
            raise ConfigurationError("A phase must name an executable UpdateTarget, not ALL")
        if type(self.stage) is not int or self.stage < 0:
            raise ConfigurationError("Phase stages must be nonnegative integers")


def _validate_phases(phases: Iterable[Phase]) -> tuple[Phase, ...]:
    phases = tuple(phases)
    if not phases or any(not isinstance(phase, Phase) for phase in phases):
        raise ConfigurationError("At least one valid Phase is required")
    if len({phase.name for phase in phases}) != len(phases):
        raise ConfigurationError("Each update target must have exactly one phase")
    return phases


@dataclass(frozen=True)
class PhaseResult:
    phase: Phase
    succeeded: bool
    detail: str = ""


@dataclass(frozen=True)
class UpdateReport:
    results: tuple[PhaseResult, ...]

    @property
    def exit_code(self) -> int:
        return int(any(not result.succeeded for result in self.results))


def _run_one(phase: Phase, update: UpdateFunction) -> PhaseResult:
    try:
        update()
    except (Error, OSError) as exc:
        return PhaseResult(phase, False, str(exc))
    return PhaseResult(phase, True)


def run_updates(updates: Mapping[Phase, UpdateFunction]) -> UpdateReport:
    """Run each stage concurrently, collecting expected failures before advancing."""
    if not isinstance(updates, Mapping):
        raise ConfigurationError("Updates must map phases to configured functions")
    items = tuple(updates.items())
    phases = _validate_phases(phase for phase, _ in items)
    if any(not callable(update) for _, update in items):
        raise ConfigurationError("Every update must be a callable with its inputs already bound")

    results = []
    for stage in sorted({phase.stage for phase in phases}):
        batch = [(phase, update) for phase, update in items if phase.stage == stage]
        if len(batch) == 1:
            results.append(_run_one(*batch[0]))
            continue
        with ThreadPoolExecutor(max_workers=len(batch), thread_name_prefix="update-host") as pool:
            futures = [pool.submit(_run_one, phase, update) for phase, update in batch]
            try:
                results.extend(future.result() for future in futures)
            except BaseException:
                # Running Python functions must stop cooperatively; never start
                # another stage after an interrupt or an unexpected exception.
                for future in futures:
                    future.cancel()
                raise
    return UpdateReport(tuple(results))


def atomic_status(runner):
    """Read pending Atomic deployments without changing the booted host."""
    try:
        status = json.loads(runner.capture(["rpm-ostree", "status", "--json"]).stdout)
        deployments = status["deployments"]
        booted = [i for i, deployment in enumerate(deployments) if deployment.get("booted")]
        if len(booted) != 1:
            raise ValueError("Missing/ambiguous booted deployment")
        pending = [
            deployment for i, deployment in enumerate(deployments)
            if not deployment.get("booted") and (deployment.get("staged") or i < booted[0])
        ]
        requested = {name for deployment in pending for name in deployment.get("requested-packages", [])}
        return bool(pending), requested
    except (ValueError, KeyError, TypeError) as exc:
        raise Error("Cannot interpret rpm-ostree deployment status") from exc


def flatpak_flags(installation):
    if installation.get("name"):
        return ["--installation=" + installation["name"]]
    return ["--" + installation["scope"]]


def update_flatpaks(
    *,
    flatpak_installations: Sequence[Mapping[str, str]],
    runner: Runner,
) -> None:
    failures = []
    for installation in flatpak_installations:
        runner.check_cancelled()
        try:
            runner.action([
                "flatpak", "update", *flatpak_flags(installation),
                "--noninteractive", "--assumeyes",
            ])
        except (Error, OSError) as exc:
            runner.emit("{}: {}".format(installation["id"], exc))
            failures.append(installation["id"])
    if failures:
        raise Error("Flatpak updates incomplete: " + ", ".join(failures))


def unknown_entries(manifest, home):
    known = set(manifest.managed)
    for tool in manifest.tools:
        relative = Path(destination(tool))
        if relative.parent == Path(".local/bin"):
            known.update([tool["executable"]] + tool["aliases"])
    for manager in manifest.managers:
        known.update(manager["entries"])
    directory = Path(home) / ".local/bin"
    return sorted(path.name for path in directory.iterdir() if path.name not in known) if directory.is_dir() else []


def reconcile_user_tools(*, manifest, home, runner, updater, update, pending_reboot=False):
    """Share installation safeguards with setup without sharing orchestration."""
    failures = []
    for entry in unknown_entries(manifest, home):
        runner.emit("Unregistered local-bin entry (left untouched): {!r}".format(entry))
        failures.append("unregistered entry")
    for tool in manifest.tools:
        runner.check_cancelled()
        try:
            updater.reconcile(tool, update=update)
        except (Error, OSError) as exc:
            runner.emit("{}: {}".format(tool["id"], exc))
            if isinstance(exc, CompatibilityError) and pending_reboot:
                runner.emit("Replacement deferred: reboot into the pending deployment and rerun update-host --user-tools.")
            failures.append(tool["id"])
    for manager in manifest.managers:
        runner.check_cancelled()
        missing = any(not os.path.lexists(Path(home) / ".local/bin" / entry) for entry in manager["entries"])
        if not update and not missing:
            continue
        try:
            runner.action(manager["update_argv"] if update else manager["install_argv"])
            if not runner.dry_run and any(not os.path.lexists(Path(home) / ".local/bin" / entry) for entry in manager["entries"]):
                raise Error("Manager did not provide its declared entries")
        except (Error, OSError) as exc:
            runner.emit("{}: {}".format(manager["id"], exc))
            failures.append(manager["id"])
    if failures:
        raise Error("User-tool phase incomplete ({}); working installations were retained where replacement failed".format(
            ", ".join(failures),
        ))


def update_user_tools(
    *,
    manifest: Manifest,
    host: Host,
    home: Path,
    state: Path,
    runner: Runner,
) -> None:
    pending_reboot = atomic_status(runner)[0] if host.os_id == "fedora" else False
    reconcile_user_tools(
        manifest=manifest, home=home, runner=runner,
        updater=ToolUpdater(home, state, host, runner),
        update=True, pending_reboot=pending_reboot,
    )


def update_rpm_ostree(*, host: Host, runner: Runner) -> None:
    if host.profile != "local" or host.os_id != "fedora":
        raise ConfigurationError("rpm-ostree updates require the local Atomic host")
    runner.action(["rpm-ostree", "upgrade"])
    pending_reboot, _ = atomic_status(runner)
    if pending_reboot:
        runner.emit("System update is staged for a manual reboot; no reboot is performed automatically.")
