"""The bot as REAL operating-system processes, against the fake Mattermost over real HTTP.

Everything else in the suite runs the bot inside the test's own process. These start
`python -m remindabot` the way the container does, and check what only a real process can show:

  * several processes at once (a `compose run --once` while the daemon is up) never send twice;
  * a process killed with SIGKILL in the middle of a message loses nothing and duplicates nothing;
  * SIGTERM stops the daemon promptly and frees its port; it answers commands over real HTTP;
  * a damaged or unwritable state file is a clean stop with a message, never a traceback or an overwrite;
  * no secret reaches the output of a process, however the server misbehaves;
  * a bad configuration is exit code 2, whatever is wrong, never a traceback.
"""
import hashlib
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

from . import test_integration as ti
from .support.fakeserver import BOT_TOKEN, Fault, noon_utc_ms
from .test_commands import CommandsBase

ROOT = Path(__file__).resolve().parent.parent
POSTS = lambda method, path: method == "POST" and path.endswith("/api/v4/posts")        # noqa: E731
U1, U2, U3 = ti.U1, ti.U2, ti.U3


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class ProcessCase(CommandsBase):
    def env(self, **extra) -> dict:
        env = {k: v for k, v in os.environ.items() if not k.startswith("REMINDER_")}
        env.update({
            "PYTHONPATH": str(ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1",
            "REMINDER_MM_URL": self.url, "REMINDER_SITE_URL": "https://chat.test", "REMINDER_BOT_TOKEN": BOT_TOKEN,
            "REMINDER_TEMPLATE_FILE": str(ti.SHIPPED_TEMPLATE), "REMINDER_STATE_FILE": str(self.state_path),
            "REMINDER_HEARTBEAT_FILE": str(Path(self.tmp.name) / "heartbeat"), "REMINDER_TZ": "Europe/Lisbon",
            "REMINDER_COMMANDS_PORT": "0",
        })
        env.update({k: v for k, v in extra.items() if v is not None})
        for key in [k for k, v in extra.items() if v is None]:
            env.pop(key, None)
        return env

    def start(self, *args, **extra) -> subprocess.Popen:
        proc = subprocess.Popen([sys.executable, "-m", "remindabot", *args], cwd=ROOT, env=self.env(**extra),
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.addCleanup(self.reap, proc)
        return proc

    @staticmethod
    def reap(proc: subprocess.Popen):
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        if proc.stdout:
            proc.stdout.close()

    def run_bot(self, *args, timeout=120, **extra) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-m", "remindabot", *args], cwd=ROOT, env=self.env(**extra),
                              capture_output=True, text=True, timeout=timeout)

    @staticmethod
    def due(days: int) -> int:
        """A due date `days` from the REAL today (the processes use the real clock)."""
        return noon_utc_ms(datetime.now(ti.LISBON).date() + timedelta(days=days))

    def wait_for(self, condition, what: str, seconds: float = 20.0):
        deadline = time.time() + seconds
        while time.time() < deadline:
            if condition():
                return
            time.sleep(0.05)
        self.fail(f"gave up waiting for {what}")

    def statuses(self) -> list[str]:
        db = sqlite3.connect(self.state_path)
        try:
            return [row[0] for row in db.execute("SELECT status FROM reminders")]
        finally:
            db.close()

    def age_the_claims(self):
        """As if the processes had died an hour ago: their unfinished claims are now fair game."""
        db = sqlite3.connect(self.state_path)
        db.execute("UPDATE reminders SET updated_at = updated_at - 3600")
        db.commit()
        db.close()

    def per_user(self) -> dict:
        counts: dict = {}
        for post in self.fake.posts:
            counts[post["user_id"]] = counts.get(post["user_id"], 0) + 1
        return counts


class Concurrency(ProcessCase):
    def test_several_processes_at_once_send_each_message_exactly_once(self):
        self.simple_board("b1", member=True)
        for n in range(6):
            self.fake.add_card("b1", f"c{n}", f"Workshop {n}", due_ms=self.due(5), notifyees=[U1, U2, U3], remind="7;3;1")
        processes = [self.start("--once") for _ in range(6)]
        outputs = [p.communicate(timeout=180)[0] for p in processes]
        self.assertEqual([p.returncode for p in processes], [0] * 6, "\n".join(outputs))
        self.assertEqual(len(self.fake.posts), 18, "6 cards x 3 people, once each")
        self.assertEqual(len({(p["user_id"], p["message"]) for p in self.fake.posts}), 18, "a message was sent twice")
        self.assertEqual(set(self.statuses()), {"sent"})


class Killed(ProcessCase):
    def setUp(self):
        super().setUp()
        self.simple_board("b1", member=True)
        self.fake.add_card("b1", "c1", "Workshop", due_ms=self.due(5), notifyees=[U1, U2], remind="7;3;1")

    def kill_while_sending(self, kind: str):
        self.fake.faults.append(Fault(POSTS, kind, times=1, delay=30))
        proc = self.start("--once")
        self.wait_for(lambda: any(POSTS(m, p) for m, p in self.fake.requests), "the first message to be in flight")
        proc.kill()                                                  # SIGKILL: no cleanup of any kind
        proc.wait(timeout=10)

    def rerun_and_check(self):
        self.age_the_claims()
        again = self.run_bot("--once")
        self.assertEqual(again.returncode, 0, again.stdout)
        self.assertEqual(self.per_user(), {U1: 1, U2: 1}, "each person exactly once after the crash")
        self.assertEqual(set(self.statuses()), {"sent"})
        third = self.run_bot("--once")
        self.assertEqual(third.returncode, 0)
        self.assertEqual(self.per_user(), {U1: 1, U2: 1}, "and nothing more on the next run")

    def test_killed_before_the_message_arrived_nothing_is_lost(self):
        self.kill_while_sending("hang")
        self.assertEqual(self.fake.posts, [], "the server never got it")
        self.assertIn("pending", self.statuses())
        self.rerun_and_check()

    def test_killed_after_the_message_arrived_it_is_not_sent_twice(self):
        self.kill_while_sending("commit_then_slow")
        self.assertEqual(len(self.fake.posts), 1, "the server did get it")
        self.assertIn("pending", self.statuses())
        self.rerun_and_check()

    def test_a_claim_that_is_not_yet_stale_is_left_alone(self):
        """Another process may still be sending it: a second one must wait, not send."""
        self.kill_while_sending("hang")
        again = self.run_bot("--once")                               # the claim is only seconds old
        self.assertEqual(again.returncode, 0, again.stdout)
        self.assertLessEqual(len(self.fake.posts), 1)                # at most the one that was not claimed


class Daemon(ProcessCase):
    def boot(self, **extra) -> tuple[subprocess.Popen, str]:
        port = free_port()
        proc = self.start(REMINDER_COMMANDS_PORT=str(port), REMINDER_COMMANDS_BIND="127.0.0.1",
                          REMINDER_DAILY_PASS="false", **extra)
        base = f"http://127.0.0.1:{port}"

        def up():
            try:
                with urllib.request.urlopen(base + "/healthz", timeout=1):
                    return True
            except OSError:
                return proc.poll() is not None
        self.wait_for(up, "the daemon to answer")
        self.assertIsNone(proc.poll(), "the daemon died at start")
        return proc, base

    def post(self, base: str, form: dict) -> dict:
        request = urllib.request.Request(base + "/command", data=urllib.parse.urlencode(form).encode(), method="POST")
        with urllib.request.urlopen(request, timeout=15) as resp:
            import json
            return json.loads(resp.read())

    def test_sigterm_stops_it_promptly_and_frees_the_port(self):
        proc, base = self.boot()
        self.assertTrue((Path(self.tmp.name) / "heartbeat").exists(), "the health check would call it dead")
        started = time.time()
        proc.send_signal(signal.SIGTERM)
        output = proc.communicate(timeout=8)[0]
        self.assertLess(time.time() - started, 5, "docker stop would have to kill it")
        self.assertEqual(proc.returncode, 0, output)
        self.assertNotIn("Traceback", output)
        with self.assertRaises(OSError):
            urllib.request.urlopen(base + "/healthz", timeout=2)

    def test_it_answers_commands_over_real_http_and_the_late_answers_arrive(self):
        self.simple_board("b1", member=True)
        self.fake.add_card("b1", "c1", "Workshop", due_ms=self.due(5), notifyees=[U1], remind="7;3;1")
        proc, base = self.boot()
        self.assertIn("Reminder bot commands", self.post(base, self.form("remind_help"))["text"])
        self.assertIn("Only system administrators",
                      self.post(base, self.form("remind_help", user=U1, username="alice"))["text"])
        self.assertIn("wrong token", self.post(base, self.form("remind_help", token="nope"))["text"])
        first = self.post(base, self.form("remind_status"))                     # slow: "working", then the answer
        self.assertIn("Working on", first["text"])
        self.wait_for(lambda: any("Reminder bot status" in (d or {}).get("text", "") for d in self.fake.delayed),
                      "the late answer of /remind_status")
        self.post(base, self.form("remind_setmessage"))
        self.wait_for(lambda: self.fake.dialogs, "the pop-up form to be opened")
        proc.send_signal(signal.SIGTERM)
        self.assertEqual(proc.wait(timeout=8), 0)

    def test_commands_that_change_things_are_remembered_by_the_next_process(self):
        proc, base = self.boot()
        self.post(base, self.form("remind_pause"))
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=8)
        self.simple_board("b1", member=True)
        self.fake.add_card("b1", "c1", "Workshop", due_ms=self.due(5), notifyees=[U1], remind="7;3;1")
        sent = self.run_bot("--once")                                           # a different process, later
        self.assertEqual(sent.returncode, 0)
        self.assertIn("PAUSED", sent.stdout)
        self.assertEqual(self.fake.posts, [])


