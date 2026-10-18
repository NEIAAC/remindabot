#!/usr/bin/env bash
# Prepares the restored sandbox (dev/sandbox) so the bot can be tried on a copy of the real boards.
# Run by dev/sandbox/restore.sh as the `setup` service, after the dump was restored and scrubbed.
#
# It does what you would do by hand in the System Console:
#   1. logs in as the sandbox admin that restore.sh created (the restored accounts have no passwords),
#   2. installs and enables the Boards plugin (the version production runs),
#   3. creates the bot account and a token for it,
#   4. adds the bot to every team.
# The bot's token is the ONLY thing written to stdout (restore.sh stores it); progress goes to stderr.
#
# Settings (environment): MM_URL (default http://mattermost:8065), SANDBOX_ADMIN_PASSWORD (required),
# BOARDS_BUNDLE (the plugin .tar.gz, baked into the toolbox image).
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=lib/mm.sh
. "$HERE/lib/mm.sh"

MM_URL=${MM_URL:-http://mattermost:8065}
BOARDS_BUNDLE=${BOARDS_BUNDLE:-/opt/boards.tar.gz}
: "${SANDBOX_ADMIN_PASSWORD:?SANDBOX_ADMIN_PASSWORD is required}"
need curl jq od

wait_for_mattermost 240
login sandboxadmin "$SANDBOX_ADMIN_PASSWORD" || die "cannot log in as sandboxadmin (HTTP $API_STATUS): $(cat "$API_BODY")"
step "logged in as sandboxadmin"

install_boards "$BOARDS_BUNDLE"

BOT_ID=$(create_bot remindabot Remindabot "Due-date reminders for Mattermost Boards cards")
BOT_TOKEN=$(new_token "$BOT_ID" sandbox)

api GET "/api/v4/teams?per_page=200"
must "list the teams"
teams=$(jq -r '.[].id' "$API_BODY")
count=0
for team in $teams; do
  api POST "/api/v4/teams/$team/members" "$(jq -n --arg t "$team" --arg u "$BOT_ID" '{team_id: $t, user_id: $u}')"
  count=$((count + 1))
done
step "bot 'remindabot' created and added to $count team(s)"

printf '%s\n' "$BOT_TOKEN"
