#!/usr/bin/env bash
# Practise the production deployment on a scrubbed COPY of production, and prove what it changed.
#
# What it builds is described in README.md (this folder): the real production docker-compose.yml, run
# unmodified in an isolated Docker project, on a copy of the production database with every real
# e-mail, password, session and name removed. You then type the deployment commands exactly as you
# will on the server, and `snapshot` + `compare` show everything they changed.
#
# USAGE (needs Docker Compose 2.24+, curl, git; run with sudo wherever Docker needs it)
#   rehearse.sh up BEFORE_COMPOSE DUMP      build the copy of production as it is NOW. BEFORE_COMPOSE is
#                                           the production docker-compose.yml without the reminder
#                                           bot; DUMP is the pg_dump of the production database (README.md)
#   rehearse.sh deploy AFTER_COMPOSE [local|published]
#                                           put the new docker-compose.yml in place, like `git pull`
#                                           on the server, and check that the server could get the bot.
#                                           Then YOU run `docker compose up -d` in work/. The bot comes
#                                           from this checkout (local, the default: use it before the
#                                           image is published) or exactly as the file says (published:
#                                           the image is pulled, as the server will)
#   rehearse.sh snapshot NAME               write down the state of the copy (rows per table, a
#                                           fingerprint of the boards, which containers run)
#   rehearse.sh compare NAME1 NAME2         what changed between two snapshots, and whether it was
#                                           expected. Exit code 1 if something that must not change did
#   rehearse.sh down                        remove the copy completely (containers, volumes, work/)
# (end of usage)
#
# Settings (environment): BOARDS_VERSION (the Boards plugin version production runs, default 9.1.3),
# REHEARSE_URL (default http://127.0.0.1:8068), REHEARSE_WORK (default: work/ next to this script).
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/../.." && pwd)
WORK=${REHEARSE_WORK:-$HERE/work}
URL=${REHEARSE_URL:-http://127.0.0.1:8068}
PROJECT=remindabot-rehearsal
OWNER=${SUDO_USER:-root}
DB=(-U mmuser -d mattermost)              # the user and database names of the production .env.example

# Tables whose CONTENT must be the same after the bot ran: it only reads boards and cards.
MUST_NOT_CHANGE="focalboard_blocks focalboard_boards teams"

usage() { sed -n '/^# USAGE/,/^# (end of usage)/p' "$0" | sed 's/^# \{0,1\}//; /^(end of usage)$/d'; exit 2; }
die()   { echo "ERROR: $*" >&2; exit 1; }
say()   { echo "== $*"; }

# Docker Compose, run in work/ so that it finds the production file and the .env there.
dc() { (cd "$WORK" && docker compose "$@"); }

hand_over() {  # the files were made by root (sudo): give them to the person who ran sudo
  chown -R "$OWNER" "$WORK" 2>/dev/null || true
}

compose_ok() {  # `!reset` in isolation.yaml needs Compose 2.24
  local v major rest minor
  v=$(docker compose version --short 2>/dev/null) || return 1
  v=${v#v}; major=${v%%.*}; rest=${v#*.}; minor=${rest%%.*}
  [ "$major" -gt 2 ] 2>/dev/null || { [ "$major" -eq 2 ] 2>/dev/null && [ "$minor" -ge 24 ]; }
}

wait_for() {  # wait_for SECONDS COMMAND...: true as soon as the command succeeds
  local seconds=$1 i; shift
  for ((i = 0; i < seconds; i++)); do "$@" >/dev/null 2>&1 && return 0; sleep 1; done
  return 1
}

set_compose_files() {  # set_compose_files a.yml:b.yml  (what `docker compose` in work/ will load)
  sed -i "s|^COMPOSE_FILE=.*|COMPOSE_FILE=$1|" "$WORK/.env"
}

provide_message() {  # the bot's message file sits next to the compose file in the infrastructure repository
  local src
  src=$(dirname "$1")/reminder/message.txt
  [ -f "$src" ] || src=$REPO/config/message.txt
  cp "$src" "$WORK/reminder/message.txt"
}

write_env() {  # the same variables as production's .env.example, with harmless values
  (umask 077; cat > "$WORK/.env" <<EOF
# Made by rehearse.sh for the rehearsal; nothing here is production's. Add the bot's token here
# (REMINDER_BOT_TOKEN) when the deployment steps tell you to, as you will in production's .env.
COMPOSE_PROJECT_NAME=$PROJECT
COMPOSE_FILE=docker-compose.yml:isolation.yaml
APP_PORT=8068
DOMAIN=localhost:8068
TZ=Europe/Lisbon
DB_USER=mmuser
DB_PASSWORD=rehearsal_only_password
DB_NAME=mattermost
MM_SQLSETTINGS_DRIVERNAME=postgres
MM_SQLSETTINGS_DATASOURCE=postgres://mmuser:rehearsal_only_password@postgres:5432/mattermost?sslmode=disable&connect_timeout=10
MM_SERVICESETTINGS_SITEURL=http://localhost:8068
MM_EMAILSETTINGS_SMTPSERVER=
REMINDER_BOT_TOKEN=
EOF
  )
}

# -- up -------------------------------------------------------------------------------------------

up_failed() {  # runs when `up` ends, however it ends
  local code=$?
  [ "$code" -eq 0 ] || echo "The rehearsal stopped early (see above). Remove what exists with:  $0 down   and start again." >&2
}

cmd_up() {
  local before=${1:-} dump=${2:-} clash
  [ -n "$before" ] && [ -n "$dump" ] || usage
  [ -s "$before" ] || die "no such compose file: $before"
  [ -s "$dump" ] || die "dump not found or empty: $dump"
  [ ! -e "$WORK" ] || die "a rehearsal exists already ($WORK). Remove it first:  $0 down"
  compose_ok || die "Docker Compose 2.24 or newer is needed ('docker compose version')"
  docker info >/dev/null 2>&1 || die "cannot talk to the Docker daemon: run this with sudo"
  clash=$(docker ps -a --format '{{.Names}}' | grep -E '^(chat-web|chat-db|chat-reminder)$' || true)
  [ -z "$clash" ] || die "containers named like production's exist on this machine (${clash//$'\n'/ }): the rehearsal must not run on the server"

  trap up_failed EXIT
  say "1/7 preparing work/ with the production compose file (copied, not edited)"
  mkdir -p "$WORK/snapshots" "$WORK/reminder"
  : > "$WORK/.rehearsal"
  cp "$before" "$WORK/docker-compose.yml"
  cp "$HERE/isolation.yaml" "$WORK/isolation.yaml"
  provide_message "$before"
  write_env
  hand_over
  dc config --quiet || die "Docker Compose does not accept the production file with the isolation layer (see above)"

  say "2/7 starting ONLY the database (Mattermost is not running, so it cannot act on the data)"
  dc up -d postgres
  wait_for 90 dc exec -T postgres pg_isready "${DB[@]}" || die "the database did not become ready"

  say "3/7 restoring the dump"
  # --clean/--if-exists makes a second run replace the first. Errors about missing roles are harmless.
  dc exec -T postgres pg_restore "${DB[@]}" --no-owner --no-acl --clean --if-exists \
    < "$dump" 2>&1 | grep -v -E 'does not exist, skipping|^$' || true

  say "4/7 scrubbing personal data"
  dc exec -T postgres psql "${DB[@]}" -v ON_ERROR_STOP=0 <<'SQL'
-- nobody can log in, nobody can be e-mailed, no device can be pushed to
UPDATE users SET password = '', authdata = NULL, authservice = '', mfaactive = false, mfasecret = '',
                 email = id || '@sandbox.invalid', emailverified = true;
-- real names are not needed to test reminders
UPDATE users SET username = 'u' || left(id, 10), nickname = '', firstname = '', lastname = ''
  WHERE NOT EXISTS (SELECT 1 FROM bots WHERE bots.userid = users.id);
DELETE FROM sessions;
DELETE FROM useraccesstokens;
DELETE FROM tokens;
DELETE FROM outgoingwebhooks;
DELETE FROM incomingwebhooks;
DELETE FROM commands;
DELETE FROM oauthapps;
DELETE FROM oauthaccessdata;
DELETE FROM oauthauthdata;
DELETE FROM preferences WHERE category IN ('notifications', 'push_notifications');
SQL
  # Do not trust the scrub, check it: if any real e-mail, session or token is left, stop here.
  local left
  left=$(dc exec -T postgres psql "${DB[@]}" -tA -c \
    "SELECT (SELECT count(*) FROM users WHERE email NOT LIKE '%@sandbox.invalid')
          + (SELECT count(*) FROM sessions) + (SELECT count(*) FROM useraccesstokens)" 2>&1 | tr -d '[:space:]')
  if [ "$left" != "0" ]; then
    echo "!! the scrub is incomplete or failed (check result: '$left'). Mattermost was NOT started." >&2
    echo "   The database is still isolated; remove it with:  $0 down" >&2
    exit 1
  fi
  echo "   verified: no real e-mail addresses, sessions or tokens remain"

  say "5/7 starting Mattermost (this takes a minute)"
  dc up -d mattermost gate
  wait_for 240 curl -fsS "$URL/api/v4/system/ping" || die "Mattermost did not answer; see:  cd $WORK && docker compose logs mattermost"

  say "6/7 a local admin and the Boards plugin (production already has the plugin; the bot is YOURS to create)"
  local admin_pw
  admin_pw="Rh-$(od -An -N12 -tx1 /dev/urandom | tr -d ' \n')"
  # (the password goes in the environment of the command, not on its command line, where `ps` shows it)
  ADMIN_PW=$admin_pw dc exec -T -e ADMIN_PW mattermost sh -c \
    'mmctl --local user create --email admin@sandbox.invalid --username sandboxadmin --password "$ADMIN_PW" --system-admin' \
    >/dev/null
  (umask 077; printf '%s' "$admin_pw" > "$WORK/.admin-password")
  docker build -q -t remindabot-toolbox:dev --build-arg "BOARDS_VERSION=${BOARDS_VERSION:-9.1.3}" "$REPO/dev/toolbox" >/dev/null
  SANDBOX_ADMIN_PASSWORD=$admin_pw docker run --rm --network "${PROJECT}_default" -v "$REPO/scripts:/scripts:ro" \
    -e MM_URL=http://mattermost:8065 -e CREATE_BOT=no -e SANDBOX_ADMIN_PASSWORD remindabot-toolbox:dev \
    bash /scripts/sandbox-setup.sh

  say "7/7 writing down the starting point"
  cmd_snapshot 00-start
  hand_over
  trap - EXIT
  cat <<DONE

Ready. This is production as it is today, minus the reminder bot.

  open it:    $URL   (user sandboxadmin, password in $WORK/.admin-password)
  next:       follow docs/setup.md. Where it says docker compose, do it in $WORK
              (cd $WORK), where the production file and its .env are.
  before the bot exists, prepare the board (Notifyees, Remind Before) in the browser, then:
              $0 snapshot ready
  to put the new compose file in place, like git pull on the server:
              $0 deploy /path/to/the/new/docker-compose.yml
  remove it all, and then the dump file too:
              $0 down
DONE
}

# -- deploy ---------------------------------------------------------------------------------------

cmd_deploy() {
  local after=${1:-} mode=${2:-local} files up_cmd image url ref cfg
  [ -n "$after" ] || usage
  [ -e "$WORK/.rehearsal" ] || die "there is no rehearsal here. Build one first:  $0 up BEFORE_COMPOSE DUMP"
  [ -s "$after" ] || die "no such compose file: $after"
  case $mode in local | published) ;; *) die "the second argument is 'local' or 'published', not '$mode'" ;; esac

  [ -e "$WORK/docker-compose.before.yml" ] || cp "$WORK/docker-compose.yml" "$WORK/docker-compose.before.yml"
  cp "$after" "$WORK/docker-compose.yml"
  provide_message "$after"
  say "what changes in docker-compose.yml"
  diff -u "$WORK/docker-compose.before.yml" "$WORK/docker-compose.yml" | sed '1,2d' || true

  files=docker-compose.yml:isolation.yaml
  up_cmd="docker compose up -d"
  if grep -q '^  reminder:' "$after"; then
    # where the file says the bot comes from: an image to pull, or a repository to build
    image=$(awk '/^  reminder:/ { p = 1; next } p && /^  [^ #]/ { p = 0 } p && /^    image:/ { print $2; exit }' "$after")
    url=$(sed -n 's/^ *build: *\(https:\/\/[^#[:space:]]*\)#\{0,1\}\([^[:space:]]*\).*$/\1/p' "$after" | head -n 1)
    ref=$(sed -n 's/^ *build: *https:\/\/[^#[:space:]]*#\([^[:space:]]*\).*$/\1/p' "$after" | head -n 1)
    if [ -n "$image" ]; then
      say "can the production server pull $image?"
      cfg=$(mktemp -d)                         # an empty Docker config: no login, like a server that never logged in
      if DOCKER_CONFIG=$cfg docker manifest inspect "$image" >/dev/null 2>&1; then
        echo "   yes: it can be pulled without a login"
      else
        echo "!! NO. That image cannot be pulled without a login: the version was not published yet (push the"
        echo "!! tag), or its package is private (make it public), or the name is wrong. Fix this BEFORE the"
        echo "!! real deployment, or the server needs 'docker login ghcr.io' (README.md, 'Before the real deployment')."
      fi
      rm -rf "$cfg"
    fi
    if [ -n "$url" ]; then
      say "can the production server build the bot from $url#${ref:-HEAD}?"
      if GIT_TERMINAL_PROMPT=0 git ls-remote --exit-code "$url" "${ref:-HEAD}" "refs/tags/${ref:-HEAD}" "refs/heads/${ref:-HEAD}" >/dev/null 2>&1; then
        echo "   yes: it can be read without credentials"
      else
        echo "!! NO. Without credentials that address cannot be read (a private repository, or a wrong"
        echo "!! branch/tag), so 'docker compose build' on the server would fail. Fix this BEFORE the real"
        echo "!! deployment (README.md, 'Before the real deployment')."
      fi
    fi
    if [ "$mode" = local ]; then
      printf '# made by rehearse.sh deploy: builds the bot from this checkout instead of getting it from where the file says\nservices:\n  reminder:\n    build:\n      context: %s\n' \
        "$REPO" > "$WORK/local-source.yaml"
      files=$files:local-source.yaml
      up_cmd="docker compose up -d --build"
      echo "   (the rehearsal builds the bot from $REPO; use 'published' to get it as the server will)"
    fi
  fi
  set_compose_files "$files"
  hand_over
  dc config --quiet || die "Docker Compose does not accept the new file with the isolation layer (see above)"
  cat <<DONE

The new file is in place. Now do it the way you will on the server:

  cd $WORK
  (add the bot's token to .env as REMINDER_BOT_TOKEN once you have created the bot)
  $up_cmd
DONE
}

# -- snapshot / compare ---------------------------------------------------------------------------

check_name() {
  case $1 in "" | *[!A-Za-z0-9._-]*) die "a snapshot name uses letters, digits, '.', '_' and '-' only" ;; esac
}

# Exact row counts of every table (count(*) per table, so no estimates).
COUNTS_SQL="SELECT table_name, (xpath('/row/c/text()', query_to_xml(format('SELECT count(*) AS c FROM %I.%I', table_schema, table_name), false, true, '')))[1]::text FROM information_schema.tables WHERE table_schema = 'public' AND table_type = 'BASE TABLE' ORDER BY 1"

cmd_snapshot() {
  local name=${1:-} file service id
  [ -n "$name" ] || usage
  check_name "$name"
  [ -e "$WORK/.rehearsal" ] || die "there is no rehearsal here"
  file=$WORK/snapshots/$name.txt
  [ ! -e "$file" ] || die "the snapshot '$name' exists already"
  local out counts hashes="" containers="" logs=""
  counts=$(dc exec -T postgres psql "${DB[@]}" -tA -F $'\t' -c "$COUNTS_SQL" | sed 's/^/T\t/')
  local table
  for table in $MUST_NOT_CHANGE; do
    if printf '%s\n' "$counts" | grep -q "^T"$'\t'"$table"$'\t'; then
      hashes+="H"$'\t'"$table"$'\t'"$(dc exec -T postgres psql "${DB[@]}" -tA -c \
        "SELECT md5(coalesce(string_agg(t::text, E'\n' ORDER BY t::text), '')) FROM $table t" | tr -d '[:space:]')"$'\n'
    fi
  done
  for id in $(dc ps -a -q); do
    containers+=$(docker inspect -f 'C{{"\t"}}{{index .Config.Labels "com.docker.compose.service"}}{{"\t"}}{{printf "%.12s" .Id}}{{"\t"}}{{.State.StartedAt}}{{"\t"}}{{.RestartCount}}{{"\t"}}{{.Config.Image}}' "$id")$'\n'
  done
  for service in $(dc ps -a --services); do
    logs+="L"$'\t'"$service"$'\t'"$(dc logs --no-color "$service" 2>&1 | grep -c -E '"level":"error"| ERROR |^ERROR:|FATAL' || true)"$'\n'
  done
  out=$counts$'\n'$hashes$containers$logs
  printf '%s\n' "$out" | sed '/^$/d' > "$file"
  hand_over
  echo "snapshot '$name': $(grep -c '^T' "$file") tables, $(grep -c '^C' "$file") containers -> $file"
}

cmd_compare() {
  local a=${1:-} b=${2:-} fa fb result
  [ -n "$a" ] && [ -n "$b" ] || usage
  check_name "$a"; check_name "$b"
  fa=$WORK/snapshots/$a.txt; fb=$WORK/snapshots/$b.txt
  [ -s "$fa" ] || die "no snapshot '$a' ($fa)"
  [ -s "$fb" ] || die "no snapshot '$b' ($fb)"
  result=$(awk -F '\t' -v must="$MUST_NOT_CHANGE" -v first="$a" -v second="$b" '
    BEGIN {
      split(must, m, " "); for (i in m) fixed[m[i]] = 1
      # what the bot and the setup steps are expected to add (and why)
      note["users"] = "the bot account (setup step 2)"
      note["bots"] = "the bot record"
      note["useraccesstokens"] = "the bot token"
      note["teammembers"] = "the bot joined the teams"
      note["channels"] = "direct-message channels opened by the bot"
      note["channelmembers"] = "members of those channels"
      note["channelmemberhistory"] = "members of those channels"
      note["posts"] = "the reminders (direct messages)"
      note["preferences"] = "direct messages shown in the sidebar"
      note["commands"] = "the /remind_* slash commands (--register-commands)"
      note["focalboard_board_members"] = "boards the bot joined (to read their cards)"
      note["systems"] = "Mattermost bookkeeping"; note["audits"] = "Mattermost bookkeeping"
      note["jobs"] = "Mattermost bookkeeping"; note["pluginkeyvaluestore"] = "plugin bookkeeping"
      note["status"] = "online status"; note["sessions"] = "logins"
      split("sessions status jobs audits", v, " "); for (i in v) volatile[v[i]] = 1
    }
    FNR == 1 { n++ }
    $1 == "T" { count[n, $2] = $3; seen[$2] = 1 }
    $1 == "H" { hash[n, $2] = $3; hashed[$2] = 1 }
    $1 == "C" { cid[n, $2] = $3; cstart[n, $2] = $4; crest[n, $2] = $5; cimg[n, $2] = $6; svc[$2] = 1 }
    $1 == "L" { errs[n, $2] = $3; lsvc[$2] = 1 }
    function out(section, key, text) { printf "%s\t%s\t%s\n", section, key, text }
    END {
      fails = 0; reviews = 0
      for (s in svc) {
        if (!((1, s) in cid)) out(1, s, sprintf("%-14s NEW (started during the rehearsal)", s))
        else if (!((2, s) in cid)) { out(1, s, sprintf("%-14s GONE", s)); fails++ }
        else if (cid[1, s] != cid[2, s]) out(1, s, sprintf("%-14s RECREATED (a new container: expected when its settings changed, with a short outage)", s))
        else if (cstart[1, s] != cstart[2, s] || crest[1, s] != crest[2, s]) out(1, s, sprintf("%-14s RESTARTED (%s restarts before, %s after)", s, crest[1, s], crest[2, s]))
        else out(1, s, sprintf("%-14s unchanged", s))
        if (((1, s) in cimg) && ((2, s) in cimg) && cimg[1, s] != cimg[2, s]) out(1, s "~", sprintf("%-14s IMAGE CHANGED %s -> %s", s, cimg[1, s], cimg[2, s]))
      }
      for (t in seen) {
        inA = ((1, t) in count); inB = ((2, t) in count)     # (asking first: reading a missing key makes it)
        if (!inA) { out(2, t, sprintf("%-34s NEW TABLE (%s rows)  REVIEW", t, count[2, t])); reviews++; continue }
        if (!inB) { out(2, t, sprintf("%-34s TABLE GONE  FAIL", t)); fails++; continue }
        x = count[1, t]; y = count[2, t]
        if (x == y) continue
        d = y - x
        if (t in fixed) { out(2, t, sprintf("%-34s %s -> %s (%+d)  FAIL: this must not change", t, x, y, d)); fails++ }
        else if (d < 0 && (t in volatile)) out(2, t, sprintf("%-34s %s -> %s (%+d)  ok: these expire by themselves", t, x, y, d))
        else if (d < 0) { out(2, t, sprintf("%-34s %s -> %s (%+d)  FAIL: rows were lost", t, x, y, d)); fails++ }
        else if (t in note) out(2, t, sprintf("%-34s %s -> %s (%+d)  expected: %s", t, x, y, d, note[t]))
        else { out(2, t, sprintf("%-34s %s -> %s (%+d)  REVIEW: not on the expected list", t, x, y, d)); reviews++ }
      }
      for (t in hashed) {
        if (hash[1, t] == "" || hash[2, t] == "") continue
        if (hash[1, t] == hash[2, t]) out(3, t, sprintf("%-34s identical", t))
        else { out(3, t, sprintf("%-34s CONTENT CHANGED  FAIL", t)); fails++ }
      }
      for (s in lsvc) {
        d = errs[2, s] - errs[1, s]
        out(4, s, sprintf("%-14s error lines: %s -> %s%s", s, errs[1, s] == "" ? "-" : errs[1, s], errs[2, s] == "" ? "-" : errs[2, s], d > 0 ? "  REVIEW: " d " new" : ""))
        if (d > 0) reviews++
      }
      out(5, "z", sprintf("VERDICT %s (%d failure(s), %d to review)", fails ? "FAIL" : (reviews ? "REVIEW" : "OK"), fails, reviews))
    }' "$fa" "$fb" | sort -s -t $'\t' -k1,1n -k2,2 | awk -F '\t' -v first="$a" -v second="$b" '
      BEGIN { h[1] = "containers"; h[2] = "rows per table (only what changed)"; h[3] = "boards and teams: fingerprint of their content"
              h[4] = "error lines in the container logs"; h[5] = "result"
              printf "compare %s -> %s\n", first, second }
      $1 != last { printf "\n== %s\n", h[$1]; last = $1 }
      { print "   " $3 }')
  printf '%s\n' "$result"
  case $result in *"VERDICT FAIL"*) return 1 ;; esac
}

# -- down -----------------------------------------------------------------------------------------

cmd_down() {
  [ -e "$WORK/.rehearsal" ] || die "there is no rehearsal here ($WORK)"
  say "removing the containers, the network and the volumes"
  dc down -v --remove-orphans || true
  rm -rf "$WORK"                       # only ever a folder that rehearse.sh itself made (it checked the marker file)
  echo "removed. Delete the dump file too."
}

case ${1:-} in
  up)       shift; cmd_up "$@" ;;
  deploy)   shift; cmd_deploy "$@" ;;
  snapshot) shift; cmd_snapshot "$@"; hand_over ;;
  compare)  shift; cmd_compare "$@" ;;
  down)     shift; cmd_down ;;
  *)        usage ;;
esac
