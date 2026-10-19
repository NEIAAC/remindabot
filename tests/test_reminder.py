"""Unit tests of the pure parts. Run from the repository root:  python3 -m unittest discover -s tests -v"""
import json
import re
import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

from remindabot import render
from remindabot.boards import (Card, decode_card, decode_date, decode_people, decode_remind_list,
                        describe_schema_problem, find_schema)
from remindabot.config import Config, ConfigError
from remindabot.driver import display_name
from remindabot.logic import evaluate, ignored_values, plan, usable_days
from remindabot.overrides import Overrides, SettingError, parse_cap, parse_send_at
from remindabot.store import NEW, RECLAIMED, Store

LISBON = ZoneInfo("Europe/Lisbon")


def make_card(**overrides) -> Card:
    base = dict(id="c1", title="Workshop", due=date(2026, 10, 25), remind_before=(12,),
                notifyees=("u1",), is_template=False, deleted=False)
    base.update(overrides)
    return Card(**base)


class DecodeDate(unittest.TestCase):
    def test_date_only_is_noon_utc_of_chosen_day(self):
        # 2026-10-25 12:00:00 UTC, which is what the Boards UI stores for a date-only value.
        ms = 1792929600000
        self.assertEqual(decode_date(json.dumps({"from": ms}), LISBON), date(2026, 10, 25))

    def test_date_only_is_independent_of_user_timezone(self):
        # Same stored value regardless of the browser timezone that created it.
        ms = 1792929600000
        for tz in ("America/Los_Angeles", "Asia/Tokyo", "Europe/Lisbon"):
            self.assertEqual(decode_date(json.dumps({"from": ms}), ZoneInfo(tz)), date(2026, 10, 25))

    def test_with_time_uses_card_timezone(self):
        # 2026-10-25 23:30 UTC is already the 26th in Tokyo, still the 25th in Lisbon (WET, UTC+0 after DST end).
        ms = 1792971000000
        raw = json.dumps({"from": ms, "includeTime": True, "timeZone": "Asia/Tokyo"})
        self.assertEqual(decode_date(raw, LISBON), date(2026, 10, 26))
        raw = json.dumps({"from": ms, "includeTime": True})
        self.assertEqual(decode_date(raw, LISBON), date(2026, 10, 25))

    def test_bad_timezone_falls_back(self):
        ms = 1792971000000
        raw = json.dumps({"from": ms, "includeTime": True, "timeZone": "Not/AZone"})
        self.assertEqual(decode_date(raw, LISBON), date(2026, 10, 25))

    def test_legacy_bare_number_and_garbage(self):
        self.assertEqual(decode_date("1792929600000", LISBON), date(2026, 10, 25))
        for bad in (None, "", "   ", "not json", "{}", '{"from": 0}', '{"from": "x"}', "[1]", "true", 123):
            self.assertIsNone(decode_date(bad, LISBON), bad)


class DecodeOthers(unittest.TestCase):
    def test_people(self):
        a, b = "a" * 26, "b" * 26
        self.assertEqual(decode_people([a, b, a]), (a, b))
        self.assertEqual(decode_people(a), (a,))
        # anything that is not a 26-character Mattermost id is dropped (it would end up in a URL path)
        self.assertEqual(decode_people(["me", "../..", "username/bob", "x?y=1", "A" * 26, "a" * 25, a]), (a,))
        self.assertEqual(decode_people(None), ())
        self.assertEqual(decode_people([]), ())

    def test_schema_matches_labels_case_insensitively_and_checks_types(self):
        board = {"cardProperties": [
            {"id": "p1", "name": "due date", "type": "date"},
            {"id": "p2", "name": " Notifyees ", "type": "multiPerson"},
            {"id": "p3", "name": "Remind Before", "type": "text"},
        ]}
        schema = find_schema(board, "Due Date", "Notifyees", "Remind Before")
        self.assertEqual((schema.due_id, schema.notifyees_id, schema.remind_id), ("p1", "p2", "p3"))
        board["cardProperties"][0]["type"] = "text"  # a text field named "Due Date" is not a date
        self.assertIsNone(find_schema(board, "Due Date", "Notifyees", "Remind Before"))
        del board["cardProperties"][2]
        board["cardProperties"][0]["type"] = "date"
        self.assertIsNone(find_schema(board, "Due Date", "Notifyees", "Remind Before"))

    def test_decode_card_flags(self):
        board = {"cardProperties": [
            {"id": "d", "name": "Due Date", "type": "date"},
            {"id": "a", "name": "Notifyees", "type": "multiPerson"},
            {"id": "r", "name": "Remind Before", "type": "text"},
        ]}
        schema = find_schema(board, "Due Date", "Notifyees", "Remind Before")
        raw = {"id": "c", "title": "T", "isTemplate": True, "deleteAt": 0,
               "properties": {"d": json.dumps({"from": 1792929600000}), "a": ["u" * 26], "r": "12"}}
        card = decode_card(raw, schema, LISBON)
        self.assertTrue(card.is_template)
        self.assertFalse(card.deleted)
        self.assertEqual((card.due, card.remind_before, card.notifyees), (date(2026, 10, 25), (12,), ("u" * 26,)))

    def test_assignees_are_not_who_gets_notified(self):
        """The old "Assignees" property is ignored: only Notifyees decides who is told."""
        board = {"cardProperties": [
            {"id": "d", "name": "Due Date", "type": "date"},
            {"id": "a", "name": "Assignees", "type": "multiPerson"},
            {"id": "n", "name": "Notifyees", "type": "multiPerson"},
            {"id": "r", "name": "Remind Before", "type": "text"},
        ]}
        schema = find_schema(board, "Due Date", "Notifyees", "Remind Before")
        raw = {"id": "c", "title": "T", "properties": {"a": ["a" * 26, "b" * 26], "n": ["b" * 26], "r": "3"}}
        self.assertEqual(decode_card(raw, schema, LISBON).notifyees, ("b" * 26,))


