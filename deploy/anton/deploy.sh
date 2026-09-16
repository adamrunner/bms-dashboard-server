#!/usr/bin/env bash
#
# Deploy the current dev branch to the production host from a workstation.
#
# The three commands this wraps are not the hard part; the checks around them
# are. Every deploy runs ensure_database_schema() against the live database
# when the service starts, which is the one step in a deploy that cannot be
# undone by checking out the previous commit, so a snapshot is taken first.
# Nothing else here is clever: it pulls, rebuilds the image because
# Dockerfile.dashboard copies templates/ and static/ in, recreates only the
# named service so the broker keeps its MQTT session, and refuses to call the
# deploy finished until the service reports healthy.
#
# Usage:
#   deploy/anton/deploy.sh                        # deploy bms-dashboard to anton
#   deploy/anton/deploy.sh --service bms-logger
#   deploy/anton/deploy.sh --host anton.local     # LAN instead of Tailscale
#   deploy/anton/deploy.sh --skip-tests           # last resort, says so in the log
#
# Environment overrides:
#   BMS_DEPLOY_HOST     default anton
#   BMS_APP_DIR         default /home/adamrunner/bms-dashboard-server
#   BMS_BRANCH          default dev
#   BMS_HEALTH_PORT     default 5000
#   BMS_KEEP_SNAPSHOTS  pre-deploy snapshots retained, default 10

set -Eeuo pipefail

host="${BMS_DEPLOY_HOST:-anton}"
app_dir="${BMS_APP_DIR:-/home/adamrunner/bms-dashboard-server}"
branch="${BMS_BRANCH:-dev}"
health_port="${BMS_HEALTH_PORT:-5000}"
keep_snapshots="${BMS_KEEP_SNAPSHOTS:-10}"
service="bms-dashboard"
run_tests=1

while [[ $# -gt 0 ]]; do
    case "$1" in
        --host) host="$2"; shift 2 ;;
        --service) service="$2"; shift 2 ;;
        --skip-tests) run_tests=0; shift ;;
        -h|--help) sed -n '2,26p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "Unknown argument: $1" >&2; exit 2 ;;
    esac
done

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$repo_root"

say() { printf '\n==> %s\n' "$*"; }
fail() { printf '\nDeploy stopped: %s\n' "$*" >&2; exit 1; }

# --- Preflight, on this machine -------------------------------------------
# The tests below run against the working tree, so they only tell you anything
# about what is being deployed if the tree matches the commit the host pulls.

say "Checking the working tree"
if [[ -n "$(git status --porcelain)" ]]; then
    fail "uncommitted changes. Commit or stash them first."
fi

current_branch="$(git rev-parse --abbrev-ref HEAD)"
[[ "$current_branch" == "$branch" ]] || fail "on branch $current_branch, expected $branch."

git fetch --quiet origin "$branch"
local_head="$(git rev-parse HEAD)"
remote_head="$(git rev-parse "origin/$branch")"
if [[ "$local_head" != "$remote_head" ]]; then
    fail "HEAD ($(git rev-parse --short HEAD)) does not match origin/$branch ($(git rev-parse --short "origin/$branch")). Push or pull first."
fi

if (( run_tests )); then
    say "Running tests"
    python_bin="python3"
    if [[ -x .venv/bin/python ]]; then
        python_bin=".venv/bin/python"
    fi
    if ! "$python_bin" -c "import pytest" 2>/dev/null; then
        fail "pytest is not installed for $python_bin. Install it with '$python_bin -m pip install pytest', or pass --skip-tests."
    fi
    "$python_bin" -m pytest tests/ -q || fail "tests failed."
else
    say "Skipping tests (--skip-tests)"
fi

ssh -o BatchMode=yes -o ConnectTimeout=10 "$host" true \
    || fail "cannot reach $host over SSH."

# --- Snapshot the database ------------------------------------------------
# Plain cp is not safe against a live SQLite database; .backup is. The snapshot
# runs inside the dashboard container, as cleanup-stale-devices.sh does:
# backups/ is a bind mount owned by the container's user, and the SSH account
# cannot write to it.

