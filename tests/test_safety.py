"""Safety: the bot is a guest on somebody else's chat server, and it holds secrets.

These tests are about what the bot must NEVER do, whatever the data and whoever calls it:

  * WriteSurface    it can only ever write the few things it is meant to (a direct message, joining an
                    open board, a pop-up form, a late answer): never a board, a card, a role, a user;
  * Secrets         no token or signing key reaches a log line, a reply, a message or the state's
                    ordinary tables, not even when the server itself echoes the credentials back;
  * StateFile       private to its owner, immune to SQL injection, never overwritten when damaged;
  * ResourceLeaks   a daemon that handles thousands of commands does not accumulate threads or files;
  * ReplySize       a reply never exceeds what Mattermost accepts, however much data there is;
  * WebhookAbuse    the chat endpoint survives idle, half-open, malformed and random connections.
"""
import ast
import gc
import json
import logging
import os
import random
import re
import socket
import sqlite3
import stat
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from unittest import mock

from remindabot import scheduler, webhook_server
from remindabot.commands import SPECS, CommandRouter, command, registry
from remindabot.commands import markup as md
from remindabot.commands.markup import MAX_REPLY_CHARS
from remindabot.driver import ApiError, Client
from remindabot.overrides import Overrides
from remindabot.store import Store
from remindabot.webhook_server import start_server

from . import test_integration as ti
from .support.fakeserver import ADMIN_ID, BOT_TOKEN, Fault, uid
from .test_commands import CommandsBase

LISBON, TODAY, U1, U2, U3 = ti.LISBON, ti.TODAY, ti.U1, ti.U2, ti.U3
ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "src" / "remindabot"

# What the bot may write, and nothing else. Anything that is not a GET must match one of these.
ALLOWED_WRITES = (
    ("POST", re.compile(r"^/api/v4/channels/direct$")),                           # open the chat with a person
    ("POST", re.compile(r"^/api/v4/posts$")),                                     # the message itself
    ("POST", re.compile(r"^/plugins/focalboard/api/v2/boards/[\w-]+/join$")),     # read access to an OPEN board
    ("POST", re.compile(r"^/api/v4/actions/dialogs/open$")),                      # the form of /remind_setmessage
    ("POST", re.compile(r"^/hooks/commands/\w+$")),                               # the late answer of a command
)

COMMAND_ARGS = {"remind_next": "7", "remind_history": "5", "remind_card": "Workshop", "remind_resend": "Workshop",
                "remind_setsend": "09:30", "remind_setcap": "100 10", "remind_reset": "all", "remind_alerts": "on"}


def writes_since(fake, start: int) -> list[tuple[str, str]]:
    return [(method, path.split("?")[0]) for method, path in fake.requests[start:] if method not in ("GET", "HEAD")]


class WriteSurface(CommandsBase):
    """The bot's blast radius on the chat server, measured on the wire."""

    def world(self):
        self.simple_board("b1", member=True)
        self.fake.add_card("b1", "c1", "Workshop", due_ms=ti.in_days(5), notifyees=[U1, U2], remind="7;3;1")
        self.fake.add_card("b1", "c2", "Tomorrow", due_ms=ti.in_days(1), notifyees=[U3], remind="1")
        self.fake.add_board("open", "Not joined yet", member=False)
        self.fake.add_card("open", "c3", "Other", due_ms=ti.in_days(4), notifyees=[U1], remind="7")
        self.fake.add_board("priv", "Private one", type_="P", member=False)
        self.fake.add_card("priv", "c4", "Secret", due_ms=ti.in_days(4), notifyees=[U1], remind="7")

    def assert_allowed(self, writes, what):
        for method, path in writes:
            self.assertTrue(any(method == m and pattern.match(path) for m, pattern in ALLOWED_WRITES),
                            f"{what}: {method} {path} is not something the bot may do")

    def test_a_real_pass_only_sends_messages_and_joins_open_boards(self):
        self.world()
        start = len(self.fake.requests)
        self.go()
        writes = writes_since(self.fake, start)
        self.assert_allowed(writes, "a pass")
        self.assertTrue(any(p.endswith("/posts") for _, p in writes), "the pass should have sent something")
        joined = [p for _, p in writes if p.endswith("/join")]
        self.assertEqual(joined, ["/plugins/focalboard/api/v2/boards/open/join"])      # the private board is left alone

    def test_a_dry_run_writes_nothing_at_all(self):
        self.world()
        start = len(self.fake.requests)
        self.go(dry_run=True)
        self.assertEqual(writes_since(self.fake, start), [])

    def test_the_looking_commands_write_nothing_at_all(self):
        self.world()
        start = len(self.fake.requests)
        for trigger in ("remind_help", "remind_status", "remind_next", "remind_boards", "remind_card",
                        "remind_history", "remind_getmessage"):
            self.run_command(trigger, COMMAND_ARGS.get(trigger, ""))
        acts = [w for w in writes_since(self.fake, start) if not w[1].startswith("/hooks/commands/")]   # (a late answer is no act)
        self.assertEqual(acts, [], "the commands that look must not act")

    def test_every_command_stays_inside_the_allowed_writes(self):
        self.world()
        start = len(self.fake.requests)
        for spec in SPECS:
            with self.subTest(command=spec.trigger):
                self.run_command(spec.trigger, COMMAND_ARGS.get(spec.trigger, ""))
        self.assert_allowed(writes_since(self.fake, start), "the commands")

    def test_the_daily_loop_over_a_week_stays_inside_the_allowed_writes(self):
        self.world()
        clock = ti.FakeClock(datetime(2026, 10, 13, 8, 0, tzinfo=LISBON))
        start = len(self.fake.requests)
        store = self.store(clock=lambda: clock.utc.timestamp())
        self.addCleanup(store.close)
        scheduler.serve(self.cfg, self.mm, self.boards, store,
                        now_fn=clock.now, sleep_fn=clock.sleep, max_iterations=7 * 24 * 60)
        self.assert_allowed(writes_since(self.fake, start), "a week of the daily loop")

    def test_nobody_is_ever_changed_by_a_pass_or_a_command(self):
        """No request ever targets a role, a membership, a user, a team or a card, at any method."""
        self.world()
        for spec in SPECS:
            self.run_command(spec.trigger, COMMAND_ARGS.get(spec.trigger, ""))
        self.go()
        forbidden = re.compile(r"/(roles|members|users|teams|cards|blocks|boards)(/[\w-]+)*$")
        for method, path in self.fake.requests:
            if method != "GET":
                self.assertFalse(forbidden.search(path.split("?")[0]) and not path.endswith("/join"),
                                 f"{method} {path}")


