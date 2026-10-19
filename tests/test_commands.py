"""The chat commands (/remind_*): who may run them, what each one does, and the HTTP endpoint.

These run the real `CommandRouter`, runner, store and client against the fake Mattermost. The slash
command is delivered the way Mattermost does it: a form with the user's id and the command's token.
"""
import json
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

from . import test_integration as ti
from remindabot import render
from remindabot.commands import SPECS, CommandRouter, find_cards
from remindabot.commands.markup import table
from remindabot.driver import Driver, for_command_reply
from remindabot.boards import Boards
from remindabot.runner import CardReport
from remindabot.webhook_server import MAX_BODY_BYTES, start_server
from remindabot.overrides import Overrides
from .support.fakeserver import ADMIN_ID, BOT_TOKEN, Fault

LISBON, TODAY, U1, U2, U3 = ti.LISBON, ti.TODAY, ti.U1, ti.U2, ti.U3
NOW = datetime(2026, 10, 13, 10, 0, tzinfo=LISBON)      # TODAY, after the 09:00 send time
RESPONSE_URL = "https://chat.example/hooks/commands/abcdefghij1234567890"
# What the Mattermost server does to the text of every slash command reply (a Slack-compatibility
# rewrite, found in the real web UI: it ate the `/remind_card` row of the help table).
SLACK_LINK = re.compile(r"<([^<|]+)\|([^>]+)>")


class CommandsBase(ti.Base):
    def setUp(self):
        super().setUp()
        self.fake.add_user(ADMIN_ID, "root", roles="system_admin system_user")
        for user in self.fake.users.values():
            user.setdefault("roles", "system_user")
        self.now = NOW

        def clients():
            mm, boards = Driver(self.url, BOT_TOKEN, timeout=5), Boards(self.url, BOT_TOKEN, timeout=5)
            for api in (mm.api, boards.api, mm._hooks):
                api._sleep = lambda s: None
            return mm, boards

        self.lock = threading.Lock()
        self.commands = CommandRouter(self.cfg, clients=clients, open_store=self.store, lock=self.lock,
                                 now_fn=lambda: self.now, background=lambda fn: fn())
        store = self.store()
        Overrides(store).set_command_tokens({"team1": {s.trigger: f"tok-{s.trigger}" for s in SPECS}})
        store.close()

    # -- helpers --------------------------------------------------------------------------------

    def form(self, trigger, args="", *, user=ADMIN_ID, username="root", token=None, team="team1"):
        return {"command": "/" + trigger, "text": args, "user_id": user, "user_name": username, "team_id": team,
                "token": f"tok-{trigger}" if token is None else token, "trigger_id": "trig123",
                "response_url": RESPONSE_URL, "channel_id": "c" * 26}

    def run_command(self, trigger, args="", **kw) -> str:
        """The reply text, whether it came at once or later through the response URL."""
        spec = next(s for s in SPECS if s.trigger == trigger)
        before = len(self.fake.delayed)
        reply = self.commands.handle_command(self.form(trigger, args, **kw))
        if spec.slow and "Working on" in reply.get("text", ""):
            self.assertEqual(len(self.fake.delayed), before + 1, "a slow command must answer through response_url")
            text = self.fake.delayed[-1]["text"]
        else:
            text = reply.get("text", "")
        self.assert_draws_right(text, trigger)
        return text

    def assert_draws_right(self, text, what=""):
        """Every reply of every command must survive Mattermost's drawing of it (checked for all
        of them here, because each command is run through run_command)."""
        self.assertNotIn("<", text, f"{what}: Mattermost would rewrite `<a|b>` into a link")
        self.assertIsNone(SLACK_LINK.search(text), what)
        lines = text.split("\n")
        for n, line in enumerate(lines):
            if not line.startswith("|"):
                continue
            above = lines[n - 1] if n else ""
            below = lines[n + 1] if n + 1 < len(lines) else ""
            self.assertTrue(not above.strip() or above.startswith("|"),
                            f"{what}: a table needs a blank line above it, got {above!r}")
            self.assertTrue(not below.strip() or below.startswith("|"),
                            f"{what}: a line right under a table becomes one of its rows: {below!r}")

    def settings(self) -> Overrides:
        store = self.store()
        self.addCleanup(store.close)
        return Overrides(store)

    def card_in_window(self, card_id="c1", title="Workshop", remind="7;3;1", due=5, notifyees=None, **extra):
        self.simple_board(member=True) if "b1" not in self.fake.cards else None
        return self.fake.add_card("b1", card_id, title, due_ms=ti.in_days(due),
                                  notifyees=notifyees or [U1], remind=remind, **extra)


