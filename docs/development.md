# Development

How the repository is organised, how to run the bot and its tests, and why things are the way they
are. If you only want to *use* the bot, [setup.md](setup.md) and [operations.md](operations.md) are
enough.

## The layout

```
src/remindabot/        the bot: Python 3.12, standard library only
  __main__.py          the command line: reads the flags and the environment, calls the Bot
  bot.py               Bot: wires everything together. run() is the daemon; once(), check()
                       and register_commands() are the one-off actions
  config.py            the static configuration, from environment variables (or files)
  driver.py            the Mattermost driver: the HTTP client and the core API calls
                       (users, teams, direct messages, pop-up forms, late answers)
  boards.py            the Boards plugin client, and EVERYTHING that knows its data model
  scheduler.py         WHEN the daily pass runs; retries; never exits on errors
  runner.py            ONE pass: collect due cards, then send; caps, audience, claim/send/confirm.
                       Also builds the report (boards, cards, people) the commands show
  logic.py             the reminder rule and the day-by-day plan (pure functions, no I/O)
  render.py            message template loading/validation, Portuguese dates, text sanitizing
  store.py             the SQLite state: claims, "sent" records, history, daily counts
  overrides.py         what administrators changed from chat, the pause, alert opt-ins, form signatures
  alerts.py            direct messages to administrators when something needs a person
  register.py          `--register-commands`: creates the slash commands in every team
  webhook_server.py    the HTTP endpoint Mattermost calls: /command, /dialog, /healthz
  commands/            the /remind_* chat commands
    registry.py        @command(...) and @dialog(...): a command is declared where it is written
    router.py          CommandRouter: token + administrator check, runs the handler, answers late if slow
    context.py         Ctx: what a handler is given
    show.py            commands that only look:      help, status, next, boards, card, history, getmessage
    control.py         commands that act:            send, resend, test, pause, resume
    adjust.py          commands that change things:  setmessage, setsend, setcap, reset, alerts
    markup.py          Markdown helpers (tables, inert text)
    cards.py           finding a card from what was typed
tests/                 unit, fuzz and integration tests; they need no Docker and no network
  support/             the fake Mattermost (fakeserver.py) and the stand-in the shell scripts are tested on
  e2e/                 the bot against the dev stack (below); skipped when there is none
config/                message.txt, the default message copied into the image
Dockerfile             the bot's image
compose.yaml           the dev stack
dev/toolbox/           a small image with bash, curl, jq and Python: what the scripts and e2e tests run in
dev/sandbox/           an isolated copy of a production database (see its README)
scripts/               shell helpers (below); scripts/lib/mm.sh is their shared HTTP helpers
docs/                  this documentation
pyproject.toml         package metadata (no dependencies); `pip install -e .` if you want `remindabot` on your PATH
```

### If you know other Mattermost bots

The pieces are the ones Mattermost bots are usually made of, under the usual names:

| Here | In [mmpy_bot](https://github.com/attzonko/mmpy_bot) (the Python bot framework) | In a Mattermost plugin (Go) |
| --- | --- | --- |
| `bot.py`: `Bot(config).run()` starts and stops everything | `bot.py`: `Bot(settings, plugins).run()` | `OnActivate` / `OnDeactivate` |
| `config.py`: `Config`, read from the environment | `settings.py`: `Settings`, inline or from the environment | the plugin's configuration struct |
| `driver.py`: `Driver`, the API calls | `driver.py` | the plugin `API` |
| `scheduler.py`: the daily job | `scheduler.py` | a scheduled job |
| `webhook_server.py`: the endpoint Mattermost calls | `webhook_server.py` | `ServeHTTP` |
| `commands/`: `@command("remind_x")` handlers behind a router | `plugins/`: `@listen_to` / `@listen_webhook` handlers behind an event handler | `RegisterCommand` / `ExecuteCommand` |
| `store.py`: the state | (left to the plugin) | the key-value store |

Two things differ on purpose. The bot listens to slash commands over HTTP (the `webhook_server`)
and not to the websocket, because it never has to read what people write, only what an
administrator types after a slash. And the reminders themselves are not a handler of an event: they
are a job on a schedule (`scheduler.py`) that uses the Boards plugin (`boards.py`), which is why
the domain code (`runner.py`, `logic.py`, `render.py`) sits next to the framework pieces instead of
inside a plugin.

### Adding a chat command

Write a function in `commands/show.py`, `control.py` or `adjust.py` (or a new module, imported in
`commands/__init__.py`) and declare it:

```python
@command("remind_example", hint="[days]", summary="One line for /remind_help.", slow=False)
def example(ctx: Ctx) -> str:
    return f"{ctx.args} from {ctx.username}"
```

`slow=True` is for a command that reads every board: it answers "working..." at once and the text
later. Nothing else has to be touched: `--register-commands`, `/remind_help` and the router read the
registry. Then add the command to [chat-commands.md](chat-commands.md); a test checks that. Return
the text of the reply; do not build Markdown tables by hand (use `markup.table`) and do not call
Mattermost's HTTP yourself (use `ctx.driver`).

The rule of this repository: **Python is the bot and its tests; everything else is shell or
compose.** A test (`tests/test_scripts.py`) fails if a `.py` file shows up outside `src/` and
`tests/`. The only thing you need on your machine is Docker; with Python 3.12 you can also run the
unit tests and the bot directly (`python3 -m unittest`, `PYTHONPATH=src python3 -m remindabot --help`).

## The dev stack

`compose.yaml` is a miniature of production on your machine, with no real data:

```shell
docker compose up -d --build     # first start: a few minutes (images, the Boards plugin, demo data)
docker compose logs -f seed      # watch it get ready; it ends with "ready: log in at ..."
docker compose ps                # `reminder` shows "healthy" when everything is up
docker compose down -v           # stop it and delete everything, volumes included
```

| Service | What it is |
| --- | --- |
| `postgres`, `mattermost` | Postgres 13 and Mattermost Team Edition 10.9.1, the versions production runs. Reachable from this machine only, on http://localhost:8066. E-mail and push are off. |
| `seed` | Runs once (`scripts/dev-seed.sh`): installs the Boards plugin, creates the team `nei`, the fake people alice, bob and carol, the bot account `remindabot`, and the demo boards below. Run again, it does nothing. |
| `register` | Runs once: `python -m remindabot --register-commands`, with the admin's token, which only this step sees. |
| `reminder` | The bot, built from the `Dockerfile` and run the way production runs it (read-only filesystem, no capabilities). It never sends by itself (`REMINDER_DAILY_PASS=false`): use `/remind_send`. |
| `unit`, `e2e` | The tests, only with `--profile test` (below). |

Log in at http://localhost:8066 as **`devadmin`**, password **`devadmin-password`** (set
`DEV_ADMIN_PASSWORD` in your environment or in a `.env` file before the first `up` to change it),
and try `/remind_help`, `/remind_status`, `/remind_send`, `/remind_setmessage`... The first
reminders are delivered to the fake people, whom nobody can log in as, **and to devadmin**: one demo
card lists devadmin as a notifyee, so the person trying the bot finds a real reminder in their direct
messages from "Remindabot", with a link that opens the card.

To work on the bot's code: edit, then `docker compose up -d --build reminder`. To change the demo
data, edit `scripts/dev-seed.sh` and start clean (`down -v`, `up -d`). Due dates are relative to the
day the stack was created (Lisbon calendar), so "5 days left" is always 5 days left; recreate the
stack on another day for fresh dates.

### What the demo boards hold

| Board | What it shows |
| --- | --- |
| Posts | The right setup (`Remind Before` is Text): lists `7;3;1`, `7; 3; 1` and a single `12`; **a card for `devadmin`**, the one account you can log in as; the typo `tres`; commas (`7,3,1`) and a unit (`7 dias`), which are reported and not guessed; a card with no Notifyees (only Assignees); 0 / -1 / empty; too early; already passed; no Due Date; a template |
| Remind Before como Numero (tipo errado) | `Remind Before` is a Number: the whole board is skipped with a warning |
| Remind Before como Multipla escolha (tipo errado) | `Remind Before` is a Multi select: skipped with a warning |
| Quadro novo (bot ainda nao entrou) | An open board with the right properties that the bot has not joined yet |
| Quadro privado | A private board the bot cannot read until a board admin adds it |
| Outro quadro (sem propriedades) | An unrelated board; must be ignored and never joined |

