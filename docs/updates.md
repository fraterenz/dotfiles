# Composable host updates

`update-host` updates the selected software on the current machine. It never
reboots automatically. Run it as your normal user, through the existing
chezmoi-managed uv launcher.

```sh
update-host                              # All updates allowed by this profile
update-host --all                        # Same selection, explicitly
update-host --flatpak                    # Local Flatpak installations only
update-host --user-tools                 # Registered user tools/managers only
update-host --rpm-ostree                  # Local OS only
update-host --flatpak --user-tools       # Both, concurrently; no OS update
update-host --user-tools --dry-run
```

Selectors are additive and repeated selectors are deduplicated. `--all` cannot
be combined with individual selectors. Existing `--manifest` and `--policy`
options remain available. There is no `--jobs` or sequential-mode option.

## Profiles and execution

| Profile | Stage 0 | Stage 1 |
| --- | --- | --- |
| `local` | Flatpak and user tools, concurrently | rpm-ostree |
| `work` | User tools | — |

Explicitly requesting Flatpak or rpm-ostree on `work` is rejected before any
updates. Work updates do not run DNF, inspect repositories, query installed
RPMs, or query Flatpak availability. They still require an owned, non-writable-by-
others machine-local policy containing `"approved": true`; unused DNF/Flatpak
policy sections are not prerequisites for user-tool updates. Site approval for
uv/Python startup downloads must be obtained separately, before launching the
helper.

An expected failure does not stop independent updates. Every configured Flatpak
installation and every registered user tool/manager is attempted. rpm-ostree
waits for both earlier phases to finish, whether they succeeded or failed. The
final report lists all completed phases and exits nonzero if any failed.

Flatpak installations are scopes such as `--user`, `--system`, or
`--installation=NAME`, not individual application IDs. Updates cover installed
applications and runtimes in each configured scope. Installations within a phase
and individual user tools remain sequential. Registered user-tool managers must
own only user-tool resources, not native packages or Flatpak installations.

One shared lock excludes concurrent setup/update runs. Interrupting a run stops
new stages and cooperatively stops active commands; it does not roll back
already completed package-manager transactions. Check completed operations
before retrying. Tool replacements retain their existing per-file recovery
guarantees. Binaries are tested on the currently booted host, not a staged OS.

Dry runs may query local state and upstream release metadata, but do not install,
replace files, download release archives, or create updater state. As with the
other helpers, uv's interpreter/environment/cache preparation happens before
the Python helper and is outside these operational dry-run guarantees. This
command updates neither uv nor managed Python interpreters.

`setup-host` keeps its prerequisite ordering and reboot checkpoints; it does not
use this independent-update scheduling policy.

## Python API

The implementation lives under `dotfiles_host/updates/`:

- `operations.py`: immutable phases/results, concrete update functions, staged
  executor.
- `policy.py`: profile registries, selector resolution, approval, and binding.
- `__init__.py`: lightweight execution-API exports.

```python
from collections.abc import Callable, Mapping
from dataclasses import dataclass

type UpdateFunction = Callable[[], None]

@dataclass(frozen=True)
class Phase:
    name: UpdateTarget
    stage: int

@dataclass(frozen=True)
class PhaseResult:
    phase: Phase
    succeeded: bool
    detail: str = ""

def run_updates(updates: Mapping[Phase, UpdateFunction]) -> UpdateReport:
    ...
```

Each `PhaseResult.phase` references the submitted immutable phase. An update
function returns normally on success or raises an existing domain/OS error on
failure. Unexpected programming errors and interrupts abort later stages.

The policy layer chooses one registry for the active profile, resolves CLI flags
to canonical phases, and uses `functools.partial` to bind operation-specific data
and a configured command runner. The mapping itself is the execution plan:

```python
report = run_updates({
    FLATPAK: flatpak_update,
    USER_TOOLS: user_tools_update,
})
```

The executor has no profile, registry, selector, dry-run, or concurrency
parameters. It validates the complete plan before invoking any function and
returns results in stage order, preserving plan order within each stage.
`UpdateTarget.ALL` is only a CLI selector and cannot be an executable phase.

The generic executor does not enforce profile approval or acquire the host
lock: those belong to the command/policy boundary. The CLI also installs the
interrupt guard before execution so the phase runners share cancellation.