class _LogCapture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record):
        self.lines.append(self.format(record))


class Secrets(CommandsBase):
    """Tokens and signing keys must not leave their file, even through error messages."""

    def setUp(self):
        super().setUp()
        self.capture = _LogCapture()
        self.capture.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
        root = logging.getLogger()
        previous = root.level
        root.addHandler(self.capture)
        root.setLevel(logging.DEBUG)
        self.addCleanup(root.removeHandler, self.capture)
        self.addCleanup(root.setLevel, previous)

    def secrets(self) -> list[str]:
        store = self.store()
        Overrides(store).sign_state("setmessage", ADMIN_ID)                  # makes sure the signing key exists
        values = [BOT_TOKEN, self.fake.admin_token, store.get_meta("secret") or "",
                  *[t for team in Overrides(store).command_tokens().values() for t in team.values()]]
        store.close()
        return [v for v in values if len(v) >= 8]

    def assert_none_of_the_secrets_in(self, text: str, where: str):
        for secret in self.secrets():
            self.assertNotIn(secret, text, f"a secret leaked into {where}")

    def test_the_logs_never_hold_a_secret_through_a_whole_bad_day(self):
        self.simple_board("b1", member=True)
        self.fake.add_card("b1", "c1", "Workshop", due_ms=ti.in_days(5), notifyees=[U1, U2], remind="7;3;1")
        # every kind of failure the server can show, one after the other
        for kind, extra in (("status", {"status": 500, "body": "boom"}), ("html", {}), ("redirect", {}), ("drop", {}),
                            ("commit_then_drop", {}), ("status", {"status": 401, "body": "nope"}),
                            ("echo_auth", {"status": 400})):
            self.fake.faults[:] = [Fault(lambda m, p: True, kind, times=6, **extra)]
            try:
                self.go()
            except Exception:                                                           # noqa: BLE001 - only the logs matter
                pass
        self.fake.faults.clear()
        for spec in SPECS:                                                                # and every command, at DEBUG
            self.run_command(spec.trigger, COMMAND_ARGS.get(spec.trigger, ""))
        self.run_command("remind_pause", user=ti.U1, username="alice")                    # refusals are logged too
        self.commands.handle_command(self.form("remind_pause", token="wrong"))            # so is a forged call
        self.assert_none_of_the_secrets_in("\n".join(self.capture.lines), "the log")

    def test_an_error_page_that_echoes_the_credentials_is_not_logged_with_them(self):
        """Some proxies repeat the request headers in their error pages. The detail goes into the log."""
        self.fake.faults.append(Fault(lambda m, p: True, "echo_auth", status=400))
        client = Client(self.url, BOT_TOKEN, retries=0)
        with self.assertRaises(ApiError) as caught:
            client.request("GET", "/api/v4/users/me")
        self.assertNotIn(BOT_TOKEN, str(caught.exception))
        self.assertIn("400", str(caught.exception))

    def test_replies_messages_and_forms_never_hold_a_secret(self):
        self.simple_board("b1", member=True)
        self.fake.add_card("b1", "c1", "Workshop", due_ms=ti.in_days(5), notifyees=[U1], remind="7")
        replies = [self.run_command(spec.trigger, COMMAND_ARGS.get(spec.trigger, "")) for spec in SPECS]
        self.assert_none_of_the_secrets_in("\n".join(replies), "a reply")
        self.assert_none_of_the_secrets_in(json.dumps(self.fake.posts), "a message")
        self.assert_none_of_the_secrets_in(json.dumps(self.fake.dialogs), "a pop-up form")
        self.assert_none_of_the_secrets_in(json.dumps(self.fake.delayed), "a late answer")

    def test_the_signing_key_and_command_tokens_stay_in_the_meta_table(self):
        store = self.store()
        Overrides(store).sign_state("setmessage", ADMIN_ID)
        keys = {row[0] for row in store.db.execute("SELECT key FROM meta")}
        secret = store.get_meta("secret")
        for table in ("reminders", "history"):
            rows = json.dumps(store.db.execute(f"SELECT * FROM {table}").fetchall())
            self.assertNotIn(secret, rows)
        self.assertIn("secret", keys)
        store.close()


