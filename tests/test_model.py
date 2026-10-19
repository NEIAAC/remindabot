"""Model-based and fuzz tests of the WHOLE pass: runner + client + store + fake Mattermost.

The unit tests check the rule in isolation. These check that the pieces still agree when they are
put together, over many random situations that nobody would think to write by hand: lists, due
dates, days the bot was switched off, list edits, due-date changes, dry runs in between.

The "model" below is deliberately dumb and shares no code with `app/logic.py` or `app/store.py`:
if both it and the application say the same thing for hundreds of random cases, that is
independent evidence, not a test of the code against itself.
"""
import json
import logging
import random
import unittest
from datetime import timedelta
from pathlib import Path
from typing import Optional

from . import test_integration as ti
from . import test_robustness as rb
from remindabot import render
from remindabot.store import Store
from .support.fakeserver import standard_props

TODAY, U1, U2, U3 = ti.TODAY, ti.U1, ti.U2, ti.U3
MAX_DAYS = 3660
SCENARIOS = 45


class Model:
    """What a person must have been told, according to the rules in the README (How a card is handled), written plainly."""

    def __init__(self):
        self.sent = {}        # (person, due offset) -> set of stages already announced

    @staticmethod
    def stage(numbers, due: int, today: int) -> Optional[int]:
        """The smallest usable listed number that is still >= the days left, or None."""
        left = due - today
        if left < 0:
            return None
        usable = [n for n in numbers if 1 <= n <= MAX_DAYS]
        reached = [n for n in usable if n >= left]
        return min(reached) if reached else None

    def pass_for(self, people, numbers, due: int, today: int):
        """One real daily pass: returns the (person, stage, days left) messages it must produce."""
        stage = self.stage(numbers, due, today)
        out = []
        if stage is None:
            return out
        for person in people:
            done = self.sent.setdefault((person, due), set())
            if not any(s <= stage for s in done):      # a smaller (or equal) stage was already announced
                done.add(stage)
                out.append((person, stage, due - today))
        return out


def written(numbers, rng: random.Random) -> str:
    """How a person might type the list: random spacing, an occasional trailing ';'."""
    parts = [" " * rng.randrange(0, 3) + str(n) + " " * rng.randrange(0, 3) for n in numbers]
    return ";".join(parts) + (";" if rng.random() < 0.15 else "")


class Quiet(ti.Base):
    """The random cases log hundreds of expected warnings; keep the test output readable."""

    def setUp(self):
        super().setUp()
        logger = logging.getLogger("reminder")
        logger.disabled = True
        self.addCleanup(setattr, logger, "disabled", False)


class WholePass(Quiet):
    def setUp(self):
        super().setUp()
        template = Path(self.tmp.name) / "model.txt"
        template.write_text("stage=$remind_before left=$days_left", encoding="utf-8")
        self.template = render.load_template(template)

    def delivered(self):
        """Every message so far as a sorted list of (person, stage, days left)."""
        out = []
        for post in self.fake.posts:
            stage, left = (int(part.split("=")[1]) for part in post["message"].split())
            out.append((post["user_id"], stage, left))
        return sorted(out)

    def one_scenario(self, seed: int):
        rng = random.Random(seed)
        self.fake.posts.clear()
        self.state_path.unlink(missing_ok=True)
        self.fake.cards["b1"].clear()

        people = rng.sample([U1, U2, U3], rng.randrange(1, 4))
        pool = [-1, 0] + list(range(1, 15)) + [3660, 5000]
        numbers = [rng.choice(pool) for _ in range(rng.randrange(1, 5))]
        due = rng.randrange(0, 17)
        card = self.fake.add_card("b1", "c1", "A", due_ms=ti.in_days(due), notifyees=list(people),
                                  remind=written(numbers, rng))
        model, expected = Model(), []

        for day in range(0, due + 2):
            if rng.random() < 0.35:                       # the bot is off on some days
                continue
            if rng.random() < 0.12:                       # somebody edits the list
                numbers = [rng.choice(pool) for _ in range(rng.randrange(1, 5))]
                card["properties"]["p_rem"] = written(numbers, rng)
            if rng.random() < 0.08:                       # ... or moves the Due Date
                due = max(day, due + rng.randrange(-2, 6))
                card["properties"]["p_due"] = '{"from": %d}' % ti.in_days(due)
            if rng.random() < 0.25:                       # a dry run first must change nothing at all
                before = len(self.fake.posts)
                dry_store = Store(self.state_path, readonly=True)
                try:
                    self.go(today=TODAY + timedelta(days=day), dry_run=True, store=dry_store)
                finally:
                    dry_store.close()
                self.assertEqual(len(self.fake.posts), before, (seed, day))
            self.go(today=TODAY + timedelta(days=day))
            expected += model.pass_for(people, numbers, due, day)
            self.assertEqual(self.delivered(), sorted(expected), f"seed {seed}, day {day}, list {numbers}, due {due}")
            # the same day again is always a no-op
            before = len(self.fake.posts)
            self.assertEqual(self.go(today=TODAY + timedelta(days=day)).sent, 0)
            self.assertEqual(len(self.fake.posts), before, (seed, day))

    def test_random_scenarios_match_the_model(self):
        self.fake.add_board("b1", "Posts", member=True)
        for seed in range(SCENARIOS):
            with self.subTest(seed=seed):
                self.one_scenario(seed)

    def test_nobody_is_ever_messaged_after_the_due_date_or_twice_for_a_stage(self):
        """The two promises that matter most, checked on their own across all of those scenarios."""
        self.fake.add_board("b1", "Posts", member=True)
        for seed in range(1000, 1000 + SCENARIOS):
            rng = random.Random(seed)
            self.fake.posts.clear()
            self.state_path.unlink(missing_ok=True)
            self.fake.cards["b1"].clear()
            numbers = [rng.randrange(1, 10) for _ in range(rng.randrange(1, 5))]
            due = rng.randrange(0, 12)
            self.fake.add_card("b1", "c1", "A", due_ms=ti.in_days(due), notifyees=[U1, U2], remind=";".join(map(str, numbers)))
            for day in range(0, due + 4):
                self.go(today=TODAY + timedelta(days=day))
            delivered = self.delivered()
            self.assertTrue(all(left >= 0 for _, _, left in delivered), (seed, delivered))
            self.assertEqual(len(delivered), len(set(delivered)), (seed, delivered))
            self.assertLessEqual(len(delivered), 2 * len(set(numbers)), (seed, numbers, delivered))


