# Explicit devbox lifecycle

`setup-containers` is a Python 3.9+ standard-library helper for one rootless
`dev` container and its generated `dev.service`. It does not install packages,
use sudo, enable generated services, manage host OpenCode, or prune images/data.
Applying chezmoi files never invokes it.

Host and container helpers share command execution, OS detection, file locking,
and durable writes in `~/.local/lib/dotfiles/common.py`. Their operational locks
and deployment/download state remain separate. The controller keeps setup,
rollback, and recovery explicit; all retain the same deployment-state format.

## Desired, active, and recovery files

| Path | Owner |
| --- | --- |
| `~/.config/dotfiles/devbox/config.json` | Chezmoi: profile, required executables, timeouts and stability interval. |
| `~/.config/dotfiles/devbox/build/` | Chezmoi: the entire build context, including `Containerfile.dev`. |
| `~/.config/dotfiles/devbox/dev.container` | Chezmoi: desired runtime, outside Quadlet search paths. |
| `~/.config/containers/systemd/dev.container` | Helper: active unit pinned to a local image ID, not a mutable tag. |
| `~/.local/state/dotfiles/devbox/deployments.json` | Helper: private current, previous, and pending deployment snapshots. |

Standard XDG configuration/state locations are honored. Do not change them
between deployments or use custom Quadlet search-path overrides. `--config PATH`
selects a desired `config.json` with sibling `dev.container` and `build/`; it
does **not** redirect active state. The helper deploys from the files already
applied on disk, not directly from a Git checkout.

State uses atomic writes and mode `0600`, inside a mode `0700` directory. One
nonblocking lock covers setup, update, and rollback. Recovery includes unit text,
immutable image identity and retained tag, build fingerprint, mount identities,
resource-policy snapshots, source revision, verification policy, and available
version diagnostics for the standard required tools. A dirty source checkout is
marked; its actual build context is also fingerprinted.

## Before first activation

1. Apply only reviewed desired inputs/helpers. Keep the active Quadlet outside
   chezmoi. A staged change must remain inert even if user systemd reloads.
2. Use the actual supported host: `local` on Fedora Sway Atomic, `work` on Rocky
   Linux 9. Work-host permission for rootless containers, registry access and
   build downloads is an external prerequisite, not inferred from an OS match or
   host package-policy approval. Live Rocky validation remains outstanding.
3. Provide usable rootless Podman, user systemd, cgroup v2/systemd management,
   subordinate UID/GID mappings, `findmnt`, `systemd-analyze`, and a Quadlet
   generator that accepts the desired definition. Missing packages or required
   Atomic reboot checkpoints belong to the separate `setup-host` workflow.
4. Use approved persistent **local** container storage, workspace, and bind
   configuration sources. The helper rejects known network filesystems such as
   NFS/CIFS; it never silently disables SELinux or changes ownership with `:U`.
   Filesystem-type checks are not a complete storage/SELinux compatibility test.
5. Verify backups of `~/dev`, project data, credentials, container tool state and
   any named volumes. Review `podman diff dev` and copy out important writable
   layer files. Stopping the generated service removes its disposable container;
   retaining an image does **not** preserve that writable layer.
6. Run `setup-containers --dry-run`. Existing deployments require explicit
   `--adopt` acknowledgment on the first real run. Adoption checks the generated
   service, ownership label, running command/HOME/mount layout, and saves a
   recovery unit pinned to the **actual running image**, not `:latest`.

Initial adoption requires the old generated service to be running. Inactive
unmanaged definitions, unrelated containers named `dev`, foreign native services,
conflicting Quadlets, and unexpected active-file edits require manual review.
Do not concurrently manipulate `dev`, reload/start its service, or run pruning
while a helper operation is in progress. The helper lock cannot serialize tools
invoked outside this workflow.

The workspace defaults to `%h/dev`. An optional private chezmoi data value
`devWorkspace = "/approved/local/path"` changes its initial staging location and
is preserved by initialization. Paths must be absolute or use `%h`, without
whitespace, quotes, escapes or colon separators. Configuration mounts still
need suitable local sources; a local graphroot/workspace alone does not fix an
NFS-backed home. Moving/removing an existing writable mount or replacing its
directory inode is blocked pending a separately reviewed data migration.

## Setup and update behavior

