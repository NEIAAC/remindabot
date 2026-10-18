#!/usr/bin/env bash
# Prepares the dev stack (compose.yaml) so the bot can be tried on it: a Mattermost with the Boards
# plugin, an admin you can log in as, fake people, the bot account and demo boards. Runs as the
# `seed` service of compose.yaml, once; running it again does nothing.
#
# It uses only the HTTP API, the way you would by hand in the browser:
#   1. creates the first user (it becomes the system admin) and logs in,
#   2. installs and enables the Boards plugin (the version production runs),
#   3. creates the team "nei" and three ordinary users (alice, bob, carol; nobody can log in as them),
#   4. creates the bot account "remindabot" with an access token,
#   5. creates demo boards with cards due in the next days (right and wrong ways to fill in
#      "Remind Before") and lets the bot join the one it should read (what --join-boards does).
#
# What it leaves behind, for the other services of the stack (never real secrets):
#   $SECRETS_BOT/token       the bot's access token
#   $SECRETS_ADMIN/token     a personal access token of devadmin (only the one-off register step and
#                            the tests get this volume)
#   $SECRETS_ADMIN/dev-state.json   ids of what was created (the tests use it; its presence also
#                            means "already prepared")
#
# Settings (environment): MM_URL (default http://mattermost:8065), DEV_ADMIN_PASSWORD, SECRETS_BOT,
# SECRETS_ADMIN, BOARDS_BUNDLE (the plugin .tar.gz, baked into the toolbox image).
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
# shellcheck source=lib/mm.sh
. "$HERE/lib/mm.sh"

MM_URL=${MM_URL:-http://mattermost:8065}
ADMIN_USER=devadmin
ADMIN_PASSWORD=${DEV_ADMIN_PASSWORD:-devadmin-password}
SECRETS_BOT=${SECRETS_BOT:-/secrets/bot}
SECRETS_ADMIN=${SECRETS_ADMIN:-/secrets/admin}
BOARDS_BUNDLE=${BOARDS_BUNDLE:-/opt/boards.tar.gz}
TEAM_NAME=nei
BOT_NAME=remindabot
STATE_FILE=$SECRETS_ADMIN/dev-state.json

need curl jq od date

if [ -s "$STATE_FILE" ]; then
  step "the dev stack was already prepared; nothing to do"
  exit 0
fi

# --- helpers for the demo data ------------------------------------------------------------------

noon_utc_ms() {  # noon_utc_ms DAYS_FROM_TODAY: a date as the Boards date picker stores it,
                 # 12:00 UTC of the chosen day (the Lisbon calendar day)
  local today epoch
  today=$(TZ=Europe/Lisbon date +%Y-%m-%d)
  epoch=$(date -u -d "$today 12:00:00" +%s)
  echo $(( (epoch + $1 * 86400) * 1000 ))
}

USERS_JSON='{}'    # {"alice": "<id>", ...}, grown as the users are created
BOARD_ID=""        # the board made last, and its property ids:
P_DUE="" P_ASSIGN="" P_NOTIFY="" P_REMIND=""
BOARDS_JSON='{}'   # {"text": "<board id>", ...}

make_user() {  # make_user USERNAME
  local id
  id=$(user_id "$1")
  if [ -z "$id" ]; then
    api POST /api/v4/users "$(NAME=$1 PW="Dev-$(random_hex 12)" jq -n \
      '{username: env.NAME, email: (env.NAME + "@dev.local"), password: env.PW}')"   # nobody logs in as these
    must "create user $1"
    id=$(field .id)
  fi
  USERS_JSON=$(jq -c --arg n "$1" --arg id "$id" '.[$n] = $id' <<<"$USERS_JSON")
}

props_schema() {  # props_schema REMIND_TYPE [OPTIONS_JSON]: the property definitions the bot looks for
  jq -c -n --arg due "$P_DUE" --arg as "$P_ASSIGN" --arg no "$P_NOTIFY" --arg re "$P_REMIND" \
    --arg type "$1" --argjson options "${2:-[]}" '[
      {id: $due, name: "Due Date",      type: "date",        options: []},
      {id: $as,  name: "Assignees",     type: "multiPerson", options: []},
      {id: $no,  name: "Notifyees",     type: "multiPerson", options: []},
      {id: $re,  name: "Remind Before", type: $type,         options: $options}]'
}