class Authorization(CommandsBase):
    def test_a_system_administrator_is_served(self):
        self.assertIn("Reminder bot commands", self.run_command("remind_help"))

    def test_an_ordinary_member_is_refused_and_nothing_is_done(self):
        text = self.run_command("remind_pause", user=U1, username="alice")
        self.assertIn("Only system administrators", text)
        self.assertIsNone(self.settings().paused())
        self.assertEqual(self.fake.delayed, [])

    def test_every_command_refuses_a_non_administrator(self):
        for spec in SPECS:
            with self.subTest(command=spec.trigger):
                reply = self.commands.handle_command(self.form(spec.trigger, user=U1, username="alice"))
                self.assertIn("Only system administrators", reply["text"])
        self.assertEqual(self.fake.dialogs, [])           # not even the pop-up form
        self.assertEqual(self.fake.posts, [])

    def test_a_wrong_token_is_refused(self):
        for token in ("", "nope", "tok-remind_send"):      # the last one is another command's token
            reply = self.commands.handle_command(self.form("remind_pause", token=token))
            self.assertIn("wrong token", reply["text"], token)
        self.assertIsNone(self.settings().paused())

    def test_a_command_of_another_team_is_refused(self):
        reply = self.commands.handle_command(self.form("remind_pause", team="other-team"))
        self.assertIn("wrong token", reply["text"])

    def test_commands_that_were_never_registered_are_refused(self):
        store = self.store()
        Overrides(store).set_command_tokens({})
        store.close()
        self.assertIn("wrong token", self.commands.handle_command(self.form("remind_help"))["text"])

    def test_unknown_commands_and_junk_forms_do_not_crash(self):
        self.assertIn("Unknown command", self.commands.handle_command({"command": "/remind_nuke"})["text"])
        self.assertIn("Unknown command", self.commands.handle_command({})["text"])
        reply = self.commands.handle_command({"command": "/remind_help", "user_id": "../../x", "team_id": "team1",
                                              "token": "tok-remind_help"})
        self.assertIn("Could not verify", reply["text"])   # an invalid id is never put into a URL

    def test_demoting_an_administrator_takes_effect_at_once(self):
        self.fake.add_user(uid_ := "dd" + "z" * 24, "dana", roles="system_admin system_user")
        self.assertIn("paused", self.run_command("remind_pause", user=uid_, username="dana"))
        self.run_command("remind_resume")
        self.fake.users[uid_]["roles"] = "system_user"
        self.assertIn("Only system administrators", self.run_command("remind_pause", user=uid_, username="dana"))

    def test_a_deactivated_administrator_is_refused(self):
        self.fake.users[ADMIN_ID]["delete_at"] = 1
        self.assertIn("Only system administrators", self.run_command("remind_pause"))

    def test_when_the_role_cannot_be_verified_nothing_is_done(self):
        self.fake.faults.append(Fault(lambda m, p: p.startswith(f"/api/v4/users/{ADMIN_ID}"), "status", status=500))
        self.assertIn("Could not verify", self.run_command("remind_pause"))
        self.assertIsNone(self.settings().paused())


