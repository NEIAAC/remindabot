# Setting it up

You need: Mattermost with the Boards plugin, Docker Compose, and system-admin rights.

1. **Allow bot accounts.** System Console → Integrations → Integration Management →
   *Enable Bot Account Creation*.
2. **Create the bot.** System Console → Integrations → Bot Accounts → *Add Bot Account*
   (for example username `remindabot`, display name `Remindabot`, description `Due-date reminders
   for Mattermost Boards cards`, role **Member**; never *System Admin*). Copy the **token**.
3. **Add the bot to every team** whose boards should be scanned.
4. **Add the service** to the Mattermost `docker-compose.yml`:

   ```yaml
     reminder:
       image: ghcr.io/neiaac/remindabot:1.1.0     # a published release; the version is what you run
       container_name: chat-reminder
       restart: unless-stopped
       depends_on:
         mattermost:
           condition: service_healthy
       # the bot only reads /config and writes /data (a volume) and /tmp, and needs no extra rights
       read_only: true
       tmpfs:
         - /tmp
       cap_drop:
         - ALL
       security_opt:
         - no-new-privileges:true
       environment:
         - TZ=${TZ}
         - REMINDER_TZ=${TZ}
         - REMINDER_MM_URL=http://mattermost:8065
         - REMINDER_SITE_URL=https://${DOMAIN}
         - REMINDER_BOT_TOKEN=${REMINDER_BOT_TOKEN}
       volumes:
         - ./reminder/message.txt:/config/message.txt:ro   # your wording; optional (the image has a default)
         - reminder:/data                                  # who was already reminded

   volumes:
     reminder:
   ```

   Put `REMINDER_BOT_TOKEN=...` in your `.env` (which must be git-ignored). To update the bot later, change the
   version in the `image:` line (releases are listed on the repository's Releases page) and run
   `docker compose up -d`; to go back, put the old version back. To run your own copy of the code
   instead, replace `image:` by `build: <path or git address of your fork>`.

   **If the package is private** (a new package is private, and an organisation can forbid public
   ones), the server logs in to the registry once; `docker compose up -d` then pulls as usual and
   the compose file does not change:

   1. On GitHub: *Settings → Developer settings → Personal access tokens → Tokens (classic)*. Give it
      **only** the `read:packages` scope and an expiry; if the organisation uses SSO, authorize the
      token for it (*Configure SSO*). It can be a token of any account that may read the package.
      The package is linked to the repository by the release workflow, so a person who can read the
      repository can read the package: no setting needs changing.
   2. On the server, as the user that runs `docker compose` (with `sudo` if you run it with `sudo`):
      ```shell
      read -rs CR_PAT        # paste the token; it is not shown and not kept in the shell history
      echo "$CR_PAT" | docker login ghcr.io -u YOUR_GITHUB_USERNAME --password-stdin
      unset CR_PAT
      ```
      Docker keeps the login in `~/.docker/config.json` of that user.

   Good to know: the token is only used when a **new** image is pulled; a running bot never needs it.
   A classic token reads every package its owner can read, so a dedicated GitHub account that has
   only *Read* on this package (package settings → *Manage access*) is the tidier long-term owner. A
   token of a person stops working when they leave or it expires: then only updates fail, and
   re-running the login with a new token fixes it. Dependabot cannot see the tags of a private
   package, so it will not propose bot updates: watch the repository's Releases page instead.
5. **Boards the bot reads.** The bot must be a member of a board to read its cards; a board it is not
   a member of is skipped (the log says so).
   - **Open boards** are joined by the bot automatically. `REMINDER_AUTO_JOIN=false` turns that off:
     it then reads only the boards it was added to.
   - **Private boards** (and open ones, when auto-join is off) must have the bot added by a board
     admin. The *Share* dialog of Boards cannot do it, because it leaves bot accounts out of its
     search: use the command in [Adding the bot to a board by hand](#adding-the-bot-to-a-board-by-hand).
6. **Do not start it for real yet.** Follow
   [Trying it safely](#trying-it-safely-before-going-live) first, then `docker compose up -d`.
7. **Optional: the chat commands** (`/remind_status`, `/remind_send`, ...). Two small steps once the
   bot runs, described in [chat-commands.md](chat-commands.md#setting-it-up-once). In short: let Mattermost call the
   bot (`MM_SERVICESETTINGS_ALLOWEDUNTRUSTEDINTERNALCONNECTIONS=reminder` on the Mattermost service)
   and run `--register-commands` once with an administrator's token.

### Adding the bot to a board by hand

The bot is not offered by the *Share* dialog, so this is the same call the dialog would make, with
`curl`. It adds the bot as a **Viewer** (it reads cards and sends messages, it cannot edit), so the
read-only step below is not needed for boards added this way. The bot must already be a member of the
board's **team**.

You need:

- the **board id**: in the browser the board's address is
  `https://chat.example.com/boards/team/TEAM_ID/BOARD_ID/VIEW_ID`; copy the `BOARD_ID` (26 letters and digits);
- a **personal access token of an administrator of that board** (Profile → Security → Personal Access
  Tokens; by default only a system administrator can create one). If that entry is missing, a system
  administrator turns on *Enable Personal Access Tokens* in System Console → Integrations →
  Integration Management (it can be switched off again afterwards). Being a system administrator is
  not enough for a private board: Boards only lets a board admin add members. Delete the token afterwards.

```shell
MM=https://chat.example.com        # your chat's address
BOARD=PASTE_THE_BOARD_ID
read -rs TOKEN                     # paste the token; it is not shown and not kept in the shell history
auth() { printf 'header = "Authorization: Bearer %s"\n' "$TOKEN" | curl -sS -K - -H 'X-Requested-With: XMLHttpRequest' "$@"; }

BOT_ID=$(auth "$MM/api/v4/users/username/remindabot" | jq -r .id)   # the bot's user id (needs jq)
echo "$BOT_ID"                    # 26 letters and digits; "null" means the bot's username is not remindabot

auth -X POST "$MM/plugins/focalboard/api/v2/boards/$BOARD/members" \
  -H 'Content-Type: application/json' -w '\nHTTP %{http_code}\n' \
  -d "{\"userId\":\"$BOT_ID\",\"schemeAdmin\":false,\"schemeEditor\":false,\"schemeCommenter\":false,\"schemeViewer\":true}"

# check: the bot is listed, with "schemeViewer": true
auth "$MM/plugins/focalboard/api/v2/boards/$BOARD/members" | jq --arg u "$BOT_ID" '.[] | select(.userId == $u)'
unset TOKEN
```

The token is given to `curl` through its standard input, so it never shows in the process list. Run
the `POST` again for the next board (change `BOARD`). `HTTP 200` means done. Otherwise:

| Answer | Meaning |
| --- | --- |
| `HTTP 401` | The token is wrong, expired or deleted. |
| `HTTP 403`, "access denied to modify board members" | The token's owner is not an admin of this board. |
| `HTTP 403`, "access denied to team" | The bot is not a member of the board's team: add it to the team first. |
| `HTTP 404` | The board id is wrong, or the token's owner cannot see the board. |

Afterwards `docker compose run --rm reminder python -m remindabot --once --dry-run` shows the cards of
the boards the bot reads, and `/remind_boards` lists them from chat.

### Optional but recommended: make the bot read-only

When the bot joins an open board it gets the board's default role, **Editor**. It never edits
anything, but a leaked token could. A system administrator can reduce the bot to **Viewer**
(it can still read cards and send messages; edits are refused):

```shell
export REMINDER_BOT_TOKEN=...        # the bot's token
export ADMIN_TOKEN=...               # a personal access token of a system admin
scripts/make-bot-read-only.sh https://chat.example.com           # shows the plan
scripts/make-bot-read-only.sh https://chat.example.com --apply   # does it
```

Run it again after new boards appear and the bot joins them. The admin account becomes a member of
the boards it changes (that is how Boards lets an admin change another member's role). Details are
in the script's header. The tool is kept out of the container image on purpose: the bot must never
hold an admin token.

## Trying it safely before going live

Levels from least to most effort. Do at least the first.

**1. Dry run on the real server (read-only).** Prints every message that *would* be sent, sends
nothing, joins nothing, writes nothing:

```shell
docker compose run --rm reminder python -m remindabot --check             # token, template, teams
docker compose run --rm reminder python -m remindabot --join-boards       # deliberately join the open boards
docker compose run --rm reminder python -m remindabot --once --dry-run    # the full list of would-be messages
```

Read every message, and click one card link to confirm it opens the right card.
(Cards already inside their window get their reminder at the first real run, so the dry run is
also your preview of that first batch.)

**2. Check the built image as a container.** `scripts/check-image.sh` verifies the image the way
it will really run: non-root user, timezone data, clean refusal of bad configuration, a read-only
and capability-free container, and daemon mode (starts, becomes healthy, stops promptly on
`docker stop`). It cannot send anything. By default it builds this checkout and runs it on the
dev stack (below); settings are environment variables, listed at the top of the script.

```shell
docker compose up -d                  # the dev stack, once
scripts/check-image.sh                # builds the image and checks it: 17 checks (needs Docker access)
```

**3. Try it on a throwaway Mattermost with demo boards.** `docker compose up -d --build` in the
repository root starts a local Mattermost + Boards with fake people, demo boards covering the usual
and the mistaken ways of filling in a card, and the bot itself with its chat commands. No real
data. `docker compose down -v` removes it all. See [development.md](development.md#the-dev-stack).

**4. Try it on a copy of your real database.** `dev/sandbox` builds an isolated sandbox from a
database dump: no network access at all, no real e-mails, passwords, sessions or names, and a
check that refuses to start if the scrub failed. See [dev/sandbox/README.md](../dev/sandbox/README.md).

**5. Practise the deployment itself.** `dev/rehearsal` runs your real production `docker-compose.yml`,
unmodified, on a scrubbed copy of the production database, isolated from the internet. You type the
deployment commands as you will on the server, and `rehearse.sh compare` shows everything they changed
(and fails if a card or a board did). It also checks that the server can build the bot from the
address in the compose file. See [dev/rehearsal/README.md](../dev/rehearsal/README.md).
