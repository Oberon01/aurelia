#!/usr/bin/env bash
# Pair devices from the server. This is how the very first device gets in; after
# that you can also approve new devices from the mailbox on any paired one.
#
#   ./scripts/pair.sh K7P-4QX        approve the device showing this code
#   ./scripts/pair.sh --list         everything paired or waiting
#   ./scripts/pair.sh --revoke 12    revoke device 12 (its key stops working)
#   ./scripts/pair.sh --terminal     give this terminal a key for scripts/ask.sh
#
# Runs inside the orchestrator container, so it needs Docker access on the server.
set -euo pipefail
cd "$(dirname "$0")/.."

run() { docker compose exec -T orchestrator python -m app.auth "$@"; }

case "${1:-}" in
  ""|-h|--help)
    sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'
    ;;
  --list)
    run list
    ;;
  --revoke)
    run revoke "${2:?which device id? see --list}"
    ;;
  --terminal)
    dir="${XDG_CONFIG_HOME:-$HOME/.config}/aurelia"
    mkdir -p "$dir"
    key="$(run terminal "$(hostname) terminal")"
    umask 077
    printf '%s\n' "$key" > "$dir/key"
    chmod 600 "$dir/key"
    echo "This terminal has a key now ($dir/key). scripts/ask.sh uses it."
    ;;
  *)
    run approve "$1"
    ;;
esac