class SendAndStatus(CommandsBase):
    def test_send_runs_a_pass_and_reports_it(self):
        self.card_in_window(notifyees=[U1, U2])
        text = self.run_command("remind_send")
        self.assertIn("2 sent", text)
        self.assertEqual(self.posted_to(), sorted([U1, U2]))
        again = self.run_command("remind_send")
        self.assertIn("0 sent, 2 already sent", again)
        self.assertEqual(len(self.fake.posts), 2)

    def test_status_tells_waiting_from_sent_today(self):
        self.card_in_window("c1", "Workshop", notifyees=[U1])
        before = self.run_command("remind_status")
        self.assertIn("Sent today (0)", before)
        self.assertIn("Waiting for the next pass (1)", before)
        self.run_command("remind_send")
        self.card_in_window("c2", "Sessao", notifyees=[U2])
        after = self.run_command("remind_status")
        self.assertIn("Sent today (1)", after)
        self.assertIn("Waiting for the next pass (1)", after)
        sent_part, waiting_part = after.split("Waiting for the next pass")
        self.assertIn("Workshop", sent_part)
        self.assertNotIn("Sessao", sent_part)
        self.assertIn("Sessao", waiting_part)
        self.assertNotIn("Workshop", waiting_part)

    def test_status_shows_the_configuration_and_where_each_value_comes_from(self):
        text = self.run_command("remind_status")
        for expected in ("09:00", "Europe/Lisbon", "100 per day, 20 per person", "from the environment",
                         "Due Date", "Notifyees", "Remind Before", "remindabot", "active"):
            self.assertIn(expected, text)
        self.run_command("remind_setsend", "10:30")
        self.run_command("remind_setcap", "50 5")
        text = self.run_command("remind_status")
        self.assertIn("10:30", text)
        self.assertIn("50 per day, 5 per person", text)
        self.assertIn("set by `@root`", text)

    def test_status_says_what_needs_attention(self):
        self.card_in_window("c1", "Nobody home", notifyees=[])
        self.fake.cards["b1"][-1]["properties"].pop("p_who", None)
        text = self.run_command("remind_status")
        self.assertIn("nobody is in", text)

    def test_titles_and_names_cannot_ping_or_link_through_a_reply(self):
        self.card_in_window("c1", "@all [click](http://evil.example) `x`", notifyees=[U1])
        for trigger in ("remind_status", "remind_next", "remind_boards"):
            text = self.run_command(trigger)
            self.assertNotIn("@all", text, trigger)
            self.assertNotIn("](http://evil", text, trigger)

    def test_send_while_another_pass_runs_is_declined_politely(self):
        self.card_in_window()
        with self.lock:
            text = self.run_command("remind_send")
        self.assertIn("already running", text)
        self.assertEqual(self.fake.posts, [])

    def test_a_late_answer_only_ever_goes_to_a_command_response_path(self):
        self.card_in_window()
        form = self.form("remind_status")
        form["response_url"] = "http://evil.example/api/v4/users/me"
        self.commands.handle_command(form)        # must not post to that path
        self.assertEqual(self.fake.delayed, [])
        self.assertNotIn(("POST", "/api/v4/users/me"), self.fake.requests)
        form["response_url"] = "http://other-host.example/hooks/commands/abcdefghij1234567890"
        self.commands.handle_command(form)        # the host is ignored, the path is used on OUR address
        self.assertEqual(len(self.fake.delayed), 1)


class ReplyFormatting(CommandsBase):
    """Found with the real web UI: two things Mattermost draws differently from what was written."""

    def test_help_shows_every_command_whole(self):
        text = self.run_command("remind_help")
        rows = [line for line in text.split("\n") if line.startswith("| `/")]
        self.assertEqual(len(rows), len(SPECS))
        for spec, row in zip(SPECS, rows):
            cells = [c.strip() for c in row.strip("|").split(" | ")]
            self.assertEqual(len(cells), 2, row)
            self.assertTrue(cells[0].startswith(f"`/{spec.trigger}"), row)
            self.assertEqual(cells[1], spec.summary, row)

    def test_help_keeps_the_arguments_that_use_angle_brackets(self):
        text = self.run_command("remind_help")
        self.assertIn("\u2039card link or title\u203a", text)      # shown with look-alike brackets
        self.assertIn("\u2039total\u203a [per person]", text)
        self.assertIn("message / time / caps / all", text)           # a `|` would split the table cell

    def test_the_rewrite_is_neutralised_wherever_the_text_comes_from(self):
        for original in ("`/remind_card <card link or title>` | Explain | `/remind_resend <x> [@user]` | Send",
                         "<a|b>", "a <b> c | d <e>", "x < y | y > z", "<<a|b>>", "<a <b|c> d>"):
            self.assertIsNone(SLACK_LINK.search(for_command_reply(original)), original)
        self.assertEqual(for_command_reply("no angle brackets | here"), "no angle brackets | here")
        self.assertEqual(for_command_reply("a \\> b"), "a \u203a b")      # no stray backslash from a title
        self.assertEqual(for_command_reply("> a quotation\n<a|b> and <c>"),
                         "> a quotation\n\u2039a|b\u203a and \u2039c\u203a")

    def test_a_title_cannot_trigger_the_rewrite(self):
        """A card title is typed by any board member; `<a|b>` in it must not eat the rest of a row."""
        self.card_in_window("c1", "Talk <a|b> and <c|d>", notifyees=[U1])
        for trigger, args in (("remind_status", ""), ("remind_next", ""), ("remind_card", "Talk"),
                              ("remind_send", ""), ("remind_history", "")):
            with self.subTest(command=trigger):
                self.assertIn("Talk", self.run_command(trigger, args))     # run_command checks the rest

    def test_a_table_is_set_off_by_blank_lines(self):
        rendered = table(["A", "B"], [["1", "2"], ["3", "4"]])
        self.assertTrue(rendered.startswith("\n| A | B |\n|---|---|\n| 1 | 2 |"), repr(rendered))
        self.assertTrue(rendered.endswith("| 3 | 4 |\n"), repr(rendered))
        joined = "\n".join(("A paragraph", table(["A"], [["x"]]), "text after"))
        self.assertIn("A paragraph\n\n| A |", joined)
        self.assertIn("| x |\n\ntext after", joined)
        self.assertEqual(table(["A"], []), "_none_")

    def test_a_cut_table_says_how_many_rows_are_missing(self):
        text = table(["A"], [[str(n)] for n in range(30)], limit=5)
        self.assertIn("| 4 |\n\n_… and 25 more_", text)
        self.assertNotIn("| 5 |", text)

    def test_every_command_with_data_draws_right(self):
        """All the read-only commands over a board with cards in every state; run_command checks each."""
        self.card_in_window("c1", "Workshop <x|y>", notifyees=[U1, U2])
        self.card_in_window("c2", "Sessao | com barra", notifyees=[U3], due=1)
        self.card_in_window("c3", "Ninguem", notifyees=[])
        self.run_command("remind_send")
        self.card_in_window("c4", "Depois", notifyees=[U1], due=2)
        for trigger, args in (("remind_help", ""), ("remind_status", ""), ("remind_next", "14"),
                              ("remind_boards", ""), ("remind_card", "Workshop"), ("remind_history", "20"),
                              ("remind_getmessage", ""), ("remind_alerts", "status"), ("remind_test", "")):
            with self.subTest(command=trigger):
                self.assertTrue(self.run_command(trigger, args).strip())


