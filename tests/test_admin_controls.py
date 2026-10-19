"""The administrator controls outside the commands themselves: registering them in Mattermost, and
what the daily loop does with a pause, a changed send time, a changed message or an alert."""
from datetime import datetime, timezone
from unittest import mock

from . import test_integration as ti
from remindabot import scheduler
from remindabot import alerts
from remindabot.commands import SPECS, CommandRouter
from remindabot.register import register_commands, unregister_commands
from remindabot.overrides import Overrides
from .support.fakeserver import ADMIN_ID, BOT_TOKEN, Fault

LISBON, U1, U2, U3 = ti.LISBON, ti.U1, ti.U2, ti.U3


class Register(ti.Base):
    def setUp(self):
        super().setUp()
        self.fake.teams = [{"id": "team1", "name": "nei"}, {"id": "team2", "name": "other"}]

    def register(self, cfg=None, token=None):
        store = self.store()
        try:
            return register_commands(cfg or self.cfg, token or self.fake.admin_token, store), Overrides(store).command_tokens()
        finally:
            store.close()

    def test_every_command_is_created_in_every_team_and_the_tokens_are_kept(self):
        (report, ok), tokens = self.register()
        self.assertTrue(ok, report)
        for team in ("team1", "team2"):
            made = {c["trigger"]: c for c in self.fake.commands[team]}
            self.assertEqual(set(made), {s.trigger for s in SPECS})
            self.assertEqual(tokens[team], {t: c["token"] for t, c in made.items()})
            command = made["remind_status"]
            self.assertEqual((command["method"], command["url"]), ("P", "http://reminder:8080/command"))
        # only /remind_help is offered in the autocomplete list that every member sees
        offered = [c["trigger"] for c in self.fake.commands["team1"] if c["auto_complete"]]
        self.assertEqual(offered, ["remind_help"])

    def test_each_command_is_named_after_the_bot_in_the_integrations_list(self):
        self.register()
        names = {c["trigger"]: c["display_name"] for c in self.fake.commands["team1"]}
        self.assertEqual(names, {s.trigger: f"Remindabot: {s.trigger}" for s in SPECS})

    def test_a_command_registered_under_an_older_name_is_renamed_in_place(self):
        self.register()
        for command in self.fake.commands["team1"]:
            command["display_name"] = "Reminder bot: " + command["trigger"]    # what an earlier version registered
        ids = {c["id"] for c in self.fake.commands["team1"]}
        (report, ok), _ = self.register()
        self.assertTrue(ok, report)
        self.assertEqual({c["id"] for c in self.fake.commands["team1"]}, ids, "renamed, not created again")
        self.assertTrue(all(c["display_name"].startswith("Remindabot: ") for c in self.fake.commands["team1"]))

    def test_the_saved_tokens_are_exactly_what_the_commands_accept(self):
        self.register()
        self.fake.add_user(ADMIN_ID, "root", roles="system_admin system_user")
        commands = CommandRouter(self.cfg, clients=lambda: (self.mm, self.boards), open_store=self.store,
                            background=lambda fn: fn())
        token = next(c["token"] for c in self.fake.commands["team2"] if c["trigger"] == "remind_help")
        reply = commands.handle_command({"command": "/remind_help", "team_id": "team2", "token": token,
                                         "user_id": ADMIN_ID, "user_name": "root"})
        self.assertIn("Reminder bot commands", reply["text"])

    def test_running_it_again_changes_nothing_and_a_new_url_updates_in_place(self):
        self.register()
        ids = {c["id"] for team in self.fake.commands.values() for c in team}
        (report, ok), _ = self.register()
        self.assertTrue(ok)
        self.assertTrue(all("0 command(s) created, 0 updated" in line for line in report if line.startswith("team ")), report)
        self.assertEqual({c["id"] for team in self.fake.commands.values() for c in team}, ids)
        moved = self.make_cfg(REMINDER_COMMANDS_URL="http://bot.internal:9000")
        (report, ok), _ = self.register(cfg=moved)
        self.assertTrue(all(c["url"] == "http://bot.internal:9000/command"
                            for team in self.fake.commands.values() for c in team))
        self.assertTrue(any("updated" in line for line in report))

    def test_unregistering_removes_only_this_bots_commands_and_forgets_the_tokens(self):
        self.register()
        other = {"team_id": "team1", "trigger": "deploy", "method": "P", "url": "http://x/y"}
        self.fake.commands["team1"].append({**other, "id": "other" + "0" * 20, "token": "t"})
        store = self.store()
        report, ok = unregister_commands(self.cfg, self.fake.admin_token, store)
        tokens = Overrides(store).command_tokens()
        store.close()
        self.assertTrue(ok, report)
        self.assertEqual([c["trigger"] for c in self.fake.commands["team1"]], ["deploy"])   # somebody else's survives
        self.assertEqual(self.fake.commands["team2"], [])
        self.assertEqual(tokens, {})
        self.assertIn(f"{2 * len(SPECS)} command(s) removed", " ".join(report))
        store = self.store()
        again, ok = unregister_commands(self.cfg, self.fake.admin_token, store)       # harmless twice
        store.close()
        self.assertTrue(ok)
        self.assertIn("0 command(s) removed", " ".join(again))

    def test_unregistering_needs_an_administrator_too(self):
        self.register()
        store = self.store()
        report, ok = unregister_commands(self.cfg, BOT_TOKEN, store)
        store.close()
        self.assertFalse(ok)
        self.assertEqual(len(self.fake.commands["team1"]), len(SPECS))

    def test_a_token_that_is_not_an_administrators_is_refused(self):
        (report, ok), tokens = self.register(token=BOT_TOKEN)
        self.assertFalse(ok)
        self.assertIn("not a system administrator", " ".join(report))
        self.assertEqual(self.fake.commands, {})
        self.assertEqual(tokens, {})

    def test_it_warns_when_mattermost_may_not_call_the_bot(self):
        self.fake.mm_config["ServiceSettings"]["AllowedUntrustedInternalConnections"] = ""
        (report, ok), _ = self.register()
        self.assertFalse(ok)
        self.assertIn("ALLOWEDUNTRUSTEDINTERNALCONNECTIONS=reminder", " ".join(report))
        self.fake.mm_config["ServiceSettings"]["AllowedUntrustedInternalConnections"] = "reminder"
        self.fake.mm_config["ServiceSettings"]["EnableCommands"] = False
        (report, ok), _ = self.register()
        self.assertFalse(ok)
        self.assertIn("switched off", " ".join(report))

    def test_one_failing_command_is_reported_and_the_rest_are_still_made(self):
        self.fake.faults.append(Fault(lambda m, p: m == "POST" and p == "/api/v4/commands", "status",
                                      times=1, status=500))
        (report, ok), tokens = self.register()
        self.assertFalse(ok)
        self.assertTrue(any("failed" in line for line in report))
        self.assertEqual(len(tokens["team1"]) + len(tokens["team2"]), 2 * len(SPECS) - 1)


