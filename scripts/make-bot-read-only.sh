#!/usr/bin/env bash
# Make the reminder bot READ-ONLY on every board it is a member of.
#
# WHY
#   When the bot joins an open board by itself, Boards gives it the board's default member role,
#   which is "Editor". The bot never edits anything, but a leaked bot token would then be able to.
#   This sets the bot's role on each of its boards to "Viewer": it can still read cards, list
#   members and send direct messages (all it needs), and it can no longer edit or create cards.
#   Joining a board again never escalates a Viewer back to Editor.
#
# WHO RUNS IT
#   A system administrator, by hand, now and then (for example after new boards appeared and the
#   bot joined them). It is deliberately NOT part of the bot or its container image: it needs an
#   admin token, which the bot's runtime must never hold.
#
# HOW IT WORKS (verified against Mattermost 10.9.1 / Boards 9.1.3)
#   * Only a BOARD admin can change a member's role, and a system admin is not automatically one.
#   * But a system admin who JOINS a board becomes a board admin of it. So for each board where the
#     bot is not yet a Viewer, this joins the board with the admin token (private boards need the
#     `allow_admin` flag) and then sets the bot's role. Side effect: the admin account becomes a
#     member of those boards.
#
# USAGE (needs bash, curl and jq)
#   export REMINDER_BOT_TOKEN=...     # the bot's token (to find the boards it belongs to)
#   export ADMIN_TOKEN=...            # a personal access token of a SYSTEM ADMIN
#   scripts/make-bot-read-only.sh https://chat.example.com           # only shows the plan
#   scripts/make-bot-read-only.sh https://chat.example.com --apply   # does it
#
#   The tokens are read from the environment so they never appear in shell history or process
#   lists. On the server itself use its internal address (http://localhost:8065, or the container
#   name on its Docker network).
#
# Exit code: 0 = done (or nothing to do), 1 = some board could not be changed, 2 = bad usage.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=lib/mm.sh
. "$HERE/lib/mm.sh"

usage() { sed -n '/^# USAGE/,/^# Exit code/p' "$0" | sed 's/^# \{0,1\}//'; exit 2; }

MM_URL=${1:-}
APPLY=no
[ -n "$MM_URL" ] || usage
case ${2:-} in
  "")      ;;
  --apply) APPLY=yes ;;
  *)       usage ;;
esac
MM_URL=${MM_URL%/}
need curl jq
BOT_TOKEN=${REMINDER_BOT_TOKEN:-}
ADMIN_TOKEN=${ADMIN_TOKEN:-}
[ -n "$BOT_TOKEN" ] && [ -n "$ADMIN_TOKEN" ] || { echo "Set REMINDER_BOT_TOKEN and ADMIN_TOKEN in the environment." >&2; exit 2; }

TOKEN=$ADMIN_TOKEN api GET /api/v4/users/me
ok && jq -e '.roles | split(" ") | index("system_admin") != null' "$API_BODY" >/dev/null \
  || die "ADMIN_TOKEN must belong to a system administrator."

# The boards the BOT is a member of, one JSON object per line:
#   {"id", "title", "type", "viewer": true|false}
# The bot sees its own boards plus the open boards of its teams, so membership is decided by whether
# it can list the members (a non-member gets 403).
TOKEN=$BOT_TOKEN api GET /api/v4/users/me
ok || die "the bot token does not work (HTTP $API_STATUS)."
BOT_ID=$(field .id)
TOKEN=$BOT_TOKEN api GET /api/v4/users/me/teams
ok || die "cannot list the bot's teams (HTTP $API_STATUS)."
TEAMS=$(jq -r '.[].id' "$API_BODY")

BOARDS=""
for team in $TEAMS; do
  TOKEN=$BOT_TOKEN api GET "$BOARDS_API/teams/$team/boards"
  ok || continue
  rows=$(jq -c '.[] | select((.deleteAt // 0) == 0) | {id, title: (.title // .id), type: (.type // "")}' "$API_BODY")
  [ -n "$rows" ] || continue
  while IFS= read -r row; do
    TOKEN=$BOT_TOKEN api GET "$BOARDS_API/boards/$(jq -r .id <<<"$row")/members"
    ok || continue                                   # not a member of this board
    line=$(jq -c --arg me "$BOT_ID" --argjson board "$row" '
      (map(select(.userId == $me)) | first) as $mine
      | if $mine == null then empty
        else $board + {viewer: (($mine.schemeViewer // false) and (($mine.schemeEditor // false) | not)
                                and (($mine.schemeAdmin // false) | not))} end' "$API_BODY")
    [ -z "$line" ] || BOARDS+="$line"$'\n'
  done <<<"$rows"
done

total=$(printf '%s' "$BOARDS" | grep -c . || true)
todo=$(printf '%s' "$BOARDS" | jq -c 'select(.viewer | not)' | grep -c . || true)
echo "The bot is a member of $total board(s); $todo still need to be made read-only."
printf '%s' "$BOARDS" | jq -r '"  " + (if .viewer then "already read-only" else "EDITOR RIGHTS     " end) + "  " + .title + "  (" + .type + ")"'
[ "$todo" -gt 0 ] || exit 0
if [ "$APPLY" != yes ]; then
  echo
  echo "Nothing was changed. Run again with --apply to do it."
  exit 0
fi

VIEWER_ONLY='{"schemeAdmin": false, "schemeEditor": false, "schemeCommenter": false, "schemeViewer": true}'
failures=0
while IFS= read -r board; do
  id=$(jq -r .id <<<"$board"); title=$(jq -r .title <<<"$board"); type=$(jq -r .type <<<"$board")
  flag=""
  [ "$type" = "O" ] || flag="?allow_admin=true"      # a private board needs this for a system admin to join
  TOKEN=$ADMIN_TOKEN api POST "$BOARDS_API/boards/$id/join$flag"
  if ! ok; then
    echo "  FAILED  $title: the admin could not join the board (HTTP $API_STATUS)"
    failures=$((failures + 1))
    continue
  fi
  TOKEN=$ADMIN_TOKEN api PUT "$BOARDS_API/boards/$id/members/$BOT_ID" "$VIEWER_ONLY"
  if ok; then
    echo "  done    $title"
  else
    echo "  FAILED  $title: could not change the role (HTTP $API_STATUS)"
    failures=$((failures + 1))
  fi
done < <(printf '%s' "$BOARDS" | jq -c 'select(.viewer | not)')
echo
echo "$((todo - failures)) board(s) changed, $failures failed. The admin account is now a member of the changed boards."
[ "$failures" -eq 0 ]
