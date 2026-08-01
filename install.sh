#!/usr/bin/env bash
# Howdy GUI installer bootstrap.
# Usage:  curl -fsSL https://raw.githubusercontent.com/emerytech/howdy/installer/install.sh -o /tmp/howdy-install.sh && bash /tmp/howdy-install.sh
# Fetches this repo and launches the graphical installer (CLI fallback: --cli).
#
# Environment overrides:
#   HOWDY_INSTALLER_REPO    git URL of the fork    (default below)
#   HOWDY_INSTALLER_BRANCH  branch to use          (default: installer)
set -euo pipefail

REPO="${HOWDY_INSTALLER_REPO:-https://github.com/emerytech/howdy}"
BRANCH="${HOWDY_INSTALLER_BRANCH:-installer}"
CACHE="${XDG_CACHE_HOME:-$HOME/.cache}/howdy-installer"

say()  { printf '\033[1;36m[howdy-installer]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[howdy-installer]\033[0m %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- distro ----
[ -r /etc/os-release ] || die "Cannot read /etc/os-release - unsupported system."
. /etc/os-release
FAMILY=""
for id in ${ID:-} ${ID_LIKE:-}; do
    case "$id" in
        arch|archlinux|cachyos|manjaro|endeavouros) FAMILY=arch; break ;;
        debian|ubuntu|linuxmint|pop)                FAMILY=debian; break ;;
        fedora|rhel|centos|nobara)                  FAMILY=fedora; break ;;
        opensuse*|suse|sles)                        FAMILY=suse; break ;;
    esac
done
[ -n "$FAMILY" ] || die "Unsupported distro: ${ID:-unknown}. Supported families: arch, debian/ubuntu, fedora, openSUSE."
say "Detected distro: ${PRETTY_NAME:-$ID} (family: $FAMILY)"

# ------------------------------------------------- privileged run helper ----
as_root() {
    if [ "$(id -u)" = 0 ]; then "$@"
    elif [ -t 0 ] && command -v sudo >/dev/null; then sudo "$@"
    elif command -v pkexec >/dev/null; then pkexec "$@"
    else die "Need root to install packages; run from a terminal with sudo available."
    fi
}

# ------------------------------------------ minimal bootstrap packages ------
# Just enough for the GUI (python3 + GTK3 via PyGObject) and fetching the repo.
have_gui_stack() { python3 -c 'import gi; gi.require_version("Gtk","3.0"); from gi.repository import Gtk' >/dev/null 2>&1; }

NEED=()
command -v git >/dev/null || NEED+=(git)
command -v python3 >/dev/null || NEED+=(python3)
if ! have_gui_stack; then
    case "$FAMILY" in
        arch)   NEED+=(python-gobject gtk3) ;;
        debian) NEED+=(python3-gi gir1.2-gtk-3.0) ;;
        fedora) NEED+=(python3-gobject gtk3) ;;
        suse)   NEED+=(python3-gobject typelib-1_0-Gtk-3_0) ;;
    esac
fi

if [ "${#NEED[@]}" -gt 0 ]; then
    say "Installing bootstrap packages: ${NEED[*]}"
    case "$FAMILY" in
        arch)   as_root pacman -S --needed --noconfirm "${NEED[@]}" ;;
        debian) as_root apt-get update -qq; as_root apt-get install -y "${NEED[@]}" ;;
        fedora) as_root dnf install -y "${NEED[@]}" ;;
        suse)   as_root zypper --non-interactive install "${NEED[@]}" ;;
    esac
fi

# ----------------------------------------------------------- fetch repo -----
SRC=""
# Already inside a checkout? (script sits next to installer/)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-.}")" 2>/dev/null && pwd || true)"
if [ -n "$SCRIPT_DIR" ] && [ -f "$SCRIPT_DIR/installer/howdy_installer.py" ]; then
    SRC="$SCRIPT_DIR"
    say "Running from existing checkout: $SRC"
else
    mkdir -p "$CACHE"
    SRC="$CACHE/src"
    if [ -d "$SRC/.git" ]; then
        say "Updating cached checkout in $SRC"
        git -C "$SRC" fetch --depth 1 origin "$BRANCH" && git -C "$SRC" reset --hard FETCH_HEAD
    else
        rm -rf "$SRC"
        say "Cloning $REPO ($BRANCH)"
        git clone --depth 1 -b "$BRANCH" "$REPO" "$SRC"
    fi
fi

# --------------------------------------------------------------- launch -----
say "Launching installer..."
exec python3 "$SRC/installer/howdy_installer.py" --source-dir "$SRC" "$@"