class DecodeRemindList(unittest.TestCase):
    """"Remind Before" is a Text property: whole numbers separated by ";", for example "7;3;1"."""

    def numbers(self, raw):
        return decode_remind_list(raw)[0]

    def test_a_list_separated_by_semicolons(self):
        self.assertEqual(self.numbers("7;3;1"), (7, 3, 1))
        self.assertEqual(self.numbers(" 7 ; 3 ;1 "), (7, 3, 1))     # spaces around a number are fine
        self.assertEqual(self.numbers("7;3;1;"), (7, 3, 1))          # so is a trailing or doubled ";"
        self.assertEqual(self.numbers("7;;3"), (7, 3))
        self.assertEqual(self.numbers("7;\n3"), (7, 3))

    def test_a_single_number_needs_no_separator(self):
        self.assertEqual(decode_remind_list("12"), ((12,), ()))

    def test_order_is_kept_and_repeats_are_dropped(self):
        self.assertEqual(self.numbers("1;7;3;7;1"), (1, 7, 3))

    def test_only_a_semicolon_separates_everything_else_is_reported_not_hidden(self):
        for typed in ("7,3,1", "7, 3, 1", "7 3 1", "7/3/1"):
            self.assertEqual(decode_remind_list(typed), ((), (typed,)), typed)
        numbers, unreadable = decode_remind_list("7;tres;1.5;1")
        self.assertEqual(numbers, (7, 1))
        self.assertEqual(unreadable, ("tres", "1.5"))

    def test_units_and_words_are_not_misread(self):
        # "7 dias" is NOT 7, and "1 semana" must not leave a bare "1" behind: both are reported whole
        numbers, unreadable = decode_remind_list("7 dias;1 semana;5;3d;+2;١")
        self.assertEqual(numbers, (5,))
        self.assertEqual(unreadable, ("7 dias", "1 semana", "3d", "+2", "١"))

    def test_leading_zeros_and_minus_zero_are_still_whole_numbers(self):
        self.assertEqual(self.numbers("007;03;-0"), (7, 3, 0))

    def test_lookalike_characters_are_reported_not_read(self):
        # a full-width semicolon / digits, an Arabic-Indic digit, a unicode minus: not what they look like
        for typed in ("7\uff1b3", "\uff17", "\u0667", "\u22121", "7\u00a03"):
            numbers, unreadable = decode_remind_list(typed)
            self.assertEqual(numbers, (), typed)
            self.assertEqual(len(unreadable), 1, typed)

    def test_only_a_semicolon_separates_not_a_new_line_or_a_tab(self):
        numbers, unreadable = decode_remind_list("7\n3\t1")
        self.assertEqual((numbers, unreadable), ((), ("7 3 1",)))       # reported with the blanks collapsed
        self.assertEqual(self.numbers("7;\n3;\t1"), (7, 3, 1))          # but blanks around a number are fine

    def test_what_is_reported_is_short_and_one_line(self):
        numbers, unreadable = decode_remind_list("a\x1b[31mred\n" + "z" * 100)
        self.assertEqual(numbers, ())
        for text in unreadable:
            self.assertLessEqual(len(text), 30)
            self.assertNotIn("\n", text)

    def test_at_most_five_unreadable_entries_are_remembered_but_the_numbers_all_count(self):
        raw = ";".join(f"x{n}" for n in range(20)) + ";4;2"
        numbers, unreadable = decode_remind_list(raw)
        self.assertEqual(numbers, (4, 2))
        self.assertEqual(len(unreadable), 5)

    def test_zero_and_negative_are_decoded_here_and_ignored_by_the_rule(self):
        self.assertEqual(self.numbers("0;-1;5"), (0, -1, 5))
        self.assertEqual(usable_days((0, -1, 5)), (5,))

    def test_empty_gives_nothing_and_nothing_to_report(self):
        for raw in (None, "", "   ", ";;;", " ; "):
            self.assertEqual(decode_remind_list(raw), ((), ()), repr(raw))

    def test_a_value_that_is_not_text_is_reported(self):
        """e.g. a list left over from when the property was a Multi select."""
        for raw in (True, 7, 1.5, {}, {"a": 1}, ["o1", "o2"], [None]):
            self.assertEqual(decode_remind_list(raw), ((), ("(not text)",)), repr(raw))

    def test_huge_inputs_stay_cheap_and_bounded(self):
        numbers, unreadable = decode_remind_list("x" * 100000 + ";5")
        self.assertEqual(numbers, ())                                 # the text is cut at 500 characters
        self.assertEqual([len(u) for u in unreadable], [30])          # and what is reported is short
        many = ";".join(str(n) for n in range(1, 3000))
        self.assertLessEqual(len(self.numbers(many)), 500)
        self.assertEqual(len(decode_remind_list(";".join("x%d" % n for n in range(50)))[1]), 5)
        self.assertEqual(usable_days(self.numbers("9" * 400)), ())    # decoded, but far out of range

    def test_only_a_text_property_is_accepted(self):
        def board(kind):
            return {"cardProperties": [
                {"id": "d", "name": "Due Date", "type": "date"},
                {"id": "n", "name": "Notifyees", "type": "multiPerson"},
                {"id": "r", "name": "Remind Before", "type": kind},
            ]}
        self.assertIsNotNone(find_schema(board("text"), "Due Date", "Notifyees", "Remind Before"))
        for kind in ("number", "multiSelect", "select"):
            self.assertIsNone(find_schema(board(kind), "Due Date", "Notifyees", "Remind Before"), kind)
            problem = describe_schema_problem(board(kind), "Due Date", "Notifyees", "Remind Before")
            self.assertIn(f"'Remind Before' has type {kind!r}", problem)
            self.assertIn("['text']", problem)


