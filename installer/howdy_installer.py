#!/usr/bin/env python3
# Howdy cross-distro installer (GTK GUI + CLI).
#
# Installs Howdy from this source checkout on Arch, Debian/Ubuntu, Fedora and
# openSUSE family distros: distro packages -> meson build -> dlib -> face
# models -> PAM integration (with password fallback) -> camera config ->
# enrollment -> live test.
#
# The GUI runs unprivileged; root-only phases are batched into small shell
# scripts executed through pkexec (or sudo in CLI mode) so the graphical
# session never runs as root and every privileged step is auditable.
#
# SPDX-License-Identifier: MIT

import argparse
import glob
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading

# --------------------------------------------------------------------------
# Distro knowledge
# --------------------------------------------------------------------------

FAMILIES = {
    "arch": {
        "ids": {"arch", "archlinux", "cachyos", "manjaro", "endeavouros", "garuda"},
        "install": "pacman -S --needed --noconfirm",
        "refresh": None,
        "packages": [
            "git", "meson", "ninja", "cmake", "pkgconf", "gcc",
            "pam", "libinih", "libevdev",
            "python", "python-pip", "python-setuptools",
            "python-opencv", "python-numpy",
            "v4l-utils", "ffmpeg",
        ],
        "pam_strategy": "insert",
        "pam_file": "/etc/pam.d/system-auth",
        "tested": "well tested",
    },
    "debian": {
        "ids": {"debian", "ubuntu", "linuxmint", "pop", "elementary", "zorin", "kali", "raspbian"},
        "install": "apt-get install -y",
        "refresh": "apt-get update",
        "packages": [
            "git", "meson", "ninja-build", "cmake", "pkg-config", "g++",
            "libpam0g-dev", "libinih-dev", "libevdev-dev",
            "python3-dev", "python3-pip", "python3-setuptools",
            "python3-opencv", "python3-numpy",
            "v4l-utils", "ffmpeg",
        ],
        "pam_strategy": "pam-auth-update",
        "pam_file": "/etc/pam.d/common-auth",
        "tested": "should work (less tested than Arch)",
    },
    "fedora": {
        "ids": {"fedora", "rhel", "centos", "rocky", "almalinux", "nobara"},
        "install": "dnf install -y",
        "refresh": None,
        "packages": [
            "git", "meson", "ninja-build", "cmake", "pkgconf-pkg-config", "gcc-c++",
            "pam-devel", "inih-devel", "libevdev-devel",
            "python3-devel", "python3-pip", "python3-setuptools",
            "python3-opencv", "python3-numpy",
            "v4l-utils",
        ],
        "pam_strategy": "authselect",
        "pam_file": "/etc/pam.d/system-auth",
        "tested": "best effort - please report issues",
    },
    "suse": {
        "ids": {"opensuse", "opensuse-tumbleweed", "opensuse-leap", "suse", "sles"},
        "install": "zypper --non-interactive install",
        "refresh": None,
        "packages": [
            "git", "meson", "ninja", "cmake", "pkg-config", "gcc-c++",
            "pam-devel", "libinih-devel", "libevdev-devel",
            "python3-devel", "python3-pip", "python3-setuptools",
            "python3-opencv", "python3-numpy",
            "v4l-utils", "ffmpeg",
        ],
        "pam_strategy": "insert",
        "pam_file": "/etc/pam.d/common-auth",
        "tested": "best effort - please report issues",
    },
}

DLIB_MODELS = [
    # (file, md5 of the .bz2 archive) - served from davisking/dlib-models
    ("dlib_face_recognition_resnet_model_v1.dat.bz2", "1b31cc4419cc8f1018117249b64bd683"),
    ("mmod_human_face_detector.dat.bz2", "5edccec8ac713d743be4865ff6ead7f7"),
    ("shape_predictor_5_face_landmarks.dat.bz2", "ef591cf713630226b35b11d0e1733118"),
]
DLIB_MODELS_URL = "https://github.com/davisking/dlib-models/raw/master"
DLIB_DATA_DIR = "/usr/share/dlib-data"

PAM_LINE = "auth       sufficient                  pam_howdy.so"

POLKIT_HELPER_DROPIN = """[Service]
PrivateDevices=no
DeviceAllow=char-video4linux rw
DeviceAllow=/dev/uinput rw
"""


