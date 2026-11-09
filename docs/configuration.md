# Configuration

Everything is an environment variable of the container (or a file, see below), read once at
startup. The message wording is a file; it is described at the end of this page.

Environment variables of the container (set them under `environment:` in the compose file):

| Variable | Default | Meaning |
| --- | --- | --- |
| `REMINDER_BOT_TOKEN` | – (required) | The bot's access token. Or give a file instead: `REMINDER_BOT_TOKEN_FILE` (see below). |
| `REMINDER_SITE_URL` | – (required) | Public address of Mattermost, used for links in messages. |
| `REMINDER_MM_URL` | `http://mattermost:8065` | Where the bot reaches Mattermost (inside the Docker network). |
| `REMINDER_TZ` | `TZ`, else `Europe/Lisbon` | Timezone for "today" and the send time. |
| `REMINDER_SEND_AT` | `14:00` | Daily run time `HH:MM`, every day of the week, weekends included. Started or restarted after that time, the bot runs the day's pass at once (once); started before it, it waits. |
| `REMINDER_DAILY_PASS` | `true` | `false` = no daily pass at all: the bot only answers chat commands and `--once`. The dev stack uses it so a playground never sends by itself. |
| `REMINDER_PROP_DUE` / `_NOTIFYEES` / `_REMIND` | `Due Date` / `Notifyees` / `Remind Before` | Property names to look for. To notify everybody in `Assignees` instead (the first version's behaviour), set `REMINDER_PROP_NOTIFYEES=Assignees`. |
| `REMINDER_AUTO_JOIN` | `true` | Join open boards by itself. `false` = only boards the bot was added to. |
| `REMINDER_MAX_MESSAGES_PER_DAY` | `100` | Safety valve: never send more than this many in one day. |
| `REMINDER_MAX_MESSAGES_PER_USER_PER_DAY` | `20` | Same, per person, so nobody can be flooded. |
| `REMINDER_TEMPLATE_FILE` | `/config/message.txt` | Message template path inside the container. |
| `REMINDER_STATE_FILE` | `/data/state.sqlite` | The "who was already reminded" file. |
| `REMINDER_COMMANDS_PORT` | `8080` | Port of the chat-command endpoint (inside the container; do not publish it). `0` switches the chat commands off. |
| `REMINDER_COMMANDS_BIND` | `0.0.0.0` | Address it listens on inside the container. |
| `REMINDER_COMMANDS_URL` | `http://reminder:8080` | How **Mattermost** reaches the endpoint (the service name on the Docker network). Used when registering the commands. |
| `REMINDER_ADMIN_TOKEN` | – | Only for `--register-commands` / `--unregister-commands`: a system administrator's token, for that one run (or `REMINDER_ADMIN_TOKEN_FILE`). Never put it in the compose file. |
| `REMINDER_HEARTBEAT_FILE` | `/tmp/heartbeat` | A file touched while the bot is alive; the container health check reads it. Leave the default: the image's `HEALTHCHECK` looks at that path. |
| `REMINDER_LOG_LEVEL` | `INFO` | `DEBUG` also logs why every card was skipped. |

**Secrets from a file.** Any setting can also be given as `REMINDER_<NAME>_FILE`, the path of a file holding
the value (the Docker secrets convention), so a token does not have to sit in the environment or in
`docker inspect`: `REMINDER_BOT_TOKEN_FILE=/run/secrets/bot_token`. The plain variable wins when both
are set, and a file that cannot be read stops the bot at startup, naming the variable.

A blank value counts as "not set" (Docker Compose passes `VAR=` through for a missing `${VAR}`).
A badly formed value stops the bot at startup with a clear message and exit code 2.

## Changing the message

The text is a plain file with `$variables`; the top of the file lists them. The image ships a
default at `/config/message.txt`; mount your own copy over it as in the compose snippet. The file is
**re-read on every daily pass**, so no rebuild or restart is needed.

Things to know when editing:

- Variables: `$responsible_name` (who receives it), `$co_responsibles` (the *other* notifyees of
  the card), `$card_title`, `$board_title`, `$due_date` (`2026-10-25`), `$due_year` (`2026`),
  `$due_month` (`10`), `$due_day` (`25`), `$days_left` (`12`, until the due date), `$remind_before`
  (the reminder behind this message: `7` for the "7 days before" one), `$card_url`, and the card's
  **last reminder**: `$last_remind_days_left` (the smallest number of `Remind Before`, e.g. `10`),
  `$last_remind_date` (the due date minus that, `2026-10-15`) and its parts `$last_remind_year`,
  `$last_remind_month`, `$last_remind_day`.
- **Counting to the last reminder.** Variables joined by `-` are subtracted, so with
  `Remind Before = 12;10` and a Due Date of 25 Oct:

  ```
  by **$last_remind_date** (**$days_left-$last_remind_days_left days left**)
  ```

  says "by **2026-10-15** (**2 days left**)" in the reminder sent on 13 Oct and "0 days left" in the one
  sent on 15 Oct. A card the bot sees after its last reminder day gets the real, negative number
  (`-2 days left`).
- **Subtracting.** Any two or more variables that hold whole numbers can be subtracted, left to right,
  with no spaces around the `-`: the days (`$days_left`, `$remind_before`, `$last_remind_days_left`) and
  the parts of a date (`$due_year`, `$due_month`, `$due_day`, `$last_remind_*`). If any of them is not a
  whole number, nothing is calculated and the text is shown as it is (`Ana-12`).
- **A hyphen between two variables is a subtraction, so write `\-` when you want the hyphen.** To show the
  date `25-10-2026` write `$due_day\-$due_month\-$due_year` (`$due_day-$due_month-$due_year` would show
  `-2011`). A `\-` is a plain `-` and ends the subtraction before it: `$days_left-$last_remind_days_left\-$due_day`
  shows `2-25`. It is only special between two variables; anywhere else it is left as you typed it.
  The same goes for a card or board whose title is a number (`$card_title-$board_title` for the cards
  `7` and `3` shows `4`): use `\-` between titles.
- **The bot adds no words of its own to a message.** Every word is in the file, so another
  language is just another file. That is why a date comes as numbers (write it your way:
  `$due_day/$due_month/$due_year` for 25/10/2026) and why there is no "N days left" phrase:
  write `$days_left days left` yourself. The number is not declined, so one day reads "1 days
  left"; that is accepted, a reminder one day ahead being rare. Where a name or a card title is
  missing, the message shows a dash (`—`).
- A variable directly followed by a letter, digit or `_` needs braces: `${board_title}`.
  (Without braces `$board_title_` would be read as a different variable.)
- Write `$$` for a literal dollar sign.
- A wrong variable name makes the bot refuse to send, and the log explains why. It will not send a
  broken message.
- Everything above the `-----8<-----` line is a comment for editors and is never sent.
