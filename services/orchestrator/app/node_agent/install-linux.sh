#!/usr/bin/env bash
# Install Aurelia's node agent on this Linux computer and pair it.
#
#   curl -fsSL https://atlas-ai.<tailnet>.ts.net:8443/node/install-linux.sh | bash -s -- https://atlas-ai.<tailnet>.ts.net:8443 "Living room TV"
#   ... | bash -s -- <address> "Living room TV" --shell      also make this screen hers (asks for sudo once)
#   ... | bash -s -- <address> --no-shell                    back to a normal desktop at login
#   ... | bash -s -- <address> --system                      one-time sudo: restart/shutdown/sleep without a
#                                                            password, shutdowns that can't hang, the system log, her player (mpv),
#                                                            installing apps you approve (PackageKit, snaps)
#   ... | bash -s -- <address> --role=server                 what it's for (presentation, workstation, server);
#                                                            guessed if left out, changeable on the devices panel
#
# The agent runs as you: it lives in ~/.local/share/aurelia-node, its key in ~/.config/aurelia-node
# (readable only by you), and a systemd user service keeps it running through its guard (which
# rolls back a bad self-update). Approve the code it shows in Aurelia's mailbox. Run it again any
# time; an already-paired computer just gets the latest agent.
#
# --shell (sudo): an "Aurelia" login session (Wayland, run by Cage) that shows only her, set as the
# automatic login, with a kiosk browser (Chromium is installed if there's no Chrome/Chromium/Edge).
# GNOME stays on the login screen as a way back in. Each step says what failed if it does.
set -euo pipefail

SERVER="" NAME="" SHELL_MODE="" ROLE="" SYSTEM=""
for arg in "$@"; do
  case "$arg" in
    --shell) SHELL_MODE=on ;;
    --no-shell) SHELL_MODE=off ;;
    --role=*) ROLE="${arg#--role=}" ;;
    --system) SYSTEM=1 ;;
    *) if [[ -z "$SERVER" ]]; then SERVER="$arg"; elif [[ -z "$NAME" ]]; then NAME="$arg"; fi ;;
  esac
done
if [[ -z "$SERVER" ]]; then
  echo "usage: install-linux.sh <Aurelia's address> [name for this computer] [--shell|--no-shell] [--system] [--role=presentation|workstation|server]"
  exit 1
fi
SERVER="${SERVER%/}"
NAME="${NAME:-$(hostname)}"
ME="${USER:-$(id -un)}"
DIR="$HOME/.local/share/aurelia-node"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
AGENT="$DIR/aurelia_node.py"

command -v python3 >/dev/null || { echo "Python 3 is needed: sudo apt install python3"; exit 1; }
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' || { echo "Python 3.9 or newer is needed."; exit 1; }

echo "Getting the agent from $SERVER..."
mkdir -p "$DIR"
for f in agent.py:aurelia_node.py guard.py:aurelia_node_guard.py; do
  curl -fsSL "$SERVER/node/${f%%:*}" -o "$DIR/${f##*:}.new"
  python3 -m py_compile "$DIR/${f##*:}.new"
  mv "$DIR/${f##*:}.new" "$DIR/${f##*:}"
done

if python3 "$AGENT" status >/dev/null 2>&1; then
  echo "Already paired; the agent is updated."
else
  python3 "$AGENT" pair "$SERVER" --name "$NAME" </dev/null
fi

set_session() {    # the account service keeps its own copy, so ask it (editing its file gets overwritten)
  local uid; uid="$(id -u "$ME")"
  if command -v busctl >/dev/null; then
    sudo busctl call org.freedesktop.Accounts "/org/freedesktop/Accounts/User$uid" org.freedesktop.Accounts.User SetSession s "$1" \
      && sudo busctl call org.freedesktop.Accounts "/org/freedesktop/Accounts/User$uid" org.freedesktop.Accounts.User SetXSession s "$1" \
      && return 0
  fi
  sudo python3 - "$ME" "$1" <<'PY'
import configparser, sys, pathlib
user, session = sys.argv[1], sys.argv[2]
acct = pathlib.Path(f"/var/lib/AccountsService/users/{user}")
ac = configparser.ConfigParser(strict=False); ac.optionxform = str
if acct.exists(): ac.read(acct)
ac.setdefault("User", {})
ac["User"]["Session"] = session; ac["User"]["XSession"] = session
acct.parent.mkdir(parents=True, exist_ok=True)
with acct.open("w") as f: ac.write(f)
PY
}

if [[ -n "$ROLE" ]]; then
  python3 "$AGENT" role "$ROLE" || exit 1
fi

if [[ -n "$SYSTEM" ]]; then
  echo "Giving Aurelia's agent what it needs from the system (sudo, once)..."
  # 1. Restart, shut down and sleep this computer without a password: this account only, these actions only.
  sudo mkdir -p /etc/polkit-1/rules.d
  sudo tee /etc/polkit-1/rules.d/49-aurelia-power.rules >/dev/null <<RULES