class StateFile(CommandsBase):
    HOSTILE = ["'; DROP TABLE reminders; --", 'x"y', "a\x00b", "é" * 400, "😀", "\\", "%s %d {} {0}",
               "Robert'); DROP TABLE history;--", "\n", "‮", " ", "x" * 5000]

    def test_the_state_file_is_private_to_its_owner(self):
        """It holds names and card titles, and the signing key: nobody else on the machine may read it."""
        Store(self.state_path).close()
        mode = stat.S_IMODE(self.state_path.stat().st_mode)
        self.assertEqual(mode & 0o077, 0, f"mode is {oct(mode)}")

    def test_a_file_that_was_world_readable_is_tightened_when_opened(self):
        Store(self.state_path).close()
        self.state_path.chmod(0o644)
        Store(self.state_path).close()
        self.assertEqual(stat.S_IMODE(self.state_path.stat().st_mode) & 0o077, 0)

    def test_hostile_text_is_data_never_sql(self):
        store = self.store()
        for n, text in enumerate(self.HOSTILE):
            day = f"2026-10-{10 + n:02d}"
            store.claim(text, text, day, 7)
            store.confirm(text, text, day, 7)
            store.record_history(card_id=text, card_title=text, board_title=text, user_id=text, username=text,
                                 due_date=day, days=7)
        tables = {row[0] for row in store.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual({"reminders", "history", "meta"} & tables, {"reminders", "history", "meta"})
        self.assertEqual(store.db.execute("SELECT count(*) FROM reminders").fetchone()[0], len(self.HOSTILE))
        for n, text in enumerate(self.HOSTILE):
            self.assertEqual(store.sent_stages(text, text, f"2026-10-{10 + n:02d}"), {7}, repr(text[:20]))
        titles = {row["card_title"] for row in store.recent_history(100)}
        self.assertEqual(titles, {text[:300] for text in self.HOSTILE})     # the history keeps at most 300 characters
        store.close()

    def test_no_sql_is_built_from_text(self):
        """Every statement is a constant, or an f-string that only inserts the (constant) stage column."""
        executes = 0
        for path in sorted(PACKAGE.rglob("*.py")):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "execute":
                    executes += 1
                    self.assertEqual(path.name, "store.py", "SQL belongs in store.py")
                    statement = node.args[0]
                    if isinstance(statement, ast.JoinedStr):
                        inserted = {ast.unparse(v.value) for v in statement.values if isinstance(v, ast.FormattedValue)}
                        self.assertLessEqual(inserted, {"self._days", "extra"}, f"{path.name}:{node.lineno}: {inserted}")
                    else:
                        self.assertIsInstance(statement, (ast.Constant, ast.Name),
                                              f"{path.name}:{node.lineno}: built at run time")
        self.assertGreater(executes, 15)
        # `extra` (an optional " AND user_id = ?") may only ever be a constant string
        source = ast.parse((PACKAGE / "store.py").read_text(encoding="utf-8"))
        for node in ast.walk(source):
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "extra" for t in node.targets):
                self.assertIsInstance(node.value, ast.Constant)
                self.assertNotIn("%", node.value.value)

    def test_a_state_file_written_by_a_newer_version_still_works_and_is_left_intact(self):
        """A rollback after an upgrade: the extra tables and columns of the newer version are not ours to touch."""
        store = Store(self.state_path)
        store.close()
        db = sqlite3.connect(self.state_path)
        db.execute("ALTER TABLE reminders ADD COLUMN channel TEXT")
        db.execute("ALTER TABLE history ADD COLUMN extra TEXT")
        db.execute("CREATE TABLE future (k TEXT, v TEXT)")
        db.execute("INSERT INTO future VALUES ('a', 'b')")
        db.commit()
        db.close()
        store = Store(self.state_path)
        self.assertEqual(store.claim("c1", "u1", "2026-10-20", 7), "new")
        store.confirm("c1", "u1", "2026-10-20", 7)
        store.record_history(card_id="c1", card_title="t", board_title="b", user_id="u1", username="x",
                             due_date="2026-10-20", days=7)
        store.prune()
        self.assertEqual(store.sent_stages("c1", "u1", "2026-10-20"), {7})
        self.assertEqual(len(store.recent_history()), 1)
        store.close()
        db = sqlite3.connect(self.state_path)
        self.assertEqual(db.execute("SELECT * FROM future").fetchall(), [("a", "b")])
        db.close()

    def test_a_store_that_cannot_be_opened_does_not_keep_the_file_open(self):
        self.state_path.write_bytes(b"this is not a database " * 200)
        opened, real = [], sqlite3.connect

        def recording(*args, **kwargs):
            connection = real(*args, **kwargs)
            opened.append(connection)
            return connection
        with mock.patch.object(sqlite3, "connect", recording):
            for readonly in (False, True):
                with self.assertRaises(sqlite3.DatabaseError):
                    Store(self.state_path, readonly=readonly)
        self.assertEqual(len(opened), 2)
        for connection in opened:
            with self.assertRaises(sqlite3.ProgrammingError, msg="the connection was left open"):
                connection.execute("SELECT 1")                    # "Cannot operate on a closed database"

    def test_a_damaged_state_file_is_never_overwritten_or_deleted(self):
        garbage = bytes(random.Random(7).randrange(256) for _ in range(4096))
        self.state_path.write_bytes(garbage)
        with self.assertRaises(sqlite3.DatabaseError):
            Store(self.state_path)
        self.assertEqual(self.state_path.read_bytes(), garbage)
        self.simple_board("b1", member=True)
        self.fake.add_card("b1", "c1", "Workshop", due_ms=ti.in_days(5), notifyees=[U1], remind="7")
        with self.assertRaises(sqlite3.DatabaseError):
            self.go(store=Store(self.state_path))
        self.assertEqual(self.fake.posts, [], "nothing may be sent when duplicates can no longer be prevented")
        self.assertEqual(self.state_path.read_bytes(), garbage)


