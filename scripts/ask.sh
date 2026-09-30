#!/usr/bin/env bash
# Talk to Aurelia from the server's terminal.
#
#   ./scripts/ask.sh                    interactive: type requests, "exit" to quit
#   ./scripts/ask.sh "echo hello"       one request
#   ./scripts/ask.sh --new "..."        start a fresh conversation
#
# The conversation carries over between runs (until it expires on the server),
# so one-off requests keep their context. Actions that need confirmation are
# shown and you're asked y/n. Needs only bash and python3 (standard on Ubuntu).
set -euo pipefail

AURELIA_URL="${AURELIA_URL:-http://localhost:8080}"
AURELIA_USER="${AURELIA_USER:-owner}"
# This terminal's key (from ./scripts/pair.sh --terminal), unless AURELIA_KEY is set.
KEY_FILE="${XDG_CONFIG_HOME:-$HOME/.config}/aurelia/key"
if [[ -z "${AURELIA_KEY:-}" && -r "$KEY_FILE" ]]; then AURELIA_KEY="$(tr -d '[:space:]' < "$KEY_FILE")"; fi
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/aurelia"
SESSION_FILE="$STATE_DIR/session"
mkdir -p "$STATE_DIR"

if [[ "${1:-}" == "--new" ]]; then
  rm -f "$SESSION_FILE"
  shift
fi

ask() {
  AURELIA_URL="$AURELIA_URL" AURELIA_USER="$AURELIA_USER" SESSION_FILE="$SESSION_FILE" AURELIA_KEY="${AURELIA_KEY:-}" \
    python3 - "$1" <<'PY'
import json, os, sys, urllib.error, urllib.request

url, user, session_file = os.environ["AURELIA_URL"], os.environ["AURELIA_USER"], os.environ["SESSION_FILE"]
key = os.environ.get("AURELIA_KEY", "")
CYAN, GREY, RED, RESET = "\033[36m", "\033[90m", "\033[31m", "\033[0m"

def post(path, body):
    req = urllib.request.Request(url + path, data=json.dumps(body).encode(),
                                 headers={"content-type": "application/json",
                                          **({"authorization": f"Bearer {key}"} if key else {})})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)

body = {"text": sys.argv[1], "user": user}
if os.path.exists(session_file):
    body["session_id"] = open(session_file).read().strip()
try:
    r = post("/ask", body)
except urllib.error.HTTPError as e:
    if e.code in (401, 403):
        print(f"{RED}  ! This terminal isn't paired with Aurelia. On the server: ./scripts/pair.sh --terminal{RESET}"); sys.exit(0)
    print(f"{RED}  ! {e.code}: {e.read().decode()[:300]}{RESET}"); sys.exit(0)
except urllib.error.URLError as e:
    print(f"{RED}  ! can't reach Aurelia at {url}: {e.reason}{RESET}"); sys.exit(0)

open(session_file, "w").write(r["session_id"])
print(f"{CYAN}Aura: {r['reply']}{RESET}")
tasks = f", tasks: {len(r['task_ids'])}" if r.get("task_ids") else ""
print(f"{GREY}      [{r['route']}, {r['latency_ms']} ms{tasks}]{RESET}")

for action in r.get("needs_confirmation", []):
    with open("/dev/tty") as tty:
        print(f"  Confirm {action['tool']} {json.dumps(action['args'])}? (y/n) ", end="", flush=True)
        answer = tty.readline().strip().lower()
    c = post("/ask/confirm", {"action_id": action["action_id"], "approve": answer.startswith("y"), "user": user})
    print(f"{CYAN}Aura: {c['reply']}{RESET}")
PY
}

if [[ $# -gt 0 ]]; then
  ask "$*"
  exit 0
fi

echo -e "\033[90mTalking to Aurelia at $AURELIA_URL. Type 'exit' to quit, 'new' for a fresh conversation.\033[0m"
while true; do
  read -r -e -p "You: " line || break
  case "$line" in
    exit|quit) break ;;
    new) rm -f "$SESSION_FILE"; echo -e "\033[90m      (new conversation)\033[0m"; continue ;;
    "") continue ;;
  esac
  history -s "$line"
  ask "$line"
done