// Aurelia's node agent runs as $ME. It may restart, shut down and suspend this computer, and lock
// its sessions, without a password. Nothing else. (Installed by install-linux.sh --system.)
polkit.addRule(function (action, subject) {
  var allowed = ["org.freedesktop.login1.reboot", "org.freedesktop.login1.reboot-multiple-sessions",
                 "org.freedesktop.login1.reboot-ignore-inhibit", "org.freedesktop.login1.power-off",
                 "org.freedesktop.login1.power-off-multiple-sessions", "org.freedesktop.login1.power-off-ignore-inhibit",
                 "org.freedesktop.login1.suspend", "org.freedesktop.login1.suspend-multiple-sessions",
                 "org.freedesktop.login1.suspend-ignore-inhibit", "org.freedesktop.login1.lock-sessions"];
  if (subject.user == "$ME" && allowed.indexOf(action.id) >= 0) { return polkit.Result.YES; }
});
RULES
  # 1b. Install and remove apps after you said yes in Aurelia: this account only, and only from the
  #     system's signed sources (Ubuntu's packages through PackageKit, and snaps). Never a local file
  #     (package-install-untrusted stays with you).
  #     (PackageKit 1.3 / Ubuntu 26.04 ships pkgcli in the packagekit package; older releases have pkcon in packagekit-tools.)
  sudo apt-get install -y packagekit >/dev/null 2>&1 || true
  if ! command -v pkgcli >/dev/null && ! command -v pkcon >/dev/null && apt-cache show packagekit-tools >/dev/null 2>&1; then
    sudo apt-get install -y packagekit-tools >/dev/null 2>&1 || true
  fi
  command -v pkgcli >/dev/null || command -v pkcon >/dev/null || \
    echo "  (PackageKit's command line isn't available: try 'sudo apt-get install packagekit'; until then I can't install apps here)"
  sudo tee /etc/polkit-1/rules.d/49-aurelia-packages.rules >/dev/null <<RULES
// Aurelia's node agent runs as $ME. After Kentrell confirms in Aurelia, it may install packages from
// the system's trusted sources, remove packages, refresh the package lists, and install or remove
// snaps. Never untrusted (local) packages. (Installed by install-linux.sh --system.)
polkit.addRule(function (action, subject) {
  var allowed = ["org.freedesktop.packagekit.package-install", "org.freedesktop.packagekit.package-remove",
                 "org.freedesktop.packagekit.system-sources-refresh", "io.snapcraft.snapd.manage"];
  if (subject.user == "$ME" && allowed.indexOf(action.id) >= 0) { return polkit.Result.YES; }
});
RULES
  # 2. Shutdowns that can't hang: a service gets 20 s to stop (not 90 s or forever), the desktop can't
  #    hold a shutdown back for long, and if it still hangs the hardware watchdog resets the machine.
  sudo mkdir -p /etc/systemd/system.conf.d /etc/systemd/user.conf.d /etc/systemd/logind.conf.d
  printf '[Manager]\nDefaultTimeoutStopSec=20s\nRebootWatchdogSec=2min\n' | sudo tee /etc/systemd/system.conf.d/90-aurelia.conf >/dev/null
  printf '[Manager]\nDefaultTimeoutStopSec=10s\n' | sudo tee /etc/systemd/user.conf.d/90-aurelia.conf >/dev/null
  printf '[Login]\nInhibitDelayMaxSec=5\n' | sudo tee /etc/systemd/logind.conf.d/90-aurelia.conf >/dev/null
  sudo systemctl daemon-reexec || true
  if ls /dev/watchdog* >/dev/null 2>&1; then
    echo "  The hardware watchdog is there: a shutdown that hangs resets the machine within 2 minutes."
  else
    echo "  No hardware watchdog here; shutdowns still can't wait on a service for more than 20 s."
  fi
  # 3. Turning the screen off and on in her shell (Wayland).
  sudo apt-get install -y wlopm >/dev/null 2>&1 || echo "  (wlopm isn't available; screen off/on may not work in her shell)"
  # 4. Media players (play, pause, next, what's playing) through playerctl.
  sudo apt-get install -y playerctl >/dev/null 2>&1 || echo "  (playerctl isn't available; I can't control media players here)"
  # 5. Her player (video and music she plays here, full screen, controlled over its own socket).
  sudo apt-get install -y mpv >/dev/null 2>&1 || echo "  (mpv isn't available; I can't play video or music here)"
  # 6. Reading the whole system log (diagnostics), not only this account's.
  sudo usermod -aG adm "$ME" && echo "  She can read the system log after the next login or restart."
  echo "Done. Restart and shutdown no longer need a password for this account."
fi

