#!/usr/bin/env sh
# Run the web app's end-to-end journeys against the compose topology on
# PostgreSQL: the real images, two workers sharing one job queue, and Better
# Auth and operational state in Postgres. See compose.e2e.yaml.
#
# Usage: deploy/e2e.sh            (builds the images, runs, tears down)
#        KEEP=1 deploy/e2e.sh     (leaves the stack running afterwards)
#        NO_BUILD=1 deploy/e2e.sh (reuses cavman-e2e-{api,worker,web} images)
set -eu
here=$(cd "$(dirname "$0")" && pwd)
repo=$(dirname "$here")
export CAVMAN_E2E_DIR="${CAVMAN_E2E_DIR:-$(mktemp -d)}"
# The web container runs as an unprivileged user and writes the email outbox here.
chmod 0777 "$CAVMAN_E2E_DIR"
compose="docker compose -p cavman-e2e -f $here/compose.yaml -f $here/compose.e2e.yaml"

cleanup() {
  status=$?
  if [ "$status" -ne 0 ]; then $compose logs --no-color --tail 200 || true; fi
  if [ -z "${KEEP:-}" ]; then $compose down -v --remove-orphans >/dev/null 2>&1 || true; fi
  exit "$status"
}
trap cleanup EXIT

# NO_BUILD=1 uses images already built (cavman-e2e-api, -worker, -web).
$compose up -d $([ -n "${NO_BUILD:-}" ] && echo --no-build || echo --build) --wait --wait-timeout 300

# The worker must run under its seccomp profile (mode 2, a filter), not unconfined.
$compose exec -T worker grep -Eq '^Seccomp:[[:space:]]+2$' /proc/1/status \
  || { echo "worker container is not seccomp-filtered" >&2; exit 1; }

# The web app answers once it can reach the API; auth tables are created on first use.
tries=0
until curl -fsS -o /dev/null http://localhost:3100/; do
  tries=$((tries + 1))
  [ "$tries" -lt 60 ] || { echo "web app did not come up" >&2; exit 1; }
  sleep 2
done

cd "$repo/web"
# Chromium only: the browser matrix runs in the e2e job against the same web build.
CAVMAN_E2E_BASE_URL=http://localhost:3100 npx playwright test --project=chromium "$@"
