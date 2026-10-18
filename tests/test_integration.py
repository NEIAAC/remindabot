"""The real client + runner (+ daily loop) against a fake Mattermost that can be made to misbehave."""
import dataclasses
import logging
import sqlite3
import tempfile
import threading
import time
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from remindabot import scheduler
from remindabot import boards as boards_mod
from remindabot import render
from remindabot.boards import Boards
from remindabot.config import Config
from remindabot.driver import ApiError, Driver
from remindabot.runner import StateError, run_once
from remindabot.store import Store
from .support.fakeserver import BOT_ID, BOT_TOKEN, FakeMattermost, Fault, noon_utc_ms, standard_props, uid

LISBON = ZoneInfo("Europe/Lisbon")
TODAY = date(2026, 10, 13)
SHIPPED_TEMPLATE = Path(__file__).resolve().parent.parent / "config" / "message.txt"
U1, U2, U3, GHOST = uid("u1"), uid("u2"), uid("u3"), uid("ghost")


def in_days(n: int) -> int:
    return noon_utc_ms(TODAY + timedelta(days=n))


class Base(unittest.TestCase):
    def setUp(self):
        self.fake = FakeMattermost()
        self.url = self.fake.start()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.fake.stop)
        self.state_path = Path(self.tmp.name) / "state.sqlite"
        self.now_ts = time.time()          # the store's clock; tests move it to age claims / change the day
        self.cfg = self.make_cfg()
        self.mm = Driver(self.url, BOT_TOKEN, timeout=5)
        self.boards = Boards(self.url, BOT_TOKEN, timeout=5)
        for api in (self.mm.api, self.boards.api):
            api._sleep = lambda s: None  # do not really wait between retries
        self.template = render.load_template(SHIPPED_TEMPLATE)
        self.fake.add_user(U1, "alice", nickname="Alice")
        self.fake.add_user(U2, "bob", first_name="Bob", last_name="Builder")
        self.fake.add_user(U3, "carol")

    def make_cfg(self, **overrides) -> Config:
        env = {
            "REMINDER_MM_URL": self.url, "REMINDER_SITE_URL": "https://chat.test",
            "REMINDER_BOT_TOKEN": BOT_TOKEN, "REMINDER_TEMPLATE_FILE": str(SHIPPED_TEMPLATE),
            "REMINDER_STATE_FILE": str(self.state_path),
            "REMINDER_HEARTBEAT_FILE": str(Path(self.tmp.name) / "hb"),
            "REMINDER_TZ": "Europe/Lisbon",
            "REMINDER_SEND_AT": "09:00",    # pinned: the timing tests below do not depend on the shipped default
        }
        env.update(overrides)
        return Config.from_env(env)

    def store(self, **kw) -> Store:
        kw.setdefault("clock", lambda: self.now_ts)
        return Store(self.state_path, **kw)

    def go(self, *, store=None, today=TODAY, **kw):
        store = store or self.store()
        return run_once(self.cfg, self.mm, self.boards, store, self.template, today, **kw)

    def posted_to(self) -> list[str]:
        return sorted(p["user_id"] for p in self.fake.posts)

    def simple_board(self, board_id="b1", **kw):
        return self.fake.add_board(board_id, "Posts", **kw)


class Basics(Base):
    def test_each_notifyee_is_reminded_exactly_once(self):
        self.simple_board()
        self.fake.add_card("b1", "c1", "Workshop de Python", due_ms=in_days(10), notifyees=[U1, U2], remind=12)
        s = self.go()
        self.assertEqual((s.sent, s.failed, s.boards_joined), (2, 0, 1))
        self.assertEqual(self.posted_to(), [U1, U2])
        s = self.go()
        self.assertEqual((s.sent, s.already_sent), (0, 2))
        self.assertEqual(len(self.fake.posts), 2)

    def test_message_content_and_link(self):
        self.simple_board()
        self.fake.add_card("b1", "c1", "Workshop de Python", due_ms=in_days(12), notifyees=[U1, U2], remind=12)
        self.go()
        msg = next(p["message"] for p in self.fake.posts if p["user_id"] == U1)
        self.assertIn("Olá Alice!", msg)
        self.assertIn("**Workshop de Python**", msg)
        self.assertIn("domingo, 25 de outubro de 2026", msg)
        self.assertIn("faltam 12 dias", msg)
        self.assertIn("https://chat.test/boards/team/team1/b1/view-b1/c1", msg)
        self.assertNotIn("$", msg)

    def test_dry_run_changes_nothing_and_respects_existing_state(self):
        self.simple_board(member=True)
        self.fake.add_card("b1", "c1", "A", due_ms=in_days(5), notifyees=[U1], remind=12)
        s = self.go(dry_run=True, store=Store(self.state_path, readonly=True))
        self.assertEqual((s.sent, len(self.fake.posts)), (1, 0))
        self.assertFalse(self.state_path.exists())         # dry-run creates no state
        self.go()                                           # real run
        s = self.go(dry_run=True, store=Store(self.state_path, readonly=True))
        self.assertEqual((s.sent, s.already_sent), (0, 1))  # dry-run now knows it was already sent
        self.assertEqual(len(self.fake.posts), 1)

    def test_dry_run_never_joins_boards(self):
        self.simple_board()                                  # open board, bot not a member yet
        self.fake.add_card("b1", "c1", "A", due_ms=in_days(5), notifyees=[U1], remind=12)
        s = self.go(dry_run=True, store=Store(self.state_path, readonly=True))
        self.assertNotIn("b1", self.fake.members)
        self.assertEqual((s.boards_unreadable, s.sent, s.boards_joined), (["Posts"], 0, 0))
        self.assertFalse(self.state_path.exists())

    def test_join_boards_only_joins_relevant_open_boards(self):
        self.simple_board()
        self.simple_board("priv", type_="P")
        self.fake.add_board("other", "Notes", props=[{"id": "x", "name": "Notes", "type": "text", "options": []}])
        self.fake.add_card("b1", "c1", "A", due_ms=in_days(5), notifyees=[U1], remind=12)
        s = self.go(join_only=True)
        self.assertEqual(self.fake.members, {"b1"})
        self.assertEqual((s.boards_joined, len(self.fake.posts)), (1, 0))

    def test_private_and_unrelated_boards_are_left_alone(self):
        self.simple_board("priv", type_="P")
        self.fake.add_card("priv", "c1", "secret", due_ms=in_days(5), notifyees=[U1], remind=12)
        self.fake.add_board("other", "Notes", props=[{"id": "x", "name": "Notes", "type": "text", "options": []}])
        s = self.go()
        self.assertEqual(self.fake.posts, [])
        self.assertEqual(s.boards_unreadable, ["Posts"])
        self.assertNotIn("other", self.fake.members)       # never joins boards it does not need

    def test_auto_join_can_be_disabled(self):
        self.cfg = self.make_cfg(REMINDER_AUTO_JOIN="false")
        self.simple_board()
        self.fake.add_card("b1", "c1", "A", due_ms=in_days(5), notifyees=[U1], remind=12)
        s = self.go()
        self.assertEqual((len(self.fake.posts), s.boards_joined), (0, 0))
        self.assertNotIn("b1", self.fake.members)

    def test_not_applicable_cards_are_skipped(self):
        self.simple_board(member=True)
        add = self.fake.add_card
        add("b1", "empty", "no remind", due_ms=in_days(5), notifyees=[U1])
        add("b1", "zero", "zero", due_ms=in_days(5), notifyees=[U1], remind=0)
        add("b1", "neg", "minus one", due_ms=in_days(5), notifyees=[U1], remind=-1)
        add("b1", "text", "text", due_ms=in_days(5), notifyees=[U1], remind="abc")
        add("b1", "huge", "huge", due_ms=in_days(5), notifyees=[U1], remind=10 ** 30)
        add("b1", "nodue", "no due", notifyees=[U1], remind=12)
        add("b1", "nobody", "no notifyee", due_ms=in_days(5), remind=12)
        add("b1", "tpl", "template", due_ms=in_days(5), notifyees=[U1], remind=12, isTemplate=True)
        add("b1", "del", "deleted", due_ms=in_days(5), notifyees=[U1], remind=12, deleteAt=1)
        add("b1", "early", "too early", due_ms=in_days(20), notifyees=[U1], remind=12)
        add("b1", "late", "passed", due_ms=in_days(-1), notifyees=[U1], remind=12)
        s = self.go()
        self.assertEqual((s.sent, s.failed, len(self.fake.posts)), (0, 0, 0))
        self.assertEqual(s.cards_checked, 11)

    def test_changed_due_date_reminds_again_once(self):
        self.simple_board(member=True)
        card = self.fake.add_card("b1", "c1", "A", due_ms=in_days(10), notifyees=[U1], remind=12)
        self.go()
        card["properties"]["p_due"] = '{"from": %d}' % in_days(8)
        s = self.go()
        self.assertEqual(s.sent, 1)
        self.assertEqual(self.go().sent, 0)
        self.assertEqual(len(self.fake.posts), 2)

    def test_person_added_later_gets_their_own_reminder(self):
        self.simple_board(member=True)
        card = self.fake.add_card("b1", "c1", "A", due_ms=in_days(10), notifyees=[U1], remind=12)
        self.go()
        card["properties"]["p_who"] = [U1, U2]
        self.go()
        self.assertEqual(self.posted_to(), [U1, U2])

    def test_deactivated_and_unknown_users(self):
        self.simple_board(member=True)
        self.fake.users[U3]["delete_at"] = 12345
        self.fake.add_card("b1", "c1", "A", due_ms=in_days(10), notifyees=[U3, GHOST, U1], remind=12)
        s = self.go()
        self.assertEqual(self.posted_to(), [U1])          # the others never block u1
        self.assertEqual(s.failed, 0)                        # a user that does not exist is permanent: no retry loop

    def test_label_with_wrong_type_is_reported(self):
        props = standard_props()
        props[2]["type"] = "select"
        self.fake.add_board("b1", "Posts", member=True, props=props)
        with self.assertLogs("reminder", level="WARNING") as logs:
            self.go()
        self.assertTrue(any("Remind Before" in m and "select" in m for m in logs.output), logs.output)

    def test_reminder_window_boundaries_via_run(self):
        self.simple_board(member=True)
        self.fake.add_card("b1", "c1", "A", due_ms=in_days(12), notifyees=[U1], remind=12)
        self.assertEqual(self.go(today=TODAY - timedelta(days=1)).sent, 0)   # 13 days before
        self.assertEqual(self.go(today=TODAY).sent, 1)                       # 12 days before


