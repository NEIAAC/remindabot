"""The bot against the running dev stack: a REAL Mattermost, a REAL Boards plugin, real chat commands.

    docker compose up -d --build
    docker compose --profile test run --rm e2e          # everything below, in order

What the stack holds is what scripts/dev-seed.sh made: the expected numbers below come from its demo
boards. If you change the seed, change them here too.

The steps run in order and build on each other (a real send happens once, the bot is made
read-only after it), so this is ONE scenario, not independent tests. Two things change the stack:
  * the real send (step 3) delivers messages to the fake people and makes the bot join a board, and
    step 5 makes the bot read-only on its boards. A stack can go through them once; on a used stack
    those steps are skipped and the rest runs. For a clean one:  docker compose down -v && up -d.
  * the chat commands (step 7) change and put back the daemon's settings, and send two messages to
    devadmin. They can be repeated.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path

from . import support as s

# What the demo data must produce (see scripts/dev-seed.sh).
# Summary line: boards seen, with the properties, joined, unreadable | cards checked, in window | messages.
DRY_RUN_COUNTS = (5, 2, 0, 1, 16, 6, 7)
SEND_COUNTS = (5, 2, 1, 0, 17, 7, 8)             # a real run also joins the new board and sends its message
MESSAGES = (                                       # (person, card title starts with, stage)
    ("devadmin", "Mensagem para o devadmin", 3),
    ("alice", "Cartaz do evento", 1),
    ("carol", "Sessao de fotos", 3),
    ("bob", "Com gralha", 7),
    ("alice", "Workshop de Python", 7),
    ("bob", "Workshop de Python", 7),
    ("bob", "Um so lembrete", 12),
)
SEND_EXTRA = ("alice", "Cartao num quadro por entrar", 7)
WARNINGS = (                                       # each must appear (as a substring) among the WARNING lines
    "ignored: tres",
    "ignored: 7,3,1",
    "ignored: 7 dias",
    "nobody is in 'Notifyees'",
    "'Remind Before' has type 'number' but must be one of ['text']",
    "'Remind Before' has type 'multiSelect' but must be one of ['text']",
    "'Quadro novo (bot ainda nao entrou)': the bot is not a member",
)
DELIVERED_PER_PERSON = {"alice": 3, "bob": 3, "carol": 1}      # in a real run, new messages per person
DEVADMIN_CARD = "Mensagem para o devadmin"                      # the one demo card whose notifyee can log in
CARD_LINK = r"/boards/team/\w+/\w+/\w+/\w+"

SUMMARY = re.compile(
    r"boards: (\d+) seen, (\d+) with the required properties, (\d+) joined, (\d+) unreadable \| "
    r"cards: (\d+) checked, (\d+) in reminder window \| messages: (\d+) (?:would be sent|sent)")
WOULD_SEND = re.compile(r"would send to @(\w+) about (['\"])(.*?)\2 \(the (\d+)-days-before reminder\)")


def setUpModule():
    if not s.stack_files_present():
        raise unittest.SkipTest("the dev stack is not running (docker compose up -d); see tests/e2e/__init__.py")


class LiveStack(unittest.TestCase):
    fresh = False          # set by the dry run: the stack is in its seeded state, nothing was sent yet

    @classmethod
    def setUpClass(cls):
        cls.bot_token = s.read(s.SECRETS_BOT / "token")
        cls.admin_token = s.read(s.SECRETS_ADMIN / "token")
        cls.seed = json.loads((s.SECRETS_ADMIN / "dev-state.json").read_text())
        cls.tmp = tempfile.TemporaryDirectory()
        cls.state = Path(cls.tmp.name) / "state.sqlite"
        cls.admin_id = s.call("/api/v4/users/me", cls.admin_token)[1]["id"]
        cls.bot_id = cls.seed["bot"]

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    # -- helpers ------------------------------------------------------------------------------------

    def check(self, condition, what, detail=""):
        """One named check: a failure is reported and the scenario goes on to the next one."""
        with self.subTest(what):
            self.assertTrue(condition, str(detail)[:1500])
        return bool(condition)

    def run_bot(self, *args, extra=None):
        """The bot's command line, as a person would run it. Returns (exit code, output)."""
        env = {k: v for k, v in os.environ.items() if not k.startswith("REMINDER_")}
        env.update({
            "REMINDER_BOT_TOKEN": self.bot_token, "REMINDER_MM_URL": s.MM_URL, "REMINDER_SITE_URL": s.SITE_URL,
            "REMINDER_TEMPLATE_FILE": str(s.REPO / "config" / "message.txt"), "REMINDER_STATE_FILE": str(self.state),
            "REMINDER_HEARTBEAT_FILE": str(self.state.parent / "heartbeat"), "REMINDER_LOG_LEVEL": "INFO",
            "REMINDER_TZ": "Europe/Lisbon", "PYTHONDONTWRITEBYTECODE": "1",
            "REMINDER_COMMANDS_PORT": "0",      # a one-off run must not start the chat endpoint
            "PYTHONPATH": str(s.REPO / "src"),
        })
        env.update(extra or {})
        done = subprocess.run([sys.executable, "-m", "remindabot", *args], cwd=s.REPO, env=env,
                              capture_output=True, text=True, timeout=300)
        return done.returncode, done.stdout + done.stderr

    @staticmethod
    def summary_numbers(output):
        found = SUMMARY.findall(output)
        return tuple(int(n) for n in found[-1]) if found else None

    @staticmethod
    def would_send(output):
        return [(who, title, int(stage)) for who, _q, title, stage in WOULD_SEND.findall(output)]

    @staticmethod
    def matches(actual, with_extra):
        """Do the messages found equal the expected ones (same people, same reminders, same cards)?"""
        wanted = list(MESSAGES) + ([SEND_EXTRA] if with_extra else [])
        remaining = list(actual)
        for who, title_start, stage in wanted:
            hit = next((row for row in remaining
                        if row[0] == who and row[1].startswith(title_start) and row[2] == stage), None)
            if hit is None:
                return False
            remaining.remove(hit)
        return not remaining

    def dm_channel(self, token, user_id):
        return s.call("/api/v4/channels/direct", token, "POST", [user_id, self.bot_id])[1]["id"]

    def dm_count(self, channel, token):
        return len(s.call(f"/api/v4/channels/{channel}/posts", token)[1].get("order", []))

    # -- 1 -------------------------------------------------------------------------------------------

    def test_01_the_bot_can_talk_to_the_stack(self):
        code, out = self.run_bot("--check")
        self.check(code == 0, "--check: token works, teams listed, template valid", out)

    # -- 2 -------------------------------------------------------------------------------------------

    def test_02_a_dry_run_says_exactly_what_is_expected_and_writes_nothing(self):
        code, out = self.run_bot("--once", "--dry-run")
        self.check(code == 0, "the dry run finishes with exit code 0", out)
        counts = self.summary_numbers(out)
        type(self).fresh = self.check(
            counts == DRY_RUN_COUNTS,
            f"summary numbers are as expected (boards seen/with properties/joined/unreadable | cards/in window | "
            f"would send = {DRY_RUN_COUNTS})",
            f"got {counts}\nIf the stack was already used by the send step, start a clean one "
            "(docker compose down -v; docker compose up -d).\n" + out[-600:])
        self.check(self.matches(self.would_send(out), with_extra=False),
                   "the seven messages are exactly: who, which card, which reminder",
                   "\n".join(map(str, self.would_send(out))))
        warnings = [line for line in out.splitlines() if " WARNING " in line]
        for wanted in WARNINGS:
            self.check(any(wanted in line for line in warnings), f"warning present: {wanted}", "\n".join(warnings))
        self.check(not any("@carol" in line and "Cartao privado" in line for line in out.splitlines()),
                   "the private board's card is not read or messaged")
        self.check(not self.state.exists(), "no state file was created by the dry run")
        links = re.findall(re.escape(s.SITE_URL) + CARD_LINK, out)
        self.check(len(links) == 7, "every message carries a full card link (team/board/view/card)", out[-400:])

    # -- 3 -------------------------------------------------------------------------------------------

    def test_03_a_real_send_delivers_to_the_right_people(self):
        if not self.fresh:
            self.skipTest("the stack was already used for a real send; docker compose down -v && up -d for a clean one")
        channels = {name: self.dm_channel(self.bot_token, user_id) for name, user_id in self.seed["users"].items()}
        before = {name: self.dm_count(channel, self.bot_token) for name, channel in channels.items()}

        code, out = self.run_bot("--once")
        self.check(code == 0, "the real run finishes with exit code 0", out)
        self.check(self.summary_numbers(out) == SEND_COUNTS, f"summary numbers are as expected ({SEND_COUNTS})",
                   f"got {self.summary_numbers(out)}\n" + out[-600:])
        for name, expected in DELIVERED_PER_PERSON.items():
            posts = s.call(f"/api/v4/channels/{channels[name]}/posts", self.bot_token)[1]
            new = len(posts.get("order", [])) - before[name]
            self.check(new == expected, f"@{name} received {expected} new message(s) (got {new})")
            messages = [posts["posts"][i]["message"] for i in posts["order"]][:new]
            self.check(all(re.search(CARD_LINK + r"\)", m) for m in messages), f"@{name}: every message links to the card")

        # what devadmin finds when logging in to the website (the one person who can)
        admin_channel = channels["devadmin"]
        posts = s.call(f"/api/v4/channels/{admin_channel}/posts", self.admin_token)[1]
        newest = [posts["posts"][i] for i in posts["order"]][:len(posts["order"]) - before["devadmin"]]
        self.check(len(newest) == 1, f"devadmin received exactly 1 new message (got {len(newest)})")
        if newest:
            message = newest[0]["message"]
            self.check(newest[0]["user_id"] == self.bot_id, "... and it is from the bot (Remindabot)")
            # devadmin has no nickname or full name, so the greeting is "@username" with an invisible
            # zero-width space after the "@" (U+200B): it reads as a name but does not ping anybody
            self.check(message.startswith("Olá @​devadmin!"), "... addressed to devadmin by name", message[:120])
            self.check(DEVADMIN_CARD in message, "... about the card that lists devadmin as a notifyee", message[:300])
            link = re.search(r"/boards/team/(\w+)/(\w+)/(\w+)/(\w+)\)", message)
            self.check(bool(link), "... with a full card link", message[-300:])
            if link:
                _team, board, view, card = link.groups()
                status, body = s.call(f"{s.BOARDS_API}/cards/{card}", self.admin_token)
                self.check(status == 200 and isinstance(body, dict) and body.get("boardId") == board
                           and body.get("title", "").startswith(DEVADMIN_CARD),
                           "... and the link's ids really are that card on that board", str(body)[:200])
                status, views = s.call(f"{s.BOARDS_API}/boards/{board}/blocks?type=view", self.admin_token)
                self.check(status == 200 and any(v.get("id") == view for v in views),
                           "... and the view in the link belongs to that board", str(views)[:200])

    # -- 4 -------------------------------------------------------------------------------------------

    def test_04_a_second_run_repeats_nothing(self):
        if not self.fresh:
            self.skipTest("needs the real send of step 3")
        code, out = self.run_bot("--once")
        numbers = self.summary_numbers(out)
        self.check(code == 0 and numbers is not None and numbers[6] == 0, "0 messages sent the second time", out[-500:])
        self.check("8 already sent" in out, "all 8 are recognised as already sent", out[-500:])

    # -- 5 -------------------------------------------------------------------------------------------

    def run_read_only_script(self, *extra):
        env = {**os.environ, "REMINDER_BOT_TOKEN": self.bot_token, "ADMIN_TOKEN": self.admin_token}
        done = subprocess.run(["bash", str(s.REPO / "scripts" / "make-bot-read-only.sh"), s.MM_URL, *extra],
                              env=env, capture_output=True, text=True, timeout=180)
        return done.returncode, done.stdout + done.stderr

    def test_05_the_bot_can_be_made_read_only(self):
        if not self.fresh:
            self.skipTest("needs a stack where the bot still has editor rights")
        code, plan = self.run_read_only_script()
        self.check(code == 0 and "EDITOR RIGHTS" in plan and "Nothing was changed" in plan,
                   "the plan lists boards where the bot is an editor and changes nothing", plan)
        code, applied = self.run_read_only_script("--apply")
        self.check(code == 0 and "0 failed" in applied, "--apply makes every board read-only", applied)
        code, again = self.run_read_only_script()
        self.check("0 still need to be made read-only" in again, "afterwards nothing is left to change", again)
        posts_board = self.seed["boards"]["text"]
        status, cards = s.call(f"{s.BOARDS_API}/boards/{posts_board}/cards", self.bot_token)
        self.check(status == 200 and cards, "the read-only bot can still read the cards", str(cards)[:200])
        status, body = s.call(f"{s.BOARDS_API}/cards/{cards[0]['id']}", self.bot_token, "PATCH",
                              {"title": "changed by the bot?"})
        self.check(status in (401, 403), f"... and can no longer edit them (HTTP {status})", str(body)[:200])

    # -- 6 -------------------------------------------------------------------------------------------

    def test_06_a_read_only_bot_still_delivers_a_brand_new_reminder(self):
        if not self.fresh:
            self.skipTest("needs the real send of step 3 and the read-only bot of step 5")
        posts_board = self.seed["boards"]["text"]
        status, board = s.call(f"{s.BOARDS_API}/boards/{posts_board}", self.admin_token)
        ids = {p["name"]: p["id"] for p in board.get("cardProperties", [])} if status == 200 else {}
        created, card = 0, None
        if {"Due Date", "Notifyees", "Remind Before"} <= set(ids):
            created, card = s.call(f"{s.BOARDS_API}/boards/{posts_board}/cards?disable_notify=true", self.admin_token,
                                   "POST", {"title": "Cartao novo (o bot ja so le)", "properties": {
                                       ids["Due Date"]: json.dumps({"from": s.noon_utc_ms(1)}),
                                       ids["Notifyees"]: [self.admin_id], ids["Remind Before"]: "7;3;1"}})
        self.check(created == 200, "a new card for devadmin was added by the admin", str(card)[:200])
        channel = self.dm_channel(self.admin_token, self.admin_id)
        before = self.dm_count(channel, self.admin_token)
        code, out = self.run_bot("--once")
        numbers = self.summary_numbers(out)
        self.check(code == 0 and numbers is not None and numbers[6] == 1, "exactly 1 new message is sent", out[-500:])
        posts = s.call(f"/api/v4/channels/{channel}/posts", self.admin_token)[1]
        self.check(len(posts.get("order", [])) == before + 1, "... and it reached devadmin")
        newest = posts["posts"][posts["order"][0]]["message"] if posts.get("order") else ""
        self.check("falta 1 dia" in newest and "Cartao novo" in newest, "... saying 1 day is left", newest[:200])

    # -- 7 -------------------------------------------------------------------------------------------

    def test_07_the_chat_commands_work_through_mattermost(self):
        """The bot that runs in the stack (service `reminder`), called the way the web app calls it."""
        admin, team = self.admin_token, self.seed["team"]
        channel = s.call(f"/api/v4/teams/{team}/channels/name/town-square", admin)[1]["id"]

        status, commands = s.call(f"/api/v4/commands?team_id={team}&custom_only=true", admin)
        ours = [c for c in commands if str(c.get("trigger", "")).startswith("remind_")] if status == 200 else []
        self.check(len(ours) >= 15 and all(c["url"].startswith(s.COMMANDS_URL) for c in ours),
                   "the commands are registered in the team and point at the running bot",
                   f"{len(ours)} found: {sorted(c.get('url', '') for c in ours)[:2]}")
        try:
            with urllib.request.urlopen(s.BOT_URL + "/healthz", timeout=5) as resp:
                up = resp.status == 200
        except OSError:
            up = False
        if not self.check(up, "the daemon is running and its chat endpoint answers /healthz"):
            return

        events = s.Events(admin)
        alice = s.call(f"/api/v4/users/{self.seed['users']['alice']}/tokens", admin, "POST",
                       {"description": "e2e"})[1]
        self.addCleanup(lambda: alice.get("id") and s.call("/api/v4/users/tokens/revoke", admin, "POST",
                                                           {"token_id": alice["id"]}))
        self.addCleanup(events.close)

        def execute(command, who=admin):
            """Run a slash command through Mattermost; returns the immediate reply text."""
            status, body = s.call("/api/v4/commands/execute", who, "POST", {"channel_id": channel, "command": command})
            if status != 200 or not isinstance(body, dict):
                return f"[HTTP {status}] {str(body)[:200]}"
            return body.get("text", "")

        def slow(command, expect, seconds=25.0):
            """A command that answers later: the text of the late ephemeral message that contains `expect`."""
            n = events.count()
            execute(command)
            return s.ephemeral_text(events.wait("ephemeral_message", expect, after=n, seconds=seconds))

        def dm_posts():
            ch = self.dm_channel(admin, self.admin_id)
            data = s.call(f"/api/v4/channels/{ch}/posts?per_page=30", admin)[1]
            return [data["posts"][i] for i in data["order"]]

        text = execute("/remind_help")
        self.check("/remind_status" in text and "/remind_setmessage" in text, "/remind_help lists the commands", text[:300])
        text = execute("/remind_pause", alice["token"]) if alice.get("token") else ""
        self.check("Only system administrators" in text, "a non-administrator is refused", text)
        text = slow("/remind_status", "Waiting for the next pass")
        self.check("Reminder bot status" in text and "active" in text,
                   "a non-administrator could not pause: status is active", text[:300])
        self.check("Sent today" in text and "Safety caps" in text and "Board properties" in text,
                   "/remind_status shows the settings and today's sent / waiting lists (late answer)", text[:600])

        text = execute("/remind_setsend 10:15")
        self.check("10:15" in text, "/remind_setsend 10:15 is accepted", text)
        self.check("10:15" in slow("/remind_status", "Waiting for the next pass"), "... and /remind_status shows it")
        self.check("defaults" in execute("/remind_reset time"), "/remind_reset time goes back")
        self.check(":warning:" in execute("/remind_setsend 25:99"), "an impossible time is refused")
        text = execute("/remind_setcap 500 50")
        self.check("**500**" in text and "**50**" in text, "/remind_setcap 500 50", text)
        execute("/remind_reset caps")

        execute("/remind_pause")
        text = slow("/remind_send", "paused")
        self.check("paused" in text.lower(), "while paused /remind_send refuses", text)
        text = execute("/remind_resume")
        self.check("**active** again" in text, "/remind_resume lifts the pause", text)

        n_before = len(dm_posts())
        text = execute("/remind_test")
        self.check("preview was sent" in text, "/remind_test says it sent a preview", text)
        posts = dm_posts()
        self.check(len(posts) == n_before + 1 and "Preview" in posts[0]["message"],
                   "... and the preview arrived in devadmin's direct messages", posts[0]["message"][:120] if posts else "")

        n_before = len(dm_posts())
        text = slow("/remind_resend Mensagem para o devadmin", "Resent")
        self.check("1 sent" in text, "/remind_resend sends the card's reminder again", text[:300])
        posts = dm_posts()
        self.check(len(posts) == n_before + 1 and "Mensagem para o devadmin" in posts[0]["message"],
                   "... and devadmin received it", posts[0]["message"][:120] if posts else "")

        text = slow("/remind_next 7", "cheduled")           # "Scheduled reminders" or "Nothing is scheduled"
        self.check("cheduled" in text and "devadmin" in text, "/remind_next lists the coming days with the people", text[:300])
        text = slow("/remind_boards", "Boards the bot can see")
        self.check("Posts" in text and "Remind Before como Numero" in text and "set up wrongly" in text,
                   "/remind_boards lists the boards with their state and why", text[:400])
        self.check("| Board | Kind | State | Detail |\n" in text and "\n\n| Board" in text,
                   "... as a table that Mattermost draws (a blank line above it)", text[:300])
        text = slow("/remind_card Mensagem para o devadmin", "read as")
        self.check("**7, 3, 1**" in text, "/remind_card explains how Remind Before was read", text[:300])
        text = execute("/remind_history 5")
        self.check("Mensagem para o devadmin" in text, "/remind_history shows what was sent", text[:300])

        # the pop-up form
        n = events.count()
        execute("/remind_setmessage")
        opened = events.wait("open_dialog", "Reminder message", after=n)
        self.check(opened is not None, "/remind_setmessage opens a pop-up form for the administrator")
        if not opened:
            return
        form = json.loads(opened["data"]["dialog"])
        dialog = form["dialog"]
        field = dialog["elements"][0]
        self.check(field["type"] == "textarea" and "$responsible_name" in field["default"],
                   "... a text box prefilled with the current message", str(field)[:200])
        submit = {"url": form["url"], "callback_id": dialog["callback_id"], "state": dialog["state"],
                  "channel_id": channel, "team_id": team, "cancelled": False,
                  "submission": {"message": "Olá $responsible_name!\nVerificação: $card_title"}}
        wrong = s.call("/api/v4/actions/dialogs/submit", admin, "POST", {**submit, "submission": {"message": "Olá $card_titel"}})
        self.check(wrong[0] == 200 and "card_titel" in json.dumps(wrong[1]),
                   "a message with a typo is rejected with the reason shown in the form", str(wrong)[:300])
        good = s.call("/api/v4/actions/dialogs/submit", admin, "POST", submit)
        self.check(good[0] == 200 and json.dumps(good[1]).count("error") == 0, "a valid message is saved", str(good)[:300])
        text = execute("/remind_getmessage")
        self.check("custom message" in text and "Verificação" in text, "/remind_getmessage shows the new text", text[:300])
        before = len(dm_posts())
        execute("/remind_test")
        preview = dm_posts()
        self.check(len(preview) == before + 1 and "Verificação: Cartão de exemplo" in preview[0]["message"],
                   "/remind_test uses the new message", preview[0]["message"][:160] if preview else "")
        self.check("message" in execute("/remind_reset message"), "/remind_reset message goes back to the file")
        forged = s.call("/api/v4/actions/dialogs/submit", admin, "POST", {**submit, "state": "forged"})
        self.check("expired" in json.dumps(forged[1]), "a forged form state is refused", str(forged)[:200])

    # -- 8 -------------------------------------------------------------------------------------------

    def test_08_the_bot_is_no_threat_to_the_rest_of_the_real_server(self):
        """Least privilege and abuse resistance, measured on the real Mattermost and the real endpoint."""
        admin, bot, team = self.admin_token, self.bot_token, self.seed["team"]
        alice_id = self.seed["users"]["alice"]

        # the bot's account is an ordinary member, never an administrator or any other special role
        status, account = s.call(f"/api/v4/users/{self.bot_id}", admin)
        self.check(status == 200 and set(str(account.get("roles")).split()) <= {"system_user"},
                   "the bot's account has no role besides the plain member role", str(account)[:200])
        # ... and its token cannot do what only an administrator can. Creating a team is NOT in this
        # list on purpose: Mattermost lets every member do that by default (the server's choice, in
        # System Console > Permissions), so the check would create a team on a stock server.
        for what, method, path, body in (
                ("create a user", "POST", "/api/v4/users", {"email": "x@dev.local", "username": "botmade", "password": "Aa1-botmade-pw"}),
                ("change somebody's roles", "PUT", f"/api/v4/users/{alice_id}/roles", {"roles": "system_user system_admin"}),
                ("read the server configuration", "GET", "/api/v4/config", None),
                ("create a command", "POST", "/api/v4/commands", {"team_id": team, "trigger": "botmade", "url": "http://x", "method": "P"})):
            status, answer = s.call(path, bot, method, body)
            self.check(status in (401, 403), f"the bot's token cannot {what} (HTTP {status})", str(answer)[:200])

        # no ordinary member can run any of the bot's commands, through the real Mattermost
        status, commands = s.call(f"/api/v4/commands?team_id={team}&custom_only=true", admin)
        triggers = sorted(c["trigger"] for c in commands if str(c.get("trigger", "")).startswith("remind_"))
        self.check(len(triggers) >= 15, "the commands are registered", str(triggers))
        alice = s.call(f"/api/v4/users/{alice_id}/tokens", admin, "POST", {"description": "e2e security"})[1]
        self.addCleanup(lambda: alice.get("id") and s.call("/api/v4/users/tokens/revoke", admin, "POST",
                                                           {"token_id": alice["id"]}))
        channel = s.call(f"/api/v4/teams/{team}/channels/name/town-square", admin)[1]["id"]
        for trigger in triggers:
            status, reply = s.call("/api/v4/commands/execute", alice["token"], "POST",
                                   {"channel_id": channel, "command": f"/{trigger}"})
            text = reply.get("text", "") if isinstance(reply, dict) else str(reply)
            self.check("Only system administrators" in text, f"an ordinary member is refused /{trigger}", f"{status} {text[:150]}")

        # the endpoint itself: forged, malformed and oversized requests change nothing and do no harm
        import urllib.parse
        forged = urllib.parse.urlencode({"command": "/remind_pause", "user_id": self.admin_id, "user_name": "devadmin",
                                         "team_id": team, "token": "not-the-token", "text": ""}).encode()
        status, text = s.post_raw(s.BOT_URL + "/command", forged)
        self.check(status == 200 and "wrong token" in text, "a forged command (no valid token) is refused", f"{status} {text[:150]}")
        status, text = s.post_raw(s.BOT_URL + "/command", b"")
        self.check(status == 200, f"an empty command is answered, not crashed (HTTP {status})")
        status, _ = s.post_raw(s.BOT_URL + "/dialog", b"{not json", "application/json")
        self.check(status == 400, f"a malformed form submission is refused (HTTP {status})")
        status, text = s.post_raw(s.BOT_URL + "/dialog", b'{"callback_id": "setmessage", "state": "forged", "user_id": "x"}', "application/json")
        self.check(status == 200 and "expired" in text, "a forged form submission is refused", f"{status} {text[:150]}")
        status, _ = s.post_raw(s.BOT_URL + "/command", b"x" * (300 * 1024))
        self.check(status in (0, 413), f"an oversized request is refused (HTTP {status})")
        status, _ = s.post_raw(s.BOT_URL + "/command", b"", method="PUT")
        self.check(status in (0, 501), f"an unknown method is refused (HTTP {status})")
        status, text = s.post_raw(s.BOT_URL + "/healthz", b"", method="GET")
        self.check(status == 200 and "ok" in text, "after all that the endpoint is still healthy", f"{status} {text[:80]}")
        answered = s.call("/api/v4/commands/execute", admin, "POST", {"channel_id": channel, "command": "/remind_status"})
        self.check(answered[0] == 200, "... and the real commands still answer")


if __name__ == "__main__":
    unittest.main()