make_view() {  # make_view BOARD_ID: a table view, as the Boards UI makes for every board; card
               # links need a view id. Best effort: without one the bot links to the board.
  local me now view
  api GET /api/v4/users/me
  me=$(field .id)
  now=$(( $(date +%s) * 1000 ))
  view=$(jq -c -n --arg id "$(random_hex 13)" --arg board "$1" --arg me "$me" --argjson now "$now" '[{
      id: $id, boardId: $board, parentId: $board, createdBy: $me, modifiedBy: $me, schema: 1,
      type: "view", title: "Tabela",
      fields: {viewType: "table", sortOptions: [], visiblePropertyIds: [], visibleOptionIds: [],
               hiddenOptionIds: [], collapsedOptionIds: [], filter: {operation: "and", filters: []},
               cardOrder: [], columnWidths: {}, columnCalculations: {}, kanbanCalculations: {},
               defaultTemplateId: ""},
      createAt: $now, updateAt: $now, deleteAt: 0}]')
  api POST "$BOARDS_API/boards/$1/blocks" "$view"
  ok || step "(could not add a view to board $1: HTTP $API_STATUS; its links will point at the board)"
}

make_board() {  # make_board KEY TITLE TYPE(O|P) PROPERTIES_JSON
  api POST "$BOARDS_API/boards" "$(jq -c -n --arg team "$TEAM_ID" --arg type "$3" --arg title "$2" \
    --argjson props "$4" '{teamId: $team, type: $type, title: $title, cardProperties: $props}')"
  must "create board '$2'"
  BOARD_ID=$(field .id)
  BOARDS_JSON=$(jq -c --arg k "$1" --arg id "$BOARD_ID" '.[$k] = $id' <<<"$BOARDS_JSON")
  make_view "$BOARD_ID"
}

new_properties() {  # fresh property ids for the next board
  P_DUE=$(random_hex 4) P_ASSIGN=$(random_hex 4) P_NOTIFY=$(random_hex 4) P_REMIND=$(random_hex 4)
}

make_card() {  # make_card "TITLE" [due=DAYS] [notifyees="a b"] [assignees="a"] [remind=TEXT]
               #                  [remind_json=JSON] [template=1]    (on the board made last)
  local title=$1 props='{}' template=false arg
  shift
  for arg in "$@"; do
    case $arg in
      due=*)         props=$(jq -c --arg id "$P_DUE" --argjson ms "$(noon_utc_ms "${arg#due=}")" \
                         '.[$id] = ({from: $ms} | tojson)' <<<"$props") ;;
      notifyees=*)   props=$(jq -c --arg id "$P_NOTIFY" --arg names "${arg#notifyees=}" --argjson users "$USERS_JSON" \
                         '.[$id] = ($names | split(" ") | map($users[.]))' <<<"$props") ;;
      assignees=*)   props=$(jq -c --arg id "$P_ASSIGN" --arg names "${arg#assignees=}" --argjson users "$USERS_JSON" \
                         '.[$id] = ($names | split(" ") | map($users[.]))' <<<"$props") ;;
      remind=*)      props=$(jq -c --arg id "$P_REMIND" --arg v "${arg#remind=}" '.[$id] = $v' <<<"$props") ;;
      remind_json=*) props=$(jq -c --arg id "$P_REMIND" --argjson v "${arg#remind_json=}" '.[$id] = $v' <<<"$props") ;;
      template=1)    template=true ;;
      *) die "make_card: unknown argument '$arg'" ;;
    esac
  done
  api POST "$BOARDS_API/boards/$BOARD_ID/cards?disable_notify=true" \
    "$(jq -c -n --arg t "$title" --argjson p "$props" --argjson tpl "$template" \
       '{title: $t, properties: $p} + (if $tpl then {isTemplate: true} else {} end)')"
  must "create card '$title'"
}

# --- the Mattermost side -----------------------------------------------------------------------

step "waiting for Mattermost"
wait_for_mattermost 240