class ResourceLeaks(CommandsBase):
    """A daemon runs for months. What it opens per request must be closed again."""

    @staticmethod
    def open_files() -> int:
        return len(os.listdir("/proc/self/fd"))

    def settle(self, threads: int):
        deadline = time.time() + 10
        while threading.active_count() > threads and time.time() < deadline:
            time.sleep(0.05)
        gc.collect()

    @unittest.skipUnless(os.path.isdir("/proc/self/fd"), "needs /proc")
    def test_thousands_of_commands_and_passes_leave_no_threads_or_files_behind(self):
        self.simple_board("b1", member=True)
        self.fake.add_card("b1", "c1", "Workshop", due_ms=ti.in_days(5), notifyees=[U1, U2], remind="7;3;1")
        router = CommandRouter(self.cfg, clients=self.commands.clients, open_store=self.commands.open_store,
                               lock=self.lock, now_fn=lambda: self.now)         # real threads for the slow ones
        mix = [("remind_help", ""), ("remind_status", ""), ("remind_history", "3"), ("remind_boards", ""),
               ("remind_pause", ""), ("remind_resume", ""), ("remind_next", "5"), ("remind_card", "Workshop")]

        def exercise(rounds):
            for n in range(rounds):
                trigger, args = mix[n % len(mix)]
                router.handle_command(self.form(trigger, args))
                router.handle_command(self.form(trigger, args, token="wrong"))                     # refused
                router.handle_command(self.form(trigger, args, user=U1, username="alice"))         # refused
            self.go(today=TODAY)

        exercise(8)                                                                  # warm-up: caches, first threads
        self.settle(threading.active_count())
        threads, files = threading.active_count(), self.open_files()
        exercise(200)
        self.settle(threads)
        self.assertLessEqual(threading.active_count(), threads, "threads piled up")
        self.assertLessEqual(self.open_files(), files + 2, "files or sockets were left open")

    def test_an_error_answer_from_mattermost_is_closed_at_once(self):
        """CPython closes a forgotten response by itself (3.13 and newer even warn), which hides the leak."""
        for kind, extra in (("status", {"status": 500, "body": "boom"}), ("status", {"status": 401, "body": "nope"}),
                            ("status", {"status": 404, "body": "{}"}), ("redirect", {})):
            with self.subTest(kind=kind, **extra):
                self.fake.faults[:] = [Fault(lambda m, p: True, kind, times=1, **extra)]
                with self.assertRaises(ApiError) as caught:
                    self.mm.api._once("GET", "/users/me", None)
                answer = caught.exception.__cause__
                self.assertIsInstance(answer, urllib.error.HTTPError)
                self.assertTrue(answer.file.closed, f"the HTTP {answer.code} answer was left open")


HOSTILE_TEXT = [
    "[x](http://evil.example)", "@all", "@channel @here", "~town-square", "![img](http://evil.example/x.png)",
    "```\nblock\n```", "# heading", "> quote", "- list", "| a | b |", "http://evil.example", "https://evil.example/p?x=1",
    "www.evil.example", "\u202eRTL", "a\u200bb", "\x00nul", "<script>alert(1)</script>", "[a](b) [c](d)",
    "$responsible_name ${card_url} $$", "%s %d {0}", "&lt;b&gt;", "emoji :skull:", "😀" * 50, "x" * 500, "\\",
    "*bold* _it_ ~~strike~~", "[[x]]", "\r\n\r\n", "\t", "'; DROP TABLE reminders; --", "javascript:alert(1)",
]