class Loop(ti.Base):
    """The daily loop (`serve`) on a fake clock, with what an administrator may have set from chat."""

    def setUp(self):
        super().setUp()
        self.simple_board("b1", member=True)
        self.fake.add_card("b1", "c1", "A", due_ms=ti.in_days(5), notifyees=[U1], remind="7;3;1")
        self.fake.add_user(ADMIN_ID, "root", roles="system_admin system_user")
        self.clock = ti.FakeClock(datetime(2026, 10, 13, 8, 0, tzinfo=LISBON).astimezone(timezone.utc))
        self.store_ = self.store(clock=lambda: self.clock.utc.timestamp())
        self.addCleanup(self.store_.close)
        self.settings = Overrides(self.store_)
        self.passes = 0

    def run_loop(self, minutes: int, cfg=None):
        real = scheduler.run_once

        def counting(*args, **kwargs):
            self.passes += 1
            return real(*args, **kwargs)

        with mock.patch.object(scheduler, "run_once", counting):
            scheduler.serve(cfg or self.cfg, self.mm, self.boards, self.store_, now_fn=self.clock.now,
                           sleep_fn=self.clock.sleep, max_iterations=minutes)

    def messages_to(self, user_id):
        return [p["message"] for p in self.fake.posts if p["user_id"] == user_id]

    # -- pause ------------------------------------------------------------------------------------

    def test_a_paused_bot_sends_nothing_and_says_so_once(self):
        self.settings.pause("root")
        with self.assertLogs("reminder", level="WARNING") as logs:
            self.run_loop(minutes=6 * 60)                       # 08:00 -> 14:00, past the send time
        self.assertEqual((self.passes, len(self.fake.posts)), (0, 0))
        self.assertEqual(sum("PAUSED" in m for m in logs.output), 1)

    def test_resuming_the_same_day_sends_that_day(self):
        self.settings.pause("root")
        self.run_loop(minutes=3 * 60)                           # 08:00 -> 11:00
        self.assertEqual(self.passes, 0)
        self.settings.resume()
        self.run_loop(minutes=5)
        self.assertEqual((self.passes, len(self.messages_to(U1))), (1, 1))

    def test_a_pause_set_from_another_process_is_noticed_without_a_restart(self):
        other_store = self.store(clock=lambda: self.clock.utc.timestamp())
        self.addCleanup(other_store.close)
        other = Overrides(other_store)                                             # "the chat commands"
        real_sleep = self.clock.sleep

        def sleep_and_pause(seconds):
            real_sleep(seconds)
            if self.clock.now().hour >= 8 and self.clock.now().minute >= 30:
                other.pause("root")
        self.clock.sleep = sleep_and_pause
        self.run_loop(minutes=3 * 60)                           # the send time (09:00) is after the pause
        self.assertEqual((self.passes, len(self.fake.posts)), (0, 0))

    # -- send time, message, caps -------------------------------------------------------------------

    def test_a_send_time_set_from_chat_replaces_the_environment_one(self):
        self.settings.set_send_at("11:30", "root")
        self.run_loop(minutes=3 * 60)                           # 08:00 -> 11:00: the old 09:00 passes, nothing runs
        self.assertEqual(self.passes, 0)
        self.run_loop(minutes=45)                               # -> 11:45
        self.assertEqual(self.passes, 1)

    def test_resetting_the_send_time_goes_back_to_the_environment(self):
        self.settings.set_send_at("20:00", "root")
        self.settings.reset("time")
        self.run_loop(minutes=2 * 60)                           # 08:00 -> 10:00
        self.assertEqual(self.passes, 1)

    def test_a_message_set_from_chat_is_what_the_daily_pass_sends(self):
        self.settings.set_message("Lembrete: $card_title ($remind_before)", "root")
        self.run_loop(minutes=2 * 60)
        self.assertEqual(self.messages_to(U1), ["Lembrete: A (7)"])

    def test_caps_set_from_chat_limit_the_daily_pass(self):
        self.fake.add_card("b1", "c2", "B", due_ms=ti.in_days(5), notifyees=[U1], remind="7")
        self.settings.set_caps(None, 1, "root")
        self.run_loop(minutes=2 * 60)
        self.assertEqual(len(self.messages_to(U1)), 1)

    # -- alerts --------------------------------------------------------------------------------------

    def test_a_failed_pass_alerts_the_opted_in_administrator_once_a_day(self):
        self.settings.set_alerts(ADMIN_ID, True)
        self.fake.faults.append(Fault(lambda m, p: m == "GET" and p == "/api/v4/users/me/teams", "status",
                                      times=9, status=500))
        self.run_loop(minutes=4 * 60)                           # 08:00 -> 12:00: fails, is retried every 10 min
        alerts_sent = [m for m in self.messages_to(ADMIN_ID) if "pass failed" in m]
        self.assertEqual(len(alerts_sent), 1)
        self.assertEqual(len(self.messages_to(U1)), 1)          # and the reminder still went out afterwards

    def test_nobody_opted_in_means_no_alert_and_no_failure(self):
        self.fake.faults.append(Fault(lambda m, p: m == "GET" and p == "/api/v4/users/me/teams", "status",
                                      times=9, status=500))
        self.run_loop(minutes=2 * 60)
        self.assertEqual(self.messages_to(ADMIN_ID), [])

    def test_a_misconfigured_board_and_a_cap_are_alerted(self):
        self.settings.set_alerts(ADMIN_ID, True)
        props = ti.standard_props()
        props[2]["type"] = "number"
        self.fake.add_board("bad", "Wrong type", member=True, props=props)
        self.settings.set_caps(None, 1, "root")
        self.fake.add_card("b1", "c2", "B", due_ms=ti.in_days(5), notifyees=[U1], remind="7")
        self.run_loop(minutes=2 * 60)
        text = "\n".join(self.messages_to(ADMIN_ID))
        self.assertIn("Wrong type", text)
        self.assertIn("daily safety cap held back 1", text)

    def test_alert_function_is_throttled_per_kind_and_survives_a_dead_server(self):
        self.settings.set_alerts(ADMIN_ID, True)
        self.assertEqual(alerts.notify(self.mm, self.settings, "x", "one", "2026-10-13"), 1)
        self.assertEqual(alerts.notify(self.mm, self.settings, "x", "again", "2026-10-13"), 0)
        self.assertEqual(alerts.notify(self.mm, self.settings, "y", "other kind", "2026-10-13"), 1)
        self.assertEqual(alerts.notify(self.mm, self.settings, "x", "next day", "2026-10-14"), 1)
        self.fake.faults.append(Fault(lambda m, p: p.endswith("/posts") and m == "POST", "status", status=500))
        self.assertEqual(alerts.notify(self.mm, self.settings, "z", "lost", "2026-10-13"), 0)   # no exception


if __name__ == "__main__":
    import unittest
    unittest.main()