def detect_family():
    info = {}
    try:
        with open("/etc/os-release") as f:
            for line in f:
                if "=" in line:
                    k, _, v = line.strip().partition("=")
                    info[k] = v.strip('"')
    except OSError:
        return None, info
    candidates = [info.get("ID", "")] + info.get("ID_LIKE", "").split()
    for cand in candidates:
        cand = cand.lower()
        for fam, data in FAMILIES.items():
            if cand in data["ids"] or cand.startswith("opensuse"):
                if cand.startswith("opensuse"):
                    return "suse", info
                return fam, info
    return None, info


# --------------------------------------------------------------------------
# Camera discovery
# --------------------------------------------------------------------------

def list_cameras():
    """Return [{path, name, ir_likely, grey_only}] for capture-capable nodes."""
    cams = []
    for sys_dir in sorted(glob.glob("/sys/class/video4linux/video*")):
        node = "/dev/" + os.path.basename(sys_dir)
        try:
            name = open(os.path.join(sys_dir, "name")).read().strip()
        except OSError:
            name = "unknown camera"
        fmts = ""
        if shutil.which("v4l2-ctl"):
            try:
                fmts = subprocess.run(
                    ["v4l2-ctl", "--device", node, "--list-formats"],
                    capture_output=True, text=True, timeout=10).stdout
            except Exception:
                pass
        fourccs = set(re.findall(r"'(\w{4})'", fmts))
        if fmts and not fourccs:
            continue  # metadata-only node
        grey_only = fourccs == {"GREY"}
        ir_likely = grey_only or "ir" in name.lower().replace("mirror", "")
        cams.append({"path": node, "name": name,
                     "ir_likely": ir_likely, "grey_only": grey_only})
    return cams


def probe_brightness(device):
    """Average luma of a few frames via ffmpeg signalstats; None if unknown."""
    if not shutil.which("ffmpeg"):
        return None
    try:
        out = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "info", "-f", "v4l2",
             "-i", device, "-frames:v", "6", "-vf",
             "signalstats,metadata=print:file=-", "-f", "null", "-"],
            capture_output=True, text=True, timeout=20)
        vals = [float(m) for m in re.findall(r"YAVG=([0-9.]+)", out.stdout + out.stderr)]
        return sum(vals) / len(vals) if vals else None
    except Exception:
        return None


# --------------------------------------------------------------------------
# Privileged execution
# --------------------------------------------------------------------------

class RootRunner:
    """Runs generated shell scripts as root via pkexec (GUI) or sudo (CLI)."""

    def __init__(self, use_sudo=False, dry_run=False, log=print):
        self.use_sudo = use_sudo
        self.dry_run = dry_run
        self.log = log

    def run(self, title, script_body):
        header = "#!/bin/bash\nset -uo pipefail\nexport DEBIAN_FRONTEND=noninteractive\n"
        full = header + script_body
        self.log(f"\n===== {title} =====")
        if self.dry_run:
            self.log("[dry-run] would execute as root:")
            for line in script_body.strip().splitlines():
                self.log("    " + line)
            return 0
        fd, path = tempfile.mkstemp(prefix="howdy-inst-", suffix=".sh")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(full)
            os.chmod(path, 0o700)
            if os.geteuid() == 0:
                argv = ["bash", path]
            elif self.use_sudo:
                argv = ["sudo", "bash", path]
            else:
                argv = ["pkexec", "bash", path]
            proc = subprocess.Popen(argv, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, bufsize=1)
            for line in proc.stdout:
                self.log(line.rstrip("\n"))
            proc.wait()
            if proc.returncode != 0:
                self.log(f"[!] step '{title}' exited with code {proc.returncode}")
            return proc.returncode
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass


# --------------------------------------------------------------------------
# Install phases (root script generators)
# --------------------------------------------------------------------------

def script_install_packages(family):
    fam = FAMILIES[family]
    lines = []
    if fam["refresh"]:
        lines.append(fam["refresh"])
    lines.append(f'{fam["install"]} {" ".join(fam["packages"])}')
    return "\n".join(lines) + "\n"


