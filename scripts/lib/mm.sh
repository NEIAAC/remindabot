# Helpers for the scripts that talk to Mattermost and Boards over HTTP. Needs bash, curl and jq.
# This file is sourced by the other scripts; it is never run on its own.
#
# SECRETS: a token or a password given to a program on its command line is visible to every user of
# the machine in `ps` while it runs. So nothing secret is ever an argument here: the bearer token goes
# to curl through its standard input (`-K -`, a config read from stdin), a request body goes through a
# private temporary file, and passwords reach jq through the environment.
#
# The convention: `api METHOD PATH [JSON]` does one call and leaves the result in
#   $API_STATUS   the HTTP status ("000" when the server could not be reached)
#   $API_BODY     the name of a file holding the response body (read it with `field`, or `cat`)
# It never exits on an HTTP error: the caller decides (`must "what"` stops with a clear message).
# The bearer token of the call is $TOKEN; set it with `login`, or from a personal access token.

MM_URL=${MM_URL:-http://localhost:8065}
BOARDS_API=/plugins/focalboard/api/v2

die()  { echo "ERROR: $*" >&2; exit 1; }
step() { echo "-- $*" >&2; }

need() {  # need curl jq ...: stop at once, with the name, if a program is missing
  local program
  for program in "$@"; do
    command -v "$program" >/dev/null 2>&1 || die "'$program' is required but not installed"
  done
}

# Temporary files for the last request and response (mktemp makes them private); removed at the end.
API_BODY=$(mktemp)
API_HEADERS=$(mktemp)
API_REQUEST=$(mktemp)
trap 'rm -f "$API_BODY" "$API_HEADERS" "$API_REQUEST"' EXIT
API_STATUS=000
TOKEN=${TOKEN-}

api() {
  local method=$1 path=$2 data=${3-} args
  args=(-sS --max-time "${API_TIMEOUT:-60}" -X "$method" -o "$API_BODY" -D "$API_HEADERS" -w '%{http_code}'
        -H 'X-Requested-With: XMLHttpRequest'                  # the Boards plugin insists on this header
        -K -)                                                  # more settings on stdin: the token, below
  if [ -n "$data" ]; then
    printf '%s' "$data" > "$API_REQUEST"                       # a body can hold a password: not an argument
    args+=(-H 'Content-Type: application/json' --data-binary "@$API_REQUEST")
  fi
  API_STATUS=$({ [ -z "$TOKEN" ] || printf 'header = "Authorization: Bearer %s"\n' "$TOKEN"; } \
               | curl "${args[@]}" "$MM_URL$path" 2>/dev/null) || API_STATUS=000
}

ok()    { [[ $API_STATUS == 2* ]]; }
field() { jq -r "$1" "$API_BODY"; }                              # field '.id'

must() {  # must "what was being done": stop with the server's answer unless the last call succeeded
  ok || die "$1 failed (HTTP $API_STATUS): $(head -c 300 "$API_BODY" | tr '\n' ' ')"
}

wait_for_mattermost() {  # wait_for_mattermost [SECONDS]
  local deadline=$((SECONDS + ${1:-180}))
  while [ "$SECONDS" -lt "$deadline" ]; do
    api GET /api/v4/system/ping
    [ "$API_STATUS" = 200 ] && return 0
    sleep 2
  done
  die "Mattermost does not answer on $MM_URL"
}

login() {  # login USER PASSWORD: sets TOKEN to the session token; returns 1 if the login is refused
  TOKEN= api POST /api/v4/users/login \
    "$(LOGIN_ID=$1 LOGIN_PASSWORD=$2 jq -n '{login_id: env.LOGIN_ID, password: env.LOGIN_PASSWORD}')"
  [ "$API_STATUS" = 200 ] || return 1
  TOKEN=$(awk 'tolower($1) == "token:" {print $2}' "$API_HEADERS" | tr -d '\r')
  [ -n "$TOKEN" ]
}

random_hex() {  # random_hex BYTES -> twice as many hex characters (no pipe that can die of SIGPIPE)
  od -An -N"$1" -tx1 /dev/urandom | tr -d ' \n'
}

install_boards() {  # install_boards BUNDLE.tar.gz: upload and enable the Boards plugin (a no-op if active)
  api GET /api/v4/plugins
  must "list the plugins"
  if jq -e '(.active // []) | map(.id) | index("focalboard") != null' "$API_BODY" >/dev/null; then
    step "Boards plugin already active"
    return 0
  fi
  [ -s "$1" ] || die "the Boards plugin bundle $1 is missing"
  step "installing the Boards plugin ($(du -h "$1" | cut -f1))"
  API_STATUS=$(printf 'header = "Authorization: Bearer %s"\n' "$TOKEN" \
               | curl -sS --max-time 300 -K - -o "$API_BODY" -w '%{http_code}' -H 'X-Requested-With: XMLHttpRequest' \
                      -F "plugin=@$1;type=application/gzip" "$MM_URL/api/v4/plugins?force=true" 2>/dev/null) \
    || API_STATUS=000
  must "upload the Boards plugin"
  local id
  id=$(field .id)
  api POST "/api/v4/plugins/$id/enable"
  must "enable the Boards plugin"
  step "Boards plugin $(jq -r '.version // empty' "$API_BODY" 2>/dev/null) enabled"
}

wait_for_boards() {  # wait_for_boards TEAM_ID: the plugin needs a few seconds after being enabled
  local deadline=$((SECONDS + 120))
  while [ "$SECONDS" -lt "$deadline" ]; do
    api GET "$BOARDS_API/teams/$1/boards"
    [ "$API_STATUS" = 200 ] && return 0
    sleep 2
  done
  die "the Boards plugin does not answer (System Console > Plugins shows why)"
}

user_id() {  # user_id USERNAME -> prints the id, or nothing if there is no such user
  api GET "/api/v4/users/username/$1"
  ok && field .id
  return 0
}

create_bot() {  # create_bot USERNAME DISPLAY_NAME DESCRIPTION -> prints the bot's user id
  local id
  api POST /api/v4/bots "$(jq -n --arg u "$1" --arg d "$2" --arg x "$3" '{username: $u, display_name: $d, description: $x}')"
  if [ "$API_STATUS" = 201 ]; then
    field .user_id
  else                                                          # already there from an earlier run
    id=$(user_id "$1")
    [ -n "$id" ] || die "cannot create or find the bot account '$1' (HTTP $API_STATUS)"
    echo "$id"
  fi
}

new_token() {  # new_token USER_ID DESCRIPTION -> prints a new personal access token
  api POST "/api/v4/users/$1/tokens" "$(jq -n --arg d "$2" '{description: $d}')"
  must "create an access token"
  field .token
}