step "creating the admin '$ADMIN_USER' and logging in"
if ! login "$ADMIN_USER" "$ADMIN_PASSWORD"; then
  TOKEN= api POST /api/v4/users "$(NAME=$ADMIN_USER PW=$ADMIN_PASSWORD jq -n \
    '{username: env.NAME, email: "admin@dev.local", password: env.PW}')"
  if [ "$API_STATUS" = 403 ]; then
    # Only the very first user can be created without logging in. So this server already has users:
    # its volumes survived an earlier run, and this password is not the admin's.
    die "this dev stack already holds data from an earlier run and '$ADMIN_PASSWORD' is not its admin's password.
It holds only throwaway data; remove it and start clean:   docker compose down -v && docker compose up -d"
  fi
  must "create the first user"
  login "$ADMIN_USER" "$ADMIN_PASSWORD" || die "cannot log in as $ADMIN_USER after creating it"
fi
ADMIN_TOKEN_SESSION=$TOKEN

install_boards "$BOARDS_BUNDLE"

step "creating the team and three users (alice, bob, carol)"
api GET "/api/v4/teams/name/$TEAM_NAME"
if ok; then
  TEAM_ID=$(field .id)
else
  api POST /api/v4/teams "$(jq -n --arg n "$TEAM_NAME" '{name: $n, display_name: "NEI", type: "O"}')"
  must "create the team"
  TEAM_ID=$(field .id)
fi
for name in alice bob carol; do
  make_user "$name"
  api POST "/api/v4/teams/$TEAM_ID/members" \
    "$(jq -n --arg t "$TEAM_ID" --arg u "$(jq -r --arg n "$name" '.[$n]' <<<"$USERS_JSON")" '{team_id: $t, user_id: $u}')"
done
# The admin is a person too: one demo card lists devadmin as a notifyee, so that whoever logs in with
# that account (the only login that exists) finds a real reminder from the bot.
api GET /api/v4/users/me
must "read the admin's own id"
ADMIN_ID=$(field .id)
USERS_JSON=$(jq -c --arg id "$ADMIN_ID" '.devadmin = $id' <<<"$USERS_JSON")

step "creating the bot account and its token"
BOT_ID=$(create_bot "$BOT_NAME" "Remindabot" "Due-date reminders for Mattermost Boards cards")
BOT_TOKEN=$(new_token "$BOT_ID" "dev stack")
api POST "/api/v4/teams/$TEAM_ID/members" "$(jq -n --arg t "$TEAM_ID" --arg u "$BOT_ID" '{team_id: $t, user_id: $u}')"
must "add the bot to the team"

step "waiting for the Boards plugin to start"
wait_for_boards "$TEAM_ID"

# --- the demo boards ---------------------------------------------------------------------------------

# Users, team and bot are get-or-create, but the demo boards are not: a run that stopped half way
# (the mark of "prepared" is written last) must not make a second copy of them.
api GET "$BOARDS_API/teams/$TEAM_ID/boards"
must "list the boards"
if jq -e 'map(select(.title == "Posts")) | length > 0' "$API_BODY" >/dev/null; then
  die "an earlier run stopped half way through the demo boards. Start clean:   docker compose down -v && docker compose up -d"
fi

step "creating the demo boards and cards"

