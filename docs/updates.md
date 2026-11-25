# Update guide

Run these commands on the host as your normal user, not with `sudo`. The
helpers are in `~/.local/bin`.

## Update the host

Run `update-host` for everything allowed by your profile, or select what to
update:

```sh
update-host                          # All updates allowed by your profile
update-host --user-tools             # Registered host tools only
update-host --flatpak                # Flatpaks only (local)
update-host --rpm-ostree             # Stage an OS update (local)
update-host --flatpak --user-tools   # Both, without an OS update (local)
```

If an OS update was staged, reboot when ready to activate it. `update-host`
never reboots automatically. On `work`, follow the
[site approval steps](../README.md#work-profile-rocky-linux-9) before running
the helper, even for a dry run.

## Update `dev`

To refresh an already managed Podman devbox, save your work and back up
important data first: deploying the new image replaces the `dev` container.

```sh
setup-containers --update
```

This pulls a fresh base and rebuilds without cache. It can refresh unpinned
dependencies, but it does not change version pins. For first-time adoption or
rollback, see the [devbox lifecycle guide](containers.md).

To change the OpenCode version, see
[Upgrade OpenCode in `dev`](containers.md#upgrade-opencode-in-dev).

## Dry runs and host tool organization

### Preview changes

Add `--dry-run` to preview the operation before running it:

```sh
update-host --user-tools --dry-run    # Preview host tool updates
setup-containers --update --dry-run   # Preview a no-cache devbox rebuild
setup-containers --dry-run            # Preview applying a staged devbox change
```

The host preview can query local state, release metadata, and checksums, but
does not download release archives, replace software, or create updater state.
The devbox preview does not build an image or restart `dev`. Either launcher can
still download Python or prepare uv's cache before the helper starts; obtain
work-site approval for those downloads before running it.

### Find what manages each host tool

| Host software | How it is updated |
| --- | --- |
| OS and RPMs, including fish and Podman | `update-host --rpm-ostree` stages updates on `local`; the site administrator manages them on `work`. |
| Flatpak apps and runtimes | `update-host --flatpak` updates all installed apps and runtimes in configured scopes (`--user`, `--system`, or a named installation) on `local`. |
| Downloaded CLI tools | `update-host --user-tools` reads the [software manifest](../dot_config/dotfiles/software.json.tmpl) and updates registered tools in `~/.local/bin`. |
| uv, helper launchers, and managed Python | Chezmoi owns uv and the launchers; manage Python separately as described in [runtime maintenance](../README.md#runtime-maintenance). |

All six `downloaded_tools` in the software manifest currently use
`"policy": "latest"`: chezmoi, bat, fzf, glow, ripgrep (`rg`), and zoxide. This
selects the release GitHub marks as Latest, not necessarily the highest tag.
The helper chooses a precompiled Linux binary for the host CPU from the
manifest's release asset list. It verifies the archive's SHA-256 and checks the
binary's architecture and `--version` before replacing the installed binary.
Nothing is compiled locally.

The user-tools phase can also run registered tool managers, but none are
configured. If `update-host --user-tools` finds an unregistered entry in
`~/.local/bin`, such as a personal script, it reports it and leaves it alone.
It still attempts the registered updates, but reports the phase as incomplete
and exits with status 1 even if all those updates succeed.

### Pin a host tool to a release

Open the manifest source through chezmoi:

```sh
chezmoi edit ~/.config/dotfiles/software.json
```

Set the tool's `version` to an exact upstream tag. For example, change the `fzf`
entry to `"version": {"policy": "pinned", "tag": "v0.67.0"}`. Then apply the
manifest and run the user-tools phase. Other registered tools still follow
their own version policies.

```sh
chezmoi apply ~/.config/dotfiles/software.json
update-host --user-tools
```

## Profiles and execution

| Profile | Default update order |
| --- | --- |
| `local` | Flatpaks and user tools run concurrently, then `rpm-ostree` runs last. |
| `work` | Only user tools; Flatpak and OS selectors are rejected. |

Selectors are additive. `--all` is the default and cannot be combined with an
individual selector. See `update-host --help` for `--manifest` and `--policy`.
On `work`, the updater checks the machine-local approval policy without using
DNF or Flatpak settings.

An expected failure does not block independent updates or stop `rpm-ostree`
from running after the earlier phases finish. The report exits nonzero if any
phase failed. Host setup and updates share a lock; interrupting an update does
not roll back completed operations. `setup-host` still uses its own prerequisite
ordering and reboot checkpoints.

For implementation details, see the
[Python code behind `update-host`](../private_dot_local/lib/dotfiles_host/updates/).
