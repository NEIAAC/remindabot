"""Fuzz / property tests: whatever the Boards data contains, the pure logic must never raise,
and the reminder rule must agree with a brute-force oracle."""
import json
import random
import string
import unittest
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from remindabot import render
from remindabot.boards import (Card, Schema, decode_card, decode_date, decode_people, decode_remind_list,
                        describe_schema_problem, find_schema)
from remindabot.config import Config, ConfigError
from remindabot.logic import MAX_REMIND_BEFORE_DAYS, evaluate, ignored_values, usable_days

LISBON = ZoneInfo("Europe/Lisbon")
SEED = 20261013


def random_scalar(rng: random.Random):
    pick = rng.randrange(14)
    if pick == 0:
        return None
    if pick == 1:
        return rng.choice([True, False])
    if pick == 2:
        return rng.randint(-10 ** 3, 10 ** 3)
    if pick == 3:
        return rng.randint(-10 ** 40, 10 ** 40)
    if pick == 4:
        return rng.choice([float("nan"), float("inf"), float("-inf"), 1e308, -1e308, 5e-324, 1e20])
    if pick == 5:
        return rng.uniform(-1e15, 1e15)
    if pick == 6:
        return rng.randint(1_700_000_000_000, 1_900_000_000_000)          # realistic epoch ms
    if pick == 7:
        return rng.randint(1_700_000_000_000, 1_900_000_000_000) * 1000  # absurd (microseconds)
    if pick == 8:
        return "".join(rng.choice(string.printable) for _ in range(rng.randrange(0, 30)))
    if pick == 9:
        return "".join(chr(rng.randrange(0, 0x2FFF)) for _ in range(rng.randrange(0, 20)))
    if pick == 10:
        return rng.choice(["", " ", "0", "-1", "12", "12.0", "1e400", "nan", "inf", "٣", "１２", "12 days"])
    if pick == 11:
        return rng.choice(["Europe/Lisbon", "UTC", "Not/AZone", "../../etc/passwd", "", "Asia/Kolkata"])
    if pick == 12:
        return str(rng.randint(10 ** 12, 2 * 10 ** 12))
    return rng.random()


def random_value(rng: random.Random, depth: int = 0):
    if depth > 2 or rng.random() < 0.6:
        return random_scalar(rng)
    if rng.random() < 0.5:
        return [random_value(rng, depth + 1) for _ in range(rng.randrange(0, 4))]
    return {rng.choice(["from", "to", "includeTime", "timeZone", "x"]): random_value(rng, depth + 1)
            for _ in range(rng.randrange(0, 4))}


