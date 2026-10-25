# dotfiles

Chezmoi-managed dotfiles for Fedora Sway Atomic and Rocky Linux 9.

## Current status

This is a migration in progress, not a validated clean-machine bootstrap.
Profile selection, local-only Sway deployment, the software manifest, and explicit
Python host setup/update and container lifecycle commands are implemented. Host helpers have unit,
sandbox-deployment, and dry-run checks; real Fedora package transactions and live
Rocky setup have not been validated. Container tests cover fault recovery,
sandbox deployment, the real isolated Quadlet generator, and a read-only host dry
run; no candidate image has been built or activated on the original host.
`chezmoi apply` only deploys files; it never installs packages or starts containers.

## Profiles

Initialization remembers `local` or `work` in the private local chezmoi config:

| Profile | Intended host | Sway |
| --- | --- | --- |
| `local` | Fedora Sway Atomic | Manage the supplied configuration, including keyboard/display settings. |
| `work` | Rocky Linux 9 | Do not manage or deploy any Sway files. |

Ignoring Sway does not delete pre-existing configuration on a work machine.
Hardware settings stay in the local profile. No wallpaper image is distributed:
local Sway uses an existing `~/Pictures/background.jpg` if present, otherwise a
solid Dayfox background. The wallpaper itself remains unmanaged.

## CI tests

GitHub Actions runs the tests on pushes to `main`, pull requests, and manual
dispatch. The small matrix uses Rocky Linux 9 with its Python 3.9 and Fedora 44
containers; Fedora also runs the real headless Neovim/ripgrep/par tests. Both
install test dependencies from distro packages (EPEL is enabled only inside the
disposable Rocky CI container), then run the suite as an unprivileged user.

CI has read-only repository permissions and no release, deployment, tarball,
or artifact-publishing jobs. CI-only package installation does not change the
standalone-binary chezmoi policy on your actual hosts.

Fedora container coverage is **not** a booted Sway Atomic system: actual OSTree,
rootless user-systemd activation, storage/SELinux and live rollback still require
the documented disposable-host/VM checks. CI never builds or activates the devbox.

## Host software

Bootstrap Git, Python 3.9+, and a verified upstream chezmoi binary at
`~/.local/bin/chezmoi` first. Do not layer chezmoi or commit downloaded binaries.
For a new checkout, initialize the public source, choose a profile, and review:

```sh
~/.local/bin/chezmoi init https://github.com/fraterenz/dotfiles.git
~/.local/bin/chezmoi diff
~/.local/bin/chezmoi apply --dry-run --verbose
# Apply only after reviewing all pending changes.
~/.local/bin/chezmoi apply
```

Chezmoi deploys `~/.config/dotfiles/software.json`, `~/.local/bin/setup-host`,
`~/.local/bin/update-host`, and their standard-library package under
`~/.local/lib/dotfiles_host/`, with shared command/host/filesystem primitives in
`~/.local/lib/dotfiles/common.py` also used by the container helper. Deploy the
helpers together with their library files, not as standalone script copies.
Source names use `private_dot_local` to keep
`~/.local` mode `0700`; the commands themselves are executable. No setup hooks run.

Run as your normal user, never with `sudo` around the whole helper:

```sh
~/.local/bin/setup-host --dry-run
~/.local/bin/setup-host
~/.local/bin/update-host --dry-run
~/.local/bin/update-host
```

Both commands accept `--manifest PATH` and `--policy PATH`. By default the
machine-local policy is `host-policy.json` beside the deployed manifest.

| Command | Behavior |
| --- | --- |
| `setup-host` | Install missing native packages, then declared Flatpaks, then missing downloaded tools. Keep usable existing tools; report a pin mismatch rather than silently changing it. |
| `update-host` | Update native software, then **every configured Flatpak installation**, then downloaded tools and registered tool managers. Stop before user-tool updates if a required host step fails. |

On `local`, native setup uses `rpm-ostree install --idempotent`; updating uses
`rpm-ostree upgrade`. If required packages are staged but not active, setup exits
`75`: reboot manually and rerun it. Updates may leave a deployment pending reboot;
replacement binaries must execute on the currently booted host. Nothing reboots
automatically. Package updates cover installed software, not just the setup list.

The manifest records Chrome, mpv, and Zotero in the default system Flatpak
installation and Papers in the user installation. Setup requires existing
approved remotes; neither command adds repositories/remotes. Configure Terra
before installing the local `ghostty` requirement, and configure Flathub in the
appropriate scopes before installing its apps. Source URLs in the manifest are
references, not automatic repository-install instructions.

### Rocky approval

The `work` profile is accepted only on actual Rocky Linux 9 and blocks **all**
host-helper operations, including dry runs, until site approval is recorded.
The default work Flatpak list is empty. After obtaining approval, prepare a local
policy; never commit actual workplace policy or private repository information:

```sh
cp ~/.local/share/chezmoi/docs/host-policy.example.json ~/.config/dotfiles/host-policy.json
chmod 600 ~/.config/dotfiles/host-policy.json
```

Replace the placeholder repository IDs with approved, already-enabled IDs;
record the approved install/update arguments, and set `approved` to `true` **only
after approval**. The example is intentionally unapproved. The helper invokes
`sudo /usr/bin/dnf` with every other repository disabled. It accepts `install`
for setup and `upgrade`, `update`, or `upgrade-minimal` for updating, with flags
from `-y`, `--assumeyes`, `--refresh`, `--best`, `--nobest`, `--security`,
`--bugfix`, `--enhancement`, and `--newpackage`. Unsupported site rules require
reviewed helper changes, not repository/config overrides or bypassing the policy.

Any work Flatpak installation must also be declared in the manifest and approved
in the policy's `flatpak_installations`: `user`, `system`, or an exact named
system installation. Approval does not create the installation or its remotes.
Rocky package availability, Podman/Quadlet versions, permissions, and storage
prerequisites still need validation on the real work host.

### Editing desired software

Edit `dot_config/dotfiles/software.json.tmpl` in the chezmoi source (or use
`chezmoi edit ~/.config/dotfiles/software.json`), review the rendered diff, apply,
then run `setup-host`. Both helpers read the same manifest without rewriting it.

- Native records belong to the selected profile. Fish is native-package-owned;
  chezmoi is standalone and OpenCode is **container-only**.
- Flatpaks declare an app ID, existing remote, installation, and applicable
  profiles. Named system installations use the same value for `id` and `name`.
- Each downloaded tool declares its upstream `owner/repository`, executable,
  home-relative destination, profiles, compatibility notes, aliases, and ordered
  `.tar.gz` asset names for `x86_64`/`aarch64`. Asset templates support `{tag}` and
  `{version}` (tag without leading `v`); checksum templates also support `{asset}`.
- Use `{"policy": "latest"}` for the latest stable release, or
  `{"policy": "pinned", "tag": "v1.2.3"}` for an exact tag. Reconcile an existing
  tool's changed pin with `update-host`; a pin can deliberately downgrade it.
- Downloads require GitHub's SHA256 release-asset digest or an exact entry in the
  declared upstream `checksum_asset`. No digest means no installation. Only
  tar.gz archives are supported; the expected regular executable is extracted,
  checked for architecture, and probed with `--version` before replacement.
- Put aliases in their tool's `aliases` list; the updater creates relative
  symlinks, never overwrites foreign aliases, and updates the owner only once.
  Register supported user-tool managers with `id`, `profiles`, `entries`,
  `install_argv`, and `update_argv` rather than downloading over their files.
  Reserve `managed_bin_entries` for actual chezmoi-owned scripts.

Every unregistered entry in `~/.local/bin` is reported and left untouched, with a
nonzero result. Registered tools at other home-relative destinations are also
handled. The host updater never installs or updates OpenCode and does not change
the active container or the user's login shell.

### Dry runs, failures, and recovery

Dry runs inspect the running OS, installed RPMs, Flatpaks, and binary versions;
they may fetch HTTPS release metadata/checksum text. They do **not** run package
transactions, download executable archives, create operational state, or replace
files. Planned post-reboot actions are labeled. A successful dry run is not proof
that later package transactions, downloads, or compatibility probes will succeed.

Real runs share a lock at `~/.local/state/dotfiles/host.lock`. Interrupted/truncated
archive downloads retry up to three times. Verified candidates are installed
atomically at their destination, with previous binaries and SHA256/version records
retained under `~/.local/state/dotfiles/tools/TOOL/backups/`. `installed.json`
records the selected release and `previous_binary`. Failed pre-install checks
leave the installed file untouched; a failed installation-record write restores
the previous binary (or removes a failed first installation).

For manual recovery, stop running helpers, inspect the tool's backup `record.json`,
verify its SHA256, copy the retained binary to a temporary file **beside the
destination**, verify its version, then rename it over the destination. Do not
copy directly over a running executable. Recheck `--version` and review the
manifest/pin before updating again. Backups are not automatically pruned. Reverting
dotfiles does not reverse native package upgrades or restore application data.

| Exit | Meaning |
| --- | --- |
| `0` | Completed, or dry-run plan completed. |
| `1` | Operation failed or the user-tool inventory is incomplete. |
| `2` | Invalid configuration, OS/profile mismatch, or missing/denied policy. |
| `75` | Required native tools are not active; reboot and rerun setup. |
| `130` | Interrupted; inspect completed package actions before retrying. |

## Neovim workflows

The development image supplies `par` and ripgrep, and container activation
requires both. Paragraph-formatting mappings refuse to change text if `par` is
missing. `<leader>r` prompts for a ripgrep pattern; `:Rg PATTERN` searches
directly and opens the built-in quickfix list without an undeclared plugin.
See the deployed [keybindings](dot_config/nvim/KEYBINDINGS.md) for navigation.
These fixes have been tested with real Neovim, ripgrep and Fedora's `par` in a
disposable container. They have not been applied to the running devbox.