if [[ "$SHELL_MODE" == "on" ]]; then
  echo "Setting up her session (sudo)..."
  have_browser=""
  for b in google-chrome-stable google-chrome chromium chromium-browser microsoft-edge-stable microsoft-edge; do
    if command -v "$b" >/dev/null || [[ -x "/snap/bin/$b" ]]; then have_browser=1; fi
  done
  if [[ -z "$have_browser" ]]; then
    echo "Installing Chromium (the screen she draws in)..."
    sudo snap install chromium || { echo "Couldn't install Chromium. Install Chrome or Chromium, then run this again."; exit 1; }
  fi
  # Her session is Wayland: Cage, a kiosk compositor, shows one thing full screen. (Ubuntu 25.10+
  # dropped X11 from the desktop; Wayland works on 24.04 too.)
  echo "Installing Cage (her session)..."
  sudo apt-get update -qq || true
  sudo apt-get install -y cage || { echo "Couldn't install Cage (apt install cage). Nothing else was changed."; exit 1; }
  sudo tee /usr/local/bin/aurelia-session >/dev/null <<'SESSION'
#!/bin/sh
# Aurelia's login session: no desktop, just her. Cage (a Wayland kiosk compositor) fills the
# screen with whatever her node agent shows (a kiosk browser); -s keeps Ctrl+Alt+F3 working.
export XDG_CURRENT_DESKTOP=Aurelia XDG_SESSION_DESKTOP=aurelia XDG_SESSION_TYPE=wayland
exec cage -d -s -- /usr/local/bin/aurelia-session-inside
SESSION
  sudo tee /usr/local/bin/aurelia-session-inside >/dev/null <<'INSIDE'
#!/bin/sh
# Inside Cage: tell the agent (a user service) where her screen is, restart it, and stay up.
systemctl --user unset-environment DISPLAY XAUTHORITY 2>/dev/null
systemctl --user import-environment WAYLAND_DISPLAY XDG_RUNTIME_DIR XDG_CURRENT_DESKTOP XDG_SESSION_DESKTOP \
  XDG_SESSION_TYPE DBUS_SESSION_BUS_ADDRESS 2>/dev/null
dbus-update-activation-environment --systemd WAYLAND_DISPLAY XDG_CURRENT_DESKTOP XDG_SESSION_TYPE 2>/dev/null
systemctl --user restart aurelia-node.service
exec sleep infinity
INSIDE
  sudo chmod 755 /usr/local/bin/aurelia-session /usr/local/bin/aurelia-session-inside
  sudo rm -f /usr/share/xsessions/aurelia.desktop                 # the old X11 session, if an earlier install made one
  sudo mkdir -p /usr/share/wayland-sessions
  sudo tee /usr/share/wayland-sessions/aurelia.desktop >/dev/null <<'DESKTOP'
[Desktop Entry]
Name=Aurelia
Comment=This screen is Aurelia
Exec=/usr/local/bin/aurelia-session
Type=Application
DesktopNames=Aurelia
DESKTOP
  # Log in to her session automatically (GDM).
  sudo python3 - "$ME" <<'PY'
import configparser, sys, pathlib
user = sys.argv[1]
gdm = pathlib.Path("/etc/gdm3/custom.conf")
cp = configparser.ConfigParser(strict=False); cp.optionxform = str
if gdm.exists(): cp.read(gdm)
cp.setdefault("daemon", {})
cp["daemon"]["AutomaticLoginEnable"] = "True"; cp["daemon"]["AutomaticLogin"] = user
if cp["daemon"].get("WaylandEnable", "").lower() == "false":
    cp["daemon"]["WaylandEnable"] = "true"               # her session is Wayland
    print("  (turned Wayland back on in the login screen: her session needs it)")
gdm.parent.mkdir(parents=True, exist_ok=True)
with gdm.open("w") as f: cp.write(f)
PY
  set_session aurelia
  if sudo grep -q "^Session=aurelia" "/var/lib/AccountsService/users/$ME" 2>/dev/null; then
    echo "Her session is set as $ME's login session."
  else
    echo "Warning: couldn't confirm her session is set. At the login screen, click the gear and pick Aurelia once."
  fi
  python3 "$AGENT" shell on
  echo "Her session is ready. It starts at the next login (sudo systemctl reboot -i)."
elif [[ "$SHELL_MODE" == "off" ]]; then
  python3 "$AGENT" shell off
  set_session ubuntu || true
  echo "Back to the normal desktop at the next login."
fi

mkdir -p "$UNIT_DIR"
cat > "$UNIT_DIR/aurelia-node.service" <<EOF
[Unit]
Description=Aurelia node agent
After=network-online.target

[Service]
ExecStart=/usr/bin/env python3 $DIR/aurelia_node_guard.py run
Restart=on-failure
RestartSec=5
# Exit 3 = this computer's key was revoked: don't keep knocking. (75 = updated itself: restart.)
RestartPreventExitStatus=3

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable aurelia-node.service >/dev/null
systemctl --user restart aurelia-node.service
# Keep it running when nobody is logged in (and start it at boot).
loginctl enable-linger "$ME" 2>/dev/null || echo "(Optional: 'sudo loginctl enable-linger $ME' starts it at boot before login.)"

sleep 2
if systemctl --user is-active --quiet aurelia-node.service; then
  echo "Aurelia's node agent is running. Logs: journalctl --user -u aurelia-node -f"
else
  echo "The agent didn't start. Logs: journalctl --user -u aurelia-node -n 50"
  exit 1
fi