class Fuzz(unittest.TestCase):
    N = 20000

    def test_decode_date_never_raises_and_stays_in_range(self):
        rng = random.Random(SEED)
        lo, hi = date(1970, 1, 1), date(2200, 12, 31)
        for _ in range(self.N):
            candidates = [
                random_value(rng),
                json.dumps(random_value(rng)),
                json.dumps({"from": random_value(rng), "includeTime": random_value(rng),
                            "timeZone": random_value(rng)}),
            ]
            for raw in candidates:
                result = decode_date(raw, LISBON)
                if result is not None:
                    self.assertIsInstance(result, date)
                    self.assertTrue(lo <= result <= hi, (raw, result))

    def test_decode_people_never_raises(self):
        rng = random.Random(SEED + 1)
        for _ in range(self.N):
            raw = random_value(rng)
            people = decode_people(raw)
            self.assertIsInstance(people, tuple)
            self.assertTrue(all(isinstance(p, str) and p for p in people))

    def test_decode_remind_list_never_raises(self):
        rng = random.Random(SEED + 8)
        for _ in range(self.N):
            raw = rng.choice([random_value(rng), [random_value(rng) for _ in range(3)],
                              ";".join(str(random_scalar(rng)) for _ in range(4))])
            numbers, unreadable = decode_remind_list(raw)
            self.assertTrue(all(isinstance(n, int) and not isinstance(n, bool) for n in numbers))
            self.assertEqual(len(set(numbers)), len(numbers))
            self.assertTrue(all(isinstance(u, str) for u in unreadable))
            usable = usable_days(numbers)
            self.assertTrue(all(1 <= n <= MAX_REMIND_BEFORE_DAYS for n in usable))
            self.assertEqual(list(usable), sorted(usable, reverse=True))

    def test_decode_remind_list_matches_a_plain_oracle(self):
        """Lists built from known entries: numbers come out in order, junk is reported, nothing else."""
        rng = random.Random(SEED + 9)
        junk = ["tres", "1.5", "7 dias", "1 semana", "7,3", "x", "3d", "--1", "+1"]
        for _ in range(self.N):
            entries = [rng.choice([str(rng.randrange(-5, 40)), rng.choice(junk)]) for _ in range(rng.randrange(0, 6))]
            raw = ";".join(" " * rng.randrange(0, 3) + e + " " * rng.randrange(0, 3) for e in entries)
            def is_number(e):   # ASCII digits with at most one leading minus: the whole rule
                return e.isascii() and (e[1:] if e[:1] == "-" else e).isdigit()
            expected_numbers = list(dict.fromkeys(int(e) for e in entries if is_number(e)))
            expected_junk = list(dict.fromkeys(e for e in entries if not is_number(e)))
            numbers, unreadable = decode_remind_list(raw)
            self.assertEqual(list(numbers), expected_numbers, raw)
            self.assertEqual(list(unreadable), expected_junk[:5], raw)

    def test_decode_card_never_raises_on_hostile_cards(self):
        rng = random.Random(SEED + 2)
        schema = Schema("d", "a", "r")
        for _ in range(self.N):
            card = {"id": "c", "title": random_value(rng), "isTemplate": random_value(rng),
                    "deleteAt": random_value(rng),
                    "properties": rng.choice([random_value(rng), {"d": random_value(rng), "a": random_value(rng),
                                                                  "r": random_value(rng)}])}
            decoded = decode_card(card, schema, LISBON)
            self.assertIsInstance(decoded.title, str)
            self.assertIsInstance(decoded.notifyees, tuple)
            self.assertIsInstance(decoded.remind_before, tuple)
            ignored_values(decoded)

    def test_evaluate_never_raises_even_on_extreme_cards(self):
        rng = random.Random(SEED + 3)
        dues = [None, date.min, date.max, date(1970, 1, 1), date(2026, 10, 25), date(2200, 1, 1)]
        for _ in range(self.N):
            card = Card(id="c", title="t", due=rng.choice(dues),
                        remind_before=tuple(rng.choice([0, -1, 1, 12, 3660, 3661, 10 ** 9, 10 ** 40, -10 ** 40])
                                            for _ in range(rng.randrange(0, 4))),
                        notifyees=rng.choice([(), ("u",)]), is_template=rng.random() < 0.1,
                        deleted=rng.random() < 0.1)
            decision = evaluate(card, rng.choice([date.min, date(2026, 10, 13), date.max]))
            self.assertIsInstance(decision.send, bool)

    def test_evaluate_matches_a_brute_force_oracle(self):
        """Walk the calendar day by day and compare with the rule: on each day exactly the card's
        current stage must be announced once, and it must be the smallest listed number >= days left."""
        rng = random.Random(SEED + 4)
        base = date(2026, 1, 1)
        for _ in range(self.N // 4):
            due = base + timedelta(days=rng.randrange(0, 800))
            written = tuple(rng.choice([0, -1, rng.randrange(1, 40), rng.randrange(1, 400),
                                        MAX_REMIND_BEFORE_DAYS + 1]) for _ in range(rng.randrange(0, 5)))
            notifyees = rng.choice([(), ("u",), ("u", "v")])
            template, deleted = rng.random() < 0.1, rng.random() < 0.1
            card = Card("c", "t", due, written, notifyees, template, deleted)
            valid = sorted({n for n in written if 1 <= n <= MAX_REMIND_BEFORE_DAYS})
            announced = []        # (day, stage): what a daily pass would send, given a state that remembers
            highest_sent = None   # the smallest stage announced so far
            start = due - timedelta(days=(max(valid) if valid else 0) + 2)
            for offset in range(0, (max(valid) if valid else 0) + 5):
                today = start + timedelta(days=offset)
                decision = evaluate(card, today)
                left = (due - today).days
                if template or deleted or not valid or left < 0 or not notifyees or left > max(valid):
                    self.assertFalse(decision.send, (card, today))
                    continue
                expected_stage = min(n for n in valid if n >= left)   # brute force
                self.assertTrue(decision.send, (card, today))
                self.assertEqual(decision.stage, expected_stage, (card, today))
                if highest_sent is None or expected_stage < highest_sent:
                    announced.append((today, expected_stage))
                    highest_sent = expected_stage
            if not (template or deleted or not notifyees) and valid:
                # one message per listed number at most, and never more than the days in the window
                self.assertLessEqual(len(announced), len(valid), (card, announced))
                self.assertEqual([stage for _, stage in announced], sorted({s for _, s in announced}, reverse=True))

    def test_config_from_env_only_ever_raises_config_error(self):
        rng = random.Random(SEED + 5)
        keys = ["MM_URL", "SITE_URL", "BOT_TOKEN", "TZ", "SEND_AT", "AUTO_JOIN", "MAX_MESSAGES_PER_DAY",
                "PROP_DUE", "TEMPLATE_FILE"]
        for _ in range(5000):
            env = {f"REMINDER_{k}": str(random_scalar(rng)) for k in keys if rng.random() < 0.8}
            if rng.random() < 0.5:
                env["TZ"] = str(random_scalar(rng))
            try:
                cfg = Config.from_env(env)
            except ConfigError:
                continue
            self.assertGreaterEqual(cfg.max_per_day, 1)
            self.assertTrue(cfg.site_url.startswith(("http://", "https://")))


def is_escaped(text: str, index: int) -> bool:
    backslashes = 0
    j = index - 1
    while j >= 0 and text[j] == "\\":
        backslashes += 1
        j -= 1
    return backslashes % 2 == 1


class SanitizeProperties(unittest.TestCase):
    SPECIAL = "[]()>*_`#+-!|{}"

    def test_sanitized_text_is_inert_for_any_input(self):
        rng = random.Random(SEED + 6)
        alphabet = string.printable + "áéíóúçãõ\u200b\u2028\u0000\x1b[]()@~#*_`<>|!:/" + "http://" + "www."
        for _ in range(20000):
            text = "".join(rng.choice(alphabet) for _ in range(rng.randrange(0, 80)))
            out = render.sanitize_inline(text, max_len=60)
            self.assertFalse(any(ord(c) < 32 or ord(c) == 127 for c in out), repr(out))
            self.assertNotIn("\n", out)
            for i, ch in enumerate(out):
                if ch in self.SPECIAL:
                    self.assertTrue(is_escaped(out, i), (text, out, i))
                if ch == "@" or ch == "~":
                    self.assertTrue(out[i + 1:i + 2] == "\u200b", (text, out))
            lowered = out.lower()
            self.assertNotIn("://", lowered)
            self.assertNotIn("www.", lowered)
            self.assertLessEqual(len(out), 60 * 4)

    def test_values_containing_dollar_signs_are_not_reinterpreted(self):
        shipped = __import__("pathlib").Path(__file__).resolve().parent.parent / "config" / "message.txt"
        template = render.load_template(shipped)
        values = {name: "$responsible_name ${card_url} $$" for name in render.PLACEHOLDERS}
        message = render.render(template, values)
        self.assertIn("$responsible_name ${card_url} $$", message)


class SchemaChecks(unittest.TestCase):
    def test_duplicate_labels_pick_the_usable_one(self):
        board = {"cardProperties": [
            {"id": "x1", "name": "Due Date", "type": "text"},
            {"id": "x2", "name": "Due Date", "type": "date"},
            {"id": "a", "name": "Notifyees", "type": "multiPerson"},
            {"id": "r", "name": "Remind Before", "type": "text"},
        ]}
        schema = find_schema(board, "Due Date", "Notifyees", "Remind Before")
        self.assertEqual(schema.due_id, "x2")

    def test_schema_lookup_never_raises_on_random_property_lists(self):
        rng = random.Random(SEED + 7)
        for _ in range(5000):
            props = [{"id": random_value(rng), "name": rng.choice(["Due Date", "Notifyees", "Remind Before",
                                                                     random_value(rng)]),
                      "type": random_value(rng)} for _ in range(rng.randrange(0, 6))]
            board = {"cardProperties": props}
            find_schema(board, "Due Date", "Notifyees", "Remind Before")
            describe_schema_problem(board, "Due Date", "Notifyees", "Remind Before")

    def test_garbage_card_properties_do_not_break_schema_lookup(self):
        for board in ({}, {"cardProperties": None}, {"cardProperties": [None, 1, "x", {}, {"id": "i"}]},
                      {"cardProperties": [{"id": 1, "name": None, "type": []}]}):
            self.assertIsNone(find_schema(board, "Due Date", "Notifyees", "Remind Before"))
            self.assertIsNone(describe_schema_problem(board, "Due Date", "Notifyees", "Remind Before"))

    def test_problem_descriptions(self):
        unrelated = {"cardProperties": [{"id": "n", "name": "Notes", "type": "text"}]}
        self.assertIsNone(describe_schema_problem(unrelated, "Due Date", "Notifyees", "Remind Before"))
        # "Due Date" exists in the standard Boards templates: not a sign of intent
        template_like = {"cardProperties": [{"id": "d", "name": "Due Date", "type": "date"},
                                            {"id": "a", "name": "Notifyees", "type": "multiPerson"}]}
        self.assertIsNone(describe_schema_problem(template_like, "Due Date", "Notifyees", "Remind Before"))
        # having the opt-in label is
        partial = {"cardProperties": [{"id": "d", "name": "Due Date", "type": "date"},
                                      {"id": "r", "name": "Remind Before", "type": "text"}]}
        text = describe_schema_problem(partial, "Due Date", "Notifyees", "Remind Before")
        self.assertIn("'Notifyees' is missing", text)
        wrong = {"cardProperties": [{"id": "d", "name": "Due Date", "type": "date"},
                                    {"id": "a", "name": "Notifyees", "type": "multiPerson"},
                                    {"id": "r", "name": "Remind Before", "type": "select"}]}
        self.assertIn("'select'", describe_schema_problem(wrong, "Due Date", "Notifyees", "Remind Before"))


class DateEdgeCases(unittest.TestCase):
    def test_ui_values_from_every_timezone_decode_to_the_picked_day(self):
        """Replays the Boards UI formula for browsers in many timezones, on DST edge days too."""
        for tz_name in ("Europe/Lisbon", "America/Los_Angeles", "Asia/Tokyo", "Asia/Kolkata", "Pacific/Auckland",
                        "Pacific/Kiritimati", "Etc/GMT+12", "Australia/Lord_Howe"):
            tz = ZoneInfo(tz_name)
            for day in (date(2026, 3, 28), date(2026, 3, 29), date(2026, 3, 30), date(2026, 10, 24),
                        date(2026, 10, 25), date(2026, 10, 26), date(2026, 12, 31), date(2027, 1, 1)):
                # the UI: local noon of that day, then minus getTimezoneOffset (minutes behind UTC)
                local_noon = datetime(day.year, day.month, day.day, 12, tzinfo=tz)
                offset_min = -int(local_noon.utcoffset().total_seconds() // 60)
                stored = int(local_noon.timestamp() * 1000) - offset_min * 60 * 1000
                self.assertEqual(decode_date(json.dumps({"from": stored}), LISBON), day, (tz_name, day))

    def test_time_of_day_near_midnight_lisbon(self):
        for hour, minute, expected_day in ((23, 59, 25), (0, 0, 26), (0, 1, 26)):
            instant = datetime(2026, 10, 26 if expected_day == 26 else 25, hour, minute, tzinfo=LISBON)
            raw = json.dumps({"from": int(instant.timestamp() * 1000), "includeTime": True})
            self.assertEqual(decode_date(raw, LISBON), date(2026, 10, expected_day))

    def test_absurd_values_are_none(self):
        for ms in (10 ** 15, 10 ** 30, 2 ** 70, -1, 0, 7_258_118_400_001, float("inf"), float("nan")):
            self.assertIsNone(decode_date(json.dumps({"from": ms}), LISBON), ms)


if __name__ == "__main__":
    unittest.main()
