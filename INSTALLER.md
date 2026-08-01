# Howdy GUI Installer

A cross-distro graphical installer for [Howdy](https://github.com/boltgolt/howdy),
Windows-Hello-style face authentication for Linux. It handles the parts that
normally make Howdy fiddly: build dependencies, the dlib models, picking the
right camera, and wiring up PAM *the way your distro expects* — without ever
removing your password as a fallback.

---

## Install

Run this in a terminal. It works in **bash, zsh, and fish**:

```bash
curl -fsSL https://raw.githubusercontent.com/emerytech/howdy/installer/install.sh -o /tmp/howdy-install.sh && bash /tmp/howdy-install.sh
```

A wizard window opens. Follow the four pages (Welcome → Camera → Install →
Enroll & test). Expect 2–3 system password dialogs: the wizard itself runs
unprivileged and only elevates for the steps that need root.

> **Why not `bash <(curl ...)`?** That process-substitution form is bash/zsh-only.
> In fish it fails immediately with `Invalid redirection target`, before anything
> is downloaded. The `curl -o` form above avoids the problem entirely.

No graphical session? Add `--cli` for the same flow in the terminal:

```bash
bash /tmp/howdy-install.sh --cli
```

---

## Requirements

- A webcam. An **infrared (IR)** camera is strongly recommended — most laptops
  that shipped with Windows Hello have one. IR works in a dark room and is much
  harder to fool with a photo. The installer detects IR sensors and preselects
  them.
- A PAM-based distro from one of the four families below.
- ~1 GB free disk space, plus 5–15 minutes if `dlib` has to be compiled.

---

## Per-distro details

The installer detects your family from `/etc/os-release` (`ID` and `ID_LIKE`),
so derivatives are handled automatically — there is no distro-specific command
to look up. The details below are what it does under the hood.

### Arch family
*Arch Linux, CachyOS, Manjaro, EndeavourOS, Garuda*

- **Packages** (`pacman -S --needed`): `git meson ninja cmake pkgconf gcc pam
  libinih libevdev python python-pip python-setuptools python-opencv
  python-numpy v4l-utils ffmpeg`
- **PAM**: inserts `auth sufficient pam_howdy.so` at the top of
  `/etc/pam.d/system-auth`, backing the original up to `system-auth.pre-howdy`.
  That one file covers sudo, the KDE/GNOME lock screen, the login greeter, and
  polkit dialogs.
- **dlib**: not in the official repos, so the installer builds it with pip if it
  isn't already importable. To skip that build, install the AUR package first
  and the installer will detect it: `paru -S python-dlib`
- **Status**: well tested.

### Debian / Ubuntu family
*Debian, Ubuntu, Linux Mint, Pop!_OS, elementary, Zorin, Raspberry Pi OS*

- **Packages** (`apt-get install`): `git meson ninja-build cmake pkg-config g++
  libpam0g-dev libinih-dev libevdev-dev python3-dev python3-pip
  python3-setuptools python3-opencv python3-numpy v4l-utils ffmpeg`
- **PAM**: uses the native **`pam-auth-update`** mechanism via a profile at
  `/usr/share/pam-configs/howdy`. This is the correct approach on Debian —
  hand-edits to `common-auth` get overwritten by package updates. If
  `pam-auth-update` is unavailable, it falls back to a direct insert into
  `/etc/pam.d/common-auth` with a backup.
- **dlib**: built via pip; allow 5–15 minutes. This is the slowest step here.
- **Ubuntu note**: `python3-opencv` lives in *universe* — enable it first if
  needed with `sudo add-apt-repository universe`.
- **Status**: should work; less tested than Arch.

### Fedora family
*Fedora, RHEL, CentOS Stream, Rocky, AlmaLinux, Nobara*

- **Packages** (`dnf install`): `git meson ninja-build cmake pkgconf-pkg-config
  gcc-c++ pam-devel inih-devel libevdev-devel python3-devel python3-pip
  python3-setuptools python3-opencv python3-numpy v4l-utils`
- **PAM**: uses **`authselect`**, the only supported way to change PAM on
  Fedora. The installer creates a custom profile based on your current one
  (`authselect create-profile howdy -b <current>`), adds the Howdy line to
  `system-auth` and `password-auth`, then selects and applies it. Editing
  `/etc/pam.d/system-auth` directly would simply be reverted by authselect.
- **dlib**: available as a package — install it first to skip the pip build:
  `sudo dnf install python3-dlib`
- **Status**: best effort — please report issues.

### openSUSE family
*Tumbleweed, Leap, SLES*

- **Packages** (`zypper install`): `git meson ninja cmake pkg-config gcc-c++
  pam-devel libinih-devel libevdev-devel python3-devel python3-pip
  python3-setuptools python3-opencv python3-numpy v4l-utils ffmpeg`
- **PAM**: inserts the Howdy line into `/etc/pam.d/common-auth` with a backup.
  Note that `pam-config` may rewrite this file on major updates; re-run the
  installer if that happens.
- **Status**: best effort — please report issues.

---

## What the wizard actually does

1. **Distro packages** — installs the dependencies listed above.
2. **Build** — `meson setup` + `meson compile`, run as your normal user.
3. **Install** — `meson install`; ensures `dlib` is importable; downloads the
   three dlib model files to `/usr/share/dlib-data`, each verified against a
   known MD5; writes your camera choice to `/etc/howdy/config.ini`; installs a
   systemd drop-in so the polkit agent can reach the camera (needed on
   polkit ≥ 126, which otherwise sandboxes `/dev/video*` away).
4. **PAM** — enables face auth using the per-distro mechanism above.
5. **Enroll** — captures your face, adapting automatically on failure: raising
   `dark_threshold` when frames are too dark (normal for IR cameras, whose
   backgrounds are mostly black), or switching the recorder to ffmpeg when
   frames come back empty.
6. **Test** — a live `pkexec` prompt to confirm it works.

## Command-line flags

| Flag | Effect |
| --- | --- |
| *(none)* | GUI wizard |
| `--cli` | terminal wizard |
| `--dry-run` | print every privileged command, change nothing |
| `--device /dev/videoN` | skip camera auto-detection |
| `-y`, `--yes` | no prompts (for automation) |
| `--uninstall` | restore PAM backups and remove installed files |

Preview everything before committing on an unfamiliar machine:

```bash
bash /tmp/howdy-install.sh --cli --dry-run
```

## Uninstall

```bash
bash /tmp/howdy-install.sh --cli --uninstall
```

Restores the PAM backups (or removes the pam-auth-update / authselect profile),
deletes installed files via the recorded manifest, and removes the polkit
drop-in. `/etc/howdy` — your config and face models — is left in place
deliberately; delete it manually for a clean slate.

## Managing Howdy afterwards

```bash
sudo howdy list
```

```bash
sudo howdy add
```

A second or third model (glasses on/off, different lighting) noticeably improves
recognition. Also useful: `howdy test`, `howdy remove <id>`, `howdy config`,
`howdy disable 1`.

---

## Troubleshooting

**"All frames were too dark"** — normal for IR cameras. The installer raises
`dark_threshold` to 90 automatically; raise it further in `/etc/howdy/config.ini`
if you still hit it.

**"Camera saw only black frames"** — the IR emitter isn't firing. Try
`recording_plugin = ffmpeg` in the config, or
[linux-enable-ir-emitter](https://github.com/EmixamPP/linux-enable-ir-emitter)
for cameras whose emitter needs an explicit trigger.

**Nothing happens over SSH** — intentional. Howdy refuses remote sessions
(`abort_if_ssh = true`) and falls through to your password.

**Face auth doesn't fire in graphical password dialogs** — usually the
polkit ≥ 126 sandbox blocking camera access. The installer's systemd drop-in
fixes it; run `sudo systemctl daemon-reload` and log out/in if it was just
installed.

**Login works, then the keyring/wallet prompts** — inherent to any passwordless
login: the wallet needs your real password to decrypt. Type it once per session,
or set the wallet to a blank password.

**Screen lock does nothing at all** — not a Howdy problem. Some gaming-oriented
images (Bazzite, SteamOS-likes, CachyOS "deckify") ship a KDE kiosk restriction,
`action/lock_screen=false` under `[KDE Action Restrictions]` in
`/etc/xdg/kdeglobals`, which disables locking system-wide. Set it to `true` and
log out and back in.

**Locked out?** You can't be, by design — the PAM line is `sufficient`, so a
camera failure falls through to the password prompt. If you hand-edited PAM and
broke it, boot to a TTY or recovery shell and restore the `.pre-howdy` backup.

## Security

Face authentication is a **convenience**, not a hardened security boundary —
upstream says the same. An RGB webcam can be defeated with a photo; IR is
substantially better but not bulletproof. Don't rely on it where an attacker
with physical access matters, and keep full-disk encryption as your real
protection.

## Support matrix

| Family | Status |
| --- | --- |
| Arch / CachyOS / Manjaro / EndeavourOS | well tested |
| Debian / Ubuntu / Mint / Pop!_OS | should work |
| Fedora / Nobara / RHEL clones | best effort — report issues |
| openSUSE Tumbleweed / Leap | best effort — report issues |

---

Upstream Howdy is by [@boltgolt](https://github.com/boltgolt/howdy) and
contributors (MIT). This fork is purely additive — `install.sh` + `installer/` +
this file — so pulling upstream changes stays conflict-free.