class HostileDataInTheMessage(ti.Base):
    """Cards, boards and names are typed by anybody with edit rights; the message carries the BOT'S authority."""

    LEGIT_LINK = re.compile(r"\[Open the card\]\(https://chat\.test/boards/[^)\s]*\)")

    def message_for(self, title="Workshop", board="Posts", nickname="", other=""):
        self.fake.posts.clear()
        self.state_path.unlink(missing_ok=True)
        self.fake.boards["team1"].clear()
        self.fake.cards.clear()
        self.fake.members.clear()
        self.fake.users[U1]["nickname"] = nickname
        self.fake.users[U2]["nickname"] = other
        self.fake.add_board("b1", board, member=True)
        self.fake.add_card("b1", "c1", title, due_ms=ti.in_days(5), notifyees=[U1, U2], remind="7;3;1")
        self.go()
        return next(p["message"] for p in self.fake.posts if p["user_id"] == U1)

    def test_nothing_hostile_survives_in_any_part_of_the_message(self):
        baseline = self.message_for(nickname="Ana", other="Rui")
        for evil in HOSTILE_TEXT:
            for where in ("title", "board", "nickname", "co-notifyee"):
                with self.subTest(text=evil[:25], where=where):
                    kwargs = {"title": "Workshop", "board": "Posts", "nickname": "Ana", "other": "Rui"}
                    kwargs[{"title": "title", "board": "board", "nickname": "nickname", "co-notifyee": "other"}[where]] = evil
                    message = self.message_for(**kwargs)
                    outside = self.LEGIT_LINK.sub("", message)
                    self.assertEqual(self.LEGIT_LINK.search(message) is not None, True, "our own link must be intact")
                    self.assertNotIn("](", outside, "a link was injected")
                    self.assertNotIn("![", outside, "an image was injected")
                    self.assertNotIn("```", outside, "a code block was injected")
                    self.assertEqual(outside.count("://"), 0, "a clickable address was injected")
                    self.assertIsNone(re.search(r"(?i)\bwww\.", outside), "a bare address was injected")
                    self.assertIsNone(re.search(r"@[A-Za-z0-9_.-]", outside), "a mention was injected")
                    self.assertIsNone(re.search(r"~[A-Za-z0-9_-]", outside), "a channel mention was injected")
                    self.assertEqual(message.count("\n"), baseline.count("\n"), "extra lines (headings, lists, quotes) were injected")
                    self.assertNotIn("\x00", message)
                    self.assertNotIn("\u202e", message)
                    self.assertLess(len(message), 3000)


class CommandFuzz(CommandsBase):
    """Whatever an administrator (or somebody who stole an administrator's session) types after a command."""

    ARGS = ["", " ", "-1", "0", "999999999999999999999", "∞", "<a|b>", "@all", "`code`", "'; DROP TABLE reminders; --",
            "../../etc/passwd", "\x00", "x" * 10000, "total=1 person=1", "person=abc", "on off", "message time caps all",
            "7;3;1", "12:30", "25:61", "🔥", "\n\n", "%s%s%s", "{0}", "$card_title", "[x](http://evil.example)",
            "Workshop @alice", "@", "total=", "=5", "1 2 3 4", "-- --", "a" * 300 + " @bob"]

    def test_random_arguments_never_break_a_command_or_its_reply(self):
        self.simple_board("b1", member=True)
        self.fake.add_card("b1", "c1", "Workshop", due_ms=ti.in_days(5), notifyees=[U1, U2], remind="7;3;1")
        rng = random.Random(99)
        with self.assertNoLogs("reminder", level="ERROR"):
            for spec in SPECS:
                for n in range(14):
                    args = rng.choice(self.ARGS) if n % 2 == 0 else " ".join(rng.choice(self.ARGS) for _ in range(3))
                    with self.subTest(command=spec.trigger, args=args[:30]):
                        reply = self.run_command(spec.trigger, args)          # also checks how Mattermost would draw it
                        self.assertIsInstance(reply, str)
                        self.assertNotIn("Unexpected error", reply, "a handler raised something it should have handled")
                        self.assertNotIn("Traceback", reply)
        store = self.store()
        self.assertEqual({"reminders", "history", "meta"} - {r[0] for r in store.db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}, set(), "the state was damaged")
        store.close()