class Audience(Base):
    """The Notifyees value is just ids anyone with edit rights can type in: it must not decide who
    gets to read a board's card titles."""

    def test_private_board_only_messages_board_members(self):
        self.fake.add_board("priv", "Secret plans", type_="P", member=True)
        self.fake.board_members["priv"] = {BOT_ID, U1}          # u2 is NOT a member of this board
        self.fake.add_card("priv", "c1", "Secret", due_ms=in_days(5), notifyees=[U1, U2], remind=12)
        with self.assertLogs("reminder", level="WARNING") as logs:
            s = self.go()
        self.assertEqual(self.posted_to(), [U1])
        self.assertTrue(any(U2 in m or "bob" in m for m in logs.output))
        self.assertEqual(s.failed, 0)                             # a permanent "no", not something to retry

    def test_open_board_only_messages_team_members(self):
        self.simple_board(member=True)
        self.fake.team_members = {U1}                           # bob (u2) is outside the team
        self.fake.add_card("b1", "c1", "A", due_ms=in_days(5), notifyees=[U1, U2], remind=12)
        self.go()
        self.assertEqual(self.posted_to(), [U1])

    def test_membership_check_failure_means_not_now_not_never(self):
        self.simple_board(member=True)
        self.fake.add_card("b1", "c1", "A", due_ms=in_days(5), notifyees=[U1], remind=12)
        fault = Fault(lambda m, p: "/teams/team1/members/" in p, "status", status=500)
        self.fake.faults.append(fault)
        s = self.go()
        self.assertEqual((len(self.fake.posts), s.failed), (0, 1))   # fail closed, and report it
        self.fake.faults.clear()
        self.mm._team_members.clear()
        self.assertEqual(self.go().sent, 1)                           # sent once the check works again


class Limits(Base):
    def test_one_person_cannot_be_flooded(self):
        self.cfg = self.make_cfg(REMINDER_MAX_MESSAGES_PER_USER_PER_DAY="5")
        self.simple_board(member=True)
        for i in range(30):
            self.fake.add_card("b1", f"c{i}", f"card {i}", due_ms=in_days(5), notifyees=[U1], remind=12)
        self.fake.add_card("b1", "other", "someone else's", due_ms=in_days(5), notifyees=[U2], remind=12)
        with self.assertLogs("reminder", level="ERROR"):
            s = self.go()
        self.assertEqual(self.posted_to().count(U1), 5)
        self.assertEqual(self.posted_to().count(U2), 1)             # other people are unaffected
        self.assertEqual(s.capped, 25)

    def test_long_notifyee_lists_stay_short(self):
        custom = Path(self.tmp.name) / "colleagues.txt"
        custom.write_text("Olá $responsible_name. Colegas: $co_responsibles.", encoding="utf-8")
        self.template = render.load_template(custom)
        self.simple_board(member=True)
        ids = []
        for i in range(40):
            self.fake.add_user(uid(f"x{i}"), f"user{i}", nickname=f"Person {i}")
            ids.append(uid(f"x{i}"))
        self.fake.add_card("b1", "c1", "Big team", due_ms=in_days(5), notifyees=ids, remind=12)
        self.go()
        self.assertEqual(len(self.fake.posts), 40)
        msg = next(p["message"] for p in self.fake.posts if p["user_id"] == uid("x0"))
        self.assertIn("e mais 29", msg)                     # 39 colleagues: 10 named + "and 29 more"
        self.assertEqual(msg.count("Person "), 10 + 1)      # the greeting plus ten listed names
        self.assertLess(max(len(p["message"]) for p in self.fake.posts), 1000)

    def test_oversized_responses_are_refused(self):
        self.simple_board(member=True)
        self.fake.add_card("b1", "c1", "A" * 5000, due_ms=in_days(5), notifyees=[U1], remind=12)
        with mock.patch("remindabot.driver.MAX_RESPONSE_BYTES", 1000):
            s = self.go()
        self.assertEqual((len(self.fake.posts), s.failed >= 1), (0, True))


class Pagination(Base):
    def test_cards_beyond_the_first_100_are_checked(self):
        self.simple_board(member=True)
        for i in range(2500):
            hit = i in (150, 1200, 2499)
            self.fake.add_card("b1", f"c{i}", f"card {i}", due_ms=in_days(5 if hit else 90),
                               notifyees=[U1], remind=12)
        s = self.go()
        self.assertEqual((s.cards_checked, s.sent), (2500, 3))

    def test_server_capping_page_size_does_not_truncate(self):
        self.simple_board(member=True)
        self.fake.server_max_per_page = 100               # the server ignores our larger per_page
        for i in range(450):
            self.fake.add_card("b1", f"c{i}", f"card {i}", due_ms=in_days(5 if i == 449 else 90),
                               notifyees=[U1], remind=12)
        s = self.go()
        self.assertEqual((s.cards_checked, s.sent), (450, 1))


