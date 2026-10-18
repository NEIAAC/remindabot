#!/usr/bin/env bash
# Checks a BUILT remindabot image the way it will really run: as a container, on a Docker network,
# against a Mattermost. It never sends a message (see "SAFETY" below), so it is safe to run
# against a real server as well as a test one.
#
# USAGE (needs Docker access, so usually with sudo; with the dev stack running: docker compose up -d)
#   sudo scripts/check-image.sh                    # build this checkout and check it on the dev stack
#   sudo scripts/check-image.sh IMAGE              # check an image that already exists
#
#   Settings (environment variables), the defaults are the dev stack of compose.yaml:
#     NETWORK     the Docker network Mattermost is on        (chat-dev_default)
#     MM_URL      Mattermost as seen from that network       (http://mattermost:8065)
#     TOKEN_FILE  a file holding the bot's access token      (the dev stack's secret volume)
#     SITE_URL    public address used for links in messages  (http://localhost:8066)
#     BUILD       "no" to use IMAGE as it is, without building it from this checkout
#
# WHAT IT CHECKS
#   1. the image runs as a non-root user, has timezone data and the default message
#   2. bad configuration is refused with exit code 2 (not a crash loop with a traceback)
#   3. it can reach Mattermost with the token (--check) and reads the boards (--once --dry-run)
#   4. it works under a locked-down container: read-only filesystem, no capabilities,
#      no-new-privileges, writable state only in its /data volume
#   5. daemon mode: starts, becomes "healthy", answers on its chat-command port from another
#      container, and stops promptly on SIGTERM (docker stop)
#
# SAFETY
#   Daemon mode is started against a state volume where today's pass is already recorded as done,
#   so it cannot send anything. The other checks only use --check and --once --dry-run.

HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(dirname "$HERE")
IMAGE=${1:-remindabot:check}
NETWORK=${NETWORK:-chat-dev_default}
MM_URL=${MM_URL:-http://mattermost:8065}
SITE_URL=${SITE_URL:-http://localhost:8066}
TOKEN_FILE=${TOKEN_FILE:-}
BUILD=${BUILD:-yes}

PASS=0
FAIL=0
ok()   { echo "  PASS  $1"; PASS=$((PASS + 1)); }
bad()  { echo "  FAIL  $1"; FAIL=$((FAIL + 1)); }
check() { # check "description" command...   (passes when the command exits 0)
  local what=$1; shift
  if "$@" >/dev/null 2>&1; then ok "$what"; else bad "$what"; fi
}

SUFFIX=$$
VOLUME="remindabot-check-$SUFFIX"
DAEMON="remindabot-check-daemon-$SUFFIX"

# the hardened way to run it (everything docs/setup.md recommends)
HARDEN=(--read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges:true)

ENVFILE=""
cleanup() {
  rm -f "$ENVFILE"
  docker rm -f "$DAEMON" >/dev/null 2>&1
  docker volume rm "$VOLUME" >/dev/null 2>&1
}
trap cleanup EXIT

if [ "$BUILD" != "no" ]; then
  echo "== building $IMAGE from $REPO"
  docker build -q -t "$IMAGE" "$REPO" >/dev/null && echo "  built" || { echo "  BUILD FAILED"; exit 1; }
fi
docker image inspect "$IMAGE" >/dev/null 2>&1 || { echo "image $IMAGE not found"; exit 1; }
docker network inspect "$NETWORK" >/dev/null 2>&1 || { echo "network $NETWORK not found (is the dev stack up? docker compose up -d)"; exit 1; }

# the bot's token: from the file given, or from the dev stack's volume
if [ -n "$TOKEN_FILE" ]; then
  [ -r "$TOKEN_FILE" ] || { echo "cannot read token file $TOKEN_FILE"; exit 2; }
  TOKEN=$(cat "$TOKEN_FILE")
else
  TOKEN=$(docker run --rm -v "${NETWORK%_default}_secret-bot:/s:ro" --entrypoint cat "$IMAGE" /s/token 2>/dev/null) \
    || { echo "cannot read the dev stack's bot token (set TOKEN_FILE=...)"; exit 2; }
fi
[ -n "$TOKEN" ] || { echo "the bot's token is empty"; exit 2; }

# The container needs: where Mattermost is, where it is reachable publicly, and the token. The token
# goes in a private file (--env-file), not on the command line, where `ps` would show it.
ENVFILE=$(mktemp)
printf 'REMINDER_BOT_TOKEN=%s\n' "$TOKEN" > "$ENVFILE"
ENVS=(-e "REMINDER_MM_URL=$MM_URL" -e "REMINDER_SITE_URL=$SITE_URL" --env-file "$ENVFILE" -e "TZ=Europe/Lisbon")

echo "== 1. the image itself"
UID_IN=$(docker run --rm --entrypoint id "$IMAGE" -u 2>/dev/null)
[ -n "$UID_IN" ] && [ "$UID_IN" != "0" ] && ok "runs as a non-root user (uid $UID_IN)" || bad "runs as root"
check "has timezone data (Europe/Lisbon)" docker run --rm --entrypoint python "$IMAGE" -c "from zoneinfo import ZoneInfo; ZoneInfo('Europe/Lisbon')"
check "ships a default message at /config/message.txt" docker run --rm --entrypoint test "$IMAGE" -s /config/message.txt
check "Python 3.12 or newer" docker run --rm --entrypoint python "$IMAGE" -c "import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)"

echo "== 2. bad configuration is refused cleanly"
docker run --rm "$IMAGE" python -m remindabot --check >/dev/null 2>&1
[ $? -eq 2 ] && ok "missing token -> exit code 2" || bad "missing token should exit with code 2"
docker run --rm -e REMINDER_BOT_TOKEN=x -e REMINDER_SITE_URL=not-a-url "$IMAGE" python -m remindabot --check >/dev/null 2>&1
[ $? -eq 2 ] && ok "invalid REMINDER_SITE_URL -> exit code 2" || bad "invalid URL should exit with code 2"

echo "== 3. talking to Mattermost"
docker run --rm --network "$NETWORK" "${ENVS[@]}" "$IMAGE" python -m remindabot --check
[ $? -eq 0 ] && ok "--check: token works, teams listed, template valid" || bad "--check failed (see output above)"
DRY=$(docker run --rm --network "$NETWORK" "${ENVS[@]}" -e REMINDER_LOG_LEVEL=INFO "$IMAGE" python -m remindabot --once --dry-run 2>&1)
echo "$DRY" | grep -E "boards:" | sed 's/^/    /'
echo "$DRY" | grep -q "nothing was sent" && ok "--once --dry-run runs and reports what it WOULD send" || bad "dry-run did not finish normally"

echo "== 4. locked-down container (read-only filesystem, no capabilities)"
docker run --rm "${HARDEN[@]}" --entrypoint touch "$IMAGE" /srv/should-not-work >/dev/null 2>&1
[ $? -ne 0 ] && ok "the root filesystem really is read-only" || bad "the root filesystem is writable"
docker run --rm --network "$NETWORK" "${HARDEN[@]}" "${ENVS[@]}" "$IMAGE" python -m remindabot --once --dry-run >/dev/null 2>&1
[ $? -eq 0 ] && ok "--once --dry-run works when locked down" || bad "dry-run fails when locked down"
docker volume create "$VOLUME" >/dev/null
OUT=$(docker run --rm "${HARDEN[@]}" -v "$VOLUME:/data" --entrypoint python "$IMAGE" -c \
  "from pathlib import Path; from remindabot.store import Store; s=Store(Path('/data/state.sqlite')); print(s.claim('c','u','2026-01-01',7))" 2>&1)
[ "$OUT" = "new" ] && ok "state can be written to the /data volume when locked down" || bad "cannot write state: $OUT"

echo "== 5. daemon mode (cannot send: today is pre-marked as done)"
docker run --rm "${HARDEN[@]}" -v "$VOLUME:/data" -e TZ=Europe/Lisbon --entrypoint python "$IMAGE" -c \
  "from datetime import datetime; from zoneinfo import ZoneInfo; from pathlib import Path; from remindabot.store import Store; \
Store(Path('/data/state.sqlite')).set_last_run(datetime.now(ZoneInfo('Europe/Lisbon')).date().isoformat())" >/dev/null 2>&1
docker run -d --name "$DAEMON" --network "$NETWORK" "${HARDEN[@]}" -v "$VOLUME:/data" "${ENVS[@]}" "$IMAGE" >/dev/null
sleep 5
[ "$(docker inspect -f '{{.State.Running}}' "$DAEMON" 2>/dev/null)" = "true" ] && ok "daemon is running" || bad "daemon is not running"
docker logs "$DAEMON" 2>&1 | grep -q "started:" && ok "daemon logged its start" || bad "daemon did not log 'started'"
echo "    waiting for the health check (up to 150 s)..."
HEALTH=starting
for _ in $(seq 1 30); do
  HEALTH=$(docker inspect -f '{{.State.Health.Status}}' "$DAEMON" 2>/dev/null)
  [ "$HEALTH" = "healthy" ] && break
  sleep 5
done
[ "$HEALTH" = "healthy" ] && ok "container became healthy" || bad "container health is '$HEALTH'"
# the chat-command endpoint, as Mattermost would reach it: from another container, by name
ENDPOINT=$(docker run --rm --network "$NETWORK" --entrypoint python "$IMAGE" -c \
  "import urllib.request; print(urllib.request.urlopen('http://$DAEMON:8080/healthz', timeout=5).read().decode())" 2>&1)
[ "$ENDPOINT" = '"ok"' ] && ok "the chat-command endpoint answers from another container (:8080/healthz)" \
  || bad "the chat-command endpoint does not answer: $ENDPOINT"
START=$(date +%s)
docker stop "$DAEMON" >/dev/null 2>&1
ELAPSED=$(( $(date +%s) - START ))
CODE=$(docker inspect -f '{{.State.ExitCode}}' "$DAEMON" 2>/dev/null)
[ "$ELAPSED" -lt 8 ] && ok "stops promptly on SIGTERM (${ELAPSED}s)" || bad "slow to stop (${ELAPSED}s): SIGTERM is being ignored"
[ "$CODE" = "0" ] && ok "exits with code 0" || bad "exit code $CODE"

echo
echo "RESULT: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
