"""A soak test under randomized failure: weeks of daily passes while the server misbehaves.

Hand-written fault tests prove each protection works once. This proves they keep working TOGETHER,
over many random combinations nobody would think to write, with the daemon's real loop and a fake
clock. The failures are the nasty ones:

  * the message is created on the server but the answer never arrives (the bot cannot know it was sent);
  * the connection drops, the server answers 500/502/503, reads fail;
  * the process dies (SIGKILL) just before or just after sending.

The faults are active during the first fifteen minutes of each daily pass; the retries (every ten
minutes, four attempts) then find a healthy server, as an outage that ends would. What must hold:

  1. nobody ever receives the same reminder twice;
  2. everybody receives exactly the reminders the rule says, no more and no fewer;
  3. nothing is left half-done in the state file;
  4. nothing is sent after a due date.

The expectation comes from an independent model (test_model.Model) that shares no code with the bot.
"""
import logging
import random
import unittest
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from unittest import mock

from remindabot import scheduler
from remindabot.driver import Driver

from . import test_integration as ti
from .support.fakeserver import Fault
from .test_model import Model

LISBON, TODAY, U1, U2, U3 = ti.LISBON, ti.TODAY, ti.U1, ti.U2, ti.U3
DAYS = 16
POSTS = lambda method, path: method == "POST" and path.endswith("/api/v4/posts")        # noqa: E731