class EveryStoreIsClosed(CommandsBase):
    """CPython would close a forgotten SQLite connection by itself, hiding the leak; count them instead."""

    def test_each_command_closes_the_connection_it_opened(self):
        opened, closed = [0], [0]
        init, close = Store.__init__, Store.close

        def counting_init(store, *args, **kwargs):
            opened[0] += 1
            init(store, *args, **kwargs)

        def counting_close(store):
            if store.db is not None:
                closed[0] += 1
            close(store)
        self.simple_board("b1", member=True)
        self.fake.add_card("b1", "c1", "Workshop", due_ms=ti.in_days(5), notifyees=[U1], remind="7;3;1")
        with mock.patch.object(Store, "__init__", counting_init), mock.patch.object(Store, "close", counting_close):
            for spec in SPECS:
                for token in (None, "wrong"):                                   # served, and refused
                    self.commands.handle_command(self.form(spec.trigger, COMMAND_ARGS.get(spec.trigger, ""),
                                                           **({"token": token} if token else {})))
                self.commands.handle_command(self.form(spec.trigger, user=U1, username="alice"))     # not an administrator
            self.commands.handle_dialog({"callback_id": "setmessage", "state": "forged", "user_id": ADMIN_ID})
            self.commands.handle_dialog({"callback_id": "nope", "user_id": ADMIN_ID})
        self.assertGreater(opened[0], 30)
        self.assertEqual(closed[0], opened[0], "a connection to the state file was left open")


class ReplySize(CommandsBase):
    """Mattermost refuses a post above 16383 characters; a reply must fit however much data there is."""

    def assert_fits(self, text: str, what: str):
        self.assertLessEqual(len(text), MAX_REPLY_CHARS + 400, f"{what}: {len(text)} characters")
        self.assertEqual(text.count("```") % 2, 0, f"{what}: a code block was left open")

    def test_a_message_file_of_any_size_is_shown_cut_and_closed(self):
        big = Path(self.tmp.name) / "big.txt"
        big.write_text("Olá $responsible_name\n" + ("linha de texto " * 30 + "\n") * 400 + "```\n$card_title\n", encoding="utf-8")
        self.commands.cfg = self.make_cfg(REMINDER_TEMPLATE_FILE=str(big))
        reply = self.run_command("remind_getmessage")
        self.assert_fits(reply, "/remind_getmessage")
        self.assertIn("cut", reply)

    def test_a_card_with_hundreds_of_notifyees_gets_a_short_reply_everywhere(self):
        people = [uid(f"n{n}") for n in range(400)]
        for n, person in enumerate(people):
            self.fake.add_user(person, f"person{n}")
        self.simple_board("b1", member=True)
        self.fake.add_card("b1", "c1", "Workshop", due_ms=ti.in_days(5), notifyees=people, remind="7;3;1")
        for trigger, args in (("remind_card", "Workshop"), ("remind_next", "30"), ("remind_status", ""),
                              ("remind_boards", ""), ("remind_send", "")):
            with self.subTest(command=trigger):
                self.assert_fits(self.run_command(trigger, args), trigger)

    def test_hundreds_of_cards_get_a_short_reply_everywhere(self):
        self.simple_board("b1", member=True)
        for n in range(150):
            self.fake.add_card("b1", f"c{n}", ("Cartão com um título bastante comprido número %d " % n) * 3,
                               due_ms=ti.in_days(1 + n % 20), notifyees=[U1, U2, U3], remind="30;7;3;1")
        for trigger, args in (("remind_status", ""), ("remind_next", "30"), ("remind_boards", ""),
                              ("remind_send", ""), ("remind_history", "50")):
            with self.subTest(command=trigger):
                self.assert_fits(self.run_command(trigger, args), trigger)

    def test_the_clamp_cuts_at_a_line_closes_a_code_block_and_says_so(self):
        long_table = "| a | b |\n|---|---|\n" + "| row | row |\n" * 5000
        cut = md.fit(long_table)
        self.assert_fits(cut, "a long table")
        self.assertTrue(cut.split("\n\n_")[0].endswith("| row | row |"), "cut in the middle of a row")
        self.assertIn("cut here", cut)
        opened = md.fit("```\n" + "line\n" * 6000)
        self.assert_fits(opened, "an open code block")
        self.assertEqual(md.fit("short"), "short")
        self.assertEqual(md.fit("x" * MAX_REPLY_CHARS), "x" * MAX_REPLY_CHARS, "exactly the limit is not cut")

    def test_a_reply_that_is_too_long_is_clamped_whichever_way_it_travels(self):
        """A handler is free to write too much; neither the direct answer nor the late one may carry it."""
        with mock.patch.object(registry, "_commands", list(registry.commands())):
            @command("remind_flood", summary="Too much.")
            def flood(ctx):
                return "linha\n" * 20000

            @command("remind_slowflood", summary="Too much, late.", slow=True)
            def slowflood(ctx):
                return "linha\n" * 20000
            store = self.store()
            Overrides(store).set_command_tokens({"team1": {"remind_flood": "tok-f", "remind_slowflood": "tok-s"}})
            store.close()
            direct = self.commands.handle_command(self.form("remind_flood", token="tok-f"))["text"]
            self.assert_fits(direct, "the direct answer")
            self.assertIn("cut here", direct)
            self.commands.handle_command(self.form("remind_slowflood", token="tok-s"))
            late = self.fake.delayed[-1]["text"]
            self.assert_fits(late, "the late answer")
            self.assertIn("cut here", late)

    def test_help_and_every_reply_with_nothing_to_show_are_short(self):
        for spec in SPECS:
            with self.subTest(command=spec.trigger):
                self.assert_fits(self.run_command(spec.trigger, COMMAND_ARGS.get(spec.trigger, "")), spec.trigger)


