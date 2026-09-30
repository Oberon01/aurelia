#!/usr/bin/env bash
# Install Aurelia's node agent on this Linux computer and pair it.
#
#   curl -fsSL https://atlas-ai.<tailnet>.ts.net:8443/node/install-linux.sh | bash -s -- https://atlas-ai.<tailnet>.ts.net:8443 "Living room TV"
#   ... | bash -s -- <address> "Living room TV" --shell      also make this screen hers (asks for sudo once)
#   ... | bash -s -- <address> --no-shell                    back to a normal desktop at login
#
# The agent runs as you: it lives in ~/.local/share/aurelia-node, its key in ~/.config/aurelia-node
# (readable only by you), and a systemd user service keeps it running through its guard (which
# rolls back a bad self-update). Approve the code it shows in Aurelia's mailbox. Run it again any
# time; an already-paired computer just gets the latest agent.
#
# --shell (sudo): an "Aurelia" login session (X11) that shows only her, set as the automatic login,
# with a kiosk browser (Chromium is installed if there's no Chrome/Chromium/Edge) and a minimal
# window manager. GNOME stays on the login screen as a way back in.
set -euo pipefail

SERVER="" NAME="" SHELL_MODE=""
for arg in "$@"; do
  case "$arg" in
    --shell) SHELL_MODE=on ;;
    --no-shell) SHELL_MODE=off ;;
    *) if [[ -z "$SERVER" ]]; then SERVER="$arg"; elif [[ -z "$NAME" ]]; then NAME="$arg"; fi ;;
  esac
done
if [[ -z "$SERVER" ]]; then
  echo "usage: install-linux.sh <Aurelia's address> [name for this computer] [--shell|--no-shell]"
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

if [[ "$SHELL_MODE" == "on" ]]; then
  echo "Setting up her session (sudo)..."
  if ! command -v google-chrome-stable >/dev/null && ! command -v google-chrome >/dev/null \
     && ! command -v chromium >/dev/null && ! command -v chromium-browser >/dev/null && ! command -v microsoft-edge >/dev/null; then
    sudo snap install chromium
  fi
  sudo apt-get install -y -qq matchbox-window-manager x11-xserver-utils >/dev/null
  sudo tee /usr/local/bin/aurelia-session >/dev/null <<'EOF'
#!/bin/sh
# Aurelia's login session: no desktop, just her. The node agent draws her screen (a kiosk
# browser); this keeps the session alive with a minimal window manager.
export XDG_CURRENT_DESKTOP=Aurelia XDG_SESSION_DESKTOP=aurelia
xset s off -dpms s noblank 2>/dev/null           # she decides when the screen sleeps
systemctl --user import-environment DISPLAY XAUTHORITY XDG_CURRENT_DESKTOP XDG_SESSION_DESKTOP XDG_SESSION_TYPE \
  XDG_RUNTIME_DIR DBUS_SESSION_BUS_ADDRESS 2>/dev/null
dbus-update-activation-environment --systemd DISPLAY XAUTHORITY XDG_CURRENT_DESKTOP 2>/dev/null
systemctl --user restart aurelia-node.service
exec matchbox-window-manager -use_titlebar no
EOF
  sudo chmod 755 /usr/local/bin/aurelia-session
  sudo tee /usr/share/xsessions/aurelia.desktop >/dev/null <<'EOF'
[Desktop Entry]
Name=Aurelia
Comment=This screen is Aurelia
Exec=/usr/local/bin/aurelia-session
Type=Application
DesktopNames=Aurelia
EOF
  # Log in to her session automatically (GDM), and make it this user's session.
  sudo python3 - "$ME" <<'PY'
import configparser, sys, pathlib
user = sys.argv[1]
gdm = pathlib.Path("/etc/gdm3/custom.conf")
cp = configparser.ConfigParser(strict=False); cp.optionxform = str
if gdm.exists(): cp.read(gdm)
cp.setdefault("daemon", {})
cp["daemon"]["AutomaticLoginEnable"] = "True"; cp["daemon"]["AutomaticLogin"] = user
with gdm.open("w") as f: cp.write(f)
acct = pathlib.Path(f"/var/lib/AccountsService/users/{user}")
ac = configparser.ConfigParser(strict=False); ac.optionxform = str
if acct.exists(): ac.read(acct)
ac.setdefault("User", {})
ac["User"]["Session"] = "aurelia"; ac["User"]["XSession"] = "aurelia"
acct.parent.mkdir(parents=True, exist_ok=True)
with acct.open("w") as f: ac.write(f)
PY
  python3 "$AGENT" shell on
  echo "Her session is ready. It starts at the next login (or reboot)."
elif [[ "$SHELL_MODE" == "off" ]]; then
  python3 "$AGENT" shell off
  if [[ -f "/var/lib/AccountsService/users/$ME" ]]; then
    sudo sed -i 's/^Session=aurelia$/Session=ubuntu/; s/^XSession=aurelia$/XSession=ubuntu/' "/var/lib/AccountsService/users/$ME"
  fi
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