def script_install_built(source_dir, family):
    """meson install + dlib(pip if missing) + models + config + polkit fix."""
    build = os.path.join(source_dir, "build")
    models = " ".join(f"{name}:{md5}" for name, md5 in DLIB_MODELS)
    return f"""
echo "[1/5] Installing Howdy (meson install)"
cd {shlex.quote(source_dir)}
meson install -C {shlex.quote(build)} || exit 10
mkdir -p /etc/howdy
meson introspect --installed {shlex.quote(build)} > /etc/howdy/.installed-files.json 2>/dev/null || true

echo "[2/5] Ensuring python dlib is importable"
if ! python3 -c 'import dlib' 2>/dev/null; then
    echo "  dlib missing - building via pip (this can take 5-15 minutes)..."
    python3 -m pip install dlib --break-system-packages 2>/dev/null || python3 -m pip install dlib || exit 11
fi
python3 -c 'import dlib; print("  dlib", dlib.__version__, "OK")' || exit 11

echo "[3/5] Downloading dlib face models"
mkdir -p {DLIB_DATA_DIR}
cd {DLIB_DATA_DIR}
for entry in {models}; do
    file="${{entry%%:*}}"; sum="${{entry##*:}}"; dat="${{file%.bz2}}"
    [ -f "$dat" ] && {{ echo "  $dat already present"; continue; }}
    curl -fsSL -o "$file" "{DLIB_MODELS_URL}/$file" || wget -q -O "$file" "{DLIB_MODELS_URL}/$file" || exit 12
    echo "$sum  $file" | md5sum -c - >/dev/null || {{ echo "  checksum mismatch for $file"; exit 13; }}
    bunzip2 -f "$file" || exit 14
    echo "  $dat OK"
done

echo "[4/5] Writing camera config"
CONF=/etc/howdy/config.ini
[ -f "$CONF" ] || {{ echo "  config.ini not found after install"; exit 15; }}
sed -i "s|^device_path = .*|device_path = __DEVICE__|" "$CONF"
sed -i "s|^dark_threshold = .*|dark_threshold = __DARKTH__|" "$CONF"
grep -E '^(device_path|dark_threshold) ' "$CONF" | sed 's/^/  /'

echo "[5/5] polkit agent helper workaround (polkit >= 126)"
if [ -f /usr/lib/systemd/system/polkit-agent-helper@.service ]; then
    mkdir -p /usr/lib/systemd/system/polkit-agent-helper@.service.d
    cat > /usr/lib/systemd/system/polkit-agent-helper@.service.d/10-howdy.conf <<'EOF'
{POLKIT_HELPER_DROPIN}EOF
    systemctl daemon-reload
    echo "  drop-in installed"
else
    echo "  not needed on this system"
fi
echo INSTALL_PHASE_OK
"""


def script_enable_pam(family):
    fam = FAMILIES[family]
    strategy = fam["pam_strategy"]
    pam_file = fam["pam_file"]
    insert = f"""
enable_insert() {{
    f={shlex.quote(pam_file)}
    [ -f /usr/lib/security/pam_howdy.so ] || [ -f /usr/lib64/security/pam_howdy.so ] || \\
    ls /usr/lib/*/security/pam_howdy.so >/dev/null 2>&1 || {{ echo "pam_howdy.so not found"; return 20; }}
    grep -q pam_howdy "$f" && {{ echo "PAM already enabled in $f"; return 0; }}
    cp -a "$f" "$f.pre-howdy"
    if head -1 "$f" | grep -q '^#%PAM-1.0'; then
        sed -i '/^#%PAM-1.0/a {PAM_LINE}' "$f"
    else
        sed -i '1i {PAM_LINE}' "$f"
    fi
    echo "Enabled in $f (backup: $f.pre-howdy)"
}}
"""
    if strategy == "pam-auth-update":
        body = """
if command -v pam-auth-update >/dev/null && [ -f /usr/share/pam-configs/howdy ]; then
    pam-auth-update --package --enable howdy && echo "Enabled via pam-auth-update"
else
    echo "pam-auth-update unavailable, falling back to direct insert"
    enable_insert || exit $?
fi
"""
    elif strategy == "authselect":
        body = """
if command -v authselect >/dev/null && authselect current >/dev/null 2>&1; then
    cur=$(authselect current --raw 2>/dev/null | awk '{print $1}')
    echo "authselect profile: $cur"
    if [[ "$cur" != custom/* ]]; then
        authselect create-profile howdy -b "$cur" >/dev/null 2>&1 || true
        target="custom/howdy"
    else
        target="$cur"
    fi
    changed=0
    for f in system-auth password-auth; do
        p="/etc/authselect/$target/$f"
        [ -f "$p" ] || continue
        grep -q pam_howdy "$p" && continue
        sed -i '0,/^auth/s//auth        sufficient                                   pam_howdy.so\\nauth/' "$p"
        changed=1
    done
    authselect select "$target" --force >/dev/null && authselect apply-changes
    echo "Enabled via authselect ($target)"
else
    echo "authselect unavailable, falling back to direct insert"
    enable_insert || exit $?
fi
"""
    else:
        body = "enable_insert || exit $?\n"
    return insert + body + 'echo PAM_PHASE_OK\n'


