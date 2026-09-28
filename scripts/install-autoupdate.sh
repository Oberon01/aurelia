#!/usr/bin/env bash
# Install (or remove) the systemd timer that runs update.sh automatically.
#
#   sudo ./scripts/install-autoupdate.sh            check every 2 minutes
#   sudo ./scripts/install-autoupdate.sh 5min       custom interval
#   sudo ./scripts/install-autoupdate.sh --remove
#
# Logs: journalctl -u aurelia-update -f
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "run with sudo"; exit 1; }
REPO=$(cd "$(dirname "$0")/.." && pwd)
RUN_AS=${SUDO_USER:-$(stat -c %U "$REPO")}
UNIT=/etc/systemd/system

if [[ "${1:-}" == "--remove" ]]; then
  systemctl disable --now aurelia-update.timer 2>/dev/null || true
  rm -f "$UNIT/aurelia-update.service" "$UNIT/aurelia-update.timer"
  systemctl daemon-reload
  echo "auto-update removed"
  exit 0
fi
INTERVAL=${1:-2min}

id -nG "$RUN_AS" | grep -qw docker || { echo "$RUN_AS must be in the docker group (sudo usermod -aG docker $RUN_AS)"; exit 1; }

cat > "$UNIT/aurelia-update.service" <<EOF
[Unit]
Description=Aurelia: pull new commits and apply what changed
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=oneshot
User=$RUN_AS
WorkingDirectory=$REPO
ExecStart=$REPO/scripts/update.sh
EOF

cat > "$UNIT/aurelia-update.timer" <<EOF
[Unit]
Description=Aurelia: check for updates every $INTERVAL

[Timer]
OnBootSec=1min
OnUnitActiveSec=$INTERVAL
Persistent=true

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now aurelia-update.timer
echo "auto-update installed: checks every $INTERVAL as $RUN_AS in $REPO"
echo "logs: journalctl -u aurelia-update -f"