class SlowCommandLimit(CommandsBase):
    """However often an administrator (or a script holding an administrator's session) types a command
    that reads every board, the chat server sees at most three of them at a time."""

    def setUp(self):
        super().setUp()
        self.simple_board("b1", member=True)
        self.fake.add_card("b1", "c1", "Workshop", due_ms=ti.in_days(5), notifyees=[U1], remind="7;3;1")
        self.pending = []
        self.router = CommandRouter(self.cfg, clients=self.commands.clients, open_store=self.commands.open_store,
                                    lock=self.lock, now_fn=lambda: self.now, background=self.pending.append)

    def ask(self, trigger="remind_status", **kw):
        return self.router.handle_command(self.form(trigger, **kw))["text"]

    def test_the_fourth_at_once_is_told_to_wait_and_costs_the_server_nothing(self):
        for _ in range(3):
            self.assertIn("Working on", self.ask())
        scans_before = len([r for r in self.fake.requests if "/boards/" in r[1] and r[0] == "GET"])
        refused = self.ask()
        self.assertIn("already running", refused)
        self.assertEqual(len(self.pending), 3, "a fourth scan was started")
        self.assertEqual(len([r for r in self.fake.requests if "/boards/" in r[1] and r[0] == "GET"]), scans_before)
        self.assertIn("Reminder bot commands", self.ask("remind_help"), "the quick commands must stay available")
        for run in self.pending:                                          # the three finish ...
            run()
        self.assertEqual(len(self.fake.delayed), 3)
        self.assertIn("Working on", self.ask())                           # ... and room is free again

    def test_a_slot_is_given_back_even_when_the_answer_cannot_be_delivered(self):
        for _ in range(3):
            self.ask()
        self.fake.faults.append(Fault(lambda m, p: p.startswith("/hooks/commands/"), "status", status=500))
        for run in list(self.pending):
            run()                                                         # the late answers fail: logged, not fatal
        self.pending.clear()
        self.fake.faults.clear()
        for _ in range(3):
            self.assertIn("Working on", self.ask(), "a slot was lost to a failed delivery")

    def test_a_slot_is_given_back_when_the_command_cannot_even_be_started(self):
        def failing(fn):
            raise RuntimeError("no thread")
        self.router.background = failing
        for _ in range(5):
            with self.assertRaises(RuntimeError):
                self.ask()
        self.router.background = self.pending.append
        for _ in range(3):
            self.assertIn("Working on", self.ask())

    def test_a_refused_caller_never_takes_a_slot(self):
        for _ in range(10):
            self.router.handle_command(self.form("remind_status", user=U1, username="alice"))
            self.router.handle_command(self.form("remind_status", token="wrong"))
        self.assertEqual(self.pending, [])
        for _ in range(3):
            self.assertIn("Working on", self.ask())