def script_enroll(target_user, label="initial-model"):
    return f"""
CONF=/etc/howdy/config.ini
for attempt in 1 2 3; do
    echo "--- capture attempt $attempt (look straight at the camera) ---"
    out=$(howdy -U {shlex.quote(target_user)} -y add {shlex.quote(label)} 2>&1); rc=$?
    echo "$out"
    [ $rc -eq 0 ] && {{ howdy -U {shlex.quote(target_user)} list; echo ENROLL_OK; exit 0; }}
    if echo "$out" | grep -q "too dark"; then
        echo ">> raising dark_threshold to 90"
        sed -i 's|^dark_threshold = .*|dark_threshold = 90|' "$CONF"
    elif echo "$out" | grep -qiE "black frames|Failed to read|Timeout"; then
        echo ">> switching recorder to ffmpeg"
        sed -i 's|^recording_plugin = .*|recording_plugin = ffmpeg|' "$CONF"
    fi
    sleep 2
done
echo ENROLL_FAILED
exit 2
"""


def script_uninstall(family):
    fam = FAMILIES[family]
    pam_file = fam["pam_file"]
    return f"""
echo "Restoring PAM..."
if command -v pam-auth-update >/dev/null && [ -f /usr/share/pam-configs/howdy ]; then
    pam-auth-update --package --remove howdy || true
    rm -f /usr/share/pam-configs/howdy
fi
f={shlex.quote(pam_file)}
[ -f "$f.pre-howdy" ] && cp -a "$f.pre-howdy" "$f" && echo "  restored $f"
sed -i '/pam_howdy/d' "$f" 2>/dev/null || true
for p in /etc/authselect/custom/howdy; do
    [ -d "$p" ] && command -v authselect >/dev/null && {{
        authselect select sssd --force >/dev/null 2>&1 || authselect select local --force >/dev/null 2>&1 || true
        rm -rf "$p"; authselect apply-changes 2>/dev/null || true
    }}
done
echo "Removing installed files..."
if [ -f /etc/howdy/.installed-files.json ]; then
    python3 - <<'PYEOF'
import json, os
try:
    data = json.load(open("/etc/howdy/.installed-files.json"))
    for path in data.values():
        try:
            os.remove(path); print("  removed", path)
        except (IsADirectoryError, FileNotFoundError):
            pass
except Exception as e:
    print("  manifest error:", e)
PYEOF
fi
rm -f /usr/lib/systemd/system/polkit-agent-helper@.service.d/10-howdy.conf
systemctl daemon-reload 2>/dev/null || true
echo "NOTE: /etc/howdy (config + face models) left in place; delete manually if desired."
echo UNINSTALL_OK
"""


# --------------------------------------------------------------------------
# User-space build phase
# --------------------------------------------------------------------------

def run_user_build(source_dir, family, log):
    """meson setup + compile as the invoking user."""
    build = os.path.join(source_dir, "build")
    fam = FAMILIES[family]
    pam_cfg = "true" if fam["pam_strategy"] == "pam-auth-update" else "false"
    cmds = [
        ["meson", "setup", build, source_dir, "--prefix", "/usr",
         f"-Dinstall_pam_config={pam_cfg}",
         f"-Ddlib_data_dir={DLIB_DATA_DIR}",
         "-Dpython_path=" + (shutil.which("python3") or "/usr/bin/python3"),
         "--wipe" if os.path.isdir(build) else "--reconfigure"],
        ["meson", "compile", "-C", build],
    ]
    # --wipe fails on first run, --reconfigure fails if not set up; normalize:
    if not os.path.isdir(build):
        cmds[0] = [a for a in cmds[0] if a not in ("--wipe", "--reconfigure")]
    for argv in cmds:
        log("$ " + " ".join(argv))
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, bufsize=1)
        for line in proc.stdout:
            log(line.rstrip("\n"))
        proc.wait()
        if proc.returncode != 0:
            return proc.returncode
    return 0


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------