class Faults(Base):
    def setUp(self):
        super().setUp()
        self.simple_board("b1", member=True)
        self.fake.add_card("b1", "c1", "A", due_ms=in_days(5), notifyees=[U1], remind=12)

    def count(self, method, fragment):
        return sum(1 for m, p in self.fake.requests if m == method and fragment in p)

    def test_transient_5xx_on_reads_is_retried(self):
        self.fake.faults.append(Fault(lambda m, p: m == "GET" and "/cards" in p, "status", times=2, status=503))
        s = self.go()
        self.assertEqual((s.sent, s.failed), (1, 0))

    def test_persistent_failure_on_one_board_does_not_stop_the_others(self):
        self.fake.add_board("b2", "Events", member=True)
        self.fake.add_card("b2", "c2", "B", due_ms=in_days(5), notifyees=[U2], remind=12)
        self.fake.faults.append(Fault(lambda m, p: "/boards/b1/cards" in p, "status", status=500))
        s = self.go()
        self.assertEqual(self.posted_to(), [U2])
        self.assertGreaterEqual(s.failed, 1)               # reported, so the loop retries later

    def test_html_instead_of_json_is_an_error_not_data(self):
        self.fake.faults.append(Fault(lambda m, p: "/boards/b1/cards" in p, "html"))
        s = self.go()
        self.assertEqual((len(self.fake.posts), s.failed), (0, 1))

    def test_failed_message_is_released_and_only_it_is_retried(self):
        self.fake.add_card("b1", "c2", "B", due_ms=in_days(5), notifyees=[U2], remind=12)
        self.fake.faults.append(Fault(lambda m, p: m == "POST" and p.endswith("/posts"), "status", times=1))
        s = self.go()
        self.assertEqual((s.sent, s.failed), (1, 1))
        s = self.go()
        self.assertEqual((s.sent, s.failed, s.already_sent), (1, 0, 1))
        self.assertEqual(self.posted_to(), [U1, U2])   # each exactly once

    def test_posts_are_never_retried_automatically(self):
        self.fake.faults.append(Fault(lambda m, p: m == "POST" and p.endswith("/posts"), "status", status=500))
        self.go()
        self.assertEqual(self.count("POST", "/posts"), 1)

    def test_redirect_is_an_error_and_never_followed(self):
        self.fake.faults.append(Fault(lambda m, p: m == "POST" and p.endswith("/posts"), "redirect"))
        s = self.go()
        self.assertEqual((len(self.fake.posts), s.failed), (0, 1))
        self.fake.faults.clear()
        self.assertEqual(self.go().sent, 1)               # claim was released, so it still goes out

    def test_dropped_connection_on_message_is_verified_before_any_resend(self):
        self.fake.faults.append(Fault(lambda m, p: m == "POST" and p.endswith("/posts"), "drop", times=1))
        s = self.go()
        self.assertEqual((s.sent, s.failed), (0, 1))                 # outcome unknown
        s = self.go()                                                 # immediately: still "in flight"
        self.assertEqual((s.sent, s.deferred, len(self.fake.posts)), (0, 1, 0))
        self.now_ts += 301                                            # the claim goes stale
        self.assertEqual(self.go().sent, 1)                           # it never arrived, so now it is sent
        self.assertEqual(len(self.fake.posts), 1)

    def test_message_delivered_but_answer_lost_is_never_duplicated(self):
        # the server created the post and then the connection dropped: the bot cannot know
        self.fake.faults.append(Fault(lambda m, p: m == "POST" and p.endswith("/posts"), "commit_then_drop", times=1))
        s = self.go()
        self.assertEqual((s.sent, s.failed, len(self.fake.posts)), (0, 1, 1))
        self.assertEqual(self.go().sent, 0)                           # claim still fresh: leave it alone
        self.now_ts += 301
        s = self.go()                                                 # claim is stale: look, then record
        self.assertEqual((s.sent, s.already_sent, len(self.fake.posts)), (0, 1, 1))
        self.assertEqual(self.go().already_sent, 1)

    def test_timeout_is_an_api_error(self):
        self.fake.faults.append(Fault(lambda m, p: p.endswith("/users/me"), "slow", delay=1.5))
        self.mm = Driver(self.url, BOT_TOKEN, timeout=0.3)
        self.mm.api._sleep = lambda s: None
        with self.assertRaises(ApiError) as ctx:
            self.go()
        self.assertEqual(ctx.exception.status, 0)

    def test_bad_token(self):
        self.mm = Driver(self.url, "wrong", timeout=5)
        with self.assertRaises(ApiError) as ctx:
            self.go()
        self.assertEqual(ctx.exception.status, 401)

    def test_server_down(self):
        self.fake.stop()
        with self.assertRaises(ApiError) as ctx:
            self.go()
        self.assertEqual(ctx.exception.status, 0)

    def test_one_broken_card_is_contained(self):
        self.fake.add_card("b1", "c2", "B", due_ms=in_days(5), notifyees=[U2], remind=12)
        real = boards_mod.decode_card

        def flaky(card, schema, tz):
            if card["id"] == "c1":
                raise RuntimeError("synthetic bug")
            return real(card, schema, tz)

        with mock.patch("remindabot.runner.decode_card", flaky):
            with self.assertLogs("reminder", level="ERROR"):
                s = self.go()
        self.assertEqual((self.posted_to(), s.failed), ([U2], 1))


class StateSafety(Base):
    def setUp(self):
        super().setUp()
        self.simple_board("b1", member=True)

    def test_only_one_of_two_stores_can_claim(self):
        a, b = self.store(), self.store()
        self.assertEqual(a.claim("c", "u", "2026-10-20", 7), "new")
        self.assertIsNone(b.claim("c", "u", "2026-10-20", 7))
        a.confirm("c", "u", "2026-10-20", 7)
        self.assertIsNone(b.claim("c", "u", "2026-10-20", 7))
        self.assertEqual(b.status("c", "u", "2026-10-20", 7), "sent")

    def test_concurrent_runs_never_duplicate(self):
        for i in range(25):
            self.fake.add_card("b1", f"c{i}", f"card {i}", due_ms=in_days(5), notifyees=[U1, U2], remind=12)
        errors = []

        def worker():
            try:
                store = Store(self.state_path)
                mm = Driver(self.url, BOT_TOKEN, timeout=10)
                run_once(self.cfg, mm, self.boards, store, self.template, TODAY)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(self.fake.posts), 50)          # 25 cards x 2 people, exactly once each

    def test_crash_between_claim_and_confirm_never_duplicates_but_recovers(self):
        self.fake.add_card("b1", "c1", "A", due_ms=in_days(5), notifyees=[U1], remind=12)
        crashing = mock.patch.object(Driver, "send_dm", side_effect=SystemExit("killed"))
        with crashing, self.assertRaises(SystemExit):
            self.go()
        # restart immediately: the claim is fresh, so it is NOT re-sent (no duplicate burst) ...
        s = self.go()
        self.assertEqual((s.sent, s.deferred, len(self.fake.posts)), (0, 1, 0))
        # ... but once the claim is stale it is retried, and then exactly once
        self.now_ts += 301
        self.assertEqual(self.go().sent, 1)
        self.assertEqual(self.go().sent, 0)
        self.assertEqual(len(self.fake.posts), 1)

    def test_state_write_failure_stops_sending(self):
        for i in range(5):
            self.fake.add_card("b1", f"c{i}", f"card {i}", due_ms=in_days(5), notifyees=[U1], remind=12)
        store = self.store()
        with mock.patch.object(Store, "confirm", side_effect=sqlite3.OperationalError("disk full")):
            with self.assertRaises(StateError):
                run_once(self.cfg, self.mm, self.boards, store, self.template, TODAY)
        self.assertEqual(len(self.fake.posts), 1)           # stopped after the first, not 5

    def test_safety_cap(self):
        self.cfg = self.make_cfg(REMINDER_MAX_MESSAGES_PER_DAY="3")
        for i in range(8):
            self.fake.add_card("b1", f"c{i}", f"card {i}", due_ms=in_days(5), notifyees=[U1], remind=12)
        with self.assertLogs("reminder", level="ERROR"):
            s = self.go()
        self.assertEqual((s.sent, s.capped, len(self.fake.posts)), (3, 5, 3))
        with self.assertLogs("reminder", level="ERROR"):
            self.assertEqual(self.go().sent, 0)            # the same day: the allowance is used up, retries do not add
        self.now_ts += 86400
        self.assertEqual(self.go().sent, 3)                # tomorrow the held-back ones continue