class World(ti.Base):
    """The shared world of the soak tests: a board of cards, a fake clock, faults, and the model's expectation."""
    def setUp(self):
        super().setUp()
        logger = logging.getLogger("reminder")          # hundreds of expected warnings
        logger.disabled = True
        self.addCleanup(setattr, logger, "disabled", False)
        template = Path(self.tmp.name) / "soak.txt"
        template.write_text("$card_title;$remind_before;$days_left", encoding="utf-8")
        self.cfg = self.make_cfg(REMINDER_TEMPLATE_FILE=str(template), REMINDER_MAX_MESSAGES_PER_DAY="1000",
                                 REMINDER_MAX_MESSAGES_PER_USER_PER_DAY="100")
        self.clock = ti.FakeClock(datetime(2026, 10, 13, 8, 0, tzinfo=LISBON))
        self.end = datetime(2026, 10, 13 + DAYS, 8, 0, tzinfo=LISBON)

    # -- the world and what must come out of it ---------------------------------------------------------------

    def world(self, rng: random.Random):
        self.simple_board("b1", member=True)
        self.cards = []
        for n in range(8):
            people = rng.sample([U1, U2, U3], rng.randrange(1, 4))
            numbers = sorted({rng.choice([1, 2, 3, 5, 7, 10]) for _ in range(rng.randrange(1, 4))}, reverse=True)
            due = rng.randrange(1, DAYS - 1)
            self.fake.add_card("b1", f"c{n}", f"Card {n}", due_ms=ti.in_days(due), notifyees=list(people),
                               remind=";".join(map(str, numbers)))
            self.cards.append((people, numbers, due))

    def expected(self) -> list:
        out = []
        for n, (people, numbers, due) in enumerate(self.cards):
            model = Model()
            for day in range(0, DAYS + 1):
                out += [(person, f"Card {n}", stage, left) for person, stage, left in model.pass_for(people, numbers, due, day)]
        return sorted(out)

    def delivered(self) -> list:
        out = []
        for post in self.fake.posts:
            title, stage, left = post["message"].split(";")
            out.append((post["user_id"], title, int(stage), int(left)))
        return sorted(out)

    # -- the failures -------------------------------------------------------------------------------------------

    def in_the_faulty_window(self) -> bool:
        return dtime(9, 0) <= self.clock.now().time() < dtime(9, 15)

    def misbehave(self, rng: random.Random):
        def chance(p, where):
            return lambda method, path: self.in_the_faulty_window() and where(method, path) and rng.random() < p
        self.fake.faults[:] = [
            Fault(chance(0.25, POSTS), "commit_then_drop"),                 # created, answer lost
            Fault(chance(0.15, POSTS), "drop"),                             # connection lost before anything
            Fault(chance(0.15, POSTS), "status", status=500),
            Fault(chance(0.10, lambda m, p: p.endswith("/channels/direct")), "status", status=502),
            Fault(chance(0.06, lambda m, p: m == "GET"), "status", status=503),
            Fault(chance(0.03, lambda m, p: m == "GET"), "drop"),
        ]

    def run_days(self, rng: random.Random, crashes: bool) -> int:
        """Run the daemon's loop to the end of the period; a "crash" kills the process and restarts it."""
        real_send = Driver.send_dm
        kills = {"n": 0}

        def maybe_dying_send(driver, user_id, text):
            die_before = crashes and self.in_the_faulty_window() and rng.random() < 0.3
            die_after = crashes and self.in_the_faulty_window() and rng.random() < 0.3
            if die_before:
                kills["n"] += 1
                raise SystemExit("killed before the message left")
            result = real_send(driver, user_id, text)
            if die_after:
                kills["n"] += 1
                raise SystemExit("killed after the message left")
            return result

        with mock.patch.object(Driver, "send_dm", maybe_dying_send):
            while self.clock.utc < self.end:
                left = int((self.end - self.clock.now()).total_seconds() // 60)
                process_store = self.store(clock=lambda: self.clock.utc.timestamp())      # a new process: new connection
                try:
                    scheduler.serve(self.cfg, self.mm, self.boards, process_store, now_fn=self.clock.now,
                                    sleep_fn=self.clock.sleep, max_iterations=left)
                except SystemExit:
                    self.clock.sleep(60)                                     # the container restarts a minute later
                finally:
                    process_store.close()
        return kills["n"]

    def check_the_invariants(self, label: str):
        delivered = self.delivered()
        pairs = [(post["user_id"], post["message"]) for post in self.fake.posts]
        self.assertEqual(len(pairs), len(set(pairs)), f"{label}: somebody got the same reminder twice")
        self.assertEqual(delivered, self.expected(), f"{label}: not exactly the reminders the rule says")
        for _, _, _, left in delivered:
            self.assertGreaterEqual(left, 0, f"{label}: a reminder after the due date")
        store = self.store(readonly=True)
        statuses = {row[0] for row in store.db.execute("SELECT status FROM reminders")}
        self.assertEqual(statuses - {"sent"}, set(), f"{label}: something was left half-done")
        self.assertIsNotNone(store.last_run())



class Soak(World):
    # -- the soaks ----------------------------------------------------------------------------------------------

    def test_weeks_of_passes_under_random_server_failures(self):
        for seed in (1, 2, 3, 4, 5):
            with self.subTest(seed=seed):
                self.setUp()
                rng = random.Random(seed)
                self.world(rng)
                self.misbehave(rng)
                self.run_days(rng, crashes=False)
                hits = sum(fault.hits for fault in self.fake.faults)
                self.assertGreater(hits, 20, "the soak did not actually misbehave")
                self.check_the_invariants(f"seed {seed}")

    def test_weeks_of_passes_with_the_process_killed_at_random(self):
        for seed in (11, 12, 13, 14):
            with self.subTest(seed=seed):
                self.setUp()
                rng = random.Random(seed)
                self.world(rng)
                self.misbehave(rng)
                kills = self.run_days(rng, crashes=True)
                self.assertGreaterEqual(kills, 5, "the soak did not actually kill the process")
                self.check_the_invariants(f"seed {seed}, {kills} kills")


class OrphanVerification(ti.Base):
    """The one decision that must never go wrong: a claim left by an earlier attempt that may have been
    delivered. Deterministic cases, each with a failure the soak above only meets by chance."""

    def setUp(self):
        super().setUp()
        self.simple_board("b1", member=True)
        self.fake.add_card("b1", "c1", "Workshop", due_ms=ti.in_days(5), notifyees=[U1], remind="7;3;1")
        self.is_posts = POSTS
        self.is_history = lambda method, path: method == "GET" and "/posts" in path and "/channels/" in path

    def first_attempt_loses_its_answer(self, kind: str):
        self.fake.faults[:] = [Fault(self.is_posts, kind, times=1)]
        summary = self.go(store=self.store())
        self.assertEqual(summary.failed, 1)
        self.fake.faults.clear()
        self.now_ts += 3600                                              # the claim is now orphaned

    def test_when_it_cannot_check_it_does_not_send_and_when_it_can_it_does_not_duplicate(self):
        self.first_attempt_loses_its_answer("commit_then_drop")          # delivered, but the bot cannot know
        self.assertEqual(len(self.fake.posts), 1)
        self.fake.faults[:] = [Fault(self.is_history, "status", status=503)]
        for _ in range(3):                                               # the check keeps failing: it keeps waiting
            summary = self.go(store=self.store())
            self.assertEqual(summary.sent, 0)
            self.assertEqual(len(self.fake.posts), 1, "it sent although it could not tell whether it was delivered")
            self.now_ts += 3600
        self.fake.faults.clear()
        summary = self.go(store=self.store())
        self.assertEqual((summary.sent, summary.already_sent), (0, 1))
        self.assertEqual(len(self.fake.posts), 1)
        self.assertEqual(self.go(store=self.store()).sent, 0)

    def test_a_message_that_never_arrived_is_sent_once_when_the_check_says_so(self):
        self.first_attempt_loses_its_answer("drop")                      # NOT delivered
        self.assertEqual(self.fake.posts, [])
        self.assertEqual(self.go(store=self.store()).sent, 1)
        self.assertEqual(self.go(store=self.store()).sent, 0)
        self.assertEqual(len(self.fake.posts), 1)

    def test_a_check_that_answers_with_rubbish_is_a_failure_not_a_yes_or_a_no(self):
        self.first_attempt_loses_its_answer("commit_then_drop")
        self.fake.faults[:] = [Fault(self.is_history, "html")]
        summary = self.go(store=self.store())
        self.assertEqual((summary.sent, summary.failed), (0, 1))
        self.assertEqual(len(self.fake.posts), 1)


class ClockJumps(World):
    """NTP puts a clock back; a restored virtual machine wakes up months late. Neither may duplicate a message."""

    def test_the_clock_going_back_or_far_forward_never_duplicates_or_crashes(self):
        rng = random.Random(5)
        self.world(rng)
        store = self.store(clock=lambda: self.clock.utc.timestamp())

        def run(days: int):
            scheduler.serve(self.cfg, self.mm, self.boards, store, now_fn=self.clock.now, sleep_fn=self.clock.sleep,
                            max_iterations=days * 24 * 60)
        run(4)
        sent = len(self.fake.posts)
        self.assertGreater(sent, 0)
        self.clock.utc -= timedelta(hours=30)                      # the clock is put back: a day is lived again
        run(1)
        self.clock.utc += timedelta(days=400)                      # and then a VM wakes up more than a year later
        run(3)
        pairs = [(post["user_id"], post["message"]) for post in self.fake.posts]
        self.assertEqual(len(pairs), len(set(pairs)), "a message was sent twice after the clock moved")
        for _, _, _, left in self.delivered():
            self.assertGreaterEqual(left, 0, "a reminder after the due date")
        self.assertEqual(len(self.fake.posts), sent, "replaying days the bot already lived must send nothing new")
        store.close()


if __name__ == "__main__":
    unittest.main()