class DamagedState(ProcessCase):
    def setUp(self):
        super().setUp()
        self.simple_board("b1", member=True)
        self.fake.add_card("b1", "c1", "Workshop", due_ms=self.due(5), notifyees=[U1], remind="7;3;1")
        self.garbage = bytes((n * 37 + 11) % 256 for n in range(5000))
        self.state_path.write_bytes(self.garbage)

    def assert_clean_stop(self, result, what):
        output = result.stdout + result.stderr if hasattr(result, "stderr") else result
        self.assertEqual(result.returncode, 1, f"{what}: {output}")
        self.assertNotIn("Traceback", output, what)
        self.assertIn("state file", output, what)
        self.assertEqual(self.state_path.read_bytes(), self.garbage, f"{what}: the file was changed")
        self.assertEqual(self.fake.posts, [], f"{what}: it sent something")

    def test_a_pass_stops_cleanly(self):
        self.assert_clean_stop(self.run_bot("--once"), "--once")

    def test_a_check_stops_cleanly(self):
        self.assert_clean_stop(self.run_bot("--check"), "--check")

    def test_the_daemon_stops_cleanly_instead_of_crashing(self):
        self.assert_clean_stop(self.run_bot("--once", REMINDER_COMMANDS_PORT="0", timeout=60), "--once")
        proc = self.start(REMINDER_DAILY_PASS="false")
        output = proc.communicate(timeout=20)[0]
        self.assertEqual(proc.returncode, 1, output)
        self.assertNotIn("Traceback", output)
        self.assertEqual(self.state_path.read_bytes(), self.garbage)

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores directory permissions")
    def test_an_unwritable_state_directory_is_a_clean_stop(self):
        locked = Path(self.tmp.name) / "locked"
        locked.mkdir()
        self.addCleanup(locked.chmod, 0o700)
        locked.chmod(0o500)
        result = self.run_bot("--once", REMINDER_STATE_FILE=str(locked / "state.sqlite"))
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertNotIn("Traceback", result.stdout + result.stderr)
        self.assertEqual(self.fake.posts, [])