# A. a correctly set up board: "Remind Before" is Text, numbers separated by ";"
new_properties
make_board text "Posts" O "$(props_schema text)"
make_card "Workshop de Python (5 dias, 7;3;1; o assignee e ignorado)" due=5 notifyees="alice bob" assignees=carol remind="7;3;1"
make_card "Sessao de fotos (3 dias, 7;3;1)" due=3 notifyees=carol remind="7;3;1"
make_card "Cartaz do evento (1 dia, '7; 3; 1' com espacos)" due=1 notifyees=alice remind="7; 3; 1"
make_card "Um so lembrete (8 dias, '12')" due=8 notifyees=bob remind="12"
make_card "Com gralha (5 dias, '7;tres;1': avisa e reporta 'tres')" due=5 notifyees=bob remind="7;tres;1"
make_card "Com virgulas (5 dias, '7,3,1': nada e enviado, e reportado)" due=5 notifyees=alice remind="7,3,1"
make_card "Com unidade (5 dias, '7 dias': nada e enviado, e reportado)" due=5 notifyees=carol remind="7 dias"
make_card "Mensagem para o devadmin (2 dias, 7;3;1): chega a quem entrar com esta conta" due=2 notifyees=devadmin remind="7;3;1"
make_card "Sem Notifyees (so Assignees: ninguem e avisado)" due=4 assignees=alice remind="7"
make_card "Muito cedo (40 dias, 7;3)" due=40 notifyees=alice remind="7;3"
make_card "Remind Before 0 (nunca)" due=5 notifyees=alice remind="0"
make_card "Remind Before -1 (nunca)" due=5 notifyees=alice remind="-1"
make_card "Sem Remind Before" due=5 notifyees=alice
make_card "Sem Due Date" notifyees=alice remind="7"
make_card "Ja passou (2 dias atras)" due=-2 notifyees=alice remind="7"
make_card "Cartao modelo (template)" due=5 notifyees=alice remind="7" template=1

# B. "Remind Before" as a Number: not Text, so the whole board is skipped with a warning
new_properties
make_board number "Remind Before como Numero (tipo errado)" O "$(props_schema number)"
make_card "Cartao num quadro com o tipo errado" due=5 notifyees=alice remind="7"

# C. the same with a Multi select
new_properties
OPTIONS=$(jq -c -n --arg a "$(random_hex 4)" --arg b "$(random_hex 4)" --arg c "$(random_hex 4)" \
  '[{id: $a, value: "1", color: "propColorDefault"}, {id: $b, value: "3", color: "propColorDefault"},
    {id: $c, value: "7", color: "propColorDefault"}]')
make_board multi "Remind Before como Multipla escolha (tipo errado)" O "$(props_schema multiSelect "$OPTIONS")"
make_card "Cartao num quadro com o tipo errado" due=5 notifyees=bob \
  remind_json="$(jq -c '[.[2].id, .[0].id]' <<<"$OPTIONS")"

# D. has the right properties but the bot has NOT joined it (an open board before --join-boards)
new_properties
make_board unjoined "Quadro novo (bot ainda nao entrou)" O "$(props_schema text)"
make_card "Cartao num quadro por entrar" due=5 notifyees=alice remind="7"

# E. a PRIVATE board with the properties: the bot can only read it if a board admin adds it
new_properties
make_board private "Quadro privado" P "$(props_schema text)"
make_card "Cartao privado" due=5 notifyees=carol remind="7"

# F. an unrelated board without the properties: must be ignored and never joined
new_properties
make_board unrelated "Outro quadro (sem propriedades)" O '[{"id":"n1","name":"Notes","type":"text","options":[]}]'
make_card "Irrelevante"

step "letting the bot join the board it should read (what --join-boards does)"
TOKEN=$BOT_TOKEN api POST "$BOARDS_API/boards/$(jq -r .text <<<"$BOARDS_JSON")/join"
must "the bot joins the Posts board"

# --- hand over -----------------------------------------------------------------------------------------

TOKEN=$ADMIN_TOKEN_SESSION
ADMIN_ACCESS_TOKEN=$(new_token "$ADMIN_ID" "dev stack")
mkdir -p "$SECRETS_BOT" "$SECRETS_ADMIN"
printf '%s' "$BOT_TOKEN" > "$SECRETS_BOT/token"
printf '%s' "$ADMIN_ACCESS_TOKEN" > "$SECRETS_ADMIN/token"
chmod 644 "$SECRETS_BOT/token" "$SECRETS_ADMIN/token"    # throwaway values; the bot's user must be able to read
# the "already prepared" mark is written last: a run that stopped half way starts over
jq -n --arg team "$TEAM_ID" --arg bot "$BOT_ID" --argjson users "$USERS_JSON" --argjson boards "$BOARDS_JSON" \
  '{team: $team, bot: $bot, users: $users, boards: $boards}' > "$STATE_FILE"
step "ready: log in at http://localhost:8066 as $ADMIN_USER (password: $ADMIN_PASSWORD)"
