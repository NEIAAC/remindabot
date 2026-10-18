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
  the card), `$card_title`, `$board_title`, `$due_date`, `$due_date_long`, `$days_left`,
  `$days_left_text`, `$remind_before` (the reminder behind this message: `7` for the "7 days
  before" one), `$card_url`.
- A variable directly followed by a letter, digit or `_` needs braces: `${board_title}`.
  (Without braces `$board_title_` would be read as a different variable.)
- Write `$$` for a literal dollar sign.
- A wrong variable name makes the bot refuse to send, and the log explains why. It will not send a
  broken message.
- Everything above the `-----8<-----` line is a comment for editors and is never sent.
