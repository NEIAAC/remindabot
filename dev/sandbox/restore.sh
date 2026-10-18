#!/usr/bin/env bash
# Restores a database dump into the isolated sandbox, anonymizes it, and starts Mattermost.
#
#   sudo dev/sandbox/restore.sh /path/to/mattermost.dump
#
# Order matters and is the point of this script:
#   1. start ONLY the database (Mattermost is not running, so it cannot act on the data),
#   2. restore the dump,
#   3. scrub it: no real emails, no passwords, no sessions/tokens, no real names,
#   4. only then start Mattermost (and the gate that publishes it on 127.0.0.1:8067),
#   5. create a local admin, install Boards, create the bot (the `setup` service).
#
# Everything lives in Docker volumes named remindabot-sandbox_*; `docker compose -f
# dev/sandbox/compose.yaml --profile app down -v` removes them all (README.md in this folder).

set -euo pipefail
DUMP=${1:?usage: restore.sh /path/to/mattermost.dump}
HERE=$(cd "$(dirname "$0")" && pwd)
COMPOSE=(docker compose -f "$HERE/compose.yaml")
OWNER=${SUDO_USER:-root}
[ -s "$DUMP" ] || { echo "dump not found or empty: $DUMP"; exit 1; }

echo "== 1/5 starting the sandbox database"
"${COMPOSE[@]}" up -d db
for _ in $(seq 1 60); do
  "${COMPOSE[@]}" exec -T db pg_isready -U mmuser -d mattermost >/dev/null 2>&1 && break
  sleep 1
done

echo "== 2/5 restoring the dump"
# --clean/--if-exists makes a second run replace the first. Errors about missing roles are harmless.
"${COMPOSE[@]}" exec -T db pg_restore -U mmuser -d mattermost --no-owner --no-acl --clean --if-exists \
  < "$DUMP" 2>&1 | grep -v -E 'does not exist, skipping|^$' || true

echo "== 3/5 scrubbing personal data"
"${COMPOSE[@]}" exec -T db psql -U mmuser -d mattermost -v ON_ERROR_STOP=0 <<'SQL'
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
LEFT=$("${COMPOSE[@]}" exec -T db psql -U mmuser -d mattermost -tA -c \
  "SELECT (SELECT count(*) FROM users WHERE email NOT LIKE '%@sandbox.invalid')
        + (SELECT count(*) FROM sessions) + (SELECT count(*) FROM useraccesstokens)" 2>&1 | tr -d '[:space:]')
if [ "$LEFT" != "0" ]; then
  echo "!! the scrub is incomplete or failed (check result: '$LEFT'). Mattermost was NOT started."
  echo "   The database is still isolated; remove it with:"
  echo "   docker compose -f $HERE/compose.yaml --profile app down -v"
  exit 1
fi
echo "   verified: no real e-mail addresses, sessions or tokens remain"

echo "== 4/5 starting Mattermost (this takes a minute)"
"${COMPOSE[@]}" --profile app up -d mattermost gate
UP=no
for _ in $(seq 1 120); do
  if curl -fsS http://127.0.0.1:8067/api/v4/system/ping >/dev/null 2>&1; then UP=yes; break; fi
  sleep 2
done
[ "$UP" = yes ] || { echo "!! Mattermost did not answer; see: docker compose -f $HERE/compose.yaml logs mattermost"; exit 1; }
echo "   sandbox is up on http://127.0.0.1:8067"

echo "== 5/5 preparing it (a local admin, the Boards plugin, the bot)"
# the restored accounts have no passwords any more, so a local admin is created through mmctl's socket
ADMIN_PW="Sbx-$(od -An -N12 -tx1 /dev/urandom | tr -d ' \n')"
# (the password goes in the environment of the command, not on its command line, where `ps` would show it)
ADMIN_PW=$ADMIN_PW "${COMPOSE[@]}" exec -T -e ADMIN_PW mattermost sh -c \
  'mmctl --local user create --email admin@sandbox.invalid --username sandboxadmin --password "$ADMIN_PW" --system-admin' \
  >/dev/null
TOKEN=$(SANDBOX_ADMIN_PASSWORD=$ADMIN_PW "${COMPOSE[@]}" run --rm -T setup | tail -n 1)
[ -n "$TOKEN" ] || { echo "!! the setup step did not produce a bot token"; exit 1; }

# the bot's token (read by the `reminder` service) and the admin's password (to log in at :8067)
umask 077
printf '%s' "$TOKEN" > "$HERE/.sandbox-bot-token"
printf '%s' "$ADMIN_PW" > "$HERE/.sandbox-admin-password"
chmod 644 "$HERE/.sandbox-bot-token"        # the bot's container user must be able to read it
chown "$OWNER" "$HERE/.sandbox-bot-token" "$HERE/.sandbox-admin-password" 2>/dev/null || true

cat <<DONE

Ready. The sandbox is on http://127.0.0.1:8067 (user sandboxadmin, password in
dev/sandbox/.sandbox-admin-password). Try the bot on the copy of your real boards:

  docker compose -f dev/sandbox/compose.yaml run --rm reminder python -m remindabot --check
  docker compose -f dev/sandbox/compose.yaml run --rm reminder python -m remindabot --join-boards
  docker compose -f dev/sandbox/compose.yaml run --rm reminder python -m remindabot --once --dry-run

Remove it, and then the dump file too:

  docker compose -f dev/sandbox/compose.yaml --profile app --profile setup --profile tools down -v
DONE