class NoSecretsInOutput(ProcessCase):
    def test_nothing_secret_is_printed_whatever_the_server_does(self):
        self.simple_board("b1", member=True)
        self.fake.add_card("b1", "c1", "Workshop", due_ms=self.due(5), notifyees=[U1], remind="7;3;1")
        secrets = [BOT_TOKEN, self.fake.admin_token]
        for kind, extra in (("echo_auth", {"status": 400}), ("status", {"status": 401}), ("drop", {}),
                            ("html", {}), ("redirect", {})):
            self.fake.faults[:] = [Fault(lambda m, p: True, kind, **extra)]
            for args in (("--once",), ("--check",), ("--once", "--dry-run"), ("--register-commands",),
                         ("--unregister-commands",)):
                with self.subTest(fault=kind, command=" ".join(args)):
                    result = self.run_bot(*args, REMINDER_LOG_LEVEL="DEBUG", REMINDER_ADMIN_TOKEN=self.fake.admin_token)
                    for secret in secrets:
                        self.assertNotIn(secret, result.stdout + result.stderr)
        self.fake.faults.clear()


class DryRun(ProcessCase):
    def snapshot(self) -> dict:
        found = {}
        for path in sorted(Path(self.tmp.name).rglob("*")):
            if path.is_file() and path.name != "heartbeat":
                found[str(path.relative_to(self.tmp.name))] = hashlib.sha256(path.read_bytes()).hexdigest()
        return found

    def test_a_dry_run_leaves_the_disk_exactly_as_it_was(self):
        self.simple_board("b1", member=True)
        self.fake.add_card("b1", "c1", "Workshop", due_ms=self.due(5), notifyees=[U1, U2], remind="7;3;1")
        self.fake.add_board("open", "Not joined", member=False)
        before = self.snapshot()
        result = self.run_bot("--once", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.fake.posts, [])
        self.assertNotIn("/plugins/focalboard/api/v2/boards/open/join", [p for m, p in self.fake.requests if m == "POST"])


class BadConfiguration(ProcessCase):
    def test_every_kind_of_bad_setting_is_exit_code_2_with_a_message_and_no_traceback(self):
        cases = {
            "no token": {"REMINDER_BOT_TOKEN": None},
            "token file missing": {"REMINDER_BOT_TOKEN": None, "REMINDER_BOT_TOKEN_FILE": "/nonexistent/token"},
            "no site url": {"REMINDER_SITE_URL": None},
            "site url without scheme": {"REMINDER_SITE_URL": "chat.example.com"},
            "mm url ftp": {"REMINDER_MM_URL": "ftp://x"},
            "bad timezone": {"REMINDER_TZ": "Mars/Olympus"},
            "bad send time": {"REMINDER_SEND_AT": "9h"},
            "bad port": {"REMINDER_COMMANDS_PORT": "99999"},
            "bad cap": {"REMINDER_MAX_MESSAGES_PER_DAY": "0"},
            "bad bool": {"REMINDER_AUTO_JOIN": "maybe"},
            "no template": {"REMINDER_TEMPLATE_FILE": "/nonexistent/message.txt"},
        }
        for name, change in cases.items():
            with self.subTest(setting=name):
                result = self.run_bot("--once", "--dry-run", **change)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertNotIn("Traceback", result.stdout + result.stderr)
                self.assertIn("ERROR", result.stdout)


if __name__ == "__main__":
    unittest.main()
