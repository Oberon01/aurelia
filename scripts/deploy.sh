#!/usr/bin/env bash
# Zero-downtime update of one service.
#
#   ./scripts/deploy.sh orchestrator
#   ./scripts/deploy.sh probe      (or any function: nebula, orbit, …)
#
# 1. Build the new image.
# 2. Start a second container next to the running one.
# 3. Wait until the new one reports healthy (Traefik only routes to healthy containers).
# 4. Stop and remove the old one; its in-flight requests finish during the grace period.
# If the new container never becomes healthy, it is removed and the old one keeps serving.
set -euo pipefail

SERVICE="${1:?usage: deploy.sh <service>}"
TIMEOUT="${HEALTH_TIMEOUT:-90}"
cd "$(dirname "$0")/.."

echo "==> building $SERVICE"
docker compose build "$SERVICE"

if [[ "$SERVICE" == "orchestrator" ]]; then
  echo "==> applying migrations (expand-only, so the running version keeps working)"
  docker compose build migrate
  docker compose run --rm migrate
fi

OLD=$(docker compose ps -q "$SERVICE" || true)
if [[ -z "$OLD" ]]; then
  echo "==> $SERVICE not running; starting it"
  docker compose up -d --no-deps "$SERVICE"
  exit 0
fi
COUNT=$(echo "$OLD" | wc -l | tr -d ' ')

echo "==> starting new $SERVICE next to the old one"
docker compose up -d --no-deps --no-recreate --scale "$SERVICE=$((COUNT + 1))" "$SERVICE"
NEW=$(comm -13 <(echo "$OLD" | sort) <(docker compose ps -q "$SERVICE" | sort))

echo "==> waiting up to ${TIMEOUT}s for new container to be healthy"
for ((i = 0; i < TIMEOUT; i += 2)); do
  STATUS=$(docker inspect -f '{{.State.Health.Status}}' $NEW 2>/dev/null || echo "missing")
  if [[ "$STATUS" == "healthy" ]]; then
    echo "==> healthy; retiring old container"
    docker stop $OLD >/dev/null
    docker rm $OLD >/dev/null
    docker compose up -d --no-deps --no-recreate --scale "$SERVICE=$COUNT" "$SERVICE"
    echo "==> $SERVICE updated with no downtime"
    exit 0
  fi
  if [[ "$STATUS" == "unhealthy" || "$STATUS" == "missing" ]]; then
    break
  fi
  sleep 2
done

echo "!! new $SERVICE failed its health check; rolling back" >&2
docker logs --tail 50 $NEW >&2 || true
docker stop $NEW >/dev/null || true
docker rm $NEW >/dev/null || true
exit 1