class DailyPassOff(CommandsBase):
    """REMINDER_DAILY_PASS=false (the dev stack): nothing runs by itself, and the commands say so."""

    def setUp(self):
        super().setUp()
        import dataclasses
        self.commands.cfg = dataclasses.replace(self.cfg, daily_pass=False)

    def test_status_says_the_pass_is_switched_off_but_still_shows_the_time(self):
        self.run_command("remind_setsend", "10:15")
        text = self.run_command("remind_status")
        self.assertIn("10:15", text)
        self.assertIn("switched off", text)
        self.assertIn("never: the daily pass is off", text)           # the "Next pass" row

    def test_next_and_setsend_do_not_promise_a_run_that_will_not_happen(self):
        self.card_in_window()
        self.assertIn("switched off", self.run_command("remind_setsend", "10:15"))
        self.assertIn("is run every day (it is switched off here", self.run_command("remind_next", "7"))

    def test_a_pass_someone_asks_for_still_works(self):
        self.card_in_window()
        self.assertIn("1 sent", self.run_command("remind_send"))


class PauseAndSettings(CommandsBase):
    def test_pause_blocks_every_way_of_sending_until_resume(self):
        self.card_in_window()
        self.assertIn("paused", self.run_command("remind_pause"))
        self.assertIn("already paused", self.run_command("remind_pause"))
        self.assertIn("PAUSED", self.run_command("remind_status"))
        self.assertIn("paused", self.run_command("remind_send"))
        self.assertIn("paused", self.run_command("remind_resend", "Workshop"))
        self.assertEqual(self.fake.posts, [])
        self.assertIn("**active** again", self.run_command("remind_resume"))
        self.assertIn("not paused", self.run_command("remind_resume"))
        self.assertIn("1 sent", self.run_command("remind_send"))

    def test_setsend_validates_and_stores(self):
        for good, shown in (("9:30", "09:30"), ("09:05", "09:05"), ("23h59", "23:59"), ("00:00", "00:00")):
            self.assertIn(shown, self.run_command("remind_setsend", good))
        self.assertEqual(self.settings().effective(self.cfg).send_at.strftime("%H:%M"), "00:00")
        for bad in ("", "25:00", "9", "ab:cd", "09:60", "-1:00", "9:5"):
            self.assertIn(":warning:", self.run_command("remind_setsend", bad), repr(bad))
        self.assertEqual(self.settings().effective(self.cfg).send_at.strftime("%H:%M"), "00:00")   # unchanged

    def test_setsend_explains_when_it_takes_effect(self):
        self.assertIn("within a minute", self.run_command("remind_setsend", "08:00"))   # NOW is 10:00
        self.assertIn("today at 23:00", self.run_command("remind_setsend", "23:00"))
        store = self.store()
        store.set_last_run(TODAY.isoformat())
        store.close()
        self.assertIn("tomorrow", self.run_command("remind_setsend", "11:00"))

    def test_setcap_forms_and_validation(self):
        self.assertIn("**200** messages in total and **20** per person", self.run_command("remind_setcap", "200"))
        self.assertIn("**200** messages in total and **5** per person", self.run_command("remind_setcap", "person=5"))
        self.assertIn("**300** messages in total and **9** per person", self.run_command("remind_setcap", "300 9"))
        self.assertIn("**10** messages in total and **9**", self.run_command("remind_setcap", "total=10"))
        for bad in ("", "0", "-5", "abc", "1 2 3", "x=4", "10001", "5.5"):
            self.assertIn(":warning:", self.run_command("remind_setcap", bad), repr(bad))
        effective = self.settings().effective(self.cfg)
        self.assertEqual((effective.max_per_day, effective.max_per_user), (10, 9))

    def test_a_cap_set_from_chat_really_limits_the_next_pass(self):
        self.card_in_window("c1", "A", notifyees=[U1])
        self.card_in_window("c2", "B", notifyees=[U1])
        self.run_command("remind_setcap", "total=1")
        text = self.run_command("remind_send")
        self.assertIn("1 sent", text)
        self.assertIn("held back by the daily safety cap", text)
        self.run_command("remind_reset", "caps")
        self.assertIn("1 sent", self.run_command("remind_send"))      # the other one, now that the cap is gone

    def test_reset_goes_back_to_the_defaults(self):
        self.run_command("remind_setsend", "11:00")
        self.run_command("remind_setcap", "50 5")
        self.assertIn("time", self.run_command("remind_reset", "time"))
        effective = self.settings().effective(self.cfg)
        self.assertEqual(effective.send_at, self.cfg.send_at)
        self.assertEqual(effective.max_per_day, 50)                     # caps were not touched
        self.assertIn("caps", self.run_command("remind_reset", "all"))
        self.assertEqual(self.settings().effective(self.cfg), self.cfg)
        self.assertIn("already the defaults", self.run_command("remind_reset", "all"))
        self.assertIn(":warning:", self.run_command("remind_reset", "everything"))
        self.assertIn(":warning:", self.run_command("remind_reset"))

    def test_alerts_opt_in_and_out(self):
        self.assertIn("**not** getting", self.run_command("remind_alerts"))
        self.assertIn("will get a direct message", self.run_command("remind_alerts", "on"))
        self.assertEqual(self.settings().alert_users(), [ADMIN_ID])
        self.run_command("remind_alerts", "on")
        self.assertEqual(self.settings().alert_users(), [ADMIN_ID])    # no duplicates
        self.assertIn("**getting**", self.run_command("remind_alerts", "status"))
        self.run_command("remind_alerts", "off")
        self.assertEqual(self.settings().alert_users(), [])
        self.assertIn(":warning:", self.run_command("remind_alerts", "maybe"))