class Rule(unittest.TestCase):
    due = date(2026, 10, 25)

    def test_window_boundaries_for_12_days(self):
        self.assertFalse(evaluate(make_card(), date(2026, 10, 12)).send)   # 13 days before: too early
        self.assertTrue(evaluate(make_card(), date(2026, 10, 13)).send)    # exactly 12 days before
        self.assertTrue(evaluate(make_card(), date(2026, 10, 25)).send)    # the due date itself
        self.assertFalse(evaluate(make_card(), date(2026, 10, 26)).send)   # already passed

    def test_per_card_remind_before(self):
        card = make_card(remind_before=(5,))
        self.assertFalse(evaluate(card, date(2026, 10, 19)).send)
        self.assertTrue(evaluate(card, date(2026, 10, 20)).send)

    def test_invalid_remind_before_never_reminds(self):
        for value in ((), (0,), (-1,), (0, -1), (3661,), (10 ** 40,)):
            self.assertFalse(evaluate(make_card(remind_before=value), date(2026, 10, 24)).send, value)

    def test_stage_is_the_smallest_listed_number_not_below_the_days_left(self):
        card = make_card(remind_before=(1, 7, 3))              # due 2026-10-25, written in any order
        expected = {date(2026, 10, 17): None,                  # 8 days left: first reminder not reached
                    date(2026, 10, 18): 7, date(2026, 10, 19): 7, date(2026, 10, 21): 7,   # 7, 6, 4 days left
                    date(2026, 10, 22): 3, date(2026, 10, 23): 3,                          # 3, 2 days left
                    date(2026, 10, 24): 1, date(2026, 10, 25): 1,                          # 1 day, due today
                    date(2026, 10, 26): None}                  # passed
        for today, stage in expected.items():
            decision = evaluate(card, today)
            self.assertEqual(decision.stage, stage, today)
            self.assertEqual(decision.send, stage is not None, today)

    def test_zero_and_negative_entries_do_not_disable_the_valid_ones(self):
        card = make_card(remind_before=(0, 3, -1))
        self.assertEqual(evaluate(card, date(2026, 10, 22)).stage, 3)
        self.assertFalse(evaluate(card, date(2026, 10, 21)).send)

    def test_no_notifyees_is_reported_not_silently_dropped(self):
        decision = evaluate(make_card(notifyees=()), date(2026, 10, 20))
        self.assertFalse(decision.send)
        self.assertTrue(decision.nobody_to_tell)
        self.assertEqual(decision.stage, 12)
        # ... but only when a reminder would really have been sent
        self.assertFalse(evaluate(make_card(notifyees=()), date(2026, 9, 1)).nobody_to_tell)
        self.assertFalse(evaluate(make_card(notifyees=(), is_template=True), date(2026, 10, 20)).nobody_to_tell)

    def test_ignored_values_lists_garbage_and_too_large_but_not_zero_or_negative(self):
        card = make_card(remind_before=(5, 0, -1, 99999), remind_unreadable=("abc",))
        self.assertEqual(ignored_values(card), ["abc", "99999"])

    def test_templates_deleted_and_incomplete_cards(self):
        today = date(2026, 10, 20)
        self.assertFalse(evaluate(make_card(is_template=True), today).send)
        self.assertFalse(evaluate(make_card(deleted=True), today).send)
        self.assertFalse(evaluate(make_card(due=None), today).send)
        self.assertFalse(evaluate(make_card(notifyees=()), today).send)

    def test_a_card_first_seen_on_its_due_day_gets_the_last_reminder(self):
        card = make_card(remind_before=(7, 3, 1))
        decision = evaluate(card, self.due)                    # 0 days left
        self.assertEqual((decision.send, decision.stage), (True, 1))
        # ... and a list with only big numbers still reaches the due day with its smallest one
        self.assertEqual(evaluate(make_card(remind_before=(14,)), self.due).stage, 14)

    def test_each_listed_number_is_reached_exactly_that_many_days_before(self):
        for n in (1, 2, 7, 30, 3660):
            card = make_card(remind_before=(n,))
            first_day = self.due.fromordinal(self.due.toordinal() - n)
            before_first = self.due.fromordinal(self.due.toordinal() - n - 1)
            self.assertTrue(evaluate(card, first_day).send, n)
            self.assertFalse(evaluate(card, before_first).send, n)

    def test_only_unusable_numbers_mean_never_even_with_a_close_due_date(self):
        for numbers in ((0,), (-1,), (0, -1, -30), (3661, 99999)):
            decision = evaluate(make_card(remind_before=numbers), date(2026, 10, 25))
            self.assertEqual((decision.send, decision.stage, decision.nobody_to_tell), (False, None, False), numbers)

    def test_card_added_late_is_still_reminded(self):
        # due date set with only 3 days left: window already open -> reminded at the next run
        self.assertTrue(evaluate(make_card(due=date(2026, 10, 23)), date(2026, 10, 20)).send)


class Plan(unittest.TestCase):
    """`plan` says on which days a card's reminders go out; the real pass must then do exactly that."""
    due = date(2026, 10, 25)

    def days(self, card, today, sent=(), horizon=30):
        return [(d.day, stage) for d, stage in plan(card, today, sent, horizon)]

    def test_each_listed_number_on_its_day(self):
        card = make_card(remind_before=(7, 3, 1))
        self.assertEqual(self.days(card, date(2026, 10, 1)), [(18, 7), (22, 3), (24, 1)])

    def test_a_card_seen_late_gets_one_reminder_for_the_current_stage(self):
        card = make_card(remind_before=(14, 7, 3, 1))
        self.assertEqual(self.days(card, date(2026, 10, 22)), [(22, 3), (24, 1)])      # 3 left: stage 3, then 1

    def test_what_was_already_sent_is_not_planned_again_nor_any_bigger_stage(self):
        card = make_card(remind_before=(7, 3, 1))
        self.assertEqual(self.days(card, date(2026, 10, 18), sent={7}), [(22, 3), (24, 1)])
        self.assertEqual(self.days(card, date(2026, 10, 18), sent={3}), [(24, 1)])
        self.assertEqual(self.days(card, date(2026, 10, 18), sent={0}), [])           # a v1 record silences all

    def test_the_horizon_is_respected_and_nothing_is_planned_after_the_due_date(self):
        card = make_card(remind_before=(7, 3, 1))
        self.assertEqual(self.days(card, date(2026, 10, 18), horizon=2), [(18, 7)])
        self.assertEqual(self.days(card, date(2026, 10, 26)), [])

    def test_cards_that_are_never_reminded_have_an_empty_plan(self):
        today = date(2026, 10, 18)
        for card in (make_card(is_template=True), make_card(deleted=True), make_card(due=None),
                     make_card(remind_before=(0, -1)), make_card(remind_before=())):
            self.assertEqual(plan(card, today, (), 30), [])