class Installer:
    def __init__(self, source_dir, family, runner, log):
        self.source_dir = source_dir
        self.family = family
        self.runner = runner
        self.log = log
        self.device = None
        self.dark_threshold = 60
        self.target_user = os.environ.get("SUDO_USER") or os.environ.get("USER") or "root"

    def phase_packages(self):
        return self.runner.run("Install distro packages", script_install_packages(self.family))

    def phase_build(self):
        self.log("\n===== Build Howdy (unprivileged) =====")
        if self.runner.dry_run:
            self.log("[dry-run] would run meson setup/compile in " + self.source_dir)
            return 0
        return run_user_build(self.source_dir, self.family, self.log)

    def phase_install(self):
        body = script_install_built(self.source_dir, self.family)
        body = body.replace("__DEVICE__", self.device or "none")
        body = body.replace("__DARKTH__", str(self.dark_threshold))
        rc = self.runner.run("Install to system + models + config", body)
        if rc != 0:
            return rc
        return self.runner.run("Enable PAM (password fallback kept)",
                               script_enable_pam(self.family))

    def phase_enroll(self):
        return self.runner.run("Enroll face", script_enroll(self.target_user))

    def phase_uninstall(self):
        return self.runner.run("Uninstall Howdy", script_uninstall(self.family))


# --------------------------------------------------------------------------
# CLI front-end
# --------------------------------------------------------------------------

def cli_main(args, family, info):
    log = print
    runner = RootRunner(use_sudo=(os.geteuid() != 0 and sys.stdin.isatty()),
                        dry_run=args.dry_run, log=log)
    inst = Installer(args.source_dir, family, runner, log)

    if args.uninstall:
        return inst.phase_uninstall()

    cams = list_cameras()
    if not cams:
        log("No cameras found (/dev/video*). Aborting."); return 1
    log("\nCameras:")
    for i, c in enumerate(cams):
        tag = " [IR - recommended]" if c["ir_likely"] else ""
        log(f"  {i}: {c['path']}  {c['name']}{tag}")
    if args.device:
        inst.device = args.device
    else:
        ir = [c for c in cams if c["ir_likely"]]
        pick = (ir or cams)[0]
        inst.device = pick["path"]
        if sys.stdin.isatty() and not args.yes:
            ans = input(f"Camera to use [{inst.device}]: ").strip()
            if ans:
                inst.device = ans if ans.startswith("/dev/") else cams[int(ans)]["path"]
    picked = next((c for c in cams if c["path"] == inst.device), None)
    if picked and picked["ir_likely"]:
        inst.dark_threshold = 90
    log(f"Using camera: {inst.device} (dark_threshold={inst.dark_threshold})")

    for phase in (inst.phase_packages, inst.phase_build, inst.phase_install):
        rc = phase()
        if rc != 0:
            log(f"\nFAILED (rc={rc}). See log above."); return rc
    if args.dry_run:
        log("\n[dry-run] complete - no changes made."); return 0
    log("\nAbout to enroll your face. Position yourself in front of the camera.")
    if sys.stdin.isatty() and not args.yes:
        input("Press Enter when ready...")
    rc = inst.phase_enroll()
    if rc == 0:
        log("\nAll done! Test with:  sudo -k; sudo whoami   (should NOT ask for a password)")
    return rc


# --------------------------------------------------------------------------
# GTK front-end
# --------------------------------------------------------------------------