Every board has a table view, like boards made in the UI, so the card link in a message is the full
`/boards/team/<team>/<board>/<view>/<card>` form. A dry run of this data reports: 5 boards seen,
2 with the required properties, 1 unreadable, 16 cards checked, 6 in the reminder window, 7 messages
that would be sent and 1 card with nobody in Notifyees. A real run sends 8 (it also joins "Quadro
novo"): 3 to alice, 3 to bob, 1 to carol and 1 to devadmin.

### Trying the bot's command line on the stack

```shell
docker compose run --rm reminder python -m remindabot --check
docker compose run --rm reminder python -m remindabot --once --dry-run
```

### Where the secrets are

`seed` leaves the bot's token and a personal access token of `devadmin` in two Docker volumes
(`secret-bot`, `secret-admin`), as files. The services get only what they need: `reminder` the bot's
token (`REMINDER_BOT_TOKEN_FILE`), `register` and the tests also the admin's. To read one by hand:

```shell
docker compose run --rm --no-deps --entrypoint cat reminder /secrets/bot/token
```

## The test battery

Three levels, each one catching what the previous one cannot. Run all of them before a release
and after changing anything that touches Boards or Mattermost (for example a new Boards version).

**1. The test suite** (no dependencies, no Docker, a few minutes). From the repository root:

```shell
python3 -m unittest                                  # with Python 3.12 on your machine; about 8 minutes
docker compose --profile test run --rm unit          # or in a container, nothing installed
```

| File | What it proves |
| --- | --- |
| `test_reminder.py` | Unit tests: the list parser, the reminder rule, the message template, the state file (and upgrading an old one), the settings, the configuration. Also checks that the README's example table is what the rule really does. |
| `test_robustness.py` | Fuzzing: hostile data must never raise; the rule agrees with a brute-force oracle. Date and timezone edge cases. |
| `test_integration.py` | The real client, runner and daily loop against `tests/support/fakeserver.py`, a fake Mattermost that reproduces the server behaviours listed below and can be made to fail (500s, timeouts, redirects, dropped connections), with a fake clock including both daylight-saving days. |
| `test_model.py` | Hundreds of random scenarios (lists, due dates, days the bot was off, list edits, moved due dates, dry runs in between) through the whole pass, checked against a small model that shares no code with the application. Plus random garbage on cards and boards. |
| `test_cli.py` | `python -m remindabot`: every flag, the exit codes, and what each mode may touch (a dry run creates nothing); registering the commands, and settings made from chat applying to `--once`. |
| `test_commands.py` | The chat commands: who may run them (a non-administrator is refused by every command), what each one does, how Mattermost draws every reply, the pop-up form and its forgery checks, the HTTP endpoint (junk, oversize, concurrency). |
| `test_admin_controls.py` | Registration in Mattermost (idempotent, token handling, warnings), and the daily loop with a pause, a changed send time, message or caps, and alerts. |
| `test_structure.py` | The shape of the bot: a command is declared once and served without touching the router; the `Bot` runs the chat endpoint and the scheduler on one shared lock and stops cleanly; a handler never touches HTTP and the engine knows nothing of chat (read from the imports); this page lists exactly the files that exist. |
| `test_safety.py` | What the bot must never do: write anything but a message, a join, a form and a late answer (measured on the wire); leak a secret into a log, a reply or a message; leave its state file readable, injectable or overwritten; pile up threads or connections; send a reply Mattermost would refuse; fall over on idle, half-open, malformed or random connections; break on random command arguments. |
| `test_process.py` | The bot as REAL processes against the fake Mattermost: six at once send each message once; `SIGKILL` before and after delivery loses and duplicates nothing; `SIGTERM` stops the daemon promptly; commands over real HTTP; a damaged or unwritable state file is a clean stop; no secret in any output; every bad setting is exit code 2. |
| `test_chaos.py` | Weeks of daily passes while the server fails at random (answers lost, connections dropped, 5xx) and the process is killed at random, checked against an independent model: nobody gets anything twice, everybody gets exactly what the rule says. Deterministic cases for the orphaned-claim decision, and clock jumps. |
| `test_load.py` | The cost to the server (requests grow linearly), the daily caps under a mass of due reminders, a gentle pace in an outage, backoff, and a state file that stays small. |
| `test_hardening.py` | Nothing secret on a command line (also measured on the live process list), `scripts/check-image.sh` against a fake `docker`, compose files and image free of privilege, the sandbox isolated and scrubbed in the right order, no key, token, e-mail address or data file committed. |
| `test_scripts.py` | The shell scripts parse and run against a stand-in server (seed, sandbox setup, make-bot-read-only), the compose files only listen on 127.0.0.1 and set only real settings, the documents name real files and document every setting and command, and there is no Python outside `src/` and `tests/`. |

**2. The real server.** The bot against the dev stack: a real Mattermost and a real Boards plugin,
the real chat commands, and, once per fresh stack, a real send:

```shell
docker compose down -v && docker compose up -d --build       # always start clean
docker compose --profile test run --rm e2e                   # about 3 minutes; waits for the bot to be healthy
```

It checks: the dry run says exactly the expected messages and warnings and writes nothing; a real
send reaches the right people with working card links (including devadmin's own direct message);
a second run repeats nothing; `scripts/make-bot-read-only.sh` makes the bot a Viewer and a read-only
bot still delivers a brand-new reminder; and every `/remind_*` command is run through Mattermost as an
administrator and as an ordinary member, including the pop-up form and its forged-state refusal.
The scenario is `tests/e2e/test_live_stack.py`; its header says what it changes in the stack.

**3. The container.** The built image, as it will run, on the dev stack's network:

```shell
scripts/check-image.sh        # needs Docker access; builds the image and runs 17 checks
docker compose down -v        # clean up
```

When a test fails after a change, the fix is either the code or the test, never to skip it: the
tests are the written-down promises of this bot.

## Scripts

All shell, all written for `bash`, `curl` and `jq` only. They run from your machine, or inside the
`dev/toolbox` image (which is how compose runs them), so nothing else has to be installed.

| Script | What it does |
| --- | --- |
| `scripts/dev-seed.sh` | Prepares the dev stack: plugin, people, bot, demo boards. Run by the `seed` service. |
| `scripts/make-bot-read-only.sh` | Makes the bot a Viewer on every board it belongs to. For production too; needs an admin token ([setup.md](setup.md#optional-but-recommended-make-the-bot-read-only)). |
| `scripts/check-image.sh` | Checks a built image as a container (the third level above). |
| `scripts/sandbox-setup.sh` | Prepares the production-copy sandbox: plugin, bot, bot in every team. Run by `dev/sandbox/restore.sh`. |
| `scripts/lib/mm.sh` | The shared helpers: `api`, `login`, `must`, `install_boards`... Sourced, never run. |

`tests/test_scripts.py` runs the first, second and fourth against `tests/support/stubserver.py`, a
small stand-in for the Mattermost and Boards API, so a change to them is tested without Docker.

## Testing against real data

`dev/sandbox` restores a database dump into an isolated sandbox, anonymizes it, and lets you run the
bot against your real boards without any possibility of contacting a real person: see
[its README](../dev/sandbox/README.md). The Boards API facts below were verified on a throwaway
Mattermost built the same way: Mattermost Team Edition + Postgres, the Boards bundle uploaded from
its GitHub release page (`PluginSettings.EnableUploads=true`).

## Changing behaviour

| I want to... | Edit |
| --- | --- |
| change *when* a card is reminded | `src/remindabot/logic.py` (`evaluate`) and its tests |
| change the message text | `config/message.txt` (no code) |
| add a message variable | `render.PLACEHOLDERS`, then fill it in `runner._remind_card` |
| add a chat command | a function with `@command` in `commands/` (see above) |
| change property names | environment variables, no code |
| change the daily time / retry behaviour | `REMINDER_SEND_AT`; constants at the top of `scheduler.py` |
| support a different Boards property type | `src/remindabot/boards.py` (`*_TYPES`, `decode_*`) |

Keep the rule of thumb: **`logic.py` and `store.py` know nothing about Boards or HTTP**, and
**`boards.py` is the only file that knows the Boards data model**, and a command handler never touches HTTP.

## Boards API facts this bot relies on

Verified against Mattermost 10.9.1 with the Boards plugin 9.1.3 (the Boards API is not formally
documented for the plugin, so re-check these after any Boards upgrade; the fake server in
`tests/support/fakeserver.py` encodes them):

- The API lives under `/plugins/focalboard/api/v2/…`, authenticated with the same bearer token as
  the core API, and needs the header `X-Requested-With: XMLHttpRequest`. Unknown routes answer
  **200 with the web app's HTML**, not 404 (the client treats non-JSON as an error).
- **Reading a board's cards requires board membership**; being a team member is not enough (403).
  Open boards can be joined with `POST /boards/{id}/join`; private boards refuse (403) and need a
  board admin to add the bot.
- **`GET /boards/{id}/cards` returns only 100 cards unless told otherwise** (`per_page`, default
  100, `page` 0-based) and has no `ORDER BY`. The bot asks for `per_page=1000` and keeps paging
  until a page adds nothing new, de-duplicating by id. Without this, cards beyond the 100th would be
  silently ignored.
- Cards include card templates (`isTemplate: true`). Deleted cards are not returned.
- Property ids differ **per board**, so properties are matched by name through the board's
  `cardProperties`. Values are in `card.properties`: Date is a JSON string
  `{"from": <epoch ms>, "includeTime"?, "timeZone"?}`, Number and Text are strings, and Multi person
  is a list of user ids. (A Select or Multi select stores option ids, with the names in the board's
  `cardProperties[].options`; the bot does not read those.)
- A date-only Date value is stored by the Boards UI (the date picker *and* the calendar view use
  the same formula) as **12:00 UTC of the chosen day**, so its UTC date is the picked date whatever
  timezone the browser was in. With a time of day it is a real instant, read in its `timeZone`.
- Joining a board gives the board's default member role (Editor unless the board sets a lower
  minimum role). Only a **board** admin can change a member's role; a system admin becomes a board
  admin by joining. A Viewer can read cards and list members, and gets 403 on edits.
- Board members: `GET /boards/{id}/members` (the bot must be a member). Team membership is the
  core API `GET /api/v4/teams/{team}/members/{user}` (404 = not a member).
- Card deep link: `{site}/boards/team/{teamId}/{boardId}/{viewId}/{cardId}`. A view can be created
  with `POST /boards/{id}/blocks` (a list holding one `type: "view"` block); a board without any view
  makes the bot link to the board instead.
- **Slash commands** (core API, verified on 10.9.1, including from a bot-backed endpoint):
  a custom command belongs to one team; the trigger may contain `_`; Mattermost posts it
  form-encoded with `token`, `user_id`, `user_name`, `team_id`, `channel_id`, `text`, `trigger_id`
  and `response_url`; the reply is JSON (`response_type: ephemeral` = visible to the author only).
  A bot **can read other users' `roles`** (`GET /users/{id}`), which is how the system-administrator
  check works. A late answer is a POST to the **path** of `response_url` (the host in it is the public
  site address, so the bot uses its own Mattermost address with that path); it answers with the text
  `ok`, not JSON.
- **Pop-up forms:** the bot opens one with `POST /actions/dialogs/open` (its own token is accepted;
  the `trigger_id` of the command is valid for a few seconds). The submission is POSTed to the
  form's URL as JSON with `user_id`, `callback_id`, `state` and `submission` (line breaks intact),
  **without any token or signature**, which is why the bot signs `state`. Answering with
  `{"errors": {"<field>": "<text>"}}` shows the text inside the form.
- **Mattermost will not call a private address** (for example a container on the Docker network)
  unless the host name is in `ServiceSettings.AllowedUntrustedInternalConnections`
  (`MM_SERVICESETTINGS_ALLOWEDUNTRUSTEDINTERNALCONNECTIONS`). Allowing a compose service *name*
  works. Without it every command fails with the same generic "Command with a trigger of ...
  failed" as when the bot is down.
- Mattermost's Markdown renderer accepts a backslash before `` ` * { } [ ] ( ) # + - . ! _ > | ~ ``
  and `\`, but **not** before `<` (see `render.sanitize_inline`).
- **The reply of a slash command is changed by Mattermost in two ways** (found in the real web UI):
  the server rewrites every `<address|words>` into a link, even inside a code span or a table, so a
  `<` in one table cell and a `>` in another swallow everything between them (that is how a help row
  once lost its explanation); and a table is only drawn when a blank line comes before it, while a
  line right after it becomes one more row. Posts and direct messages made through the API are not
  rewritten. `mattermost.for_command_reply` (shows `<` and `>` as `‹` `›`) and `commands._table`
  (blank lines around it) deal with both, every reply goes through them, and the tests check every
  reply of every command (`CommandsBase.run_command`). Write replies with `_table`, never by hand.

## When Boards is replaced

Mattermost has said the Boards plugin is in maintenance mode and will eventually be replaced by a
built-in task feature, with no date. Everything that knows the Boards data model is in
`src/remindabot/boards.py` (plus the `Boards` calls in `runner.py`). When the replacement arrives:

1. Write a new reader that produces the same `Card` objects (`id, title, due, remind_before` as a
   tuple of ints, `notifyees`, `is_template`, `deleted`) and a function that lists the boards/containers
   to scan.
2. Leave `logic.py`, `store.py`, `render.py` and the daily loop alone.
3. Update the "API facts" above and the fake server, and keep the tests green.

## Design decisions, and why

- **A container, not a Mattermost plugin.** Nobody on the team needed to learn Go, and a compiled
  plugin would have to be rebuilt and retested on every Mattermost upgrade. This bot only needs
  public REST APIs.
- **Standard library only.** No dependency can break, be hijacked, or need updating. The base image
  is the only thing Dependabot has to watch.
- **Claim before send.** "Check, send, then record" can duplicate when two things run at once or a
  crash lands in between; claiming first and verifying orphans afterwards cannot.
- **Retry at the level of the daily pass, not the HTTP call.** A sending request is never repeated
  within seconds; a failed message is retried minutes later, after checking it was not delivered.
- **Caps per day, not per run.** Retries must not multiply the allowance.
- **The audience check ignores `Notifyees` alone.** Any editor can type any id there.
- **`Notifyees`, not `Assignees`.** "Who is responsible" and "who should be nagged" are not always the
  same people, so the bot only uses the explicit list. (Pointing `REMINDER_PROP_NOTIFYEES` at
  `Assignees` gives the other behaviour without code changes.)
- **`Remind Before` is Text, nothing else.** One way to fill it in is easier to explain, to check
  and to maintain than three. Number and Multi select were supported in an earlier version and
  removed on request; a Multi select stores a list of option ids with the names in
  `cardProperties[].options` (confirmed on Boards 9.1.3), which is what `src/remindabot/boards.py` used to map.
- **One message per card per day, for the smallest reminder reached.** A list such as `7;3;1` must
  never produce a burst when a card is seen late; the state key carries the reminder number and a
  smaller one already sent silences the bigger ones, so editing the list cannot repeat a message.

- **Chat commands are an endpoint inside the bot, not a plugin.** Same reasoning as the container:
  no Go, no rebuild on a Mattermost upgrade. The price is one allow-list entry in Mattermost.
- **Settings overrides live in the state file, not in a file the bot edits.** The compose file and
  the mounted `message.txt` stay what the repository says; the override is one reversible layer
  (`/remind_reset`), and there is still a single volume to back up.
- **Every command re-checks the administrator role, nothing is cached.** Demoting somebody must work
  at once, and a role check is one cheap call.
- **A pass is a pass.** `/remind_send` and `/remind_resend` call the same function as the daily loop
  and share its lock, so a command can never do something the daily pass could not (a duplicate, a
  cap bypass). `forget` removes a record only for `/remind_resend`, and the delivered message is
  still counted for the caps through the history.
- **While the settings cannot be read the loop does not send.** "Paused" would be unknown, and a
  delayed reminder is better than one sent against an administrator's wish.
- **Only `/remind_help` is in the autocomplete list.** Mattermost cannot hide a command from
  non-administrators; offering sixteen commands to everybody would only invite refusals.
- **The dev stack is plain compose, with the bot in it.** One `docker compose up` gives a stack that
  behaves like production (same images, same hardening, commands registered, the bot reaching
  Mattermost by service name), so what you try is what runs. Nothing but Docker is needed, there is no
  per-machine setup, and `down -v` is a complete clean-up.
- **Helpers are shell, not Python.** They are glue around HTTP calls and `docker`, which `curl`, `jq`
  and `bash` do in few lines, and a maintainer should not need a Python environment (or to know which
  Python tool does what) to start the stack. The one exception is the live-stack test suite, which
  needs a websocket client and so is a test, run in a container.
- **Secrets can be files** (`REMINDER_*_FILE`). The dev stack passes the bot's token as a file from a
  volume the bot cannot write; production can do the same with Docker secrets.
- **The dev bot has its daily pass switched off** (`REMINDER_DAILY_PASS=false`; production leaves it on, which is the default). A playground that
  sends on its own at the daily time makes the demo non-repeatable and the live-stack test racy; sending is a
  decision (`/remind_send`).
