# Trying the bot on a copy of your real database

This folder builds a **throwaway sandbox** from a dump of your production database, so you can see
exactly what the bot would do with your real boards, with no possibility of contacting a real
person or touching production.

## What protects you

| Risk | Protection |
| --- | --- |
| The sandbox e-mails or pushes to a real person | E-mail and push notifications are switched off, no SMTP server is configured, **and** the containers have no route to the internet at all (`internal` Docker network). |
| Someone logs in with a real account | Passwords, sessions, access tokens and OAuth data are deleted. |
| Real names / addresses end up in logs and screenshots | E-mail addresses become `<id>@sandbox.invalid`; usernames become `u<id>`; first/last names and nicknames are cleared. |
| Private messages are copied around | The dump command below leaves out **all message content** (posts, files, reactions, drafts...). Only users, teams, channels and boards are needed. |
| The scrub silently fails | `restore.sh` checks the result and **refuses to start Mattermost** if any real e-mail, session or token is left. |
| The sandbox stays around | One command (step 5) removes every container, network and volume. |

It is still personal data about real people, so treat the dump file like the production database:
keep it on this machine only, and delete it when you are done. Card titles and board names are
kept as they are (they are what the reminders are about).

## Steps

### 1. Make the dump (on the production server)

This takes a consistent snapshot without stopping anything and leaves out message content:

```shell
docker exec chat-db pg_dump -U mmuser -d mattermost --format=custom --no-owner --no-acl \
  --exclude-table-data='posts' --exclude-table-data='postspriority' \
  --exclude-table-data='postacknowledgements' --exclude-table-data='postreminders' \
  --exclude-table-data='reactions' --exclude-table-data='fileinfo' --exclude-table-data='drafts' \
  --exclude-table-data='threads' --exclude-table-data='threadmemberships' \
  --exclude-table-data='linkmetadata' --exclude-table-data='emoji' \
  --exclude-table-data='sessions' --exclude-table-data='useraccesstokens' --exclude-table-data='tokens' \
  --exclude-table-data='status' --exclude-table-data='audits' --exclude-table-data='jobs' \
  --exclude-table-data='uploadsessions' --exclude-table-data='compliances' \
  --exclude-table-data='outgoingwebhooks' --exclude-table-data='incomingwebhooks' \
  --exclude-table-data='commands' --exclude-table-data='commandwebhooks' \
  --exclude-table-data='oauthapps' --exclude-table-data='oauthaccessdata' --exclude-table-data='oauthauthdata' \
  > mattermost.dump
```

(Use your own container name, user and database if they differ. The names above are the ones in
the NEI/AAC compose file.) Copy `mattermost.dump` to the machine that will run the sandbox.

### 2. Restore into the sandbox (needs Docker)

```shell
sudo dev/sandbox/restore.sh /path/to/mattermost.dump
```

It starts only the database, restores, scrubs and verifies, only then starts Mattermost on
`http://127.0.0.1:8067` (reachable from this machine only), and prepares it for the bot: a local
admin (`sandboxadmin`, password in `dev/sandbox/.sandbox-admin-password`), the Boards plugin (the same
version as production: `BOARDS_VERSION`), the bot account `remindabot` and its token
(`dev/sandbox/.sandbox-bot-token`), and the bot added to every team. All of that runs inside the
sandbox's own network, so nothing in it reaches the internet.

### 3. Run the bot against it

The bot is the `reminder` service of the sandbox's compose file, for one-off runs:

```shell
SANDBOX="docker compose -f dev/sandbox/compose.yaml run --rm reminder python -m remindabot"
$SANDBOX --check
$SANDBOX --join-boards        # joins the relevant open boards, in the sandbox only
$SANDBOX --once --dry-run     # everything it WOULD send, from your real board data
$SANDBOX --once               # actually sends, but only inside the sandbox
```

Things worth looking at in the output: how many boards have the three properties, which boards are
"unreadable" (private boards the bot would need to be added to), whether any card title or date
looks wrong, and the card links.

To try `scripts/make-bot-read-only.sh` too, give it an admin token of the sandbox (log in at
http://127.0.0.1:8067 as `sandboxadmin`, Profile > Security > Personal Access Tokens) and the bot's
token: `REMINDER_BOT_TOKEN=$(cat dev/sandbox/.sandbox-bot-token) ADMIN_TOKEN=... scripts/make-bot-read-only.sh http://127.0.0.1:8067`.

### 4. Clean up

```shell
docker compose -f dev/sandbox/compose.yaml --profile app --profile setup --profile tools down -v
rm dev/sandbox/.sandbox-* /path/to/mattermost.dump
```

## Notes

- Set `MM_VERSION` (and `BOARDS_VERSION`) to match production if it differs from 10.9.1 / 9.1.3.
  The restore needs the **same** Mattermost version as the dump, otherwise Mattermost would try to
  migrate the data.
- The restored data does not have the Boards plugin's files; the bundle comes from its GitHub release
  page, which is the only thing downloaded from the internet. It is downloaded when the `dev/toolbox`
  image is built, on your machine, not by the sandbox.