class Greeting(unittest.TestCase):
    """Who the message calls the person: the nickname, else the full name, else their @username."""

    def test_the_nickname_comes_first_then_the_full_name(self):
        user = {"username": "alice", "nickname": "Ali", "first_name": "Alice", "last_name": "Silva"}
        self.assertEqual(display_name(user), "Ali")
        self.assertEqual(display_name({**user, "nickname": " "}), "Alice Silva")
        self.assertEqual(display_name({"username": "alice", "first_name": "Alice"}), "Alice")

    def test_with_no_name_at_all_the_person_is_greeted_by_username(self):
        self.assertEqual(display_name({"username": "alice"}), "@alice")
        self.assertEqual(display_name({"username": "alice", "nickname": "", "first_name": "", "last_name": None}), "@alice")

    def test_only_a_user_without_even_a_username_is_called_colega(self):
        self.assertEqual(display_name({}), "colega")

    def test_the_username_in_the_message_is_shown_but_does_not_mention_anyone(self):
        shown = render.sanitize_inline(display_name({"username": "alice"}), 80, fallback="colega")
        self.assertEqual(shown.replace("\u200b", ""), "@alice")
        self.assertNotIn("@alice", shown, "an invisible character must break the mention")


class Templates(unittest.TestCase):
    def write(self, text: str) -> Path:
        d = tempfile.mkdtemp()
        p = Path(d) / "message.txt"
        p.write_text(text, encoding="utf-8")
        return p

    def test_comment_header_is_cut_and_placeholders_render(self):
        p = self.write("header $not_a_placeholder\n-----8<-----\nOlá $responsible_name, falta $days_left_text.\n")
        t = render.load_template(p)
        self.assertEqual(render.render(t, {n: n for n in render.PLACEHOLDERS}), "Olá responsible_name, falta days_left_text.")

    def test_unknown_placeholder_is_rejected(self):
        with self.assertRaises(render.TemplateError):
            render.load_template(self.write("Olá $card_titel"))

    def test_empty_template_is_rejected(self):
        with self.assertRaises(render.TemplateError):
            render.load_template(self.write("comentário\n-----8<-----\n   \n"))

    def test_portuguese_dates(self):
        self.assertEqual(render.format_long_pt(date(2026, 10, 25)), "domingo, 25 de outubro de 2026")
        self.assertEqual(render.days_text(0), "é hoje")
        self.assertEqual(render.days_text(1), "falta 1 dia")
        self.assertEqual(render.days_text(12), "faltam 12 dias")

    def test_shipped_template_is_valid(self):
        render.load_template(Path(__file__).resolve().parent.parent / "config" / "message.txt")

    def test_the_shipped_template_header_documents_every_placeholder_and_only_those(self):
        text = (Path(__file__).resolve().parent.parent / "config" / "message.txt").read_text(encoding="utf-8")
        header = text.split(render.CUT_MARKER)[0]
        documented = set(re.findall(r"^\s+\$([a-z_]+)\s{2,}\S", header, flags=re.MULTILINE))
        self.assertEqual(documented, set(render.PLACEHOLDERS))

    def test_parse_template_validates_text_the_same_way_as_a_file(self):
        self.assertEqual(render.render(render.parse_template("Olá $card_title"), {n: "X" for n in render.PLACEHOLDERS}),
                         "Olá X")
        for bad in ("", "   \n", "Olá $card_titel", "custo $5"):
            with self.assertRaises(render.TemplateError, msg=repr(bad)):
                render.parse_template(bad)
        body = render.template_body("header\n" + render.CUT_MARKER + "\n\nCorpo $card_title\n")
        self.assertEqual(body, "Corpo $card_title")

    def test_remind_before_placeholder_is_the_stage_and_is_whole_days(self):
        t = render.load_template(self.write("$remind_before|${remind_before}"))
        self.assertEqual(render.render(t, {n: "7" for n in render.PLACEHOLDERS}), "7|7")


