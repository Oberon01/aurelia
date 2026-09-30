#!/usr/bin/env bash
# Install Aurelia's node agent on this Linux computer and pair it.
#
#   curl -fsSL https://atlas-ai.<tailnet>.ts.net:8443/node/install-linux.sh | bash -s -- https://atlas-ai.<tailnet>.ts.net:8443 "Living room TV"
#
# Runs as you (no sudo): the agent lives in ~/.local/share/aurelia-node, its key in
# ~/.config/aurelia-node (readable only by you), and a systemd user service keeps it
# running. Approve the code it shows in Aurelia's mailbox. Run it again to update.
set -euo pipefail

if [[ -z "${1:-}" ]]; then
  echo "usage: install-linux.sh <Aurelia's address> [name for this computer]"
  exit 1
fi
SERVER="$1"
SERVER="${SERVER%/}"
NAME="${2:-$(hostname)}"
DIR="$HOME/.local/share/aurelia-node"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

command -v python3 >/dev/null || { echo "Python 3 is needed: sudo apt install python3"; exit 1; }
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' || { echo "Python 3.9 or newer is needed."; exit 1; }

echo "Getting the agent from $SERVER..."
mkdir -p "$DIR"
curl -fsSL "$SERVER/node/agent.py" -o "$DIR/aurelia_node.py.new"
python3 -m py_compile "$DIR/aurelia_node.py.new"
mv "$DIR/aurelia_node.py.new" "$DIR/aurelia_node.py"

if python3 "$DIR/aurelia_node.py" status >/dev/null 2>&1; then
  echo "Already paired; updating the agent only."
else
  python3 "$DIR/aurelia_node.py" pair "$SERVER" --name "$NAME" </dev/null
fi

mkdir -p "$UNIT_DIR"
cat > "$UNIT_DIR/aurelia-node.service" <<EOF
[Unit]
Description=Aurelia node agent
After=network-online.target

[Service]
ExecStart=/usr/bin/env python3 $DIR/aurelia_node.py run
Restart=on-failure
RestartSec=5
# Exit 3 = this computer's key was revoked: don't keep knocking.
RestartPreventExitStatus=3

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable aurelia-node.service >/dev/null
systemctl --user restart aurelia-node.service
# Keep it running when nobody is logged in (and start it at boot).
ME="${USER:-$(id -un)}"
loginctl enable-linger "$ME" 2>/dev/null || echo "(Optional: 'sudo loginctl enable-linger $ME' starts it at boot before login.)"

sleep 2
if systemctl --user is-active --quiet aurelia-node.service; then
  echo "Aurelia's node agent is running. Logs: journalctl --user -u aurelia-node -f"
else
  echo "The agent didn't start. Logs: journalctl --user -u aurelia-node -n 50"
  exit 1
fi