class Security(Base):
    def test_card_title_and_names_cannot_inject_markdown_links_or_mentions(self):
        self.simple_board(member=True)
        evil = "[Confirma aqui](http://evil.example/x) @all ~town-square www.evil.example\n# Heading\n`code` **bold**"
        self.fake.users[U1]["nickname"] = "[Admin](http://evil.example)"
        self.fake.add_card("b1", "c1", evil, due_ms=in_days(5), notifyees=[U1], remind=12)
        self.go()
        msg = self.fake.posts[0]["message"]
        for bad in ("](http://", "(http://evil", "http://evil", "@all", "~town-square", "www.evil", "\n# Heading", "`code`", "**bold**"):
            self.assertNotIn(bad, msg, bad)
        self.assertIn("evil", msg)                          # it is still shown, just inert
        self.assertIn("[Abrir o cartão](https://chat.test/", msg)   # our own link is intact

    def test_bidi_overrides_and_invisible_characters_are_removed(self):
        s = render.sanitize_inline
        self.assertEqual(s("\u202eABC\u202c done"), "ABC done")        # right-to-left override
        self.assertEqual(s("a\u200bb\u2066c\u2069"), "abc")           # zero-width and isolates
        self.assertEqual(s("\x00\x01\u200b", fallback="(sem título)"), "(sem título)")

    def test_a_title_of_only_invisible_characters_still_gives_a_readable_message(self):
        self.simple_board(member=True)
        self.fake.add_card("b1", "c1", "\x00\u200b\u202e", due_ms=in_days(5), notifyees=[U1], remind=12)
        self.go()
        self.assertIn("(sem título)", self.fake.posts[0]["message"])
        self.assertNotIn("****", self.fake.posts[0]["message"])

    def test_sanitize_inline_unit(self):
        s = render.sanitize_inline
        self.assertEqual(s("  a \n\t b  "), "a b")
        self.assertEqual(s(None), "")
        self.assertLessEqual(len(s("x" * 1000, max_len=50)), 50)
        self.assertTrue(s("x" * 1000, max_len=50).endswith("…"))
        self.assertNotIn("](", s("[a](b)"))
        self.assertEqual(s("plain text 123"), "plain text 123")


class FakeClock:
    """Advances only when the bot sleeps; presents Lisbon wall-clock time."""

    def __init__(self, start_utc: datetime):
        self.utc = start_utc

    def now(self) -> datetime:
        return self.utc.astimezone(LISBON)

    def sleep(self, seconds: float):
        self.utc += timedelta(seconds=seconds)