say "Snapshotting the database on $host"
snapshot="$(ssh -o BatchMode=yes "$host" "APP_DIR='$app_dir' KEEP='$keep_snapshots' CONTAINER='bms-dashboard' bash -s" <<'REMOTE'
set -Eeuo pipefail
cd "$APP_DIR"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
name="bms_telemetry-pre-deploy-${stamp}.db"
docker exec "$CONTAINER" mkdir -p /app/data/backups
docker exec "$CONTAINER" sqlite3 /app/data/bms_telemetry.db ".backup '/app/data/backups/${name}'"
check="$(docker exec "$CONTAINER" sqlite3 "/app/data/backups/${name}" 'PRAGMA quick_check;')"
[[ "$check" == "ok" ]] || { echo "snapshot failed quick_check: $check" >&2; exit 1; }
# Keep only the newest pre-deploy snapshots. Snapshots labelled by hand for a
# migration or a cleanup do not match the glob and are left alone.
stale="$(docker exec "$CONTAINER" sh -c 'ls -1t /app/data/backups/bms_telemetry-pre-deploy-*.db 2>/dev/null' | tail -n +$((KEEP + 1)) || true)"
if [[ -n "$stale" ]]; then
    echo "$stale" | while read -r old; do docker exec "$CONTAINER" rm -f "$old"; done
fi
echo "backups/${name}"
REMOTE
)" || fail "could not snapshot the database. Nothing has been changed on $host."
echo "    $snapshot"

# --- Deploy ---------------------------------------------------------------

previous_sha="$(ssh -o BatchMode=yes "$host" "cd '$app_dir' && git rev-parse HEAD")"

say "Pulling and rebuilding $service on $host"
ssh -o BatchMode=yes "$host" "APP_DIR='$app_dir' BRANCH='$branch' SERVICE='$service' EXPECTED='$local_head' bash -s" <<'REMOTE' || fail "the pull or build failed. The running container was not replaced."
set -Eeuo pipefail
cd "$APP_DIR"
git pull --ff-only origin "$BRANCH"
actual="$(git rev-parse HEAD)"
if [[ "$actual" != "$EXPECTED" ]]; then
    echo "host is at $actual, expected $EXPECTED" >&2
    exit 1
fi
docker compose build "$SERVICE"
docker compose up -d "$SERVICE"
REMOTE

# --- Verify ---------------------------------------------------------------
# docker compose reports the container's own healthcheck, which is the same
# signal the boot unit relies on. For the dashboard, also confirm the API
# answers and that the logger is still writing telemetry behind it.

say "Waiting for $service to report healthy"
if ! ssh -o BatchMode=yes "$host" "APP_DIR='$app_dir' SERVICE='$service' PORT='$health_port' bash -s" <<'REMOTE'
set -Eeuo pipefail
cd "$APP_DIR"
deadline=$((SECONDS + 120))
while (( SECONDS < deadline )); do
    status="$(docker compose ps --format '{{.Status}}' "$SERVICE" || true)"
    if [[ "$status" == *"(healthy)"* ]]; then
        if [[ "$SERVICE" != "bms-dashboard" ]]; then
            exit 0
        fi
        health="$(curl -sf --max-time 5 "http://localhost:${PORT}/api/health" || true)"
        if [[ "$health" == *'"status":"healthy"'* ]]; then
            echo "$health"
            exit 0
        fi
    fi
    sleep 5
done
echo "timed out after 120s; last status: ${status:-unknown}" >&2
exit 1
REMOTE
then
    say "Health check failed. Recent logs:"
    ssh -o BatchMode=yes "$host" "cd '$app_dir' && docker compose logs --tail 40 '$service'" || true
    cat <<ROLLBACK

The new image is running but unhealthy. To go back to ${previous_sha:0:7}:

  ssh $host "cd $app_dir \\
    && git switch --detach $previous_sha \\
    && docker compose build $service \\
    && docker compose up -d $service"

Return the checkout to the branch afterwards with 'git switch $branch'.
If the database itself is wrong, the pre-deploy snapshot is at:

  $app_dir/$snapshot

ROLLBACK
    exit 1
fi

say "Deployed $(git rev-parse --short HEAD) to $host"
printf '    service:  %s\n' "$service"
printf '    previous: %s\n' "${previous_sha:0:7}"
printf '    snapshot: %s\n' "$snapshot"
