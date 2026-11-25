# Podman devbox

Run these commands from a **host terminal** as your normal user, not with
`sudo`. Apply the [dotfiles](../README.md#shared-setup) first. You need rootless
Podman with Quadlet, user systemd, and local storage. On `work`, get site
approval for containers and downloads, including uv/Python downloads that can
happen on a dry run. Live deployment and rollback still need end-to-end testing.

## Set up `dev`

Chezmoi stages the build and config in `~/.config/dotfiles/devbox/`. It does
not start or restart `dev`. Preview what the helper will do:

```sh
setup-containers --dry-run
```

**Before replacing an existing `dev`,** save your work, back up its workspace
(normally `~/dev`), and copy out any files stored only in the container.
The workspace is mounted at `/home/dev`, but files in the container's writable
layer won't survive replacement.

If the preview looks good, run `setup-containers`. If it reports first-time
adoption, leave the existing `dev.service` running and use
`setup-containers --adopt` instead. You only need `--adopt` the first time the
helper takes over an existing devbox.

Once setup succeeds, open a shell in the running container:

```sh
podman exec -it dev fish -l
```

## Update `dev`

To refresh the base image and unpinned tools, run:

```sh
setup-containers --update
```

Preview it with `setup-containers --update --dry-run`. `--update` pulls the
base and rebuilds without cache. It doesn't change version pins. For a pin
change, edit the Containerfile, apply it with chezmoi, and run plain
`setup-containers`, which uses the build cache.

### How Podman, Quadlet, and systemd work together

- **Podman** builds the image from `Containerfile.dev` and runs the `dev` container.
- **Quadlet** turns `dev.container` (the image and mounts) into `dev.service`.
- **User systemd** starts and stops `dev.service`, which calls Podman.

Podman builds the image while your existing container keeps running. Once the
image passes its checks, `setup-containers` stops `dev.service`, points the
Quadlet at the new image, and reloads user systemd. Quadlet regenerates the
service, and the helper starts and verifies it. Your `dev` shells close at that
final switch. No manual restart or Quadlet edit is needed.

### Upgrade OpenCode in `dev`

OpenCode is installed in the image, not managed by `update-host`.

1. **Change the pin.** Open the Containerfile source and set `ARG OPENCODE_VERSION`
   to the version you want. If moving from 1.x to 2.x, see the
   [installer change below](#example-upgrade-opencode-to-2x).

   ```sh
   chezmoi edit ~/.config/dotfiles/devbox/build/Containerfile.dev
   ```

2. **Stage and preview.** For an already managed devbox, review and apply just
   the Containerfile. For a legacy devbox, use `~/.config/dotfiles/devbox` in
   both chezmoi commands to stage the current build and Quadlet together.

   ```sh
   chezmoi diff ~/.config/dotfiles/devbox/build/Containerfile.dev
   chezmoi apply ~/.config/dotfiles/devbox/build/Containerfile.dev
   setup-containers --dry-run
   ```

3. **Deploy and verify.** Save your work and back up your data, then run
   `setup-containers` without `--update`. If the preview reports first-time
   adoption, leave `dev.service` running and use `setup-containers --adopt`
   instead. Check that the service is `active` and OpenCode reports the version
   you pinned:

   ```sh
   systemctl --user is-active dev.service
   podman exec dev /usr/local/bin/opencode --version
   ```

#### Example: Upgrade OpenCode to 2.x

If your Containerfile still uses 1.x, set `ARG OPENCODE_VERSION=2.4.1` and
change the installer URL from `https://opencode.ai/install` to
`https://opencode.ai/v2/install`. If the checkout already has those changes,
start at step 2. See the [OpenCode migration guide](https://opencode.ai/docs/migrate-v1)
before first launch.

### Renovate pin updates

The [Renovate GitHub App](https://github.com/apps/renovate) can open PRs for
annotated pins. It doesn't switch installers or deploy changes. After merging a
pin PR, pull it into your chezmoi checkout, then stage, preview, and deploy as above.

## Failure recovery and rollback

A failed build leaves the running container alone. If the switch fails, the
helper tries to restore the previous deployment. To undo a successful update,
preview with `setup-containers --rollback --dry-run`, then run:

```sh
setup-containers --rollback
```

Rollback restores the previous image and Quadlet, not your workspace files or
the version pin. Review the staged change before deploying again.