class Serve(Base):
    def setUp(self):
        super().setUp()
        self.simple_board("b1", member=True)
        self.fake.add_card("b1", "c1", "A", due_ms=noon_utc_ms(date(2026, 12, 20)), notifyees=[U1], remind=12)

    def serve(self, start_local: datetime, minutes: int, *, cfg=None, store=None):
        """Run the daily loop on a fake clock for `minutes` iterations; returns (passes started, clock)."""
        clock = FakeClock(start_local.astimezone(timezone.utc))
        real, calls = scheduler.run_once, {"n": 0}

        def counting(*args, **kwargs):
            calls["n"] += 1
            return real(*args, **kwargs)

        store = store or self.store(clock=lambda: clock.utc.timestamp())
        with mock.patch.object(scheduler, "run_once", counting):
            scheduler.serve(cfg or self.cfg, self.mm, self.boards, store,
                           now_fn=clock.now, sleep_fn=clock.sleep, max_iterations=minutes)
        return calls["n"], clock

    def test_one_pass_per_day_at_send_time(self):
        passes, _ = self.serve(datetime(2026, 12, 10, 8, 0, tzinfo=LISBON), minutes=24 * 60)  # 08:00 -> 08:00 next day
        self.assertEqual(passes, 1)
        self.assertEqual(len(self.fake.posts), 1)

    def test_not_before_send_time(self):
        passes, _ = self.serve(datetime(2026, 12, 10, 3, 0, tzinfo=LISBON), minutes=300)      # until 08:00
        self.assertEqual(passes, 0)

    def test_with_the_daily_pass_off_nothing_is_ever_sent_by_itself(self):
        cfg = dataclasses.replace(self.cfg, daily_pass=False)
        passes, _ = self.serve(datetime(2026, 12, 10, 8, 0, tzinfo=LISBON), minutes=2 * 24 * 60, cfg=cfg)
        self.assertEqual(passes, 0)
        self.assertEqual(self.fake.posts, [])
        # the same card is sent by a pass someone asks for (--once / /remind_send), as always
        self.assertEqual(run_once(cfg, self.mm, self.boards, self.store(), self.template,
                                  date(2026, 12, 10)).sent, 1)

    def test_catches_up_after_a_restart_later_in_the_day(self):
        passes, _ = self.serve(datetime(2026, 12, 10, 15, 30, tzinfo=LISBON), minutes=5)
        self.assertEqual(passes, 1)

    def test_a_restart_after_the_run_does_not_run_again(self):
        self.serve(datetime(2026, 12, 10, 9, 0, tzinfo=LISBON), minutes=5)
        passes, _ = self.serve(datetime(2026, 12, 10, 11, 0, tzinfo=LISBON), minutes=60, store=self.store())
        self.assertEqual(passes, 0)

    def test_with_the_shipped_defaults_the_pass_runs_every_day_at_14_00_weekends_included(self):
        cfg = self.make_cfg(REMINDER_SEND_AT="")            # blank = the shipped default, not the pinned time
        self.assertEqual((cfg.send_at.hour, cfg.send_at.minute, cfg.daily_pass), (14, 0, True))
        clock = FakeClock(datetime(2026, 12, 11, 12, 0, tzinfo=LISBON).astimezone(timezone.utc))   # a Friday
        started, real = [], scheduler.run_once

        def recording(*args, **kwargs):
            started.append(clock.now())
            return real(*args, **kwargs)

        store = self.store(clock=lambda: clock.utc.timestamp())
        with mock.patch.object(scheduler, "run_once", recording):       # Friday 12:00 -> Monday 15:00
            scheduler.serve(cfg, self.mm, self.boards, store, now_fn=clock.now, sleep_fn=clock.sleep,
                            max_iterations=75 * 60)
        self.assertEqual([t.strftime("%a %H:%M") for t in started], ["Fri 14:00", "Sat 14:00", "Sun 14:00", "Mon 14:00"])

    def test_every_day_gets_its_own_pass(self):
        self.fake.add_card("b1", "c2", "B", due_ms=noon_utc_ms(date(2026, 12, 14)), notifyees=[U2], remind=5)
        passes, _ = self.serve(datetime(2026, 12, 8, 9, 0, tzinfo=LISBON), minutes=3 * 24 * 60)
        self.assertEqual(passes, 3)
        # c2's window opens 9 Dec; c1 (due 20 Dec, remind 12) opens 8 Dec. Each person exactly once.
        self.assertEqual(self.posted_to(), [U1, U2])

    def test_unreachable_server_retries_in_10_minutes_then_succeeds(self):
        # 3 failures = the whole first pass (1 try + 2 client retries); the pass after 10 min succeeds
        self.fake.faults.append(Fault(lambda m, p: p == "/api/v4/users/me", "status", times=3, status=502))
        with self.assertLogs("reminder", level="ERROR"):
            passes, _ = self.serve(datetime(2026, 12, 10, 9, 0, tzinfo=LISBON), minutes=60)
        self.assertEqual(passes, 2)                         # one failed pass, one retry 10 minutes later
        self.assertEqual(len(self.fake.posts), 1)

    def test_partial_failure_retries_only_what_failed_then_stops(self):
        self.fake.add_card("b1", "c2", "B", due_ms=noon_utc_ms(date(2026, 12, 20)), notifyees=[U2], remind=12)
        self.fake.faults.append(Fault(lambda m, p: m == "POST" and p.endswith("/posts"), "status", times=1))
        with self.assertLogs("reminder", level="WARNING"):
            self.serve(datetime(2026, 12, 10, 9, 0, tzinfo=LISBON), minutes=90)
        self.assertEqual(self.posted_to(), [U1, U2])

    def test_gives_up_after_four_attempts_and_waits_for_tomorrow(self):
        self.fake.faults.append(Fault(lambda m, p: m == "POST" and p.endswith("/posts"), "status"))
        with self.assertLogs("reminder", level="WARNING"):
            passes, _ = self.serve(datetime(2026, 12, 10, 9, 0, tzinfo=LISBON), minutes=12 * 60)
        self.assertEqual(passes, scheduler.MAX_ATTEMPTS_PER_DAY)

    def test_unexpected_exception_does_not_kill_the_loop(self):
        real, state = scheduler.run_once, {"n": 0}

        def flaky(*args, **kwargs):
            state["n"] += 1
            if state["n"] == 1:
                raise RuntimeError("synthetic bug")
            return real(*args, **kwargs)

        with mock.patch.object(scheduler, "run_once", flaky), self.assertLogs("reminder", level="ERROR"):
            passes, _ = self.serve(datetime(2026, 12, 10, 9, 0, tzinfo=LISBON), minutes=30)
        self.assertEqual(passes, 2)                         # failed once, retried 10 minutes later
        self.assertEqual(len(self.fake.posts), 1)

    def test_bad_template_does_not_kill_the_loop_or_send(self):
        bad = Path(self.tmp.name) / "bad.txt"
        bad.write_text("Olá $nope", encoding="utf-8")
        cfg = self.make_cfg(REMINDER_TEMPLATE_FILE=str(bad))
        with self.assertLogs("reminder", level="ERROR"):
            self.serve(datetime(2026, 12, 10, 9, 0, tzinfo=LISBON), minutes=30, cfg=cfg)
        self.assertEqual(self.fake.posts, [])

    def test_state_failure_halts_for_the_day_without_spamming(self):
        for i in range(2, 6):
            self.fake.add_card("b1", f"c{i}", f"card {i}", due_ms=noon_utc_ms(date(2026, 12, 20)),
                               notifyees=[U1], remind=12)
        with mock.patch.object(Store, "confirm", side_effect=sqlite3.OperationalError("disk full")):
            with self.assertLogs("reminder", level="CRITICAL"):
                passes, _ = self.serve(datetime(2026, 12, 10, 9, 0, tzinfo=LISBON), minutes=10 * 60)
        self.assertEqual(passes, 1)
        self.assertEqual(len(self.fake.posts), 1)

    def test_dst_fall_back_day_runs_once(self):
        # 2026-10-25: clocks go back 02:00 -> 01:00, so 01:30 happens twice.
        cfg = self.make_cfg(REMINDER_SEND_AT="01:30")
        self.fake.cards["b1"][0]["properties"]["p_due"] = '{"from": %d}' % noon_utc_ms(date(2026, 11, 2))
        passes, _ = self.serve(datetime(2026, 10, 25, 0, 0, tzinfo=LISBON), minutes=6 * 60, cfg=cfg)
        self.assertEqual(passes, 1)

    def test_dst_spring_forward_skipped_time_still_runs(self):
        # 2026-03-29: 01:00 -> 02:00, so 01:30 does not exist; the pass must still happen that day.
        cfg = self.make_cfg(REMINDER_SEND_AT="01:30")
        self.fake.cards["b1"][0]["properties"]["p_due"] = '{"from": %d}' % noon_utc_ms(date(2026, 4, 5))
        passes, _ = self.serve(datetime(2026, 3, 29, 0, 0, tzinfo=LISBON), minutes=5 * 60, cfg=cfg)
        self.assertEqual(passes, 1)


class ReviewFixes(Base):
    """One test per finding of the independent code review (see git history)."""

    def test_lookups_are_forgotten_between_passes(self):
        self.simple_board(member=True)
        self.fake.team_members = set()                      # alice is not in the team yet
        self.fake.add_card("b1", "c1", "A", due_ms=in_days(5), notifyees=[U1], remind=12)
        with self.assertLogs("reminder", level="WARNING"):
            self.go()
        self.assertEqual(self.fake.posts, [])
        self.fake.team_members = {U1}                       # she joins; the SAME client object is reused
        self.assertEqual(self.go().sent, 1)

    def test_deactivation_between_passes_is_noticed(self):
        self.simple_board(member=True)
        self.fake.add_card("b1", "c1", "A", due_ms=in_days(5), notifyees=[U1, U2], remind=12)
        # a dry-run looks everyone up (and caches them in this client) without sending anything
        self.go(dry_run=True, store=Store(self.state_path, readonly=True))
        self.assertEqual(self.fake.posts, [])
        self.fake.users[U2]["delete_at"] = 999              # deactivated afterwards; the client is the same object
        self.go()
        self.assertEqual(self.posted_to(), [U1])

    def test_most_urgent_reminders_go_first_when_a_cap_bites(self):
        self.cfg = self.make_cfg(REMINDER_MAX_MESSAGES_PER_DAY="2")
        self.simple_board(member=True)
        self.fake.add_card("b1", "late", "due in 10", due_ms=in_days(10), notifyees=[U1], remind=12)
        self.fake.add_card("b1", "soon", "due in 3", due_ms=in_days(3), notifyees=[U2], remind=12)
        self.fake.add_card("b1", "mid", "due in 6", due_ms=in_days(6), notifyees=[U3], remind=12)
        with self.assertLogs("reminder", level="ERROR"):
            self.go()
        self.assertEqual(self.posted_to(), sorted([U2, U3]))   # the two earliest deadlines, not board order

    def test_per_user_cap_counts_what_earlier_passes_of_the_day_sent(self):
        self.cfg = self.make_cfg(REMINDER_MAX_MESSAGES_PER_USER_PER_DAY="2")
        self.simple_board(member=True)
        for i in range(2):
            self.fake.add_card("b1", f"a{i}", f"card {i}", due_ms=in_days(5), notifyees=[U1], remind=12)
        self.assertEqual(self.go().sent, 2)
        self.fake.add_card("b1", "late-addition", "new card", due_ms=in_days(5), notifyees=[U1], remind=12)
        with self.assertLogs("reminder", level="ERROR"):
            s = self.go()
        self.assertEqual((s.sent, s.capped), (0, 1))        # a second pass the same day is not a new allowance

    def test_unknown_user_is_permanent_but_a_server_error_is_transient(self):
        self.simple_board(member=True)
        self.fake.add_card("b1", "c1", "A", due_ms=in_days(5), notifyees=[GHOST, U1], remind=12)
        self.assertEqual(self.go().failed, 0)               # 404: that user does not exist, never retry
        self.fake.add_card("b1", "c2", "B", due_ms=in_days(5), notifyees=[U2], remind=12)
        self.fake.faults.append(Fault(lambda m, p: p.endswith(f"/users/{U2}"), "status", status=500))
        s = self.go()
        self.assertGreaterEqual(s.failed, 1)                # 500: worth another look later

    def test_notifyee_values_that_are_not_ids_never_reach_the_server(self):
        self.simple_board(member=True)
        self.fake.add_card("b1", "c1", "A", due_ms=in_days(5),
                           notifyees=["me", "../..", "username/alice", "x?per_page=1", U1], remind=12)
        self.go()
        paths = [p for _, p in self.fake.requests]
        user_calls = [p for p in paths if p.startswith("/api/v4/users")]
        # the only /users calls: the bot itself, its teams, and the ONE well-formed notifyee
        self.assertEqual(sorted(user_calls),
                         sorted(["/api/v4/users/me", "/api/v4/users/me/teams", f"/api/v4/users/{U1}"]))
        self.assertFalse(any(".." in p or "username" in p for p in paths))
        self.assertEqual(self.posted_to(), [U1])

    def test_the_bot_never_messages_itself(self):
        self.simple_board(member=True)
        self.fake.add_user(BOT_ID, "remindabot")
        self.fake.add_card("b1", "c1", "A", due_ms=in_days(5), notifyees=[BOT_ID, U1], remind=12)
        self.go()
        self.assertEqual(self.posted_to(), [U1])

    def test_every_http_attempt_updates_the_heartbeat(self):
        beats = []
        mm = Driver(self.url, BOT_TOKEN, timeout=5, beat=lambda: beats.append(1))
        mm.api._sleep = lambda s: None
        self.fake.faults.append(Fault(lambda m, p: p == "/api/v4/users/me", "status", times=2, status=503))
        mm.me()                                             # two failures and one success = three attempts
        self.assertEqual(len(beats), 3)

    def test_unreadable_error_bodies_and_bad_urls_become_api_errors(self):
        from remindabot.driver import Client
        for base in ("mattermost", "", "ftp://x"):
            with self.assertRaises(ApiError):
                Client(base, "t", retries=0).request("GET", "/x")


