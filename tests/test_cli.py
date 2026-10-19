"""The command line (`python -m app ...`): flags, exit codes and what each mode is allowed to touch."""
import contextlib
import io
import logging
import os
import signal
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from . import test_integration as ti
from remindabot import __main__ as main_mod
from remindabot.commands import SPECS
from remindabot.overrides import Overrides
from remindabot.store import Store
from .support.fakeserver import BOT_TOKEN, noon_utc_ms

U1, U2 = ti.U1, ti.U2


class Cli(ti.Base):
    """Runs `main(argv)` for real, with the environment a container would have, against the fake server."""

    def setUp(self):
        super().setUp()
        self.env = {
            "REMINDER_MM_URL": self.url, "REMINDER_SITE_URL": "https://chat.test",
            "REMINDER_BOT_TOKEN": BOT_TOKEN, "REMINDER_TEMPLATE_FILE": str(ti.SHIPPED_TEMPLATE),
            "REMINDER_STATE_FILE": str(self.state_path),
            "REMINDER_HEARTBEAT_FILE": str(Path(self.tmp.name) / "hb"),
            "REMINDER_TZ": "Europe/Lisbon",
        }
        # main() installs a SIGTERM handler and configures logging: undo both so other tests are unaffected
        previous = signal.getsignal(signal.SIGTERM)
        self.addCleanup(signal.signal, signal.SIGTERM, previous)
        patcher = mock.patch.object(logging, "basicConfig")
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_cli(self, *argv, **env_changes):
        env = {**self.env, **env_changes}
        env = {k: v for k, v in env.items() if v is not None}
        with mock.patch.dict(os.environ, env, clear=True):
            return main_mod.main(list(argv))

    def read_store(self) -> Store:
        """The state file, opened read-only to look at what a command left in it."""
        store = Store(self.state_path, readonly=True)
        self.addCleanup(store.close)
        return store

    def due_in(self, days: int) -> int:
        """A due date `days` from the REAL today (the CLI uses the clock, not the tests' fixed day)."""
        return noon_utc_ms(datetime.now(ti.LISBON).date() + timedelta(days=days))

    def board_with_card(self, member=True):
        self.simple_board(member=member)
        self.fake.add_card("b1", "c1", "Workshop", due_ms=self.due_in(5), notifyees=[U1, U2], remind="7;3;1")

    # --- --check -----------------------------------------------------------------------------

    def test_check_reports_the_bot_the_teams_and_the_template(self):
        with self.assertLogs("reminder", level="INFO") as logs:
            code = self.run_cli("--check")
        self.assertEqual(code, 0)
        text = "\n".join(logs.output)
        self.assertIn("@remindabot", text)
        self.assertIn("message OK (file)", text)
        self.assertIn("daily run at 14:00", text)
        self.assertEqual(self.fake.posts, [])

    def test_check_with_a_bad_token_exits_1(self):
        with self.assertLogs("reminder", level="ERROR"):
            self.assertEqual(self.run_cli("--check", REMINDER_BOT_TOKEN="wrong"), 1)

    def test_a_missing_token_or_a_bad_setting_exits_2(self):
        for change in ({"REMINDER_BOT_TOKEN": None}, {"REMINDER_SITE_URL": "not-a-url"},
                       {"REMINDER_SEND_AT": "9h"}, {"REMINDER_TZ": "Mars/Olympus"}):
            with self.assertLogs("reminder", level="ERROR"):
                self.assertEqual(self.run_cli("--check", **change), 2, change)

    def test_a_broken_message_template_exits_2(self):
        bad = Path(self.tmp.name) / "bad.txt"
        bad.write_text("Olá $card_titel", encoding="utf-8")
        with self.assertLogs("reminder", level="ERROR"):
            self.assertEqual(self.run_cli("--once", REMINDER_TEMPLATE_FILE=str(bad)), 2)
        self.assertEqual(self.fake.posts, [])

    # --- flags -------------------------------------------------------------------------------

    def test_dry_run_alone_is_refused_instead_of_starting_the_real_daemon(self):
        with self.assertRaises(SystemExit) as raised, contextlib.redirect_stderr(io.StringIO()) as err:
            self.run_cli("--dry-run")
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("--dry-run only makes sense together with --once", err.getvalue())

    def test_modes_cannot_be_combined(self):
        with self.assertRaises(SystemExit) as raised, contextlib.redirect_stderr(io.StringIO()):
            self.run_cli("--once", "--check")
        self.assertEqual(raised.exception.code, 2)

    # --- --once ------------------------------------------------------------------------------

    def test_once_dry_run_sends_nothing_and_does_not_even_create_the_state_file(self):
        self.board_with_card()
        with self.assertLogs("reminder", level="INFO") as logs:
            code = self.run_cli("--once", "--dry-run")
        self.assertEqual(code, 0)
        self.assertEqual(self.fake.posts, [])
        self.assertFalse(self.state_path.exists())
        self.assertTrue(any("DRY-RUN would send to @alice" in m for m in logs.output), logs.output)
        self.assertIn("2 would be sent (DRY-RUN, nothing was sent)", logs.output[-1])

    def test_once_sends_every_message_once_and_remembers_it(self):
        self.board_with_card()
        with self.assertLogs("reminder", level="INFO") as logs:
            self.assertEqual(self.run_cli("--once"), 0)
        self.assertEqual(sorted(p["user_id"] for p in self.fake.posts), sorted([U1, U2]))
        self.assertIn("2 sent", logs.output[-1])
        with self.assertLogs("reminder", level="INFO") as logs:
            self.assertEqual(self.run_cli("--once"), 0)             # the same day again
        self.assertEqual(len(self.fake.posts), 2)
        self.assertIn("0 sent, 2 already sent", logs.output[-1])

    def test_once_dry_run_after_a_real_run_knows_what_was_sent(self):
        self.board_with_card()
        self.run_cli("--once")
        with self.assertLogs("reminder", level="INFO") as logs:
            self.assertEqual(self.run_cli("--once", "--dry-run"), 0)
        self.assertIn("0 would be sent (DRY-RUN, nothing was sent), 2 already sent", logs.output[-1])
        self.assertEqual(self.read_store().sent_since(0), 2)   # and it left the file as it was

    def test_a_server_that_is_down_exits_1_not_with_a_traceback(self):
        self.board_with_card()
        with self.assertLogs("reminder", level="ERROR"):
            self.assertEqual(self.run_cli("--once", REMINDER_MM_URL="http://127.0.0.1:9"), 1)

    # --- --register-commands -----------------------------------------------------------------

    def test_register_commands_needs_an_administrator_token(self):
        with self.assertLogs("reminder", level="ERROR"):
            self.assertEqual(self.run_cli("--register-commands"), 2)
        with self.assertLogs("reminder", level="ERROR"):
            self.assertEqual(self.run_cli("--register-commands", REMINDER_ADMIN_TOKEN=BOT_TOKEN), 1)   # a non-admin
        self.assertEqual(self.fake.commands, {})

    def test_register_commands_creates_them_and_remembers_the_tokens(self):
        with self.assertLogs("reminder", level="INFO") as logs:
            self.assertEqual(self.run_cli("--register-commands", REMINDER_ADMIN_TOKEN=self.fake.admin_token), 0)
        made = self.fake.commands["team1"]
        self.assertEqual(len(made), len(SPECS))
        store = self.read_store()
        saved = Overrides(store).command_tokens()["team1"]
        self.assertEqual(saved, {c["trigger"]: c["token"] for c in made})
        self.assertTrue(any("Try /remind_help" in line for line in logs.output), logs.output)

    def test_the_secrets_can_be_files(self):
        """What compose does: no token in the environment, a file for the bot's and one for the admin's."""
        bot_file, admin_file = Path(self.tmp.name) / "bot-token", Path(self.tmp.name) / "admin-token"
        bot_file.write_text(BOT_TOKEN + "\n")
        admin_file.write_text(self.fake.admin_token + "\n")
        with self.assertLogs("reminder", level="INFO"):
            code = self.run_cli("--register-commands", REMINDER_BOT_TOKEN=None, REMINDER_BOT_TOKEN_FILE=str(bot_file),
                                REMINDER_ADMIN_TOKEN_FILE=str(admin_file))
        self.assertEqual(code, 0)
        self.assertEqual(len(self.fake.commands["team1"]), len(SPECS))
        # a file that is not there is a configuration error naming the variable, not a traceback
        with self.assertLogs("reminder", level="ERROR") as logs:
            code = self.run_cli("--register-commands", REMINDER_BOT_TOKEN=None, REMINDER_BOT_TOKEN_FILE=str(bot_file),
                                REMINDER_ADMIN_TOKEN_FILE=str(Path(self.tmp.name) / "missing"))
        self.assertEqual(code, 2)
        self.assertIn("REMINDER_ADMIN_TOKEN_FILE", "\n".join(logs.output))

    def test_unregister_commands_removes_them_and_needs_the_token(self):
        self.run_cli("--register-commands", REMINDER_ADMIN_TOKEN=self.fake.admin_token)
        with self.assertLogs("reminder", level="ERROR"):
            self.assertEqual(self.run_cli("--unregister-commands"), 2)
        self.assertEqual(len(self.fake.commands["team1"]), len(SPECS))
        with self.assertLogs("reminder", level="INFO") as logs:
            self.assertEqual(self.run_cli("--unregister-commands", REMINDER_ADMIN_TOKEN=self.fake.admin_token), 0)
        self.assertEqual(self.fake.commands["team1"], [])
        self.assertIn(f"{len(SPECS)} command(s) removed", "\n".join(logs.output))
        self.assertEqual(Overrides(self.read_store()).command_tokens(), {})

    # --- settings made from chat apply to the one-off commands too ---------------------------

    def test_check_mentions_the_message_source_the_pause_and_the_chat_commands(self):
        with self.assertLogs("reminder", level="INFO") as logs:
            self.assertEqual(self.run_cli("--check"), 0)
        text = "\n".join(logs.output)
        self.assertIn("message OK (file)", text)
        self.assertIn("registered in 0 team(s) (run --register-commands)", text)
        store = Store(self.state_path)
        settings = Overrides(store)
        settings.set_message("Olá $card_title", "root")
        settings.set_send_at("10:45", "root")
        settings.pause("root")
        store.close()
        with self.assertLogs("reminder", level="INFO") as logs:
            self.assertEqual(self.run_cli("--check"), 0)
        text = "\n".join(logs.output)
        self.assertIn("message OK (custom)", text)
        self.assertIn("daily run at 10:45 [PAUSED]", text)
        with self.assertLogs("reminder", level="INFO") as logs:
            self.run_cli("--check", REMINDER_COMMANDS_PORT="0")
        self.assertIn("switched off", "\n".join(logs.output))

    def test_once_does_not_send_while_paused_but_a_dry_run_still_shows_what_would_be(self):
        self.board_with_card()
        store = Store(self.state_path)
        Overrides(store).pause("root")
        store.close()
        with self.assertLogs("reminder", level="WARNING") as logs:
            self.assertEqual(self.run_cli("--once"), 0)
        self.assertIn("PAUSED", logs.output[0])
        self.assertEqual(self.fake.posts, [])
        with self.assertLogs("reminder", level="INFO") as logs:
            self.assertEqual(self.run_cli("--once", "--dry-run"), 0)
        self.assertIn("2 would be sent", logs.output[-1])

    def test_once_sends_the_message_and_caps_set_from_chat(self):
        self.board_with_card()
        store = Store(self.state_path)
        settings = Overrides(store)
        settings.set_message("Custom: $card_title", "root")
        settings.set_caps(None, 1, "root")
        store.close()
        self.assertEqual(self.run_cli("--once"), 0)
        self.assertEqual([p["message"] for p in self.fake.posts], ["Custom: Workshop", "Custom: Workshop"])
        self.assertEqual(sorted(p["user_id"] for p in self.fake.posts), sorted([U1, U2]))   # 1 per person: both

    # --- --join-boards -----------------------------------------------------------------------

    def test_join_boards_joins_the_relevant_open_boards_and_sends_nothing(self):
        self.board_with_card(member=False)
        self.fake.add_board("other", "Unrelated", props=[{"id": "x", "name": "Notes", "type": "text", "options": []}])
        self.assertEqual(self.run_cli("--join-boards"), 0)
        self.assertIn("b1", self.fake.members)
        self.assertNotIn("other", self.fake.members)
        self.assertEqual(self.fake.posts, [])


if __name__ == "__main__":
    unittest.main()