class WebhookAbuse(CommandsBase):
    """The chat endpoint is reachable by everything on the Docker network: it must shrug off abuse."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(webhook_server, "REQUEST_TIMEOUT_S", 2.0)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.server = start_server(self.commands, "127.0.0.1", 0)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"

    def raw(self, payload: bytes, wait: float = 3.0) -> bytes:
        with socket.create_connection(("127.0.0.1", self.port), timeout=wait) as sock:
            sock.sendall(payload)
            sock.shutdown(socket.SHUT_WR)
            data = b""
            try:
                while chunk := sock.recv(65536):
                    data += chunk
            except OSError:
                pass
            return data

    def healthy(self):
        with urllib.request.urlopen(self.base + "/healthz", timeout=5) as resp:
            self.assertEqual(json.loads(resp.read()), "ok")

    def test_idle_connections_are_dropped_and_do_not_block_anybody(self):
        idle = [socket.create_connection(("127.0.0.1", self.port)) for _ in range(60)]
        self.addCleanup(lambda: [s.close() for s in idle])
        started = time.time()
        self.healthy()
        self.assertLess(time.time() - started, 1.5, "a valid request waited behind idle connections")
        time.sleep(2.7)
        for sock in idle:
            sock.settimeout(2)
            self.assertEqual(sock.recv(10), b"", "the server should have hung up on a silent client")

    def test_more_connections_than_the_limit_are_hung_up_on_and_no_thread_is_started_for_them(self):
        self.server.shutdown()
        self.server.server_close()
        with mock.patch.object(webhook_server, "MAX_CONNECTIONS", 5):
            server = start_server(self.commands, "127.0.0.1", 0)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        port = server.server_address[1]
        threads = threading.active_count()
        sockets = [socket.create_connection(("127.0.0.1", port)) for _ in range(20)]
        self.addCleanup(lambda: [sock.close() for sock in sockets])
        time.sleep(0.4)
        hung_up = 0
        for sock in sockets:
            sock.settimeout(0.2)
            try:
                hung_up += sock.recv(1) == b""
            except OSError:
                pass
        self.assertGreaterEqual(hung_up, 15, "the extra connections should have been refused at once")
        self.assertLessEqual(threading.active_count(), threads + 6, "a thread was started for every connection")
        time.sleep(2.7)                                                    # the idle ones time out ...
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=5) as resp:   # ... and room is free again
            self.assertEqual(json.loads(resp.read()), "ok")

    def test_a_body_that_never_finishes_is_dropped_without_a_stack_trace_in_the_log(self):
        with self.assertNoLogs("reminder", level="ERROR"):
            with socket.create_connection(("127.0.0.1", self.port), timeout=5) as sock:
                sock.sendall(b"POST /command HTTP/1.1\r\nHost: x\r\nContent-Length: 5000\r\n\r\nabc")
                time.sleep(2.8)
            self.healthy()

    def test_odd_requests_get_an_answer_and_never_a_crash(self):
        cases = {
            "chunked": b"POST /command HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n5\r\ncommand\r\n0\r\n\r\n",
            "put": b"PUT /command HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\n\r\n",
            "delete": b"DELETE /command HTTP/1.1\r\nHost: x\r\n\r\n",
            "options": b"OPTIONS * HTTP/1.1\r\nHost: x\r\n\r\n",
            "negative length": b"POST /command HTTP/1.1\r\nHost: x\r\nContent-Length: -5\r\n\r\n",
            "text length": b"POST /command HTTP/1.1\r\nHost: x\r\nContent-Length: abc\r\n\r\n",
            "huge length": b"POST /command HTTP/1.1\r\nHost: x\r\nContent-Length: 99999999999\r\n\r\n",
            "long path": b"GET /" + b"a" * 100000 + b" HTTP/1.1\r\nHost: x\r\n\r\n",
            "many headers": b"GET /healthz HTTP/1.1\r\nHost: x\r\n" + b"X-A: b\r\n" * 500 + b"\r\n",
            "not http": b"\x16\x03\x01\x02\x00\x01\x00\x01\xfc\x03\x03" + bytes(range(200)),
            "no path": b"POST HTTP/1.1\r\n\r\n",
            "traversal": b"GET /../../etc/passwd HTTP/1.1\r\nHost: x\r\n\r\n",
        }
        with self.assertNoLogs("reminder", level="ERROR"):
            for name, payload in cases.items():
                with self.subTest(request=name):
                    answer = self.raw(payload)
                    if answer.startswith(b"HTTP/"):
                        status = int(answer.split(b" ", 2)[1])
                        self.assertIn(status, (200, 400, 404, 405, 413, 414, 431, 501, 505), (name, answer[:80]))
                    elif answer:       # a request line that is not HTTP/1: the stdlib answers with its plain error page
                        self.assertIn(b"Error response", answer, (name, answer[:80]))
            self.healthy()

    def test_random_junk_never_breaks_the_endpoint_or_produces_a_stack_trace(self):
        rng = random.Random(2026)
        alphabet = "abcdef0123456789&=%+{}[]\"'<>|\\/ \n\x00é😀"
        with self.assertNoLogs("reminder", level="ERROR"):
            for n in range(300):
                body = "".join(rng.choice(alphabet) for _ in range(rng.randrange(0, 400))).encode("utf-8", "replace")
                path = rng.choice(["/command", "/dialog"])
                ctype = rng.choice(["application/x-www-form-urlencoded", "application/json", "text/plain", ""])
                request = urllib.request.Request(self.base + path, data=body, method="POST",
                                                 headers={"Content-Type": ctype} if ctype else {})
                try:
                    with urllib.request.urlopen(request, timeout=10) as resp:
                        status, answer = resp.status, resp.read()
                except urllib.error.HTTPError as exc:
                    with exc:
                        status, answer = exc.code, exc.read()
                self.assertIn(status, (200, 400), (n, status, body[:60]))
                self.assertIsInstance(json.loads(answer or b"null"), (dict, type(None)), (n, answer[:80]))
            self.healthy()

    def test_forged_commands_over_http_do_nothing(self):
        before = self.fake.requests[:]
        for form in (self.form("remind_pause", token=""), self.form("remind_pause", token="wrong"),
                     self.form("remind_pause", user="../../x", token="tok-remind_pause"),
                     self.form("remind_send", token="tok-remind_pause"), self.form("remind_pause", team="elsewhere")):
            request = urllib.request.Request(self.base + "/command", method="POST",
                                             data=urllib.parse.urlencode(form).encode())
            with urllib.request.urlopen(request, timeout=10) as resp:
                self.assertIn("ephemeral", resp.read().decode())
        self.assertIsNone(self.settings().paused())
        self.assertEqual(self.fake.posts, [])
        self.assertEqual([r for r in self.fake.requests[len(before):] if r[0] != "GET"], [])


if __name__ == "__main__":
    unittest.main()
