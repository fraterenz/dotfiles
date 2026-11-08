# dotfiles

Chezmoi-managed dotfiles for Fedora Sway Atomic and Rocky Linux 9.

## Profiles

| Profile | Host | Sway |
| --- | --- | --- |
| `local` | Fedora Sway Atomic | Managed, including hardware settings. |
| `work` | Rocky Linux 9 | Unmanaged; existing files are left alone. |

Local Sway uses an existing `~/Pictures/background.jpg` or a solid background.
The wallpaper is not distributed.

## Setup

Install Git and a checksum-verified standalone chezmoi binary at
`~/.local/bin/chezmoi` first. Do not layer chezmoi. Python does not need to be
installed manually. Initialize, choose a profile, then review before applying:

```sh
~/.local/bin/chezmoi init https://github.com/fraterenz/dotfiles.git
~/.local/bin/chezmoi diff
~/.local/bin/chezmoi apply --dry-run --verbose
~/.local/bin/chezmoi apply
```

`chezmoi apply` deploys files only: package installation and container activation
are explicit. Git identity/profile data stays in the private local
`~/.config/chezmoi/chezmoi.toml` (mode `0600`).

For both profiles, chezmoi also installs only `~/.local/bin/uv` from a versioned,
checksum-verified upstream archive declared in
[`.chezmoiexternal.toml.tmpl`](.chezmoiexternal.toml.tmpl). It does not install
`uvx`, run setup helpers, or install Python during apply. Chezmoi may fetch/cache
the external archive while preparing a diff or dry run.

## Everyday commands

Helpers live in `~/.local/bin`. Run them as your normal user, not with `sudo`;
all support `--dry-run`. Use `--help` for options.

The launchers use the absolute chezmoi-managed uv path and declare Python
**3.12 or newer**, with no third-party Python dependencies. uv finds a compatible
interpreter or downloads one when needed and maintains per-script cached
environments. System Python is not replaced. `--no-config` disables discovered
uv configuration and Python-version files; inherited environment variables can
still affect uv, including intentional site settings.

A helper's `--dry-run` suppresses its operational changes, not uv's interpreter
downloads or runtime/cache preparation before the helper starts. On work hosts,
obtain approval for uv and Python downloads **before** using chezmoi externals or
launching helpers: the Python-level host-policy checks happen after uv startup.

Chezmoi owns uv updates: review a new version and architecture checksums in the
external definition, then apply `~/.local/bin/uv`. `update-host` leaves uv alone.
Managed-Python patch upgrades are separate, explicit maintenance with
`~/.local/bin/uv python upgrade VERSION` for the intended installed minor version;
do not blindly upgrade every managed interpreter. These interpreters can also be
shared with project environments. System-Python updates remain OS-managed.

| Command | Purpose |
| --- | --- |
| `setup-host` | Install missing packages, declared Flatpaks, then downloaded tools. |
| `update-host` | Update native packages, all configured Flatpak installations, then user tools. |
| `setup-containers` | Build/reuse an image and reconcile the devbox. |
| `setup-containers --update` | Refresh image inputs, respecting pins. |
| `setup-containers --rollback` | Restore the previous runtime without building or pulling. |

Edit the [software manifest](dot_config/dotfiles/software.json.tmpl) through
chezmoi, review/apply it, then run `setup-host`. Downloaded tools are verified
before replacement; backups stay under `~/.local/state/dotfiles/tools/`.
Unknown local-bin entries are reported and left untouched.

## Host policy

- **Local:** uses rpm-ostree and the declared Flatpak scopes. Configure Terra for
  Ghostty and the required Flathub remotes beforehand; helpers never add them.
  If setup exits `75`, reboot manually and rerun it.
- **Work:** host operations, including dry runs, require site approval recorded
  in a private local policy. The default work Flatpak list is empty.

After obtaining approval, prepare the policy:

```sh
cp ~/.local/share/chezmoi/docs/host-policy.example.json ~/.config/dotfiles/host-policy.json
chmod 600 ~/.config/dotfiles/host-policy.json
```

Use approved repositories/Flatpak scopes and set `approved` to `true` only with
site permission. Never commit the real policy. Fish is package-owned, chezmoi is
standalone, and OpenCode installation/update belongs in the image; existing host
OpenCode files are not removed.

## Devbox

Chezmoi stages inputs under `~/.config/dotfiles/devbox/`. Only `setup-containers`
owns the active `~/.config/containers/systemd/dev.container`.

Back up persistent data before replacing an existing devbox. First takeover
requires `--adopt`; unchanged images are reused, and replaced configuration binds
can be refreshed without rebuilding. Rollback restores the runtime, **not** host
dotfiles or persistent project/tool data.

See [container prerequisites and recovery](docs/containers.md) and the
[Neovim keybindings](dot_config/nvim/KEYBINDINGS.md).

## Privacy and tests

Keep SSH files, credentials, workplace policy, application state and backups
outside this public repository. SSH configuration is currently unmanaged.
Use the GitHub no-reply identity for source commits, separately from your
deployed Git identity:

```sh
git -C ~/.local/share/chezmoi config user.name fraterenz
git -C ~/.local/share/chezmoi config user.email 35176032+fraterenz@users.noreply.github.com
```

From the source directory:

```sh
~/.local/bin/uv run --no-config --no-project --python '>=3.12' \
  python -m unittest discover -s tests -v
```

CI installs uv from the same chezmoi external definition into a disposable
directory and runs tests on Rocky Linux 9/Python 3.12 and Fedora 44/Python 3.14,
including editor tests on Fedora. There are no release or deployment jobs.
Booted Atomic/work-host setup, storage/SELinux, live activation and rollback still
need disposable-host/VM validation; this is not yet a validated clean-machine
bootstrap.
