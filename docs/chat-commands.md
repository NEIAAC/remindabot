# Chat commands for administrators

System administrators control the bot from Mattermost with slash commands. Everything they do could
also be done by editing the compose file and the message file; the commands just make it a
conversation, and they show what the bot sees without reading logs.

| Command | What it does |
| --- | --- |
| `/remind_status` | The settings (and where each comes from), **what was sent today** and **what is waiting** for the next pass, shown separately, plus anything needing attention. |
| `/remind_send` | Runs a pass now: checks every card and sends what is due. Same rules, same caps; people already told are not told again. |
| `/remind_setmessage` | Opens a pop-up form with the current message to edit. It is checked before it is saved (a wrong `$variable` shows the reason inside the form). |
| `/remind_setsend HH:MM` | Sets the time of the daily pass, e.g. `/remind_setsend 09:30`. |
| `/remind_next [days]` | What will go out in the next days (default 7, at most 30), day by day, with the people. |
| `/remind_boards` | Which boards are read or skipped and why (wrong property type, private and not added, not joined). |
| `/remind_card <link or title>` | For one card: how `Remind Before` was read, today's decision, who gets what, what is coming. |
| `/remind_history [n]` | The last messages actually sent (default 10, at most 50). |
| `/remind_pause`, `/remind_resume` | Stop all sending until resumed. Survives restarts. Status and previews keep working. |
| `/remind_test` | Sends **you** a sample reminder with the current wording and a link, to preview a change. Nobody else gets it, and it uses no cap. |
| `/remind_resend <card> [@user]` | Sends today's reminder of one card again (to everyone in `Notifyees`, or one person). Counts against the caps. |
| `/remind_getmessage` | Shows the message in use and whether it is the file or one set from chat. |
| `/remind_setcap <total> [per person]` | Sets the daily safety caps, e.g. `/remind_setcap 200 30`, or `total=200` / `person=30`. |
| `/remind_reset message`, `time`, `caps` or `all` | Goes back to the defaults of the compose file and the message file. |
| `/remind_alerts on`, `off` or `status` | Opt in to a direct message from the bot when a pass fails, a board is set up wrongly or a cap is hit (at most once a day for each problem). |
| `/remind_help` | Lists these. It is the only one offered by autocomplete, so the rest do not clutter every member's list. |

Only **system administrators** may use them. At most three of the commands that read every board
(`status`, `next`, `boards`, `card`, `send`, `resend`) run at the same time; a fourth is told to try again in a minute.
 The bot asks Mattermost *at that moment* whether the
caller is one, so removing the role takes effect immediately, and anybody else gets a polite
refusal and nothing happens. Replies are visible only to the person who typed the command
(ephemeral). Commands that read every board answer "working..." at once and deliver the result a
few seconds later.

### How the settings behave

- **Where the settings live.** The compose file and `message.txt` are the *defaults*. What you
  change from chat is stored in the bot's state file (`/data/state.sqlite`, the same volume) and
  wins over them. `/remind_reset` removes the override, so the default applies again. Nothing
  changes the compose file or the message file.
- **Message.** The pop-up form edits the part of the message that is sent (not the comment header
  of the file). Line breaks and Markdown work; `$$` is a literal dollar sign. It is validated
  exactly like the file, and is at most 3000 characters.
- **Send time.** Takes effect within a minute. If today's pass already ran, it takes effect
  tomorrow; if the new time already passed today and the pass has not run, it runs at once.
- **Pause.** While paused the daily pass, `/remind_send`, `/remind_resend` and `--once` send
  nothing; a pass that was due is simply late and runs the minute you resume. `--once --dry-run`
  and `/remind_status` still show what *would* happen.
- **Caps.** A resent reminder counts as a message for the caps, so `/remind_resend` cannot be used
  to get around them.
- **Alerts.** Each opted-in administrator gets a direct message from the bot for: a failed pass,
  the state file being unusable (sending stops for the day), a broken message template, a board
  that is set up wrongly, cards due a reminder with nobody in `Notifyees`, or a daily cap being hit.

### Setting it up (once)

Slash commands call a small endpoint inside the bot (port 8080, only on the Docker network, never
published). Mattermost refuses to call private addresses unless told it may, so there are two steps:

1. **Allow Mattermost to call the bot.** In the Mattermost service of the compose file add
   `MM_SERVICESETTINGS_ALLOWEDUNTRUSTEDINTERNALCONNECTIONS=reminder` (the name of the bot's
   service), and recreate Mattermost. (Or System Console > Environment > Developer > *Allow
   untrusted internal connections*.)
2. **Register the commands** with the token of a system administrator (a personal access token;
   used for this one run only, the bot never keeps it):

   ```shell
   docker compose run --rm -e REMINDER_ADMIN_TOKEN=<admin token> reminder python -m remindabot --register-commands
   ```

   It creates the commands in every team and remembers their secret tokens. Run it again when a
   team is added. It also tells you if step 1 is missing.

Then type `/remind_help`. To turn the chat commands off, set `REMINDER_COMMANDS_PORT=0`.

To take the commands out of Mattermost (for example before removing the bot), run the same command
with `--unregister-commands` instead of `--register-commands`. Do that rather than just stopping
the bot: a command whose bot is gone answers `Command with a trigger of ... failed`, which looks
like a fault, while a removed one answers "unknown command".

### If a command does not work

| You see | Cause | Fix |
| --- | --- | --- |
| `Command with a trigger of 'remind_status' failed` | Mattermost cannot reach the bot: **the bot is not running** (the commands stay registered after it stops), step 1 is missing, a firewall blocks port 8080, or `REMINDER_COMMANDS_URL` points at the wrong name. | `docker compose ps reminder` first; then step 1 (`--register-commands` prints a warning for it). Remove commands you no longer want with `--unregister-commands`. |
| `not registered with the bot (wrong token)` | The commands were created again, or in a team after registering. | Run `--register-commands` again. |
| `Only system administrators can use the reminder commands` | The caller is not a system administrator. | Use an administrator account. |
| `Could not verify that you are a system administrator` | Mattermost did not answer the bot's role check. | Try again; check the bot's log. |
| `Unknown slash command` | The commands were never registered in that team. | `--register-commands`. |
| Nothing at all, no reply | The pop-up form or the late answer could not be delivered. | `docker compose logs reminder` shows the reason. |

### Security model

- The endpoint is reachable only on the Docker network, and each request carries a secret token
  that only Mattermost and the bot know (created when the commands are registered).
- Every command re-checks the caller's system administrator role with Mattermost. A forged
  `user_id` is useless without the token, and a forged token is useless to anybody not on the
  Docker network.
- The pop-up form's submission carries a signature the bot made (bound to the person, valid for 15
  minutes), because Mattermost does not sign it.
- Replies are built from text that is made inert first: a card titled `@all [click](http://evil)`
  neither pings anyone nor links anywhere in a reply.
- A command can only do what a pass can: sending goes through the same claim/confirm protocol and
  daily caps, so it cannot duplicate a message.
- Late answers go only to a command-response path of the bot's own Mattermost address, never to
  an address taken from the request.