class ReviewFixesServe(Serve):
    """Daily-loop findings; reuses the Serve fixtures (fake clock, one card due 20 Dec)."""

    def test_a_claim_held_by_another_process_is_retried_not_forgotten(self):
        start = datetime(2026, 12, 10, 9, 0, tzinfo=LISBON)
        other = Store(self.state_path, clock=lambda: start.timestamp())
        self.assertEqual(other.claim(self.fake.cards["b1"][0]["id"], U1, "2026-12-20", 12), "new")
        passes, _ = self.serve(start, minutes=30)
        self.assertEqual(len(self.fake.posts), 1)           # not lost: sent once the other claim went stale
        self.assertEqual(passes, 2)

    def test_the_retry_waits_ten_minutes_from_the_END_of_a_slow_pass(self):
        clock = FakeClock(datetime(2026, 12, 10, 9, 0, tzinfo=LISBON).astimezone(timezone.utc))
        real, starts = scheduler.run_once, []

        def slow_then_ok(*args, **kwargs):
            starts.append(clock.utc)
            if len(starts) == 1:
                clock.utc += timedelta(minutes=25)           # a pass full of timeouts
                raise ApiError(0, "server unreachable")
            return real(*args, **kwargs)

        with mock.patch.object(scheduler, "run_once", slow_then_ok), self.assertLogs("reminder", level="ERROR"):
            scheduler.serve(self.cfg, self.mm, self.boards, self.store(clock=lambda: clock.utc.timestamp()),
                           now_fn=clock.now, sleep_fn=clock.sleep, max_iterations=60)
        self.assertEqual(len(starts), 2)
        ended = starts[0] + timedelta(minutes=25)
        self.assertGreaterEqual(starts[1] - ended, timedelta(minutes=10))


