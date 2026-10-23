# Rehearsing the deployment on a copy of production

This folder lets you **practise the real deployment**, step by step, on a copy of your production
chat, and then **prove what it changed**. It is the dress rehearsal; the [sandbox](../sandbox/README.md)
is the tool to look at what the bot would send, this one is the tool to practise putting it in.

What you get, on your own machine and nowhere else:

- your **real `docker-compose.yml`**, run unmodified (same images, volumes, health checks, start
  order, container names), in an isolated Docker project;
- a **copy of the production database** with every real e-mail, password, session and name removed;
- the commands to type are the ones you will type on the server, in the same order;
- **snapshots** that show, table by table and container by container, everything the deployment
  changed, and fail loudly if a card or a board changed.

## What protects the real people and the real server

| Risk | Protection |
| --- | --- |
| The copy e-mails or pushes to a real person | E-mail and push are off, no mail server is set, **and** the containers have no route to the internet (`internal` Docker network, see `isolation.yaml`). |
| Someone logs in with a real account | Passwords, sessions, access tokens and OAuth data are deleted; e-mails become `<id>@sandbox.invalid`; usernames become `u<id>`. |
| Message content is copied around | The dump leaves out every post, file, reaction and draft (the command in the [sandbox README](../sandbox/README.md#1-make-the-dump-on-the-production-server)). |
| The scrub silently fails | `rehearse.sh` checks the result and **refuses to start Mattermost** if a real e-mail, session or token is left. It is the same SQL as the sandbox's (a test keeps them identical). |
| It touches production | It never connects to production. It refuses to run on a machine that has containers named `chat-web`, `chat-db` or `chat-reminder`. |
| It stays around | `rehearse.sh down` removes every container, network, volume and file. |

The dump is still personal data about real people: keep it on this machine, and delete it when you
are done. Card titles and board names are kept (they are what the reminders are about).

## Before the real deployment: the server must be able to get the bot

The production file names a published image, `image: ghcr.io/neiaac/remindabot:<version>`, which the
repository's release workflow builds and publishes when you push a version tag (`CONTRIBUTING.md`,
"Releasing a version"). `rehearse.sh deploy` tests that image **without a login**, the way a server
that never logged in to GitHub would pull it, and says `NO` loudly if it cannot.

When this was first checked (2026-10-19) the repository was private and refused anonymous access, so
a server could not have fetched anything. Do this once:

1. Make `NEIAAC/remindabot` public (Settings, last section). It is MIT-licensed and holds no secrets
   or real people (tests enforce it).
2. Push the first tag (`v1.0.0`): the workflow runs the tests and publishes `ghcr.io/neiaac/remindabot:1.0.0`.
3. Open the package (the organisation's *Packages*) and check that its visibility is **Public**. A new
   package is sometimes created private, and an organisation can forbid public packages.
4. Check it as the server will: `docker logout ghcr.io; docker pull ghcr.io/neiaac/remindabot:1.0.0`, or
   run `rehearse.sh deploy ... published`.

If the package must stay private, the server needs `docker login ghcr.io` once, with a token that can
only read packages.

## Before you start

You need Docker with Compose 2.24 or newer, `curl` and `git`, on a machine that is **not** the
production server, and these three files:

1. **The dump** of the production database, made on the server without stopping anything and
   without message content: step 1 of the [sandbox README](../sandbox/README.md#1-make-the-dump-on-the-production-server).
   Mattermost on this machine must be the **same version** as production's (the compose file says
   which), or it would try to migrate the data. If production runs another Boards version than 9.1.3,
   `export BOARDS_VERSION=<version>` first.
2. **The production compose file as it is today**, before the bot: in the infrastructure checkout,
   `git show origin/main:docker-compose.yml > /tmp/compose-before.yml`.
3. **The compose file with the bot**, which is the change you are about to deploy: the working copy
   or the branch of the pull request (`/path/to/chat/docker-compose.yml`). If its `reminder/message.txt`
   sits next to it, that file is used too.

## 1. Build the copy of production as it is today

```shell
sudo dev/rehearsal/rehearse.sh up /tmp/compose-before.yml /path/to/mattermost.dump
```

It starts only the database, restores and scrubs it, **checks the scrub**, only then starts
Mattermost (a minute), makes a local admin, installs the Boards plugin, and takes the first snapshot.
You can open the copy at http://127.0.0.1:8068 as `sandboxadmin` (the password is in
`dev/rehearsal/work/.admin-password`).

Everything that follows happens in `dev/rehearsal/work/`, which holds the production compose file and
a `.env`, exactly like the folder on the server:

```shell
cd dev/rehearsal/work
sudo docker compose ps          # production as it is today: Mattermost and its database
```

## 2. Practise the deployment

Do these in order. They are the steps of [docs/setup.md](../../docs/setup.md); the right-hand
column says what is different in the rehearsal and what to look at.

| Step | Do | Look at |
| --- | --- | --- |
| a. The board | In the browser, give the real board a `Notifyees` property (Multi person) and make `Remind Before` a Text property with values like `7;3;1`, on a few cards. Then `dev/rehearsal/rehearse.sh snapshot ready`. | This is the manual part production needs too. Do it as you will do it there; write down how long it takes. |
| b. The new file | `dev/rehearsal/rehearse.sh deploy /path/to/chat/docker-compose.yml` | The printed **diff** is exactly what changes on the server. Read the line "can the production server pull the image": see [above](#before-the-real-deployment-the-server-must-be-able-to-get-the-bot). By default the bot is built from this checkout (the image may not be published yet); use `... published` as the second argument to pull the real image, as the server will. |
| c. Bot accounts | System Console → Integrations → *Enable Bot Account Creation*. | Same screen as production. |
| d. The bot | System Console → Bot Accounts → *Add Bot Account* (`remindabot`, role Member). Copy the token into `dev/rehearsal/work/.env` as `REMINDER_BOT_TOKEN=`. Add the bot to every team. | Never paste the token anywhere else. |
| e. Start it | `cd dev/rehearsal/work && sudo docker compose up -d` (the script prints the exact command; with `local` it adds `--build`) | **Mattermost is recreated** (the new `ALLOWEDUNTRUSTEDINTERNALCONNECTIONS` setting): time how long the chat is unreachable (`while sleep 1; do curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8068/api/v4/system/ping; done`). On the server that is a short outage; pick a quiet moment. |
| f. Look before it sends | `sudo docker compose run --rm reminder python -m remindabot --check`, then `--join-boards`, then `--once --dry-run` | Read every line: boards seen, cards in the window, warnings, nobody in `Notifyees`, unreadable private boards. Open a card link. |
| g. The chat commands | `sudo docker compose run --rm -e REMINDER_ADMIN_TOKEN=... reminder python -m remindabot --register-commands` (an admin's personal access token), then `/remind_help`, `/remind_status`, `/remind_send` in the browser | Shows whether Mattermost can reach `reminder:8080`. |
| h. The daily run | `/remind_setsend` to two minutes from now, wait, then `/remind_status` and `/remind_history` | The real scheduler, in the real container, sends once; nothing twice. Restore the default with `/remind_reset`. |
| i. Compare | `dev/rehearsal/rehearse.sh snapshot after-bot` then `dev/rehearsal/rehearse.sh compare ready after-bot` | See below. |

### Reading the comparison

`compare` lists the containers (Mattermost *recreated*, the bot *new*, the database *unchanged*), every
table whose number of rows changed, the fingerprints of the boards, and the number of error lines in
each container's log. Each change is `expected` (with the reason), `REVIEW` (not on the expected
list: look at it, then decide) or `FAIL`. It fails, with exit code 1, when:

- a card or a board changed (`focalboard_blocks`, `focalboard_boards`) or a team changed: **the bot
  only reads them**, so this must never happen;
- rows disappeared from a table that does not expire by itself.

The list of expected changes is at the top of `cmd_compare` in `rehearse.sh`. After your first real
rehearsal, add to it what you decided is normal, so the next person sees only what is new.

## 3. Drills worth doing once

Each one answers "what if" before it can happen on the server. Take a snapshot before and after.

| Drill | Do | Expect |
| --- | --- | --- |
| The bot crashes | `sudo docker kill chat-reminder` | Docker restarts it (`restart: unless-stopped`); it logs a start; nobody is reminded twice. |
| Mattermost is down | `sudo docker compose stop mattermost`, wait a minute, `start mattermost` | The bot logs the failure and keeps running; `docker compose ps` shows it healthy again. |
| Roll back | `sudo docker compose rm -sf reminder` | Mattermost and the database are untouched (`compare` shows only the bot gone); the bot's state volume is kept, so starting it again forgets nothing. |
| Update the bot | change the version in the `image:` line, `sudo docker compose up -d` | Only the bot's container is replaced, the state volume survives. Rolling back is the same edit in the other direction. |
| Back up the state | `sudo docker run --rm -v remindabot-rehearsal_reminder:/data:ro -v "$PWD":/backup alpine cp /data/state.sqlite /backup/` | A copy of "who was already reminded", the one file you cannot rebuild. |

## 4. Having Claude check it

After each phase, save the output in files and tell Claude which ones; Claude reads the files and
nothing is pasted or uploaded:

```shell
dev/rehearsal/rehearse.sh compare 00-start ready       > /tmp/rh-compare-1.log 2>&1
dev/rehearsal/rehearse.sh compare ready after-bot      > /tmp/rh-compare-2.log 2>&1
cd dev/rehearsal/work && sudo docker compose logs --no-color reminder > /tmp/rh-bot.log 2>&1
```

The bot's log, the `--check` and `--dry-run` summaries and the comparison contain counts, board
names, and (after the scrub) `u<id>` usernames. Read the **message texts** yourself: they contain
card titles.

## 5. Clean up

```shell
sudo dev/rehearsal/rehearse.sh down       # containers, volumes, network, work/ (including the token and password)
rm /path/to/mattermost.dump
```

## What the rehearsal does not prove

Be honest about the gap between the copy and the server, and close it by hand:

| Difference | What to do on the real server |
| --- | --- |
| **Settings made in the System Console** are in production's `config.json`, which the copy does not have (only the settings in the compose file are carried over). | Look at these on production *before* the deployment: *Enable Bot Account Creation* (setup step 1), *Enable Custom Slash Commands* (the `/remind_*` commands need it), and who may open direct messages (`TeamSettings.RestrictDirectMessage`: if it is "team members only", the bot must be in the teams of the people it writes to). |
| No reverse proxy and no TLS: card links are `https://localhost:8068/...` and do not open. | Open one card link of the first real dry run. |
| The messages and files of the chat are not in the copy; names are replaced by `u<id>`. | Read the first real `--once --dry-run` with the real names. |
| The server's Docker, firewall and clock. | `docker compose build` must reach GitHub from the server; check `date` and the time zone (`TZ` in `.env`). |
| Real traffic and size. | The first real run is a dry run; watch `docker compose logs -f reminder` during it. |
| Ports, e-mail and the isolation layer itself (`isolation.yaml`). | Nothing: they only exist to keep the copy harmless. |
