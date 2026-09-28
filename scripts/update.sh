#!/usr/bin/env bash
# Pull new commits and apply exactly what changed.
#
#   ./scripts/update.sh              pull + apply (what the timer runs)
#   ./scripts/update.sh --dry-run    show what would happen, change nothing
#
# What changed                       → what happens
#   config/**                        → nothing: the orchestrator hot-reloads it
#   services/orchestrator/**         → zero-downtime deploy (runs migrations first)
#   services/functions/<name>/**     → zero-downtime deploy of that function
#   docker-compose.yml               → reconcile the stack (new/removed services)
#   anything else (docs, scripts)    → nothing
#
# Safety:
#   - fast-forward only: local edits on the server block the update instead of merging
#   - one run at a time (lock file)
#   - a commit whose deploy failed is not retried until a newer commit arrives
#     (deploy.sh already rolled the failing service back)
set -euo pipefail

cd "$(dirname "$0")/.."
DRY_RUN=false
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=true

STATE_DIR=".aurelia-state"
mkdir -p "$STATE_DIR"
exec 9>"$STATE_DIR/update.lock"
flock -n 9 || { echo "another update is running"; exit 0; }

log() { echo "[$(date '+%F %T')] $*"; }

BRANCH=$(git rev-parse --abbrev-ref HEAD)
git fetch --quiet origin "$BRANCH"
OLD=$(git rev-parse HEAD)
NEW=$(git rev-parse "origin/$BRANCH")

if [[ "$OLD" == "$NEW" ]]; then
  exit 0   # nothing new; stay quiet so the timer's log stays readable
fi
if [[ -f "$STATE_DIR/failed" && "$(cat "$STATE_DIR/failed")" == "$NEW" ]]; then
  exit 0   # this exact commit already failed to deploy; wait for a fix
fi
if ! git merge-base --is-ancestor "$OLD" "$NEW"; then
  log "!! origin/$BRANCH is not a fast-forward of the server's copy; not updating (fix by hand)"
  exit 1
fi

CHANGED=$(git diff --name-only "$OLD" "$NEW")
log "update ${OLD:0:7} → ${NEW:0:7}"
git log --oneline "$OLD..$NEW" | sed 's/^/    /'

# ---- work out the plan from the changed paths ------------------------------
DEPLOY=()
COMPOSE=false
CONFIG=false
while IFS= read -r path; do
  case "$path" in
    config/*)                     CONFIG=true ;;
    services/orchestrator/*)      DEPLOY+=("orchestrator") ;;
    services/functions/*/*)       fn=${path#services/functions/}; DEPLOY+=("${fn%%/*}") ;;
    docker-compose.yml)           COMPOSE=true ;;
  esac
done <<< "$CHANGED"
# services that failed last time are retried with the next commit, whatever it touches
if [[ -f "$STATE_DIR/pending" ]]; then
  while IFS= read -r s; do [[ -n "$s" ]] && DEPLOY+=("$s"); done < "$STATE_DIR/pending"
  log "retrying services that failed last time: $(tr '\n' ' ' < "$STATE_DIR/pending")"
fi

# unique, orchestrator first (its migrations run before anything that might rely on them)
ORDERED=()
for s in "${DEPLOY[@]}"; do [[ "$s" == "orchestrator" ]] && { ORDERED=("orchestrator"); break; }; done
for s in "${DEPLOY[@]}"; do
  [[ "$s" == "orchestrator" || " ${ORDERED[*]} " == *" $s "* ]] || ORDERED+=("$s")
done
DEPLOY=("${ORDERED[@]}")

$CONFIG  && log "config changed → hot-reloads, nothing to do"
$COMPOSE && log "docker-compose.yml changed → reconcile the stack"
for s in "${DEPLOY[@]}"; do log "service changed → deploy $s"; done
if ! $CONFIG && ! $COMPOSE && [[ ${#DEPLOY[@]} -eq 0 ]]; then
  log "only docs/scripts changed → nothing to deploy"
fi

if $DRY_RUN; then
  log "dry run: nothing applied"
  exit 0
fi

# ---- apply --------------------------------------------------------------------
git merge --ff-only --quiet "origin/$BRANCH"

fail() {
  echo "$NEW" > "$STATE_DIR/failed"
  printf '%s\n' "${REMAINING[@]}" > "$STATE_DIR/pending"   # the failed service and any not yet deployed
  log "!! $1 — stopped. The previous version keeps running. Push a fix to continue."
  exit 1
}

# Compose first when it changed: it may define a brand-new function to start.
REMAINING=("${DEPLOY[@]}")
if $COMPOSE; then
  docker compose up -d --build --remove-orphans || fail "docker compose up failed"
fi
REMAINING=("${DEPLOY[@]}")
for s in "${DEPLOY[@]}"; do
  if ! docker compose config --services | grep -qx "$s"; then
    log "   $s isn't in docker-compose.yml yet; skipping"
    continue
  fi
  ./scripts/deploy.sh "$s" || fail "deploy of $s failed and was rolled back"
  REMAINING=("${REMAINING[@]:1}")
done

rm -f "$STATE_DIR/failed" "$STATE_DIR/pending"
log "update complete: now at ${NEW:0:7}"