class Message(CommandsBase):
    def test_setmessage_opens_a_form_prefilled_with_the_current_text(self):
        reply = self.commands.handle_command(self.form("remind_setmessage"))
        self.assertEqual(reply, {})
        dialog = self.fake.dialogs[-1]
        self.assertEqual(dialog["trigger_id"], "trig123")
        self.assertEqual(dialog["url"], "http://reminder:8080/dialog")
        element = dialog["dialog"]["elements"][0]
        self.assertEqual((element["type"], element["name"]), ("textarea", "message"))
        self.assertEqual(element["default"], render.template_body(ti.SHIPPED_TEMPLATE.read_text(encoding="utf-8")))
        self.assertLessEqual(element["max_length"], 3000)
        self.assertIn("$card_title", dialog["dialog"]["introduction_text"])

    def submit(self, text, *, user=ADMIN_ID, state=None, cancelled=False, callback="setmessage"):
        if state is None:
            store = self.store()
            state = Overrides(store).sign_state("setmessage", user)
            store.close()
        return self.commands.handle_dialog({"type": "dialog_submission", "callback_id": callback, "state": state,
                                            "user_id": user, "cancelled": cancelled, "submission": {"message": text}})

    def test_a_saved_message_is_what_the_next_pass_sends(self):
        self.card_in_window()
        self.assertEqual(self.submit("Olá $responsible_name: $card_title falta $days_left"), {})
        self.run_command("remind_send")
        to_alice = [p["message"] for p in self.fake.posts if p["user_id"] == U1]
        self.assertEqual(to_alice, ["Olá Alice: Workshop falta 5"])
        dms = [p for p in self.fake.posts if p["user_id"] == ADMIN_ID]
        self.assertEqual(len(dms), 1)                         # the confirmation, as a direct message
        self.assertIn("updated", dms[0]["message"])
        self.assertIn("custom message", self.run_command("remind_getmessage"))
        self.assertIn("set by `@root`", self.run_command("remind_status"))

    def test_a_bad_message_is_rejected_with_the_reason_and_changes_nothing(self):
        for text, reason in (("Olá $card_titel", "card_titel"), ("   \n ", "empty"), ("x" * 3001, "3001"),
                             ("preço $5", "")):
            reply = self.submit(text)
            self.assertIn("errors", reply, text[:20])
            self.assertIn(reason, reply["errors"]["message"])
        self.assertIsNone(self.settings().custom_message())

    def test_line_breaks_and_markdown_survive(self):
        text = "Olá $responsible_name!\r\n\r\n- **ponto 1**\r\n- ponto 2\r\n\r\n[Abrir]($card_url)"
        self.assertEqual(self.submit(text), {})
        saved = self.settings().custom_message()
        self.assertEqual(saved.count("\n"), 5)
        self.assertNotIn("\r", saved)

    def test_a_form_cannot_be_forged_replayed_for_somebody_else_or_used_late(self):
        store = self.store()
        good = Overrides(store).sign_state("setmessage", ADMIN_ID)
        store.close()
        self.assertIn("error", self.submit("Olá $card_title", state="forged"))
        self.assertIn("error", self.submit("Olá $card_title", state=None, user=U1))              # alice, own state
        self.assertIn("error", self.submit("Olá $card_title", state=good, user=U1))              # alice, admin's state
        self.assertIn("error", self.submit("Olá $card_title", state=good, callback="other"))
        self.now_ts += 16 * 60                                                                    # the form expired
        self.assertIn("error", self.submit("Olá $card_title", state=good))
        self.assertIsNone(self.settings().custom_message())

    def test_a_demoted_administrator_cannot_submit_an_old_form(self):
        self.fake.add_user(uid_ := "dd" + "z" * 24, "dana", roles="system_user")
        reply = self.submit("Olá $card_title", user=uid_)
        self.assertIn("error", reply)
        self.assertIsNone(self.settings().custom_message())

    def test_cancelling_does_nothing(self):
        self.assertEqual(self.submit("x", cancelled=True), {})
        self.assertIsNone(self.settings().custom_message())

    def test_getmessage_reset_and_the_file_default(self):
        self.assertIn("the file", self.run_command("remind_getmessage"))
        self.submit("Texto novo $card_title")
        self.assertIn("Texto novo", self.run_command("remind_getmessage"))
        self.assertIn("message", self.run_command("remind_reset", "message"))
        self.assertIn("the file", self.run_command("remind_getmessage"))
        self.assertIn("Olá", self.run_command("remind_getmessage"))

    def test_test_sends_a_preview_only_to_the_caller_and_records_nothing(self):
        self.submit("Pré $responsible_name $days_left_text")
        self.card_in_window()
        before = len(self.fake.posts)               # the form's own confirmation message is already there
        text = self.run_command("remind_test")
        self.assertIn("preview was sent", text)
        previews = self.fake.posts[before:]
        self.assertEqual([p["user_id"] for p in previews], [ADMIN_ID])
        # root has no name filled in, so the greeting is their @username, with the mention made inert
        self.assertIn("Pré @​root faltam 7 dias", previews[0]["message"])
        self.assertIn("not a real reminder", previews[0]["message"])
        store = self.store()
        self.assertEqual(store.sent_since(0), 0)           # no record, no cap used
        self.assertEqual(store.recent_history(), [])
        store.close()

    def test_a_template_file_that_broke_is_reported_not_a_traceback(self):
        broken = Path(self.tmp.name) / "broken.txt"
        broken.write_text("Olá $card_titel", encoding="utf-8")
        self.commands.cfg = self.make_cfg(REMINDER_TEMPLATE_FILE=str(broken))
        text = self.run_command("remind_send")
        self.assertIn("template", text.lower())
        self.assertNotIn("Traceback", text)
        self.assertEqual(self.fake.posts, [])


