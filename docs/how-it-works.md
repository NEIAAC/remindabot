# How it works inside, and what it guarantees

## The pieces

```
                                  ┌────────────────────────── your docker-compose ──────────────────────────┐
  people ◄── direct message ──────┤                                                                          │
                                  │   ┌──────────────┐   REST (bearer token)   ┌───────────────────────┐   │
                                  │   │  Mattermost  │◄────────────────────────┤  reminder  (this bot) │   │
                                  │   │  + Boards    │                         │  Python, daily loop   │   │
                                  │   └──────────────┘                         └──────────┬────────────┘   │
                                  │                                                       │ SQLite          │
                                  │                                          /data/state.sqlite (volume)   │
                                  └──────────────────────────────────────────────────────────────────────────┘
```

Inside the bot there are two ways in, and one place where the work is done:

```
  the clock ─────────────► scheduler.py ───────────────────────────┐
                                                                    ▼
                                                  runner.py: ONE pass (rule in logic.py, text in render.py)
                                                                    ▲        │ reads cards: boards.py
  an administrator                                                  │        │ sends messages: driver.py
  types /remind_x ──► Mattermost ──► webhook_server.py              │        └ remembers: store.py
                                        └─► commands/router.py ──► commands/*.py  (a handler never
                                              (token, admin check)                  touches HTTP)
```

`bot.py` creates all of it and starts it: `Bot.run()` is what the container runs. See
[development.md](development.md#the-layout) for the files, and how they compare to other
Mattermost bots.

**The daily loop** (`src/remindabot/scheduler.py`, function `serve`) wakes up every minute. Once it is past
`REMINDER_SEND_AT` and today's pass has not been completed, it runs one pass (`src/remindabot/runner.py`):

1. **Collect.** For every team the bot belongs to, list the boards. For each board that has the
   three properties, read all cards (joining open boards first if needed), and keep the cards that
   have a reminder to send today, together with which one (`src/remindabot/logic.py`).
2. **Send.** Sort those cards by Due Date (most urgent first) and, for each notifyee, in
   order: check they may see the board, check the daily caps, build the message from
   `message.txt`, **claim** the reminder in the state file, send the direct message, then
   **confirm** it.

**The state file** (`src/remindabot/store.py`) is one SQLite table with one row per
*(card, person, due date, reminder)*, where "reminder" is one number of the list (`7`, `3`...).
A row is `pending` while a message is being sent and `sent` afterwards. The claim is atomic, which
is what makes duplicates impossible even with two processes at once. A reminder also counts as
sent when a *smaller* one was already sent for the same card and person.

**If something fails** the loop does not give up: Mattermost unreachable or an unexpected error is
retried every 10 minutes; messages that failed are retried up to four times that day; a state file
that cannot be written stops sending for the rest of the day (see
[Reliability and safety](#reliability-and-safety) below).

## Reliability and safety

What the bot guarantees, and what it cannot:

- **Never sends the same reminder twice.** Every message is *claimed* in the state file before it
  is sent and *confirmed* after. Claims are atomic across processes, so two instances at once (say
  `compose run ... --once` while the service is up) cannot both send.
- **A crash cannot cause a duplicate either.** A claim left behind by a crash or a lost answer goes
  stale after 5 minutes; before sending again the bot checks whether the message is already in the
  person's chat with the bot, and records it instead of sending a second copy. The remaining
  theoretical gap: someone deleting the message in between, in which case it is sent again.
- **Retries.** Messages that failed are retried every 10 minutes, up to four passes a day, then
  tomorrow, for as long as that reminder still applies. A message that may have been delivered
  (a timeout) is verified before any re-send. Messages "in progress elsewhere" are looked at again
  too. Permanent conditions (a user that no longer exists, no access to a board) are logged
  and never retried.
- **One bad card, board or user never blocks the others.** Unexpected errors are logged with a
  traceback and counted; the daily loop never exits on its own.
- **If the state file cannot be written, it stops sending for the day** and logs a critical error
  (without the state it could no longer prevent duplicates).
- **Daily caps** (`REMINDER_MAX_MESSAGES_PER_DAY`, `..._PER_USER_PER_DAY`). Hitting one is logged as
  an error and the most urgent reminders go first; the rest continue the next day. It should only
  happen on the very first run on a busy workspace, or when something is wrong. Retries never
  multiply the allowance: the day's total is counted from the state file.
- **The bot never writes to a board.** It reads cards, joins open boards, and sends direct
  messages. Its account should be a plain *Member*, never a system admin. See
  [make it read-only](setup.md#optional-but-recommended-make-the-bot-read-only).
- **Network hygiene.** Redirects are never followed (a wrong `REMINDER_MM_URL` is a loud error,
  not a silent one), responses are size-limited, ids that do not look like Mattermost ids never
  reach a URL.
- **The container** runs as an unprivileged user and works with `read_only: true`, `tmpfs: [/tmp]`,
  `cap_drop: [ALL]` and `security_opt: ["no-new-privileges:true"]` (checked by
  `scripts/check-image.sh`).

Known limits, honestly:

- It depends on the Boards plugin, which is in **maintenance mode** (see
  [development.md](development.md#when-boards-is-replaced)).
- It cannot know whether a person read the message, only that it was delivered.
- Dates are stored by Boards as a calendar day; the bot treats that day as the Lisbon calendar day.
  A due date typed with a time of day is converted to the Lisbon day.