```sh
~/.local/bin/setup-containers --dry-run
~/.local/bin/setup-containers             # empty machine or already adopted
~/.local/bin/setup-containers --adopt     # reviewed existing devbox, first time
~/.local/bin/setup-containers --update --dry-run
~/.local/bin/setup-containers --update
```

- Unchanged context/UID/GID/architecture reuses the recorded local image. A
  missing image or changed input builds a distinct candidate with the actual
  host UID/GID and container user `dev`. The entire context is fingerprinted,
  conservatively including excluded inputs; external symlinks/special files are
  rejected. It is not restricted to the Containerfile's bytes.
- Ordinary builds use `--pull=missing` and the build cache. Update deliberately
  uses `--pull=always --no-cache`. Neither changes source pins. Even an ordinary
  required rebuild can resolve newer floating dependencies after a cache miss.
- The real generator validates an isolated pinned unit. A disposable candidate
  precheck has no network, real mounts, ports, host credentials or `:Z` relabels.
- After prechecks, the helper retains both images and durably records a pending
  activation **before stopping** the old service. It atomically promotes the
  unit, reloads user systemd, resets service failure limits, and starts it.
- The active and recovery units use immutable local image IDs, `Pull=never` and
  `PodmanArgs=--replace=false`, not a moved active tag. Ownership is checked
  before stopping because Quadlet cleanup may remove containers by name.
- Verification checks service stability, actual image, numeric UID/GID, declared
  mount sources/modes/current file inodes, required executables, workspace
  writability, and unchanged external resource policies. Only then is the new
  deployment committed as current.

Runtime-only changes and atomically replaced configuration bind files restart
the service without rebuilding. An unchanged running runtime is checked without
restarting; changed verification requirements are also honored. Directory binds
see changes below their mountpoint, while individually bind-mounted files can
retain an old inode until explicitly reconciled.

Fish shares only reviewed static files read-only. The parent configuration
directory and container-local universal variables remain writable in the
persistent dev home; the host's `fish_variables` and entire `~/.local/bin` are
not mounted. New host configuration can affect a running container immediately;
chezmoi deployment is not a runtime/data transaction. Container credentials and
OpenCode authentication must be provisioned deliberately in the persistent dev
home, never imported from host authentication stores into public source.

### Input version policy

| Inputs | Current policy |
| --- | --- |
| Fedora base | Release tag `44`, not a digest; tag contents can change. |
| Fedora packages, including Neovim, ripgrep, par, fish, Go and LaTeX | Current packages from the image's configured Fedora repositories. |
| uv, ruff, ty | Floating upstream installs. |
| Rust | Floating `stable`, with clippy/rustfmt/rust-analyzer/rust-src. |
| Neovim plugins | Commit references in Containerfile arguments, some abbreviated. |
| tree-sitter parsers | The pinned plugin's parser lockfile. |
| gopls, tokei, OpenCode | Explicit version arguments in `Containerfile.dev`. |

This is not a bit-for-bit reproducible build or a comprehensive dependency
integrity audit. Review/pin installers and inputs where stronger guarantees are
required. Update cannot advance a pinned OpenCode version by itself: edit/apply
the version argument first. Source changes alone do not affect the running image.

### External policy and unsupported overrides

Existing passive systemd resource drop-ins (CPU/IO/memory limits, selected
timeouts, etc.) are snapshotted and left in place. Policies changed since the
recorded deployment cause a stop-for-review, not an automatic overwrite. New
on-disk drop-ins not yet loaded are inspected **before** the helper's reload.
Lifecycle overrides such as `ExecStart`/`ExecStop`, custom service identities,
arbitrary Podman arguments and Quadlet drop-ins are rejected. Merge reviewed
Quadlet changes into the desired definition first; new resource-policy adoption
needs a separately reviewed bookkeeping change. Do not delete state to bypass
these checks.

## Failure recovery and rollback

Build, generator and precheck failures leave the running deployment untouched.
Promotion/startup/verification or final-state-write failure triggers restoration
of the saved unit and exact local image, followed by verification. With no prior
deployment, recovery stops the failed helper-owned service and removes only its
active unit. The command still returns nonzero after successful recovery.

Interrupted activation is detected from durable `pending` state on the next
invocation, before consulting possibly broken desired files. Recovery runs first
and exits nonzero to require review rather than silently retrying the bad inputs.
If recovery cannot complete, preserve the pending state, unit and images; fix the
reported prerequisite/conflict or use manual recovery. Never rebuild/pull as a
substitute for a missing saved image.