class StoreTests(unittest.TestCase):
    """The state is keyed by (card, person, due date, stage). `7` below is the "7 days before" stage."""

    def open(self, path, **kwargs) -> Store:
        """A store that is closed when the test ends (a forgotten connection is only closed by the garbage
        collector, and Python 3.13 and newer warn about it)."""
        store = Store(path, **kwargs)
        self.addCleanup(store.close)
        return store

    def test_lifecycle_new_sent_and_a_changed_due_date(self):
        with tempfile.TemporaryDirectory() as d:
            store = self.open(Path(d) / "s.sqlite")
            self.assertEqual(store.status("c", "u", "2026-10-25", 7), "none")
            self.assertEqual(store.claim("c", "u", "2026-10-25", 7), NEW)
            self.assertEqual(store.status("c", "u", "2026-10-25", 7), "pending")
            self.assertIsNone(store.claim("c", "u", "2026-10-25", 7))       # already held
            store.release("c", "u", "2026-10-25", 7)                        # send failed -> give it back
            self.assertEqual(store.claim("c", "u", "2026-10-25", 7), NEW)
            store.confirm("c", "u", "2026-10-25", 7)
            self.assertEqual(store.status("c", "u", "2026-10-25", 7), "sent")
            self.assertIsNone(store.claim("c", "u", "2026-10-25", 7))
            self.assertEqual(store.status("c", "u", "2026-11-02", 7), "none")      # due date changed
            self.assertEqual(store.status("c", "other", "2026-10-25", 7), "none")  # another notifyee
            self.assertIsNone(store.last_run())
            store.set_last_run("2026-10-13")
            self.assertEqual(store.last_run(), "2026-10-13")

    def test_each_stage_is_its_own_reminder(self):
        with tempfile.TemporaryDirectory() as d:
            store = self.open(Path(d) / "s.sqlite")
            store.claim("c", "u", "2026-10-25", 7)
            store.confirm("c", "u", "2026-10-25", 7)
            self.assertEqual(store.status("c", "u", "2026-10-25", 3), "none")   # the smaller stage is still due
            self.assertEqual(store.claim("c", "u", "2026-10-25", 3), NEW)
            store.confirm("c", "u", "2026-10-25", 3)
            self.assertEqual(store.sent_since(0), 2)                            # two messages were sent

    def test_a_smaller_stage_already_sent_silences_the_bigger_ones(self):
        """Told "3 days left" -> never "7 days" afterwards, even if somebody edits the list."""
        with tempfile.TemporaryDirectory() as d:
            store = self.open(Path(d) / "s.sqlite")
            store.claim("c", "u", "2026-10-25", 3)
            store.confirm("c", "u", "2026-10-25", 3)
            self.assertEqual(store.status("c", "u", "2026-10-25", 7), "sent")
            self.assertEqual(store.status("c", "u", "2026-10-25", 3), "sent")
            self.assertIsNone(store.claim("c", "u", "2026-10-25", 7))           # the atomic guard agrees
            self.assertEqual(store.status("c", "u", "2026-10-25", 1), "none")   # but a SMALLER one is new
            self.assertEqual(store.status("c", "u", "2026-11-02", 7), "none")   # and a new due date is new

    def test_a_recent_claim_on_a_smaller_stage_counts_as_in_progress(self):
        with tempfile.TemporaryDirectory() as d:
            store = self.open(Path(d) / "s.sqlite")
            store.claim("c", "u", "2026-10-25", 3)
            self.assertEqual(store.status("c", "u", "2026-10-25", 7), "pending")
            self.assertEqual(store.status("c", "u", "2026-10-25", 1), "none")

    def test_orphaned_claim_goes_stale_and_can_be_taken_over(self):
        with tempfile.TemporaryDirectory() as d:
            now = [1000.0]
            store = self.open(Path(d) / "s.sqlite", clock=lambda: now[0], stale_after=300)
            self.assertEqual(store.claim("c", "u", "2026-10-25", 7), NEW)
            now[0] += 299
            self.assertEqual(store.status("c", "u", "2026-10-25", 7), "pending")
            self.assertIsNone(store.claim("c", "u", "2026-10-25", 7))
            now[0] += 2
            self.assertEqual(store.status("c", "u", "2026-10-25", 7), "stale")
            self.assertEqual(store.claim("c", "u", "2026-10-25", 7), RECLAIMED)
            self.assertIsNone(store.claim("c", "u", "2026-10-25", 7))       # and is fresh again

    def test_release_never_removes_a_sent_record(self):
        with tempfile.TemporaryDirectory() as d:
            store = self.open(Path(d) / "s.sqlite")
            store.claim("c", "u", "2026-10-25", 7)
            store.confirm("c", "u", "2026-10-25", 7)
            store.release("c", "u", "2026-10-25", 7)
            self.assertEqual(store.status("c", "u", "2026-10-25", 7), "sent")

    def test_sent_since_counts_per_day_and_per_person(self):
        with tempfile.TemporaryDirectory() as d:
            now = [10_000.0]
            store = self.open(Path(d) / "s.sqlite", clock=lambda: now[0])
            for card, user in (("c1", "u1"), ("c2", "u1"), ("c3", "u2")):
                store.claim(card, user, "2026-10-25", 7)
                store.confirm(card, user, "2026-10-25", 7)
            store.claim("c4", "u1", "2026-10-25", 7)                        # pending does not count
            self.assertEqual(store.sent_since(0), 3)
            self.assertEqual(store.sent_since(0, "u1"), 2)
            self.assertEqual(store.sent_since(now[0] + 1), 0)

    def test_readonly_store_never_creates_or_changes_anything(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "missing.sqlite"
            ro = self.open(path, readonly=True)
            self.assertEqual(ro.status("c", "u", "2026-10-25", 7), "none")
            self.assertEqual(ro.sent_since(0), 0)
            self.assertIsNone(ro.last_run())
            self.assertFalse(path.exists())

    def test_readonly_store_copes_with_a_file_that_has_no_table(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "empty.sqlite"
            sqlite3.connect(path).close()                                # exists, but is not ours
            ro = self.open(path, readonly=True)
            self.assertEqual(ro.status("c", "u", "2026-10-25", 7), "none")

    def test_readonly_path_with_uri_special_characters_is_not_misread(self):
        with tempfile.TemporaryDirectory() as d:
            odd = Path(d) / "a#b?c%d"
            odd.mkdir()
            path = odd / "state.sqlite"
            self.open(path).confirm("c", "u", "x", 7)                        # create a real file there
            self.open(path).claim("c", "u", "2026-10-25", 7)
            ro = self.open(path, readonly=True)
            self.assertEqual(ro.status("c", "u", "2026-10-25", 7), "pending")
            self.assertEqual(sorted(p.name for p in Path(d).iterdir()), ["a#b?c%d"])  # no stray file next to it

    def test_prune_forgets_only_old_entries(self):
        with tempfile.TemporaryDirectory() as d:
            store = self.open(Path(d) / "s.sqlite")
            for due in ("2020-01-01", "2999-01-01"):
                store.claim("c", "u", due, 7)
                store.confirm("c", "u", due, 7)
            self.assertEqual(store.prune(), 1)
            self.assertEqual(store.status("c", "u", "2999-01-01", 7), "sent")

    # -- settings area, history, forgetting -----------------------------------------------------

    def test_meta_values_round_trip_and_read_only_stores_read_them(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "s.sqlite"
            store = self.open(path)
            self.assertIsNone(store.get_meta("k"))
            store.set_meta("k", "v1")
            store.set_meta("k", "v2")
            self.assertEqual(store.get_meta("k"), "v2")
            self.assertEqual(self.open(path, readonly=True).get_meta("k"), "v2")
            store.delete_meta("k")
            self.assertIsNone(store.get_meta("k"))
            self.assertIsNone(self.open(Path(d) / "missing.sqlite", readonly=True).get_meta("k"))

    def test_history_is_newest_first_limited_and_filterable_by_time(self):
        now = [1000.0]
        with tempfile.TemporaryDirectory() as d:
            store = self.open(Path(d) / "s.sqlite", clock=lambda: now[0])
            for i, title in enumerate(("A", "B", "C")):
                now[0] = 1000.0 + i * 100
                store.record_history(card_id=f"c{i}", card_title=title, board_title="Posts", user_id="u", username="alice",
                                     due_date="2026-10-25", days=7)
            self.assertEqual([h["card_title"] for h in store.recent_history(10)], ["C", "B", "A"])
            self.assertEqual([h["card_title"] for h in store.recent_history(2)], ["C", "B"])
            self.assertEqual([h["card_title"] for h in store.recent_history(10, since=1100)], ["C", "B"])
            self.assertEqual(self.open(Path(d) / "s.sqlite", readonly=True).recent_history(1)[0]["username"], "alice")

    def test_history_on_a_file_from_before_it_existed(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "s.sqlite"
            self.make_v1_file(path)
            self.assertEqual(self.open(path, readonly=True).recent_history(), [])      # no history table, no error
            self.assertEqual(self.open(path, readonly=True).sent_since(0), 1)

    def test_forget_reopens_a_stage_but_the_message_still_counts_for_the_caps(self):
        now = [5000.0]
        with tempfile.TemporaryDirectory() as d:
            store = self.open(Path(d) / "s.sqlite", clock=lambda: now[0])
            self.assertEqual(store.claim("c", "u", "2026-10-25", 7), NEW)
            store.confirm("c", "u", "2026-10-25", 7)
            store.record_history(card_id="c", card_title="T", board_title="B", user_id="u", username="a",
                                due_date="2026-10-25", days=7)
            self.assertEqual(store.sent_stages("c", "u", "2026-10-25"), {7})
            self.assertEqual(store.forget("c", "u", "2026-10-25", 7), 1)
            self.assertEqual(store.sent_stages("c", "u", "2026-10-25"), set())
            self.assertEqual(store.status("c", "u", "2026-10-25", 7), "none")
            self.assertEqual(store.sent_since(0), 1)               # the history still remembers the message
            self.assertEqual(store.claim("c", "u", "2026-10-25", 7), NEW)
            store.confirm("c", "u", "2026-10-25", 7)
            store.record_history(card_id="c", card_title="T", board_title="B", user_id="u", username="a",
                                 due_date="2026-10-25", days=7)
            self.assertEqual(store.sent_since(0), 2)               # two messages were sent: the cap sees both
            self.assertEqual(store.sent_since(0, "u"), 2)
            self.assertEqual(store.sent_since(0, "somebody-else"), 0)

    def test_forget_removes_smaller_stages_too_because_they_are_what_silences_a_bigger_one(self):
        with tempfile.TemporaryDirectory() as d:
            store = self.open(Path(d) / "s.sqlite")
            store.db.execute("INSERT INTO reminders VALUES ('c','u','2026-10-25',3,'sent',1.0)")
            store.db.execute("INSERT INTO reminders VALUES ('c','u','2026-10-25',7,'sent',1.0)")
            self.assertEqual(store.forget("c", "u", "2026-10-25", 7), 2)
            self.assertEqual(store.forget("c", "other", "2026-10-25", 7), 0)         # only that person

    def test_prune_forgets_old_history_too(self):
        now = [10 ** 9]
        with tempfile.TemporaryDirectory() as d:
            store = self.open(Path(d) / "s.sqlite", clock=lambda: now[0])
            store.record_history(card_id="c", card_title="T", board_title="B", user_id="u", username="a",
                                 due_date="2026-10-25", days=7)
            now[0] += 401 * 86400
            store.prune()
            self.assertEqual(store.recent_history(), [])

    def test_close_releases_the_file(self):
        with tempfile.TemporaryDirectory() as d:
            store = self.open(Path(d) / "s.sqlite")
            store.close()
            store.close()                                           # harmless twice
            self.assertEqual(store.sent_since(0), 0)

    # -- a state file written by the first version (one reminder per card, no stage) -----------

    def make_v1_file(self, path: Path) -> None:
        db = sqlite3.connect(path)
        db.execute("""CREATE TABLE reminders (card_id TEXT NOT NULL, user_id TEXT NOT NULL,
                      due_date TEXT NOT NULL, status TEXT NOT NULL, updated_at REAL NOT NULL,
                      PRIMARY KEY (card_id, user_id, due_date))""")
        db.execute("INSERT INTO reminders VALUES ('c', 'u', '2026-10-25', 'sent', 5000.0)")
        db.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        db.execute("INSERT INTO meta VALUES ('last_run', '2026-10-13')")
        db.commit()
        db.close()

    def test_old_state_file_is_upgraded_and_keeps_what_was_sent(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "s.sqlite"
            self.make_v1_file(path)
            store = self.open(path)
            for stage in (30, 7, 1):   # a reminder sent by the old version silences every stage of that date
                self.assertEqual(store.status("c", "u", "2026-10-25", stage), "sent", stage)
                self.assertIsNone(store.claim("c", "u", "2026-10-25", stage))
            self.assertEqual(store.status("c", "u", "2026-11-02", 7), "none")
            self.assertEqual(store.last_run(), "2026-10-13")
            self.assertEqual(store.sent_since(0), 1)
            self.open(path)                                                  # opening again changes nothing
            self.assertEqual(store.status("c", "u", "2026-10-25", 7), "sent")

    def test_old_state_file_can_be_read_without_upgrading_it(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "s.sqlite"
            self.make_v1_file(path)
            ro = self.open(path, readonly=True)                              # what --dry-run does
            self.assertEqual(ro.status("c", "u", "2026-10-25", 7), "sent")
            self.assertEqual(ro.status("c", "u", "2026-11-02", 7), "none")
            db = sqlite3.connect(path)
            columns = [r[1] for r in db.execute("PRAGMA table_info(reminders)")]
            db.close()
            self.assertNotIn("days", columns)                            # still untouched


class SettingsTests(unittest.TestCase):
    """What an administrator may change from chat, layered over the configuration."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = [1_000_000.0]
        self.store = Store(Path(self.tmp.name) / "s.sqlite", clock=lambda: self.now[0])
        self.addCleanup(self.store.close)
        self.settings = Overrides(self.store)
        message = Path(self.tmp.name) / "message.txt"
        message.write_text("cabeçalho\n" + render.CUT_MARKER + "\nOlá $responsible_name\n", encoding="utf-8")
        self.cfg = Config.from_env({"REMINDER_BOT_TOKEN": "t", "REMINDER_SITE_URL": "https://x.example",
                                    "REMINDER_TEMPLATE_FILE": str(message)})

    def test_without_overrides_the_configuration_is_untouched(self):
        self.assertIs(self.settings.effective(self.cfg), self.cfg)
        self.assertEqual(self.settings.active(), {})
        self.assertIsNone(self.settings.paused())

    def test_time_parsing(self):
        for text, expected in (("9:30", (9, 30)), ("09:30", (9, 30)), ("0:00", (0, 0)), ("23h59", (23, 59)),
                               (" 7:05 ", (7, 5))):
            parsed = parse_send_at(text)
            self.assertEqual((parsed.hour, parsed.minute), expected, text)
        for bad in ("", "24:00", "9", "9:5", "9:60", "ab", "9.30", "-1:00", "09:30:00", None):
            with self.assertRaises(SettingError, msg=repr(bad)):
                parse_send_at(bad)

    def test_cap_parsing(self):
        self.assertEqual(parse_cap("25", "x"), 25)
        for bad in ("0", "-1", "10001", "abc", "", "1.5"):
            with self.assertRaises(SettingError, msg=bad):
                parse_cap(bad, "x")

    def test_overrides_are_applied_and_attributed(self):
        self.settings.set_send_at("11:15", "root")
        self.settings.set_caps(300, 40, "root")
        effective = self.settings.effective(self.cfg)
        self.assertEqual((effective.send_at.hour, effective.send_at.minute), (11, 15))
        self.assertEqual((effective.max_per_day, effective.max_per_user), (300, 40))
        self.assertEqual(effective.prop_due, self.cfg.prop_due)         # nothing else changed
        info = self.settings.active()
        self.assertEqual(info["time"], {"by": "root", "at": 1_000_000.0})
        self.assertEqual(set(info), {"time", "caps"})
        self.assertEqual(self.settings.reset("time"), ["time"])
        self.assertEqual(self.settings.effective(self.cfg).send_at, self.cfg.send_at)
        self.assertEqual(self.settings.effective(self.cfg).max_per_day, 300)

    def test_a_damaged_stored_value_is_ignored_not_fatal(self):
        self.store.set_meta("set.send_at", "garbage")
        self.store.set_meta("set.max_per_day", "-5")
        self.assertIs(self.settings.effective(self.cfg), self.cfg)

    def test_setting_only_one_cap_keeps_the_other(self):
        self.settings.set_caps(None, 7, "root")
        effective = self.settings.effective(self.cfg)
        self.assertEqual((effective.max_per_day, effective.max_per_user), (self.cfg.max_per_day, 7))
        with self.assertRaises(SettingError):
            self.settings.set_caps(None, None, "root")

    def test_the_message_is_the_file_until_one_is_set(self):
        template, source = self.settings.template(self.cfg)
        self.assertEqual(source, "file")
        self.assertEqual(self.settings.message_body(self.cfg), "Olá $responsible_name")   # without the header
        self.settings.set_message("Novo $card_title\r\nlinha 2", "root")
        template, source = self.settings.template(self.cfg)
        self.assertEqual(source, "custom")
        self.assertEqual(self.settings.message_body(self.cfg), "Novo $card_title\nlinha 2")
        self.assertEqual(self.settings.reset("message"), ["message"])
        self.assertEqual(self.settings.template(self.cfg)[1], "file")

    def test_a_bad_message_is_refused_and_leaves_the_old_one(self):
        self.settings.set_message("Bom $card_title", "root")
        for bad in ("", "  ", "x" * 3001, "mau $nada_assim", "$"):
            with self.assertRaises(SettingError, msg=bad[:20]):
                self.settings.set_message(bad, "root")
        self.assertEqual(self.settings.custom_message(), "Bom $card_title")

    def test_a_stored_message_that_no_longer_parses_falls_back_to_the_file(self):
        self.store.set_meta("set.message", "estragado $nada")
        self.assertEqual(self.settings.template(self.cfg)[1], "file")

    def test_pause_resume_and_alert_lists(self):
        self.settings.pause("root")
        self.assertEqual(self.settings.paused()["by"], "root")
        self.assertTrue(self.settings.resume())
        self.assertFalse(self.settings.resume())
        self.settings.set_alerts("u1", True)
        self.settings.set_alerts("u2", True)
        self.settings.set_alerts("u1", True)
        self.assertEqual(self.settings.alert_users(), ["u2", "u1"])
        self.settings.set_alerts("u2", False)
        self.assertEqual(self.settings.alert_users(), ["u1"])
        self.store.set_meta("alerts", "not json")
        self.assertEqual(self.settings.alert_users(), [])

    def test_dialog_state_is_bound_to_the_person_the_purpose_and_the_time(self):
        state = self.settings.sign_state("setmessage", "u1")
        self.assertTrue(self.settings.verify_state(state, "setmessage", "u1"))
        self.assertFalse(self.settings.verify_state(state, "setmessage", "u2"))
        self.assertFalse(self.settings.verify_state(state, "other", "u1"))
        expires, nonce, mac = state.split(".")
        flipped = mac[:-1] + ("1" if mac[-1] == "0" else "0")        # always a different signature
        self.assertFalse(self.settings.verify_state(f"{expires}.{nonce}.{flipped}", "setmessage", "u1"))
        self.assertFalse(self.settings.verify_state(f"{int(expires) + 99}.{nonce}.{mac}", "setmessage", "u1"))
        for junk in (None, 5, "", "a.b", "x.y.z", "1.2.3.4"):
            self.assertFalse(self.settings.verify_state(junk, "setmessage", "u1"), repr(junk))
        self.now[0] += 15 * 60 + 1
        self.assertFalse(self.settings.verify_state(state, "setmessage", "u1"))      # expired

    def test_the_signing_secret_is_per_installation(self):
        other_store = Store(Path(self.tmp.name) / "other.sqlite", clock=lambda: self.now[0])
        self.addCleanup(other_store.close)
        other = Overrides(other_store)
        state = self.settings.sign_state("setmessage", "u1")
        self.assertFalse(other.verify_state(state, "setmessage", "u1"))

    def test_command_tokens_round_trip_and_survive_garbage(self):
        self.settings.set_command_tokens({"team": {"remind_send": "tok"}})
        self.assertEqual(self.settings.command_tokens(), {"team": {"remind_send": "tok"}})
        self.store.set_meta("command_tokens", "[1,2]")
        self.assertEqual(self.settings.command_tokens(), {})
        self.store.set_meta("command_tokens", "{oops")
        self.assertEqual(self.settings.command_tokens(), {})

    def test_a_read_only_store_can_be_read_but_not_written(self):
        self.settings.set_send_at("10:00", "root")
        ro_store = Store(Path(self.tmp.name) / "s.sqlite", readonly=True)
        self.addCleanup(ro_store.close)
        ro = Overrides(ro_store)
        self.assertEqual(ro.effective(self.cfg).send_at.hour, 10)
        with self.assertRaises(AssertionError):
            ro.pause("root")


class ConfigTests(unittest.TestCase):
    def test_defaults_and_required(self):
        with self.assertRaises(ConfigError):
            Config.from_env({})
        cfg = Config.from_env({"REMINDER_BOT_TOKEN": "t", "REMINDER_SITE_URL": "https://x.example/"})
        self.assertEqual(cfg.site_url, "https://x.example")
        self.assertEqual(cfg.mm_url, "http://mattermost:8065")
        self.assertEqual((cfg.send_at.hour, cfg.send_at.minute), (14, 0))
        self.assertTrue(cfg.daily_pass, "the daily pass is on unless someone switches it off")
        self.assertEqual(cfg.prop_remind, "Remind Before")
        self.assertTrue(cfg.auto_join)
        self.assertEqual((cfg.max_per_day, cfg.max_per_user), (100, 20))

    def test_property_names_default_and_can_be_changed(self):
        base = {"REMINDER_BOT_TOKEN": "t", "REMINDER_SITE_URL": "https://x.example"}
        cfg = Config.from_env(base)
        self.assertEqual((cfg.prop_due, cfg.prop_notifyees, cfg.prop_remind),
                         ("Due Date", "Notifyees", "Remind Before"))
        cfg = Config.from_env({**base, "REMINDER_PROP_DUE": "Data limite", "REMINDER_PROP_NOTIFYEES": " Avisar ",
                               "REMINDER_PROP_REMIND": "Avisar antes"})
        self.assertEqual((cfg.prop_due, cfg.prop_notifyees, cfg.prop_remind),
                         ("Data limite", "Avisar", "Avisar antes"))        # surrounding spaces are trimmed

    def test_the_chat_command_endpoint_settings(self):
        base = {"REMINDER_BOT_TOKEN": "t", "REMINDER_SITE_URL": "https://x.example"}
        cfg = Config.from_env(base)
        self.assertEqual((cfg.commands_bind, cfg.commands_port, cfg.commands_url),
                         ("0.0.0.0", 8080, "http://reminder:8080"))
        cfg = Config.from_env({**base, "REMINDER_COMMANDS_PORT": "9000"})
        self.assertEqual((cfg.commands_port, cfg.commands_url), (9000, "http://reminder:9000"))
        cfg = Config.from_env({**base, "REMINDER_COMMANDS_URL": "http://bot.internal:9000/",
                               "REMINDER_COMMANDS_BIND": "127.0.0.1"})
        self.assertEqual((cfg.commands_bind, cfg.commands_url), ("127.0.0.1", "http://bot.internal:9000"))   # no trailing slash
        self.assertEqual(Config.from_env({**base, "REMINDER_COMMANDS_PORT": "0"}).commands_port, 0)   # switched off
        for bad in ("-1", "70000", "http", "80.5"):
            with self.assertRaises(ConfigError, msg=bad):
                Config.from_env({**base, "REMINDER_COMMANDS_PORT": bad})
        with self.assertRaises(ConfigError):
            Config.from_env({**base, "REMINDER_COMMANDS_URL": "reminder:8080"})

    def test_the_old_assignees_setting_no_longer_does_anything(self):
        base = {"REMINDER_BOT_TOKEN": "t", "REMINDER_SITE_URL": "https://x.example"}
        cfg = Config.from_env({**base, "REMINDER_PROP_ASSIGNEES": "Responsaveis"})
        self.assertEqual(cfg.prop_notifyees, "Notifyees")        # who is told is decided by Notifyees only
        self.assertFalse(hasattr(cfg, "prop_assignees"))

    def test_blank_values_fall_back_to_defaults(self):
        # docker compose passes "VAR=" through when ${VAR} is unset in .env
        cfg = Config.from_env({"REMINDER_BOT_TOKEN": "t", "REMINDER_SITE_URL": "https://x.example",
                               "REMINDER_TZ": "", "REMINDER_SEND_AT": "  ", "TZ": ""})
        self.assertEqual(str(cfg.tz), "Europe/Lisbon")
        self.assertEqual(cfg.send_at.hour, 14)

    def test_a_setting_can_come_from_a_file(self):
        """The Docker secrets convention: REMINDER_X_FILE names a file holding the value of REMINDER_X."""
        with tempfile.TemporaryDirectory() as d:
            token = Path(d) / "token"
            token.write_text("secret-from-file\n")
            base = {"REMINDER_SITE_URL": "https://x.example"}
            cfg = Config.from_env({**base, "REMINDER_BOT_TOKEN_FILE": str(token)})
            self.assertEqual(cfg.bot_token, "secret-from-file")                    # the newline is dropped
            cfg = Config.from_env({**base, "REMINDER_BOT_TOKEN": "plain", "REMINDER_BOT_TOKEN_FILE": str(token)})
            self.assertEqual(cfg.bot_token, "plain")                               # the variable wins
            with self.assertRaises(ConfigError) as raised:                         # named, not a traceback
                Config.from_env({**base, "REMINDER_BOT_TOKEN_FILE": str(Path(d) / "missing")})
            self.assertIn("REMINDER_BOT_TOKEN_FILE", str(raised.exception))
            token.write_text("  \n")
            with self.assertRaises(ConfigError):                                   # an empty file is "not set"
                Config.from_env({**base, "REMINDER_BOT_TOKEN_FILE": str(token)})

    def test_the_daily_pass_can_be_switched_off(self):
        base = {"REMINDER_BOT_TOKEN": "t", "REMINDER_SITE_URL": "https://x.example"}
        self.assertTrue(Config.from_env(base).daily_pass)
        self.assertFalse(Config.from_env({**base, "REMINDER_DAILY_PASS": "false"}).daily_pass)
        self.assertTrue(Config.from_env({**base, "REMINDER_DAILY_PASS": "yes"}).daily_pass)
        with self.assertRaises(ConfigError):
            Config.from_env({**base, "REMINDER_DAILY_PASS": "sometimes"})

    def test_bad_values(self):
        base = {"REMINDER_BOT_TOKEN": "t", "REMINDER_SITE_URL": "https://x.example"}
        with self.assertRaises(ConfigError):
            Config.from_env({**base, "REMINDER_SEND_AT": "9h"})
        with self.assertRaises(ConfigError):
            Config.from_env({**base, "REMINDER_TZ": "Mars/Olympus"})
        for bad_url in ("mattermost", "https://", "ftp://x", "http:///x"):
            with self.assertRaises(ConfigError):
                Config.from_env({**base, "REMINDER_MM_URL": bad_url})
            with self.assertRaises(ConfigError):
                Config.from_env({**base, "REMINDER_SITE_URL": bad_url})
        for name in ("REMINDER_MAX_MESSAGES_PER_DAY", "REMINDER_MAX_MESSAGES_PER_USER_PER_DAY"):
            for bad in ("0", "-1", "many"):
                with self.assertRaises(ConfigError):
                    Config.from_env({**base, name: bad})


if __name__ == "__main__":
    unittest.main()