class Inspect(CommandsBase):
    def test_history_lists_what_was_sent_newest_first(self):
        self.card_in_window("c1", "Primeiro", notifyees=[U1])
        self.run_command("remind_send")
        self.now_ts += 60
        self.card_in_window("c2", "Segundo", notifyees=[U2])
        self.run_command("remind_send")
        text = self.run_command("remind_history")
        self.assertLess(text.index("Segundo"), text.index("Primeiro"))
        self.assertIn("@alice", text)
        self.assertIn("The last 2 message(s)", self.run_command("remind_history", "5"))
        self.assertIn("The last 1 message(s)", self.run_command("remind_history", "1"))
        for bad in ("0", "51", "many"):
            self.assertIn(":warning:", self.run_command("remind_history", bad), bad)

    def test_history_when_nothing_was_sent(self):
        self.assertIn("No message has been sent", self.run_command("remind_history"))

    def test_next_shows_each_listed_day(self):
        self.card_in_window("c1", "Evento", due=10, remind="7;3;1")
        text = self.run_command("remind_next", "14")
        # due 10 days from the 13th = the 23rd: 7 days before = the 16th, 3 = the 20th, 1 = the 22nd
        for label, stage in (("Fri 16 Oct", "7 days"), ("Tue 20 Oct", "3 days"), ("Thu 22 Oct", "1 day")):
            day_part = text.split(f"**{label}**")[1].split("**")[0]
            self.assertIn(f"{stage} before", day_part, label)
        self.assertNotIn("today", text)
        for bad in ("0", "31", "soon"):
            self.assertIn(":warning:", self.run_command("remind_next", bad), bad)

    def test_next_does_not_list_what_was_already_sent(self):
        self.card_in_window("c1", "Evento", due=5, remind="7;3;1")         # 7 is reached today
        self.assertIn("today", self.run_command("remind_next"))
        self.run_command("remind_send")
        text = self.run_command("remind_next")
        self.assertNotIn("today", text)
        self.assertIn("3 days before", text)

    def test_next_with_nothing_scheduled(self):
        self.assertIn("Nothing is scheduled", self.run_command("remind_next"))

    def test_boards_explains_every_state(self):
        self.simple_board("ok", member=True)
        self.fake.add_card("ok", "c", "Card", due_ms=ti.in_days(5), notifyees=[U1], remind="7")
        self.fake.add_board("open", "Not joined yet", member=False)
        self.fake.add_board("priv", "Private one", type_="P", member=False)
        from .support.fakeserver import standard_props
        wrong = standard_props()
        wrong[2]["type"] = "number"
        self.fake.add_board("bad", "Wrong type", member=True, props=wrong)
        self.fake.add_board("other", "Unrelated", props=[{"id": "n", "name": "Notes", "type": "text", "options": []}])
        text = self.run_command("remind_boards")
        for expected in ("Posts", "read", "Not joined yet", "open, not joined", "Private one", "private, no access",
                         "Wrong type", "set up wrongly", "must be one of", "Unrelated", "not used"):
            self.assertIn(expected, text)
        self.assertEqual(self.fake.members, {"ok", "bad"})                   # looking never joins anything

    def test_card_explains_what_it_sees(self):
        self.card_in_window("c1", "Workshop de Python", remind="7;tres;3", due=5, notifyees=[U1, U2])
        self.run_command("remind_send")
        text = self.run_command("remind_card", "workshop")
        for expected in ("Workshop de Python", "read as: **7, 3**", "tres", "separate numbers with", "7 days before",
                         "@alice", "was already told", "Coming up"):
            self.assertIn(expected, text)

    def test_card_by_link_and_by_ambiguous_title(self):
        self.card_in_window("c1" + "a" * 24, "Alpha one")
        self.card_in_window("c2" + "a" * 24, "Alpha two")
        self.assertIn("2 cards match", self.run_command("remind_card", "alpha"))
        link = f"https://chat.test/boards/team/{'t' * 26}/{'b' * 26}/{'v' * 26}/{'c2' + 'a' * 24}"
        self.assertIn("Alpha two", self.run_command("remind_card", link))
        self.assertIn("No card matches", self.run_command("remind_card", "zzz"))
        self.assertIn("Give the card", self.run_command("remind_card", ""))

    def test_find_cards_prefers_an_exact_title(self):
        def report(card_id, title):
            return CardReport("b", "b", "t", card_id, title, None, (), (), False, False, False, None, "", False, ())
        reports = [report("a", "Post"), report("b", "Post party"), report("c", "Another")]
        self.assertEqual([r.card_id for r in find_cards(reports, "post")], ["a"])
        self.assertEqual([r.card_id for r in find_cards(reports, "par")], ["b"])
        self.assertEqual(find_cards(reports, ""), [])


