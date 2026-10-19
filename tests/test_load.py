"""Load and growth: how the bot behaves when the workspace is big, when it is not, and when it is long-lived.

The bot is a client of someone else's server. It must cost that server a predictable, small amount of
work, however many cards there are, and it must not grow without bound itself:

  * the number of requests grows with boards and people, not with boards x people or cards x people;
  * a mass of due reminders is held back by the daily caps, however large;
  * an outage is answered at a gentle, fixed pace, with backoff, never a storm of retries;
  * the state file stays small after a long time of use.
"""
import collections
import logging
import re
import time
import unittest
from datetime import datetime, timedelta, timezone

from remindabot import scheduler
from remindabot.driver import ApiError, Client
from remindabot.store import Store

from . import test_integration as ti
from .support.fakeserver import Fault, uid

LISBON, TODAY, U1, U2, U3 = ti.LISBON, ti.TODAY, ti.U1, ti.U2, ti.U3


class Quiet(ti.Base):
    def setUp(self):
        super().setUp()
        logger = logging.getLogger("reminder")
        logger.disabled = True
        self.addCleanup(setattr, logger, "disabled", False)


class Scale(Quiet):
    BOARDS, CARDS_PER_BOARD, USERS = 30, 40, 100

    def test_a_big_workspace_costs_a_linear_number_of_requests(self):
        people = [uid(f"p{n}") for n in range(self.USERS)]
        for n, person in enumerate(people):
            self.fake.add_user(person, f"person{n}")
        due_soon = 0
        for b in range(self.BOARDS):
            self.simple_board(f"b{b}", member=True)
            for c in range(self.CARDS_PER_BOARD):
                soon = (b * self.CARDS_PER_BOARD + c) % 8 == 0               # one card in eight is inside its window
                due_soon += soon
                self.fake.add_card(f"b{b}", f"b{b}c{c}", f"Card {b}-{c}",
                                   due_ms=ti.in_days(3 if soon else 200),
                                   notifyees=[people[(b + c + k) % self.USERS] for k in range(3)], remind="7;3;1")
        self.cfg = self.make_cfg(REMINDER_MAX_MESSAGES_PER_DAY="5000", REMINDER_MAX_MESSAGES_PER_USER_PER_DAY="500")
        start = len(self.fake.requests)
        started = time.time()
        summary = self.go()
        elapsed = time.time() - started
        requests = self.fake.requests[start:]

        messages = due_soon * 3
        self.assertEqual(summary.sent, messages)
        self.assertEqual(len(self.fake.posts), messages)
        self.assertLess(elapsed, 60, "a pass over 1,200 cards should take seconds")

        gets = collections.Counter(path.split("?")[0] for method, path in requests if method == "GET")
        user_lookups = {p: n for p, n in gets.items() if re.fullmatch(r"/api/v4/users/[\w-]+", p)}
        self.assertLessEqual(max(user_lookups.values(), default=0), 1, "a person was looked up more than once")
        for prefix in ("/api/v4/teams/team1/members/", "/plugins/focalboard/api/v2/boards/"):
            repeated = {p: n for p, n in gets.items() if p.startswith(prefix) and n > 3}
            self.assertEqual(repeated, {}, "the same thing was asked for again and again")
        budget = 6 * self.BOARDS + 4 * self.USERS + 3 * messages + 50
        self.assertLessEqual(len(requests), budget, f"{len(requests)} requests for {self.BOARDS} boards, "
                                                    f"{self.USERS} people and {messages} messages")

    def test_a_mass_of_due_reminders_is_held_back_by_the_caps(self):
        people = [U1, U2, U3, uid("p4"), uid("p5")]
        self.fake.add_user(people[3], "dave")
        self.fake.add_user(people[4], "erin")
        self.simple_board("b1", member=True)
        for n in range(500):
            self.fake.add_card("b1", f"c{n}", f"Card {n}", due_ms=ti.in_days(1), notifyees=people, remind="1")
        summary = self.go()                         # the default caps: 100 a day, 20 a person
        per_person = collections.Counter(p["user_id"] for p in self.fake.posts)
        self.assertLessEqual(len(self.fake.posts), 100)
        self.assertLessEqual(max(per_person.values()), 20)
        self.assertGreater(summary.capped, 0)
        before = len(self.fake.posts)
        self.go()                                    # a second pass the same day sends nothing more
        self.assertEqual(len(self.fake.posts), before)