class MultipleReminders(Base):
    """"Remind Before" is a Text list ("7;3;1") and only the people in "Notifyees" are told."""

    def messages_to(self, user_id: str) -> list[str]:
        return [p["message"] for p in self.fake.posts if p["user_id"] == user_id]

    def test_each_listed_number_sends_one_message_when_its_day_comes(self):
        self.simple_board(member=True)
        self.fake.add_card("b1", "c1", "Workshop", due_ms=in_days(10), notifyees=[U1], remind="7;3;1")
        sent_on = {}
        for offset in range(0, 12):                       # TODAY .. TODAY+11 (due on TODAY+10)
            day = TODAY + timedelta(days=offset)
            before = len(self.fake.posts)
            self.go(today=day)
            if len(self.fake.posts) > before:
                sent_on[offset] = self.fake.posts[-1]["message"]
        # 7 days before = day 3, 3 days before = day 7, 1 day before = day 9
        self.assertEqual(sorted(sent_on), [3, 7, 9])
        self.assertIn("faltam 7 dias", sent_on[3])
        self.assertIn("faltam 3 dias", sent_on[7])
        self.assertIn("falta 1 dia", sent_on[9])

    def test_a_late_card_gets_one_message_not_one_per_missed_stage(self):
        self.simple_board(member=True)
        self.fake.add_card("b1", "c1", "Late", due_ms=in_days(2), notifyees=[U1], remind="14;7;3;1")
        s = self.go()                                      # 2 days left: stages 14, 7 and 3 all passed
        self.assertEqual((s.sent, len(self.fake.posts)), (1, 1))
        self.assertIn("faltam 2 dias", self.fake.posts[0]["message"])
        self.assertEqual(self.go().sent, 0)                # same day again: nothing
        self.assertEqual(self.go(today=TODAY + timedelta(days=1)).sent, 1)   # 1 day left: the last stage
        self.assertEqual(self.go(today=TODAY + timedelta(days=2)).sent, 0)   # due today: already announced
        self.assertEqual(len(self.fake.posts), 2)

    def test_the_bot_being_off_for_days_still_sends_the_missed_stage_once(self):
        self.simple_board(member=True)
        self.fake.add_card("b1", "c1", "A", due_ms=in_days(10), notifyees=[U1], remind="7;3")
        self.go(today=TODAY + timedelta(days=3))           # stage 7
        self.assertEqual(len(self.fake.posts), 1)
        s = self.go(today=TODAY + timedelta(days=9))       # off during days 4-8: stage 3 is late but is sent
        self.assertEqual(s.sent, 1)
        self.assertEqual(len(self.fake.posts), 2)

    def test_only_notifyees_are_told_never_the_assignees(self):
        props = standard_props() + [{"id": "p_ass", "name": "Assignees", "type": "multiPerson", "options": []}]
        self.fake.add_board("b1", "Posts", member=True, props=props)
        card = self.fake.add_card("b1", "c1", "A", due_ms=in_days(5), notifyees=[U2], remind=7)
        card["properties"]["p_ass"] = [U1, U2, U3]
        s = self.go()
        self.assertEqual(self.posted_to(), [U2])
        self.assertEqual(s.sent, 1)

    def test_nobody_in_notifyees_is_reported(self):
        self.simple_board(member=True)
        self.fake.add_card("b1", "c1", "Forgotten", due_ms=in_days(5), remind="7;3")
        with self.assertLogs("reminder", level="WARNING") as logs:
            s = self.go()
        self.assertEqual((s.sent, s.no_notifyees, len(self.fake.posts)), (0, 1, 0))
        self.assertTrue(any("Forgotten" in m and "Notifyees" in m for m in logs.output), logs.output)
        self.assertIn("nobody in Notifyees", s.line())

    def test_a_board_that_still_has_only_assignees_is_reported(self):
        props = [{"id": "p_due", "name": "Due Date", "type": "date", "options": []},
                 {"id": "p_who", "name": "Assignees", "type": "multiPerson", "options": []},
                 {"id": "p_rem", "name": "Remind Before", "type": "text", "options": []}]
        self.fake.add_board("b1", "Posts", member=True, props=props)
        with self.assertLogs("reminder", level="WARNING") as logs:
            s = self.go()
        self.assertTrue(any("'Notifyees' is missing" in m for m in logs.output), logs.output)
        self.assertEqual((s.boards_relevant, s.sent), (0, 0))

    def test_notifyees_property_name_can_be_pointed_back_at_assignees(self):
        props = standard_props()
        props[1]["name"] = "Assignees"
        self.fake.add_board("b1", "Posts", member=True, props=props)
        self.cfg = self.make_cfg(REMINDER_PROP_NOTIFYEES="Assignees")
        self.fake.add_card("b1", "c1", "A", due_ms=in_days(5), notifyees=[U1, U2], remind=7)
        self.assertEqual(self.go().sent, 2)

    def test_a_remind_before_that_is_not_text_is_refused_and_reported(self):
        """Number, Select and Multi select are not read at all: one way to do it, and a clear warning."""
        for board_id, kind in (("b1", "number"), ("b2", "multiSelect"), ("b3", "select")):
            props = standard_props()
            props[2]["type"] = kind
            self.fake.add_board(board_id, f"Board {kind}", member=True, props=props)
            self.fake.add_card(board_id, f"c-{kind}", "A", due_ms=in_days(5), notifyees=[U1], remind="7")
        with self.assertLogs("reminder", level="WARNING") as logs:
            s = self.go()
        self.assertEqual((s.boards_relevant, s.sent, len(self.fake.posts)), (0, 0, 0))
        for kind in ("number", "multiSelect", "select"):
            self.assertTrue(any(f"Board {kind}" in m and f"has type {kind!r}" in m and "['text']" in m
                                for m in logs.output), (kind, logs.output))

    def test_a_card_value_that_is_not_text_is_reported_not_guessed(self):
        """e.g. a list of option ids left over from when the property was a Multi select."""
        self.simple_board(member=True)
        self.fake.add_card("b1", "c1", "Left over", due_ms=in_days(5), notifyees=[U1], remind=["o3", "o1"])
        with self.assertLogs("reminder", level="WARNING") as logs:
            s = self.go()
        self.assertEqual((s.sent, len(self.fake.posts)), (0, 0))
        self.assertTrue(any("Left over" in m and "(not text)" in m for m in logs.output), logs.output)

    def test_commas_do_not_separate_and_the_warning_says_what_to_write(self):
        self.simple_board(member=True)
        self.fake.add_card("b1", "c1", "A", due_ms=in_days(5), notifyees=[U1], remind="7, 3, 1")
        with self.assertLogs("reminder", level="WARNING") as logs:
            s = self.go()
        self.assertEqual((s.sent, len(self.fake.posts)), (0, 0))          # reported, never guessed
        self.assertTrue(any("7, 3, 1" in m and "separated by ';'" in m for m in logs.output), logs.output)

    def test_a_single_number_needs_no_separator(self):
        self.simple_board(member=True)
        self.fake.add_card("b1", "c1", "A", due_ms=in_days(5), notifyees=[U1], remind=12)
        self.assertEqual(self.go().sent, 1)

    def test_unreadable_entries_are_warned_about_but_the_rest_still_works(self):
        self.simple_board(member=True)
        self.fake.add_card("b1", "c1", "A", due_ms=in_days(5), notifyees=[U1], remind="7;tres;1.5;1 semana")
        with self.assertLogs("reminder", level="WARNING") as logs:
            s = self.go()
        self.assertEqual(s.sent, 1)
        self.assertTrue(any("tres" in m and "1.5" in m and "1 semana" in m for m in logs.output), logs.output)
        self.assertIn("faltam 5 dias", self.fake.posts[0]["message"])   # the "1" of "1 semana" did not become a reminder

    def test_zero_and_minus_one_are_silent_and_never_remind(self):
        self.simple_board(member=True)
        self.fake.add_card("b1", "z", "zero", due_ms=in_days(5), notifyees=[U1], remind="0")
        self.fake.add_card("b1", "m", "minus", due_ms=in_days(5), notifyees=[U1], remind="-1")
        with self.assertLogs("reminder", level="DEBUG") as logs:   # assertLogs needs at least one record
            logging.getLogger("reminder").debug("start")
            s = self.go()
        self.assertEqual((s.sent, len(self.fake.posts)), (0, 0))
        self.assertFalse(any(rec.levelno >= logging.WARNING for rec in logs.records))

    def test_template_cards_are_never_reminded_whatever_the_list(self):
        self.simple_board(member=True)
        self.fake.add_card("b1", "t", "template", due_ms=in_days(5), notifyees=[U1], remind="7;3",
                           isTemplate=True)
        self.assertEqual(self.go().sent, 0)

    def test_changed_due_date_starts_the_list_over(self):
        self.simple_board(member=True)
        card = self.fake.add_card("b1", "c1", "A", due_ms=in_days(5), notifyees=[U1], remind="7;3;1")
        self.go()                                           # stage 7
        card["properties"]["p_due"] = '{"from": %d}' % in_days(20)
        self.assertEqual(self.go().sent, 0)                 # 20 days left: too early again
        card["properties"]["p_due"] = '{"from": %d}' % in_days(6)
        self.assertEqual(self.go().sent, 1)                 # new date, stage 7 is current again
        self.assertEqual(len(self.fake.posts), 2)

    def test_editing_the_list_after_a_reminder_does_not_repeat_it(self):
        self.simple_board(member=True)
        card = self.fake.add_card("b1", "c1", "A", due_ms=in_days(5), notifyees=[U1], remind="7;3")
        self.go(today=TODAY + timedelta(days=2))            # 3 days left -> stage 3 sent
        self.assertEqual(len(self.fake.posts), 1)
        card["properties"]["p_rem"] = "7"                   # somebody removes the 3
        self.assertEqual(self.go(today=TODAY + timedelta(days=2)).sent, 0)
        card["properties"]["p_rem"] = "7;4;3"               # ... and adds a 4: already past, stage 3 stays done
        self.assertEqual(self.go(today=TODAY + timedelta(days=2)).sent, 0)
        card["properties"]["p_rem"] = "7;3;1"               # a new, smaller stage still comes
        self.assertEqual(self.go(today=TODAY + timedelta(days=4)).sent, 1)

    def test_each_notifyee_gets_each_stage_once(self):
        self.simple_board(member=True)
        self.fake.add_card("b1", "c1", "A", due_ms=in_days(5), notifyees=[U1, U2], remind="5;2")
        for offset in range(0, 6):
            self.go(today=TODAY + timedelta(days=offset))
        self.assertEqual(self.posted_to(), [U1, U1, U2, U2])

    def test_daily_cap_counts_every_message_of_a_list(self):
        self.cfg = self.make_cfg(REMINDER_MAX_MESSAGES_PER_USER_PER_DAY="2")
        self.simple_board(member=True)
        for i in range(4):
            self.fake.add_card("b1", f"c{i}", f"card {i}", due_ms=in_days(5), notifyees=[U1], remind="7;3")
        s = self.go()
        self.assertEqual((s.sent, s.capped), (2, 2))

    def test_message_placeholder_remind_before_is_the_stage(self):
        custom = Path(self.tmp.name) / "m.txt"
        custom.write_text("stage $remind_before, left $days_left", encoding="utf-8")
        self.template = render.load_template(custom)
        self.simple_board(member=True)
        self.fake.add_card("b1", "c1", "A", due_ms=in_days(2), notifyees=[U1], remind="7;3;1")
        self.go()
        self.assertEqual(self.fake.posts[0]["message"], "stage 3, left 2")

    def test_dry_run_shows_the_stage(self):
        self.simple_board(member=True)
        self.fake.add_card("b1", "c1", "A", due_ms=in_days(2), notifyees=[U1], remind="7;3;1")
        with self.assertLogs("reminder", level="INFO") as logs:
            self.go(dry_run=True, store=Store(self.state_path, readonly=True))
        self.assertTrue(any("3-days-before" in m for m in logs.output), logs.output)
        self.assertEqual(self.fake.posts, [])


