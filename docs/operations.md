# Day-to-day operation and troubleshooting

```shell
docker compose logs -f reminder      # what it does: one line per message, one summary per pass
docker compose ps reminder           # "unhealthy" = the main loop stopped
```

The summary line, once per pass:

```
boards: 12 seen, 3 with the required properties, 0 joined, 1 unreadable | cards: 140 checked,
5 in reminder window | messages: 4 sent, 1 already sent, 0 failed
```

| Log message | Meaning | What to do |
| --- | --- | --- |
| `daily pass finished: ... N sent` | Normal. | Nothing. |
| `joined open board 'X' so its cards can be read` | The bot became a member of an open board. | Nothing (optionally run the read-only tool). |
| `open board 'X': the bot is not a member and may not join now` | `REMINDER_AUTO_JOIN=false`, or dry-run. | Run `--join-boards`, or add the bot to the board. |
| `cannot read board 'X' (private boards need the bot added...)` | A private board the bot is not a member of. | A board admin adds the bot as a member (Viewer is enough). |
| `board 'X' is set up for reminders but cannot be used: property 'Notifyees' is missing` | Wrong property type or missing property. | Fix the board (`Due Date` = Date, `Notifyees` = Multi person, `Remind Before` = Text). |
| `card 'X' ... is due a reminder but nobody is in 'Notifyees'` | The card has a reminder due and nobody to tell. | Add the people to `Notifyees` on the card. |
| `card 'X' ... 'Remind Before' contains values that are not a whole number of days ... ignored: tres` | Something between two `;` is not a whole number (a typo, a comma, a unit). | Fix the card, writing numbers separated by `;`. The other numbers in the list still work. |
| `a notifyee but not a member of board ... or its team; not messaging` | Someone outside the board/team is in Notifyees. | Usually nothing; check who edited the card. |
| `notifyee ... does not exist; skipped` | A deleted user or an invalid id is in Notifyees. | Fix the card. Not retried. |
| `sending to @x has an unknown outcome ... will verify before retrying` | A timeout or lost answer while sending. | Nothing: it checks before any re-send. |
| `daily safety cap reached` | More than the allowed messages in a day. | Check whether the volume is expected; raise the cap if so. |
| `pass failed (retrying in 10 min)` | Mattermost was unreachable or the token is invalid. | Check Mattermost and the token; it keeps retrying. |
| `message template problem, nothing sent` | A typo in `message.txt`. | Fix the variable named in the message. |
| `sending is PAUSED (by @x): today's pass waits for /remind_resume` | An administrator paused it. | `/remind_resume`. |
| `command /remind_x ... by @y` (INFO) / `command ... refused for @y: not a system administrator` | A chat command was run / refused. | Nothing; this is the audit trail of who changed what. |
| `command /remind_x from 'y' rejected: wrong or missing token` | A request to the endpoint without the right secret. | Re-run `--register-commands` if it was a real command; otherwise look at who can reach the Docker network. |
| `chat commands are NOT available: cannot listen on ...` | The port is taken. | Change `REMINDER_COMMANDS_PORT`; the reminders themselves are unaffected. |
| `cannot read the settings from the state file; not sending this minute` | The state file was busy or damaged. | Nothing if it stops; otherwise check the `reminder` volume. |
| `cannot open the state file ...` / `cannot use the state file ...` | The state file is damaged, or its directory is not writable. The bot touched nothing and sent nothing. | Restore the `reminder` volume from a backup, or move the file away to start afresh (everyone still inside a window is then reminded once more). |
| `CRITICAL ... Stopping for today to avoid duplicate messages` | The state file cannot be written. | Check the `reminder` volume (disk space, permissions) and restart. |

Common situations:

- **Nobody got a message although a card is due.** Run the dry run with `REMINDER_LOG_LEVEL=DEBUG`.
  It prints, for every card, why it was skipped (no valid `Remind Before`, a reminder not reached
  yet, no notifyees, template card...).
- **I changed the due date and nothing was sent.** Check the new date is within the largest number
  of `Remind Before`: the first reminder is that many days before the Due Date.
- **Send everything again** (for example after a test): stop the service and delete the
  `reminder` volume (`docker volume rm <project>_reminder`). Everyone still inside a window is then
  reminded once more.
- **Rotate the token.** Create a new token for the bot account, update `.env`, then
  `docker compose up -d reminder`. Revoke the old one.
- **Upgrading Mattermost.** The bot only uses public REST APIs, but Boards is unmaintained. After
  any upgrade, run `--check` and `--once --dry-run` and read the summary line.

## FAQ

**Can it remind about cards on a board it was never added to?**
Open boards: yes, it joins them (unless `REMINDER_AUTO_JOIN=false`). Private boards: only if a
board admin adds it.

**Why does a card with `Remind Before` empty get nothing?**
That is the opt-in: only cards with at least one number greater than 0 are reminded. Put a default
such as `7;1` on your card template so new cards inherit it.

**Why `Notifyees` and not just `Assignees`?**
So you can choose exactly who is told on each card, without changing who the card is assigned to.
If you would rather notify all assignees, set `REMINDER_PROP_NOTIFYEES=Assignees`.

**Can I be reminded on the Due Date itself?**
Not today: the smallest reminder is `1` (the day before), and `0` means "never". The rule lives in
`logic.usable_days` if you want to change that.

**Do template cards need `0` or `-1`?**
No. The bot skips cards Boards flags as templates by itself. Use `0` / `-1` for ordinary cards you
want ignored.

**Can I send the reminder to a channel instead of a direct message?**
Not today; sending is isolated in `Mattermost.send_dm` and `runner._remind_card`, so it is a small
change if you want it.

**Does it handle people who are on holiday, weekends, etc.?**
It reminds on the listed days, weekends too. The days are calendar days.

**Where is the data?**
Only in the `reminder` Docker volume (`/data/state.sqlite`): card ids and user ids of reminders
already sent, plus the date of the last pass. No message text is stored.

**Can I change the message or the time without redeploying?**
Yes, from chat: `/remind_setmessage` and `/remind_setsend`, as a system administrator. They are
stored in the bot's volume and override the file / compose file until `/remind_reset`.

**I changed the message in `message.txt` and nothing happened.**
A message set from chat wins over the file. `/remind_getmessage` says which one is in use;
`/remind_reset message` makes the file apply again.

**Who can use the commands?**
Only system administrators, checked on every call. Nobody else can pause the bot, change the
message, or see who is waiting for a reminder.

**Somebody says they never got a reminder.**
`/remind_card <title>` says whether it was due, who it was for and whether it was sent;
`/remind_history` shows the send; `/remind_resend <title> @them` sends it again.