class Resend(CommandsBase):
    def test_resend_repeats_todays_reminder_for_the_card(self):
        self.card_in_window(notifyees=[U1, U2])
        self.run_command("remind_send")
        self.assertEqual(len(self.fake.posts), 2)
        text = self.run_command("remind_resend", "Workshop")
        self.assertIn("2 sent", text)
        self.assertEqual(len(self.fake.posts), 4)
        self.assertEqual(self.posted_to(), sorted([U1, U1, U2, U2]))

    def test_resend_to_one_person(self):
        self.card_in_window(notifyees=[U1, U2])
        self.run_command("remind_send")
        text = self.run_command("remind_resend", "Workshop @bob")
        self.assertIn("1 sent", text)
        self.assertEqual(self.posted_to(), sorted([U1, U2, U2]))
        self.assertIn("not in the card's Notifyees", self.run_command("remind_resend", "Workshop @carol"))

    def test_resend_does_not_disturb_other_cards(self):
        self.card_in_window("c1", "One", notifyees=[U1])
        self.card_in_window("c2", "Two", notifyees=[U1])
        self.run_command("remind_send")
        self.run_command("remind_resend", "One")
        self.assertEqual(len(self.fake.posts), 3)
        self.assertEqual(sum("One" in p["message"] for p in self.fake.posts), 2)

    def test_resend_works_when_nothing_was_sent_yet(self):
        self.card_in_window()
        self.assertIn("1 sent", self.run_command("remind_resend", "Workshop"))

    def test_resend_refuses_a_card_without_a_reminder_due_today(self):
        self.card_in_window(due=40)
        text = self.run_command("remind_resend", "Workshop")
        self.assertIn("No reminder is due today", text)
        self.assertEqual(self.fake.posts, [])

    def test_resend_still_obeys_the_daily_caps(self):
        self.card_in_window(notifyees=[U1])
        self.run_command("remind_send")
        self.run_command("remind_setcap", "total=1")
        text = self.run_command("remind_resend", "Workshop")
        self.assertIn("held back", text)
        self.assertEqual(len(self.fake.posts), 1)