class Outage(Quiet):
    def test_a_long_outage_is_met_with_one_gentle_retry_every_ten_minutes(self):
        self.simple_board("b1", member=True)
        self.fake.add_card("b1", "c1", "Workshop", due_ms=ti.in_days(5), notifyees=[U1], remind="7;3;1")
        clock = ti.FakeClock(datetime(2026, 10, 13, 8, 0, tzinfo=LISBON))
        self.fake.faults.append(Fault(lambda m, p: True, "status", status=500))
        seen_at: list[datetime] = []
        original = self.fake._dispatch

        def spy(handler, method, body):
            seen_at.append(clock.now())
            return original(handler, method, body)
        self.fake._dispatch = spy
        store = self.store(clock=lambda: clock.utc.timestamp())
        self.addCleanup(store.close)
        scheduler.serve(self.cfg, self.mm, self.boards, store, now_fn=clock.now, sleep_fn=clock.sleep,
                        max_iterations=3 * 24 * 60)
        moments = sorted(set(seen_at))                                 # one moment = one pass (retries share it)
        gaps = [(b - a).total_seconds() for a, b in zip(moments, moments[1:])]
        self.assertGreater(len(moments), 100, "it should keep trying, patiently")
        self.assertGreaterEqual(min(gaps), 600, "it must never retry faster than every ten minutes")
        per_pass = collections.Counter(seen_at)
        self.assertLessEqual(max(per_pass.values()), 4, "a failing pass must not hammer the server")
        self.assertEqual(self.fake.posts, [])
        self.assertLessEqual(len(seen_at), 3 * 24 * 6 * 4 + 20, "three days of outage cost too many requests")

    def test_rate_limiting_is_answered_with_backoff_then_a_clean_failure(self):
        self.fake.faults.append(Fault(lambda m, p: True, "status", status=429))
        pauses: list[float] = []
        client = Client(self.url, ti.BOT_TOKEN, sleep=pauses.append, retries=2)
        with self.assertRaises(ApiError) as caught:
            client.request("GET", "/api/v4/users/me")
        self.assertEqual(caught.exception.status, 429)
        self.assertEqual(pauses, [1, 2], "exponential backoff")
        self.assertEqual(len(self.fake.requests), 3, "one try and two retries, no more")

    def test_a_message_is_never_retried_inside_one_pass_whatever_the_answer(self):
        for kind, extra in (("status", {"status": 429}), ("status", {"status": 500}), ("drop", {}), ("html", {})):
            with self.subTest(fault=kind, **extra):
                self.fake.requests.clear()
                self.fake.faults[:] = [Fault(lambda m, p: m == "POST" and p.endswith("/posts"), kind, **extra)]
                client = Client(self.url, ti.BOT_TOKEN, sleep=lambda s: None, retries=5)
                with self.assertRaises(ApiError):
                    client.request("POST", "/api/v4/posts", {"channel_id": "x", "message": "m"}, idempotent=False)
                self.assertEqual(sum(1 for m, p in self.fake.requests if m == "POST"), 1)


class Growth(unittest.TestCase):
    def test_the_state_file_stays_small_after_a_long_time_of_use(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            today = datetime.now(timezone.utc).date()          # prune() counts days by SQLite's UTC clock
            clock = {"now": 0.0}
            store = Store(Path(tmp) / "state.sqlite", clock=lambda: clock["now"])
            store.db.execute("PRAGMA synchronous=OFF")                  # the test's speed, not the daemon's setting
            for back in range(150, -1, -1):                            # 150 days of daily use, 30 messages a day
                day = today - timedelta(days=back)
                clock["now"] = datetime(day.year, day.month, day.day, 9, 0, tzinfo=LISBON).timestamp()
                for n in range(30):
                    key = (f"card{back}-{n}", f"user{n % 7}", day.isoformat(), 3)
                    store.claim(*key)
                    store.confirm(*key)
                    store.record_history(card_id=key[0], card_title="Cartão", board_title="Posts", user_id=key[1],
                                         username="someone", due_date=key[2], days=3)
                store.set_last_run(day.isoformat())
                store.prune(keep_days=30)                               # what the daemon does after every pass
            reminders = store.db.execute("SELECT count(*) FROM reminders").fetchone()[0]
            history = store.db.execute("SELECT count(*) FROM history").fetchone()[0]
            self.assertLessEqual(reminders, 33 * 30, "old reminders are never forgotten")       # (a day or two of slack:
            self.assertLessEqual(history, 33 * 30, "old history is never forgotten")             # UTC against Lisbon)
            self.assertGreaterEqual(reminders, 30, "recent reminders must be kept: they prevent duplicates")
            self.assertTrue(store.sent_stages("card0-0", "user0", today.isoformat()))
            store.close()
            self.assertLess((Path(tmp) / "state.sqlite").stat().st_size, 2 * 1024 * 1024)


if __name__ == "__main__":
    unittest.main()
