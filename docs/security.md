# Security: what the bot can do to the rest of the system

The bot is a guest on your chat server, and it holds two secrets (a bot token, and the key that
signs its pop-up forms). This page says what it can and cannot do, where the secrets are, and which
test proves each statement. If you change something here, the named test is the one to read first.

## What it can do on Mattermost

It reads boards and cards, looks up people, and **writes exactly five kinds of thing**, nothing else:

| It writes | Why |
| --- | --- |
| a direct message (`POST /api/v4/posts`, after opening the chat with `POST /api/v4/channels/direct`) | the reminder itself |
| `POST /boards/{id}/join` on an **open** board | to be allowed to read its cards (a private board is never joined) |
| `POST /api/v4/actions/dialogs/open` | the pop-up form of `/remind_setmessage` |
| `POST /hooks/commands/{id}` | the late answer of a slow slash command |
| its own state file | what was sent |

It never edits a board, a card, a role, a member, a user, a team or a command (it creates the
slash commands only in the one-off `--register-commands`, with an administrator's token that it does
not keep). Its account should be a plain *Member*; `scripts/make-bot-read-only.sh` reduces it to
*Viewer* on the boards it joined.

Proof: `tests/test_safety.py` (`WriteSurface`) records every request of a pass, of every chat command
and of a week of the daily loop, and fails on anything outside that table; a dry run and the commands
that only look must write nothing at all. `tests/e2e` (`test_08`) checks on the real server that the
bot's token cannot create a team or a user, change a role or read the configuration.

## Its secrets

| Secret | Where it lives | Where it must never be |
| --- | --- | --- |
| the bot's token | `REMINDER_BOT_TOKEN` or a file (`REMINDER_BOT_TOKEN_FILE`) | a log line, a reply, a message, the state file, the process list |
| an administrator's token (only for `--register-commands`) | an environment variable or file for that one run | the bot's own container |
| the key that signs pop-up forms, and the commands' tokens | the state file's `meta` table | any reply, message or log line |

- **Logs and errors.** An error page that repeats the request's headers (some proxies do) has the
  token replaced by `***` before it is logged. Proof: `Secrets` in `tests/test_safety.py`, which
  runs a whole bad day at DEBUG level, and `NoSecretsInOutput` in `tests/test_process.py`, which does
  it with real processes.
- **Process list.** On a shared machine a token given to a program on its command line can be read
  by every user in `ps`. The shell scripts feed `curl` its token through standard input and passwords
  through the environment, never as arguments. Proof: `ScriptHygiene` and `ProcessList` in
  `tests/test_hardening.py`, which sample the live process list while the scripts run.
- **The state file** holds names, card titles and the signing key, so it is created with mode `0600`
  and an older, more open file is tightened when opened. Proof: `StateFile` in `tests/test_safety.py`.

## What can reach it

The chat-command endpoint (port 8080) is for Mattermost, on the Docker network only; the compose file
never publishes it. Everything that arrives is checked again:

- a secret **token** per command and team (compared in constant time), then Mattermost is asked **at
  that moment** whether the caller is a system administrator (nothing is cached);
- a request body above 256 KB is refused, a client that stops talking is dropped after 30 seconds,
  at most 64 connections are served at once (the rest are hung up on, no thread is started for them),
  and at most three commands that read every board run at the same time (the next one is told to
  wait), so a flood of commands cannot become a flood of requests on the chat server;
- a late answer can only go to a command-response path of the bot's own Mattermost address;
- a pop-up form's submission carries a signature the bot made, bound to the person, valid 15 minutes.

Proof: `WebhookAbuse` and `SlowCommandLimit` in `tests/test_safety.py` (idle and half-open connections,
malformed and random requests, forged commands, the two limits), the authorization tests in `tests/test_commands.py`, and the live checks
in `tests/e2e` (`test_08`: every command refused to an ordinary member through the real Mattermost,
forged requests to the real endpoint).

What it does **not** protect against: somebody on the Docker network who has stolen a command token
and an administrator's user id (the token is the secret; keep the network to the services that need
it); a Mattermost administrator (they can do anything anyway); a bug in the Boards plugin.

## Text that people type

Card titles, board titles and names are typed by anyone with edit rights, but the message carries
the bot's authority. They are made inert before they reach a message or a reply: no link, image,
mention, code block, heading or extra line can be injected, invisible and right-to-left characters
are removed, and in a command reply even `<a|b>` (which Mattermost rewrites into a link) cannot
appear. The audience is limited to people who may see the board. Proof: `HostileDataInTheMessage` in
`tests/test_safety.py` (over thirty hostile strings in every position of the real message), the fuzz tests in
`tests/test_robustness.py`, and `ReplyFormatting` in `tests/test_commands.py`.

## Being a good guest

| Promise | Proof |
| --- | --- |
| **Never the same reminder twice**, even when the answer to a send is lost, the connection drops or the process is killed | `tests/test_chaos.py` (weeks of passes under random failures and random process deaths, checked against an independent model), `tests/test_process.py` (real processes, `SIGKILL` before and after delivery, several at once) |
| **A cost to the server that is small and predictable**: requests grow with boards and people, never with boards × people | `Scale` in `tests/test_load.py` |
| **A gentle pace in an outage**: one retry every ten minutes, backoff on rate limiting, a message is never retried within a pass | `Outage` in `tests/test_load.py` |
| **Limits**: at most 100 messages a day and 20 per person (settable), however many cards are due | `Scale` in `tests/test_load.py`, `test_integration.py` |
| **No growth**: no thread or connection left behind by thousands of commands, a state file that stays small over years, replies that always fit in a Mattermost post | `ResourceLeaks`, `EveryStoreIsClosed`, `ReplySize` in `tests/test_safety.py`, `Growth` in `tests/test_load.py` |
| **Never destroys its own state**: a damaged file is left untouched and the bot stops with a message; a file written by a newer version still works after a rollback | `StateFile` in `tests/test_safety.py`, `DamagedState` in `tests/test_process.py` |
| **A bad clock does no harm**: a clock put back, or a machine waking up a year late, duplicates nothing | `ClockJumps` in `tests/test_chaos.py` |

## What it ships

The image runs as an unprivileged user with a fixed id, fetches nothing at build time, copies in only
the code and the default message, and runs with a read-only filesystem and no capabilities. Nothing
in the compose files is privileged or shares the host, and the sandbox for a copy of production data
has no route to the internet and is scrubbed before Mattermost starts. No key, token, personal e-mail
address or data file is committed. Proof: `tests/test_hardening.py` (`ImageHardening`,
`ComposeHardening`, `SandboxIsolation`, `RepoHygiene`, and `CheckImage`, which runs
`scripts/check-image.sh` against a fake `docker`), and `scripts/check-image.sh` itself on a real one.