class HttpEndpoint(CommandsBase):
    def setUp(self):
        super().setUp()
        self.commands.background = lambda fn: fn()
        self.server = start_server(self.commands, "127.0.0.1", 0)
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def post(self, path, data: bytes, ctype="application/x-www-form-urlencoded"):
        req = urllib.request.Request(self.base + path, data=data, method="POST", headers={"Content-Type": ctype})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read() or b"null")
        except urllib.error.HTTPError as exc:
            with exc:                      # an error answer holds the connection open until it is closed
                return exc.code, json.loads(exc.read() or b"null")

    def test_a_form_post_is_answered_as_mattermost_expects(self):
        data = urllib.parse.urlencode(self.form("remind_help")).encode()
        status, body = self.post("/command", data)
        self.assertEqual(status, 200)
        self.assertEqual(body["response_type"], "ephemeral")
        self.assertIn("Reminder bot commands", body["text"])

    def test_a_dialog_submission_over_http(self):
        store = self.store()
        state = Overrides(store).sign_state("setmessage", ADMIN_ID)
        store.close()
        payload = {"callback_id": "setmessage", "state": state, "user_id": ADMIN_ID,
                   "submission": {"message": "Olá $card_title"}}
        status, body = self.post("/dialog", json.dumps(payload).encode(), "application/json")
        self.assertEqual((status, body), (200, {}))
        self.assertEqual(self.settings().custom_message(), "Olá $card_title")

    def test_health_check_and_unknown_paths(self):
        with urllib.request.urlopen(self.base + "/healthz", timeout=5) as resp:
            self.assertEqual((resp.status, json.loads(resp.read())), (200, "ok"))
        self.assertEqual(self.post("/nope", b"x")[0], 404)
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(self.base + "/command", timeout=5)         # a GET is not a command
        caught.exception.close()
        self.assertEqual(caught.exception.code, 404)

    def test_junk_is_refused_without_harm(self):
        self.assertEqual(self.post("/dialog", b"{not json", "application/json")[0], 400)
        self.assertEqual(self.post("/dialog", b"[1,2]", "application/json")[0], 400)
        status, body = self.post("/command", b"")                      # an empty form: unknown command
        self.assertEqual(status, 200)
        self.assertIn("Unknown command", body["text"])
        self.assertEqual(self.post("/command", b"x" * (MAX_BODY_BYTES + 1))[0], 413)
        status, body = self.post("/command", urllib.parse.urlencode(self.form("remind_help")).encode())
        self.assertEqual(status, 200)                                  # still serving after all of that

    def test_an_internal_error_becomes_a_message_not_a_dropped_connection(self):
        self.commands.open_store = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
        status, body = self.post("/command", urllib.parse.urlencode(self.form("remind_help")).encode())
        self.assertEqual(status, 200)
        self.assertIn("Unexpected error", body["text"])

    def test_concurrent_requests(self):
        results = []
        def one():
            results.append(self.post("/command", urllib.parse.urlencode(self.form("remind_history")).encode()))
        threads = [threading.Thread(target=one) for _ in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual([status for status, _ in results], [200] * 12)


if __name__ == "__main__":
    import unittest
    unittest.main()
