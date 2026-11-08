# dotfiles

Dotfiles and automation for Fedora Sway Atomic and Rocky Linux 9.

Chezmoi deploys configuration and bootstraps uv, which provides Python 3.12+
for the helper commands. These manage host software and the rootless Podman
devbox when explicitly invoked; applying the dotfiles never runs them.

## Shared setup

Install Git, then follow the
[official chezmoi installation guide](https://www.chezmoi.io/install/#one-line-binary-install)
to install a standalone binary at `~/.local/bin/chezmoi`. See also the guide's
[download verification instructions](https://www.chezmoi.io/install/#verify-your-download).

Initialize the repository, choose
[`local`](#local-profile-fedora-sway-atomic) or
[`work`](#work-profile-rocky-linux-9), and review the changes before applying:

```sh
~/.local/bin/chezmoi init https://github.com/fraterenz/dotfiles.git
~/.local/bin/chezmoi diff
~/.local/bin/chezmoi apply --dry-run --verbose
~/.local/bin/chezmoi apply
```

Git identity and profile data stay in the private local
`~/.config/chezmoi/chezmoi.toml` (mode `0600`). Host package installation and
container activation are separate operations, described below.

## Local profile: Fedora Sway Atomic

The `local` profile manages host software in three layers:

| Layer | What belongs here | How it is managed |
| --- | --- | --- |
| **OS and layered packages** | The Fedora deployment and required host RPMs. | `rpm-ostree`; a new deployment is activated by rebooting. |
| **Flatpak** | Desktop applications from Flathub. | `flatpak`, using the configured user and system installations. |
| **User binaries** | Standalone CLI tools and helper launchers in `~/.local/bin`. | Host helpers manage downloaded tools; chezmoi manages uv and the launchers. |

Configure Terra for Ghostty and the required Flathub remotes before running
`setup-host`; helpers never add repositories or remotes. If setup exits `75`,
reboot manually and rerun it.

This profile also manages Sway and its hardware-specific configuration.
Development toolchains belong in the [devbox](#devbox), not the host OS layer.

## Work profile: Rocky Linux 9

The `work` profile leaves OS updates to the cluster administrator. Existing Sway
and desktop configuration are left untouched.

| Software category | How it is managed |
| --- | --- |
| **System packages** | Updates are cluster-managed. `setup-host` can install missing packages using explicitly approved DNF repositories and options. |
| **Flatpak** | Excluded from `update-host`; no installations or applications are configured by default. |
| **User binaries** | Standalone tools in `~/.local/bin`, with uv and helper launchers managed by chezmoi. |

Host operations, including dry runs, require site approval recorded in a private
local policy.

After obtaining approval, copy the example. For `setup-host`, fill in the
approved repositories and Flatpak scopes. `update-host` updates only user tools
and requires recorded approval and secure policy ownership/permissions, not
DNF/Flatpak settings. Set `approved` to `true` only with site permission:

```sh
cp ~/.local/share/chezmoi/docs/host-policy.example.json ~/.config/dotfiles/host-policy.json
chmod 600 ~/.config/dotfiles/host-policy.json
```

Never commit the real policy.

## Host commands

After completing the prerequisites for your profile, use the shared helpers in
`~/.local/bin`. Run them as your normal user, not with `sudo`.

| Command | Purpose |
| --- | --- |
| `setup-host` | Install missing software declared for the selected profile. |
| `update-host` | Update all permitted phases, or select them using additive flags. |

Review `setup-host --dry-run` before the first setup. Both commands support
`--help` for options.

```sh
update-host --flatpak --user-tools  # Local: both concurrently, no OS update
update-host --rpm-ostree            # Local: stage an OS update only
update-host --user-tools            # Either profile: registered user tools only
update-host --all --dry-run         # Review every update permitted by the profile
```

With no selectors, `update-host` updates all permitted phases. On local hosts,
Flatpak and user tools run concurrently, then rpm-ostree runs last. On work
hosts, only user tools are available. Expected failures do not block independent
updates; the final report exits nonzero if any failed. See
[update behaviour and Python API](docs/updates.md) for details.

To change the software selection, edit the
[software manifest](dot_config/dotfiles/software.json.tmpl) through chezmoi,
review and apply it, then run `setup-host` to install newly declared software.

Downloaded tools are verified before replacement; backups stay under
`~/.local/state/dotfiles/tools/`. Unregistered local-bin entries are reported and
left untouched. Fish remains package-owned on both profiles.

## Devbox

Both profiles use a rootless Podman devbox for development tools. OpenCode
installation and updates belong in its image; existing host OpenCode files are
left untouched.

Chezmoi stages build and runtime inputs under `~/.config/dotfiles/devbox/`.
Only `setup-containers` manages the active
`~/.config/containers/systemd/dev.container`.

| Command | Purpose |
| --- | --- |
| `setup-containers` | Build or reuse an image and reconcile the devbox. |
| `setup-containers --update` | Pull a fresh base and rebuild without cache, preserving explicit pins. |
| `setup-containers --rollback` | Restore the previous runtime without building or pulling. |

Back up persistent data before replacing an existing devbox. First takeover
requires `--adopt`; unchanged images are reused, and configuration binds can be
refreshed without rebuilding. Rollback restores the runtime, **not** host
dotfiles or persistent project/tool data. Use `--dry-run` to review operations.

See [container prerequisites, deployment, and recovery](docs/containers.md)
before activating the devbox. Editor shortcuts are documented in the
[Neovim keybindings](dot_config/nvim/KEYBINDINGS.md).

## Runtime maintenance

Chezmoi owns uv updates: review a new version and architecture checksums in the
[external definition](.chezmoiexternal.toml.tmpl), then apply `~/.local/bin/uv`.
`update-host` leaves uv alone.

Managed-Python patch upgrades are explicit maintenance with
`~/.local/bin/uv python upgrade VERSION` for the intended installed minor version.
These interpreters can also be shared with projects, so avoid blindly upgrading
every managed interpreter. System-Python updates remain OS-managed.

The launchers use `--no-config` to ignore discovered uv configuration and
Python-version files. Inherited environment variables still apply, including
intentional site settings.

uv can download a compatible Python and prepare cached environments before the
helper starts. `--dry-run` suppresses helper operations, not this runtime
preparation. Obtain work-site approval for uv/Python downloads before launching
helpers; Python-level policy checks happen afterward.

## Development

Run the tests from the source directory:

```sh
~/.local/bin/uv run --no-config --no-project --python '>=3.12' \
  python -m unittest discover -s tests -v
```

CI installs uv from the same chezmoi definition into a disposable directory.
It tests Rocky Linux 9/Python 3.12 and Fedora 44/Python 3.14, including editor
tests on Fedora. There are no release or deployment jobs.

Booted Atomic/work-host setup, storage/SELinux, live activation and rollback still
need disposable-host/VM validation; this is not yet a validated clean-machine
bootstrap.

### Privacy and Git identity

Keep SSH files, credentials, workplace policy, application state and backups
outside this public repository. SSH configuration is currently unmanaged.

Use the GitHub no-reply identity for source commits, separately from your
deployed Git identity:

```sh
git -C ~/.local/share/chezmoi config user.name fraterenz
git -C ~/.local/share/chezmoi config user.email 35176032+fraterenz@users.noreply.github.com
```