```sh
~/.local/bin/setup-containers --rollback --dry-run
~/.local/bin/setup-containers --rollback
```

Rollback uses the saved previous deployment and never builds, pulls, or reads
the desired config. It is itself transactional, retaining the displaced current
runtime. It does not revert chezmoi source, host dotfiles, mutable dev-home data,
tool databases, or projects. Review/revert the failed desired change before the
next normal setup; the restored source revision is reported for reference.

Images receive helper-specific build/retention tags, which are not pruned by this
helper. Tags prevent ordinary dangling-image pruning, **not** explicit deletion
or `podman image prune --all`. Retain current/previous/pending images and data
backups. No bounded automatic cleanup policy is enabled yet.

### Manual runtime restoration if the helper is unusable

Keep helpers and unrelated Podman/service operations paused. Copy the private
deployment state and active unit for diagnosis, then inspect the record. For an
interrupted activation use `pending.before`; for a deliberate previous-runtime
restore use `previous`. A missing predecessor/image needs separate recovery,
not a guessed mutable tag. Extract the selected record's `unit` to a private
recovery file and note its `image`; verify that `Image=` matches that full ID,
`Pull=never`, and `--replace=false` is present.

Before stopping anything, inspect ownership and passive drop-ins:

```sh
systemctl --user show dev.service --property=SourcePath,FragmentPath,DropInPaths
podman inspect dev --format '{{index .Config.Labels "PODMAN_SYSTEMD_UNIT"}}'
podman image exists 'sha256:REPLACE_WITH_SAVED_FULL_IMAGE_ID'
```

Do not proceed if the unit/container is unrelated, the saved image is absent,
or external policies changed without review. Once verified, replace the two
placeholder paths below with the saved pinned recovery unit and actual active
path. Use a temporary sibling and rename, not a partial write over the active
file:

```sh
systemctl --user stop dev.service
install -m 0600 /path/to/saved-recovery.container ~/.config/containers/systemd/dev.container.recovery-new
mv -T ~/.config/containers/systemd/dev.container.recovery-new ~/.config/containers/systemd/dev.container
systemctl --user daemon-reload
systemctl --user reset-failed dev.service
systemctl --user start dev.service
systemctl --user is-active dev.service
podman inspect dev --format '{{.Image}}'
podman exec dev id
podman inspect dev --format '{{json .Mounts}}'
podman exec dev test -w /home/dev
```

Recheck required tools, read-only modes and file inodes. Do not restore resource
drop-ins over later user edits, delete persistent data, or force-remove a
conflicting container. Manual restoration does not reconcile deployment JSON;
keep helpers paused until its current/pending/history records are reviewed and
reconciled with the restored runtime. Preserve those records rather than starting
over by deleting them.

## Validation status

Unit/fault-injection tests cover failed builds, input races, failed promotion,
startup and smoke checks, interrupted activation, failed recovery, state-write
failures, explicit rollback, resource-policy conflicts and bind-file refreshes.
Both profiles deploy in scratch homes; the real installed generator is exercised
only with isolated `QUADLET_UNIT_DIRS` and `--user --dryrun`. A real original-host
dry run was checked against before/after container, service, unit, image-tag and
state snapshots, with no changes.

No real candidate build, devbox replacement, or live rollback has been performed.
Disposable Fedora and Rocky build/activation/storage/credential tests remain
necessary before claiming a validated clean-machine workflow.

Editor integration tests additionally run real `rg` searches and all three
`par` formatting mappings in headless Neovim, without loading personal config.
Coverage includes regex errors/no matches, ignored files, filenames containing
spaces/colons or non-UTF8 bytes, shell/Ex-command punctuation, prompt cancellation,
quickfix navigation, and protecting text when `par` is unavailable. Fedora's
`par` package was installed and tested **only in a disposable container** based
on the existing image, with no host mounts; the running devbox was unchanged.

`tests/test_nvim.py` runs these runtime checks when Neovim, `rg`, and `par` are
available locally. Otherwise it skips the runtime test unless
`DOTFILES_NVIM_TEST_COMMAND` contains a complete JSON argv for headless Neovim in
a disposable environment. The command must accept a Lua script on stdin (for
example, Neovim with `--headless --noplugin -u NONE -i NONE -n -l /dev/stdin`).
It must not load the live editor configuration or mount private project/tool data.