## Fish functions

The managed set is `fish_prompt`, `clc`, `clippy`, `l`, `la`, `ll`, `lla`, and
`rm`. The `l`, `la`, `ll`, and `lla` functions wrap the corresponding `ls`
options.

`rm` runs `command rm -i $argv`, prompting before deletion without recursing into
the fish function. This is not Trash or a backup, and an explicit `-f` can
override the prompt. The old `g`, `v`, and legacy SSH-agent helpers are not
deployed. Removing them from this source does not silently delete existing live
files; original-host retirement remains an explicit migration step.

## Public-repository privacy

This repository is public. Keep authentication state, private keys, recovery backups, and local
chezmoi data out of this repository. A clean credential scan reduces risk but
does not prove that arbitrary future additions are safe to publish.

Git name/email are prompted at initialization and stored only in the local
`~/.config/chezmoi/chezmoi.toml` (mode `0600`). The source uses templates rather
than embedding the original personal identity. The obsolete host OpenCode PATH
entry is omitted rather than templated or deployed to other machines.
Local Git identity fields must be single-line values valid in Git configuration.

Source-repository commits use the public GitHub no-reply identity, separate from
the identity deployed to `~/.gitconfig`. On each new source checkout, configure:

```sh
git -C ~/.local/share/chezmoi config user.name fraterenz
git -C ~/.local/share/chezmoi config user.email 35176032+fraterenz@users.noreply.github.com
```

Check commit metadata before pushing. Enable GitHub's email privacy and
command-line push protection if desired. Do not publish unapproved workplace
configuration, private endpoints, or secrets, even when templating other data.

Run the local safety/privacy checks before publishing:

```sh
python3 -m unittest discover -s ~/.local/share/chezmoi/tests -v
```

The commit-email check is skipped until a commit exists; rerun it before pushing.
These checks do not replace reviewing the staged changes.

## Original-host baseline review

Chezmoi is installed as a checksum-verified upstream binary at
`~/.local/bin/chezmoi`, not as a layered package. No reboot is required to use
chezmoi. The downloaded-tool updater will own this executable; do not add its
binary contents to the source. Operational migration/recovery notes stay local.

Review the baseline before applying:

```sh
~/.local/bin/chezmoi diff
~/.local/bin/chezmoi apply --dry-run --verbose
```

Review inert container inputs under `~/.config/dotfiles/devbox/`, the selected
profile, fish function cleanup, and removal of the old host OpenCode PATH entry.
These source changes do not uninstall the existing host executable or remove
its session data. The active Quadlet at
`~/.config/containers/systemd/dev.container` must remain unchanged.
After checking the diff:

```sh
~/.local/bin/chezmoi apply
```

Verify fish, Git, Neovim in its existing environment, Ghostty, and local Sway
before committing and pushing the baseline. New OpenCode installation/update
belongs in the container. Existing host OpenCode files are audit-only: do not
uninstall the host executable or delete its configuration, credentials, or data.

## Devbox lifecycle

- Chezmoi owns the desired build context and staged Quadlet under
  `~/.config/dotfiles/devbox/`, including `config.json` and
  `build/Containerfile.dev`.
- The active `~/.config/containers/systemd/dev.container` is intentionally not
  managed by chezmoi. Only the explicit `setup-containers` command promotes it.
- This baseline has no setup hooks. Applying files does not build or restart
  the devbox.
- Recovery records and retained immutable image identities stay privately under
  `~/.local/state/dotfiles/devbox/`, outside chezmoi and Git.

After reviewing/applying the helper and desired files and satisfying rootless
Podman prerequisites, run as the normal user:

```sh
~/.local/bin/setup-containers --dry-run
# On an existing devbox, first review backups and use --adopt explicitly.
~/.local/bin/setup-containers --adopt
```

| Goal | Command |
| --- | --- |
| First setup on an empty machine, or reconcile desired changes | `setup-containers` |
| Refresh upstream image inputs, respecting explicit pins | `setup-containers --update` |
| Restore the previous verified image and active unit, without builds/pulls | `setup-containers --rollback` |

All modes support `--dry-run`. Normal setup reuses a matching image, uses cached
builds when needed, and refreshes replaced bind-mounted configuration files
without rebuilding the image. Update uses `--pull=always --no-cache`. Candidates
are pinned to immutable local image IDs with `Pull=never`; failed activation
recovers the previous runtime and returns nonzero. Rollback does **not** restore
host dotfiles, persistent project data, credentials, or tool state.

See [container prerequisites, input/version policy, and recovery](docs/containers.md)
before first adoption. Backups and real disposable-host build/activation tests
remain prerequisites for replacing the original running devbox.

The reviewed import allowlist is in `docs/baseline-files.txt`; private recovery
notes and backups are deliberately not published.
