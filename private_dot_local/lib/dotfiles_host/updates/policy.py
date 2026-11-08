"""Profile policy and construction of fully bound update plans."""

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum, auto
from functools import partial
from pathlib import Path
from threading import Event, Lock

from ..core import ConfigurationError, Host, Runner
from ..manifest import Manifest, work_approval
from .operations import (
    Phase, UpdateFunction, UpdateTarget, _validate_phases,
    update_flatpaks, update_rpm_ostree, update_user_tools,
)


class Profile(StrEnum):
    LOCAL = auto()
    WORK = auto()


@dataclass(frozen=True)
class PhaseRegistry:
    phases: tuple[Phase, ...]

    def __init__(self, phases: Iterable[Phase]) -> None:
        object.__setattr__(self, "phases", _validate_phases(phases))


FLATPAK = Phase(name=UpdateTarget.FLATPAK, stage=0)
USER_TOOLS = Phase(name=UpdateTarget.USER_TOOLS, stage=0)
RPM_OSTREE = Phase(name=UpdateTarget.RPM_OSTREE, stage=1)

REGISTRIES = {
    Profile.LOCAL: PhaseRegistry([FLATPAK, USER_TOOLS, RPM_OSTREE]),
    Profile.WORK: PhaseRegistry([USER_TOOLS]),
}


def resolve_phases(
    registry: PhaseRegistry,
    *,
    requested: Iterable[UpdateTarget],
) -> tuple[Phase, ...]:
    requested = tuple(requested)
    if not requested or any(not isinstance(target, UpdateTarget) for target in requested):
        raise ConfigurationError("Select at least one UpdateTarget enum member")
    targets = set(requested)
    if UpdateTarget.ALL in targets:
        if len(targets) != 1:
            raise ConfigurationError("--all cannot be combined with individual update selectors")
        return registry.phases
    unavailable = targets - {phase.name for phase in registry.phases}
    if unavailable:
        raise ConfigurationError("Updates unavailable for this profile: " + ", ".join(sorted(unavailable)))
    return tuple(phase for phase in registry.phases if phase.name in targets)


def _phase_output(target, output, lock, message):
    with lock:
        for line in str(message).splitlines() or [""]:
            output("[{}] {}".format(target.value, line))


def build_updates(
    phases: Sequence[Phase],
    *,
    manifest: Manifest,
    host: Host,
    home: Path,
    state: Path,
    policy_path: Path,
    dry_run: bool,
    cancelled: Event,
    output: Callable[[str], None] = print,
) -> dict[Phase, UpdateFunction]:
    phases = _validate_phases(phases)
    try:
        profile = Profile(manifest.profile)
    except ValueError as exc:
        raise ConfigurationError("Unsupported update profile") from exc
    if host.profile != profile:
        raise ConfigurationError("Software manifest and detected host profiles differ")
    if any(phase not in REGISTRIES[profile].phases for phase in phases):
        raise ConfigurationError("Update plan contains a noncanonical or unavailable phase")
    if profile is Profile.WORK:
        # User updates require site approval, never DNF configuration, enabled
        # repositories, Flatpak permissions, or native package availability.
        work_approval(policy_path)

    updates = {}
    output_lock = Lock()
    for phase in phases:
        runner = Runner(
            dry_run=dry_run,
            emit=partial(_phase_output, phase.name, output, output_lock),
            cancelled=cancelled,
        )
        if phase.name is UpdateTarget.FLATPAK:
            update = partial(update_flatpaks, flatpak_installations=manifest.installations, runner=runner)
        elif phase.name is UpdateTarget.USER_TOOLS:
            update = partial(update_user_tools, manifest=manifest, host=host, home=home, state=state, runner=runner)
        else:
            update = partial(update_rpm_ostree, host=host, runner=runner)
        updates[phase] = update
    return updates