class Planner(Quiet):
    def test_plan_predicts_exactly_what_the_runner_then_sends(self):
        """`/remind_next` shows `logic.plan`; if it ever disagreed with the real pass it would lie."""
        from remindabot.boards import Card
        from remindabot.logic import plan
        self.fake.add_board("b1", "Posts", member=True)
        for seed in range(2000, 2000 + SCENARIOS):
            rng = random.Random(seed)
            self.fake.posts.clear()
            self.state_path.unlink(missing_ok=True)
            self.fake.cards["b1"].clear()
            numbers = [rng.choice([-1, 0] + list(range(1, 12))) for _ in range(rng.randrange(1, 5))]
            due = rng.randrange(0, 14)
            start = rng.randrange(0, due + 1)               # the card is first seen on this day
            self.fake.add_card("b1", "c1", "A", due_ms=ti.in_days(due), notifyees=[U1],
                               remind=";".join(map(str, numbers)))
            card = Card("c1", "A", TODAY + timedelta(days=due), tuple(numbers), (U1,), False, False)
            expected = [(d - TODAY).days for d, _ in plan(card, TODAY + timedelta(days=start), set(), due + 2)]
            actual = []
            for day in range(start, due + 3):
                before = len(self.fake.posts)
                self.go(today=TODAY + timedelta(days=day))
                if len(self.fake.posts) > before:
                    actual.append(day)
            self.assertEqual(actual, expected, f"seed {seed}: list {numbers}, due {due}, first seen {start}")


class HostileData(Quiet):
    """Whatever Boards data looks like, a pass finishes, stays inside its promises, and is repeatable."""

    def test_random_garbage_on_cards_and_boards_never_breaks_a_pass(self):
        rng = random.Random(20261015)
        for i in range(3):
            props = standard_props()
            if i == 1:        # a board with garbage property definitions mixed in
                props += [rb.random_value(rng) for _ in range(5)]
            self.fake.add_board(f"b{i}", f"Board {i}", member=True, props=props)
            for n in range(25):
                card_props = rng.choice([
                    rb.random_value(rng),
                    {"p_due": rb.random_value(rng), "p_who": rb.random_value(rng), "p_rem": rb.random_value(rng)},
                    {"p_due": json.dumps({"from": ti.in_days(rng.randrange(0, 6))}),
                     "p_who": rng.choice([[U1], [U2, U3], rb.random_value(rng)]),
                     "p_rem": rng.choice(["7;3;1", "3", rb.random_value(rng), "9" * 50, ";;;"])},
                ])
                self.fake.cards[f"b{i}"].append({
                    "id": f"c{i}-{n}", "boardId": f"b{i}", "title": rb.random_value(rng),
                    "isTemplate": rng.random() < 0.1, "deleteAt": 0, "properties": card_props})
        first = self.go()
        known = {U1, U2, U3}
        self.assertTrue(all(p["user_id"] in known for p in self.fake.posts))
        self.assertLessEqual(len(self.fake.posts), self.cfg.max_per_day)
        sent_after_first = len(self.fake.posts)
        second = self.go()                                # nothing may be repeated, whatever the data was
        self.assertEqual((second.sent, len(self.fake.posts)), (0, sent_after_first))
        self.assertEqual(first.sent, sent_after_first)


if __name__ == "__main__":
    unittest.main()
