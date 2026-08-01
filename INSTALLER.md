# Howdy GUI Installer (this fork)

A cross-distro graphical installer for [Howdy](https://github.com/boltgolt/howdy)
face authentication. One command on a fresh machine:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/emerytech/howdy/installer/install.sh)
```

It bootstraps GTK if needed, clones this fork, and opens a wizard that:

1. Detects your distro family — **Arch** (pacman), **Debian/Ubuntu** (apt),
   **Fedora** (dnf), **openSUSE** (zypper)
2. Installs build dependencies with your native package manager
3. Builds Howdy from this source tree (meson/ninja) as your user
4. Installs it, fetches the dlib face models (checksum-verified), and enables
   PAM using the *right mechanism per distro*:
   - Arch: line inserted at the top of `/etc/pam.d/system-auth` (backup kept at
     `system-auth.pre-howdy`)
   - Debian/Ubuntu: `pam-auth-update` profile
   - Fedora: `authselect` custom profile
   - openSUSE: `/etc/pam.d/common-auth` insert (backup kept)
5. Picks your camera — IR sensors are auto-detected and preferred — and tunes
   `dark_threshold` for IR strobe cameras
6. Enrolls your face (with automatic retry that adapts config on failure) and
   runs a live authentication test

The wizard itself never runs as root: privileged phases are short generated
shell scripts executed via `pkexec`, so you'll see 2–3 system password dialogs.

## CLI / automation

```bash
./install.sh --cli                 # terminal wizard
./install.sh --cli --dry-run      # print every privileged step, change nothing
./install.sh --cli -y --device /dev/video2   # unattended
./install.sh --cli --uninstall    # restore PAM backups + remove installed files
```

## Safety notes

- The PAM line is `sufficient`: if the camera fails or doesn't match, you get
  the normal password prompt. Your password always works.
- Pre-edit backups: `*.pre-howdy` next to each modified PAM file.
- Face auth on a regular RGB webcam can be spoofed with a photo; prefer the IR
  camera if your laptop has one (the installer flags it). Treat Howdy as a
  convenience, not a hardened security boundary.

## Support matrix

| Family | Status |
| --- | --- |
| Arch / CachyOS / Manjaro / EndeavourOS | well tested |
| Debian / Ubuntu / Mint / Pop!_OS | should work |
| Fedora / Nobara | best effort — report issues |
| openSUSE Tumbleweed / Leap | best effort — report issues |

Upstream Howdy is by @boltgolt and contributors (MIT). The installer in this
fork is additive: `install.sh` + `installer/` only, so syncing with upstream
stays trivial.