class NotifyeesAndSettings(Base):
    """The Notifyees property and the property-name settings, as the runner really uses them."""

    def custom_template(self, text: str) -> None:
        path = Path(self.tmp.name) / "m.txt"
        path.write_text(text, encoding="utf-8")
        self.template = render.load_template(path)

    def test_a_single_person_property_works_as_notifyees(self):
        props = standard_props()
        props[1]["type"] = "person"
        self.fake.add_board("b1", "Posts", member=True, props=props)
        self.fake.add_card("b1", "c1", "A", due_ms=in_days(5), notifyees=U2, remind="7")   # one id, not a list
        s = self.go()
        self.assertEqual((s.sent, self.posted_to()), (1, [U2]))

    def test_property_names_come_from_the_settings(self):
        props = [{"id": "p_due", "name": "Data limite", "type": "date", "options": []},
                 {"id": "p_who", "name": "Avisar", "type": "multiPerson", "options": []},
                 {"id": "p_rem", "name": "Avisar antes", "type": "text", "options": []}]
        self.fake.add_board("b1", "Posts", member=True, props=props)
        self.fake.add_card("b1", "c1", "A", due_ms=in_days(5), notifyees=[U1], remind="7")
        self.assertEqual(self.go().sent, 0)                       # default names: the board is not set up for us
        self.cfg = self.make_cfg(REMINDER_PROP_DUE="Data limite", REMINDER_PROP_NOTIFYEES="Avisar",
                                 REMINDER_PROP_REMIND="Avisar antes")
        self.assertEqual(self.go().sent, 1)

    def test_property_names_match_ignoring_case_and_surrounding_spaces(self):
        props = [{"id": "p_due", "name": "  due DATE ", "type": "date", "options": []},
                 {"id": "p_who", "name": "NOTIFYEES", "type": "multiPerson", "options": []},
                 {"id": "p_rem", "name": "remind before  ", "type": "text", "options": []}]
        self.fake.add_board("b1", "Posts", member=True, props=props)
        self.fake.add_card("b1", "c1", "A", due_ms=in_days(5), notifyees=[U1], remind="7")
        self.assertEqual(self.go().sent, 1)

    def test_a_person_listed_twice_is_told_once(self):
        self.simple_board(member=True)
        self.fake.add_card("b1", "c1", "A", due_ms=in_days(5), notifyees=[U1, U1, U2, U1], remind="7")
        s = self.go()
        self.assertEqual((s.sent, self.posted_to()), (2, sorted([U1, U2])))

    def test_co_notifyees_are_the_other_notifyees_never_the_assignees(self):
        props = standard_props() + [{"id": "p_ass", "name": "Assignees", "type": "multiPerson", "options": []}]
        self.fake.add_board("b1", "Posts", member=True, props=props)
        card = self.fake.add_card("b1", "c1", "A", due_ms=in_days(5), notifyees=[U1, U2], remind="7")
        card["properties"]["p_ass"] = [U3]
        self.custom_template("$responsible_name -> [$co_responsibles]")
        self.go()
        texts = {p["user_id"]: p["message"] for p in self.fake.posts}
        self.assertEqual(texts[U1], "Alice -> [Bob Builder]")       # the other notifyee, not carol (an assignee)
        self.assertEqual(texts[U2], "Bob Builder -> [Alice]")

    def test_a_notifyees_value_of_only_invalid_ids_counts_as_nobody(self):
        self.simple_board(member=True)
        self.fake.add_card("b1", "c1", "A", due_ms=in_days(5), notifyees=["me", "../x", "short"], remind="7")
        with self.assertLogs("reminder", level="WARNING"):
            s = self.go()
        self.assertEqual((s.sent, s.no_notifyees), (0, 1))

    def test_the_first_message_on_the_due_day_says_it_is_today(self):
        self.simple_board(member=True)
        self.fake.add_card("b1", "c1", "A", due_ms=in_days(0), notifyees=[U1], remind="7;3;1")
        self.assertEqual(self.go().sent, 1)
        self.assertIn("é hoje", self.fake.posts[0]["message"])

    def test_a_number_above_the_maximum_is_reported_and_the_rest_still_works(self):
        self.simple_board(member=True)
        self.fake.add_card("b1", "c1", "A", due_ms=in_days(5), notifyees=[U1], remind="7;5000")
        with self.assertLogs("reminder", level="WARNING") as logs:
            s = self.go()
        self.assertEqual(s.sent, 1)
        self.assertTrue(any("5000" in m for m in logs.output), logs.output)

    def test_no_warnings_for_cards_nobody_will_be_reminded_about_anyway(self):
        """A typo on a finished card, a template or a card without a Due Date is not worth a log line."""
        self.simple_board(member=True)
        self.fake.add_card("b1", "past", "Past", due_ms=in_days(-3), notifyees=[U1], remind="tres")
        self.fake.add_card("b1", "tpl", "Template", due_ms=in_days(5), notifyees=[U1], remind="tres", isTemplate=True)
        self.fake.add_card("b1", "nodue", "No due", notifyees=[U1], remind="tres")
        self.fake.add_card("b1", "gone", "Deleted", due_ms=in_days(5), notifyees=[U1], remind="tres", deleteAt=1)
        with self.assertNoLogs("reminder", level="WARNING"):
            s = self.go()
        self.assertEqual((s.sent, s.no_notifyees), (0, 0))

    def test_a_state_file_from_the_first_version_still_prevents_a_resend(self):
        """Upgrading the bot must never repeat what the old version already sent."""
        self.simple_board(member=True)
        due = TODAY + timedelta(days=5)
        self.fake.add_card("b1", "c1", "A", due_ms=in_days(5), notifyees=[U1], remind="7;3;1")
        db = sqlite3.connect(self.state_path)
        db.execute("""CREATE TABLE reminders (card_id TEXT NOT NULL, user_id TEXT NOT NULL,
                      due_date TEXT NOT NULL, status TEXT NOT NULL, updated_at REAL NOT NULL,
                      PRIMARY KEY (card_id, user_id, due_date))""")
        db.execute("INSERT INTO reminders VALUES (?, ?, ?, 'sent', ?)", ("c1", U1, due.isoformat(), self.now_ts))
        db.commit()
        db.close()
        s = self.go()
        self.assertEqual((s.sent, s.already_sent, len(self.fake.posts)), (0, 1, 0))
        # ... and the later reminders of that date stay silent too (the old version had sent its one message)
        for offset in (2, 4):
            self.assertEqual(self.go(today=TODAY + timedelta(days=offset)).sent, 0)


if __name__ == "__main__":
    unittest.main()