def gui_main(args, family, info):
    import gi
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk, GLib, Pango

    fam = FAMILIES[family]

    class Win(Gtk.Assistant):
        def __init__(self):
            super().__init__(title="Howdy Installer")
            self.set_default_size(760, 560)
            self.connect("cancel", lambda *a: Gtk.main_quit())
            self.connect("close", lambda *a: Gtk.main_quit())
            self.runner = RootRunner(dry_run=args.dry_run, log=self.log_line)
            self.inst = Installer(args.source_dir, family, self.runner, self.log_line)
            self.cams = list_cameras()
            self._build_pages()
            self.busy = False

        # ---- logging into the TextView from worker threads
        def log_line(self, text):
            GLib.idle_add(self._append, text)

        def _append(self, text):
            buf = self.logview.get_buffer()
            buf.insert(buf.get_end_iter(), text + "\n")
            mark = buf.create_mark(None, buf.get_end_iter(), False)
            self.logview.scroll_mark_onscreen(mark)
            return False

        # ---- pages
        def _build_pages(self):
            # 1. intro
            intro = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                            margin=24)
            title = Gtk.Label()
            title.set_markup("<span size='x-large' weight='bold'>Howdy - face authentication for Linux</span>")
            body = Gtk.Label(label=(
                f"Detected system: {info.get('PRETTY_NAME', family)}\n"
                f"Distro family: {family} ({fam['tested']})\n\n"
                "This wizard will:\n"
                "  1. Install build dependencies with your package manager\n"
                "  2. Build Howdy from this source checkout\n"
                "  3. Install it, download face-recognition models, enable PAM\n"
                "  4. Enroll your face and test it\n\n"
                "Your password ALWAYS keeps working - Howdy is added as an\n"
                "optional first factor, never a replacement.\n\n"
                "Root steps run through pkexec: expect 2-3 system password\n"
                "dialogs during installation."))
            body.set_xalign(0)
            intro.pack_start(title, False, False, 0)
            intro.pack_start(body, False, False, 0)
            self.append_page(intro)
            self.set_page_type(intro, Gtk.AssistantPageType.INTRO)
            self.set_page_title(intro, "Welcome")
            self.set_page_complete(intro, True)

            # 2. camera
            campage = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin=24)
            campage.pack_start(Gtk.Label(label="Select the camera Howdy should use "
                                               "(IR cameras work in the dark and resist photo spoofing):",
                                         xalign=0), False, False, 0)
            self.cam_group = None
            self.cam_radios = []
            for c in self.cams:
                tag = "   [IR - recommended]" if c["ir_likely"] else ""
                rb = Gtk.RadioButton.new_with_label_from_widget(
                    self.cam_group, f"{c['path']}  -  {c['name']}{tag}")
                self.cam_group = self.cam_group or rb
                rb.cam = c
                campage.pack_start(rb, False, False, 0)
                self.cam_radios.append(rb)
            # preselect first IR cam
            for rb in self.cam_radios:
                if rb.cam["ir_likely"]:
                    rb.set_active(True)
                    break
            probe_btn = Gtk.Button(label="Probe brightness of selected camera")
            probe_lbl = Gtk.Label(label="", xalign=0)

            def do_probe(_b):
                sel = self._selected_cam()
                probe_lbl.set_text("probing...")
                def work():
                    val = probe_brightness(sel["path"]) if sel else None
                    GLib.idle_add(probe_lbl.set_text,
                                  f"average brightness: {val:.1f}" if val else "no reading (device busy?)")
                threading.Thread(target=work, daemon=True).start()
            probe_btn.connect("clicked", do_probe)
            campage.pack_start(probe_btn, False, False, 6)
            campage.pack_start(probe_lbl, False, False, 0)
            if not self.cams:
                campage.pack_start(Gtk.Label(
                    label="No cameras detected! Plug one in and restart the installer."), False, False, 0)
            self.append_page(campage)
            self.set_page_title(campage, "Camera")
            self.set_page_complete(campage, bool(self.cams))

            # 3. install (log page)
            logpage = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin=12)
            self.logview = Gtk.TextView(editable=False, monospace=True)
            self.logview.modify_font(Pango.FontDescription("Monospace 9"))
            sw = Gtk.ScrolledWindow()
            sw.add(self.logview)
            self.go_btn = Gtk.Button(label="Start installation")
            self.go_btn.connect("clicked", self.on_install)
            logpage.pack_start(self.go_btn, False, False, 0)
            logpage.pack_start(sw, True, True, 0)
            self.logpage = logpage
            self.append_page(logpage)
            self.set_page_title(logpage, "Install")
            self.set_page_complete(logpage, False)

            # 4. enroll + test
            endpage = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin=24)
            self.enroll_lbl = Gtk.Label(label=(
                "Installation finished. Now enroll your face:\n"
                "look straight at the camera, then press the button.\n"
                "(a system password dialog will appear first)"), xalign=0)
            enroll_btn = Gtk.Button(label="Enroll my face")
            enroll_btn.connect("clicked", self.on_enroll)
            test_btn = Gtk.Button(label="Test: authenticate by face (pkexec)")
            test_btn.connect("clicked", self.on_test)
            self.end_status = Gtk.Label(label="", xalign=0)
            for w in (self.enroll_lbl, enroll_btn, test_btn, self.end_status):
                endpage.pack_start(w, False, False, 0)
            self.append_page(endpage)
            self.set_page_type(endpage, Gtk.AssistantPageType.CONFIRM)
            self.set_page_title(endpage, "Enroll & test")
            self.set_page_complete(endpage, True)

        def _selected_cam(self):
            for rb in self.cam_radios:
                if rb.get_active():
                    return rb.cam
            return None

        # ---- actions
        def on_install(self, _btn):
            if self.busy:
                return
            sel = self._selected_cam()
            if sel:
                self.inst.device = sel["path"]
                self.inst.dark_threshold = 90 if sel["ir_likely"] else 60
            self.busy = True
            self.go_btn.set_sensitive(False)

            def work():
                ok = True
                for phase in (self.inst.phase_packages, self.inst.phase_build,
                              self.inst.phase_install):
                    if phase() != 0:
                        ok = False
                        break
                GLib.idle_add(self._install_done, ok)
            threading.Thread(target=work, daemon=True).start()

        def _install_done(self, ok):
            self.busy = False
            self.go_btn.set_sensitive(not ok)
            self.go_btn.set_label("Installation complete" if ok else "Retry installation")
            self.set_page_complete(self.logpage, ok)
            if ok:
                self.log_line("\n>>> Success - continue to the next page to enroll your face.")
            return False

        def on_enroll(self, _btn):
            if self.busy:
                return
            self.busy = True

            def work():
                rc = self.inst.phase_enroll()
                GLib.idle_add(self.end_status.set_text,
                              "Face enrolled! Try the test button." if rc == 0
                              else "Enrollment failed - check the Install page log.")
                self.busy = False
            threading.Thread(target=work, daemon=True).start()

        def on_test(self, _btn):
            def work():
                try:
                    rc = subprocess.run(["pkexec", "true"], timeout=30).returncode
                except Exception:
                    rc = 1
                GLib.idle_add(self.end_status.set_text,
                              "Authenticated! (if no password dialog appeared, that was Howdy)"
                              if rc == 0 else "Test failed or was cancelled.")
            threading.Thread(target=work, daemon=True).start()

    win = Win()
    win.show_all()
    Gtk.main()
    return 0


# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Howdy cross-distro installer")
    ap.add_argument("--source-dir", default=None, help="howdy source checkout")
    ap.add_argument("--cli", action="store_true", help="terminal mode (no GUI)")
    ap.add_argument("--dry-run", action="store_true", help="print privileged steps instead of running them")
    ap.add_argument("--device", help="camera device path, e.g. /dev/video2")
    ap.add_argument("--yes", "-y", action="store_true", help="no interactive prompts (CLI)")
    ap.add_argument("--uninstall", action="store_true", help="remove howdy + restore PAM")
    args = ap.parse_args()

    if not args.source_dir:
        args.source_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if not os.path.isfile(os.path.join(args.source_dir, "meson.build")):
        print(f"error: {args.source_dir} does not look like a howdy checkout", file=sys.stderr)
        return 1

    family, info = detect_family()
    if not family:
        print("error: unsupported distro (need arch/debian/fedora/openSUSE family)", file=sys.stderr)
        return 1

    want_gui = not args.cli and bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    if want_gui:
        try:
            return gui_main(args, family, info)
        except Exception as e:
            print(f"GUI unavailable ({e}); falling back to CLI.", file=sys.stderr)
    return cli_main(args, family, info)


if __name__ == "__main__":
    sys.exit(main())
