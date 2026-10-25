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

Install Git, Python 3.9+, and a checksum-verified standalone chezmoi binary at
`~/.local/bin/chezmoi` first. Do not layer chezmoi. Initialize, choose a profile,
then review before applying:

```sh
~/.local/bin/chezmoi init https://github.com/fraterenz/dotfiles.git
~/.local/bin/chezmoi diff
~/.local/bin/chezmoi apply --dry-run --verbose
~/.local/bin/chezmoi apply
```

`chezmoi apply` deploys files only: package installation and container activation
are explicit. Git identity/profile data stays in the private local
`~/.config/chezmoi/chezmoi.toml` (mode `0600`).

## Everyday commands

Helpers live in `~/.local/bin`. Run them as your normal user, not with `sudo`;
all support `--dry-run`. Use `--help` for options.

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
python3 -m unittest discover -s tests -v
```

CI runs tests on Rocky Linux 9/Python 3.9 and Fedora 44 userspace, including editor
tests on Fedora. There are no release or deployment jobs. Booted Atomic/work-host
setup, storage/SELinux, live activation and rollback still need disposable-host/VM
validation; this is not yet a validated clean-machine bootstrap.
