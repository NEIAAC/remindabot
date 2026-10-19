"""The shell scripts, the compose files and the documentation around the bot.

They are not part of the bot, but they are how it gets developed, checked and rolled out, and a stale
script is how a "16 checks" run ends with a failure that is the script's own fault (it did, once:
check-image.sh kept calling an old signature). So they get tests too:

  * every script at least parses, and the Python inside check-image.sh really runs;
  * the seed, sandbox-setup and read-only scripts are run against a small stand-in for the Mattermost
    and Boards API (tests/support/stubserver.py), which catches typos and a wrong call order without
    needing Docker;
  * the compose files only publish ports on 127.0.0.1 and set only settings that exist;
  * the documentation names only files that exist and documents every setting the code reads;
  * the repository keeps its rule: no tools in Python (shell only; Python lives in src/ and tests/).
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .support.stubserver import ADMIN_TOKEN, BOT_TOKEN, StubMattermost

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
COMPOSE_FILES = [ROOT / "compose.yaml", ROOT / "dev" / "sandbox" / "compose.yaml"]
DOC_FILES = [ROOT / "README.md", ROOT / "CONTRIBUTING.md", *sorted((ROOT / "docs").glob("*.md")),
             ROOT / "dev" / "sandbox" / "README.md"]
HAVE_TOOLS = all(shutil.which(p) for p in ("bash", "curl", "jq"))


def doc_text() -> str:
    return "\n".join(path.read_text(encoding="utf-8") for path in DOC_FILES)


def shell_files() -> list[Path]:
    return sorted([*SCRIPTS.rglob("*.sh"), *(ROOT / "dev").rglob("*.sh")])


class Layout(unittest.TestCase):
    def test_there_are_no_python_tools(self):
        """Python is the bot (src/) and its tests (tests/). Helpers are shell, so nobody needs a
        Python environment to run the stack, and a maintainer reads one language per job."""
        found = [p for p in ROOT.rglob("*.py")
                 if not {".git", "__pycache__"} & set(p.parts)
                 and p.relative_to(ROOT).parts[0] not in ("src", "tests")]
        self.assertEqual(found, [], "Python outside src/ and tests/: write it as a shell script")

    def test_the_package_is_where_pyproject_says(self):
        text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertRegex(text, r'(?m)^name = "remindabot"$')
        self.assertTrue((ROOT / "src" / "remindabot" / "__main__.py").exists())
        self.assertFalse((ROOT / "app").exists(), "the old package directory is back")


class ScriptsParse(unittest.TestCase):
    def test_every_shell_script_has_valid_syntax(self):
        scripts = shell_files()
        self.assertGreaterEqual(len(scripts), 5)
        for path in scripts:
            with self.subTest(script=str(path.relative_to(ROOT))):
                done = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True)
                self.assertEqual(done.returncode, 0, done.stderr)

    def test_every_script_is_executable_except_the_sourced_library(self):
        for path in shell_files():
            if path.parent.name == "lib":
                continue
            with self.subTest(script=str(path.relative_to(ROOT))):
                self.assertTrue(path.stat().st_mode & 0o100, "not executable (chmod +x)")

    def test_every_script_stops_at_the_first_error(self):
        for path in shell_files():
            if path.parent.name == "lib":
                continue
            with self.subTest(script=str(path.relative_to(ROOT))):
                text = path.read_text(encoding="utf-8")
                self.assertTrue(text.startswith("#!/usr/bin/env bash"), "no bash shebang")
                # check-image.sh runs every check and reports a count, so it cannot stop at the first
                if path.name != "check-image.sh":
                    self.assertIn("set -euo pipefail", text)


class CheckImageScript(unittest.TestCase):
    """check-image.sh runs a few lines of Python INSIDE the image. Run those lines here, for real."""

    def snippets(self) -> list[str]:
        text = (SCRIPTS / "check-image.sh").read_text(encoding="utf-8")
        found = re.findall(r'-c \\\n\s*"(.*?)"(?= (?:2>&1|>/dev/null))', text, flags=re.DOTALL)
        return [snippet.replace("\\\n", "") for snippet in found]

    def run_snippet(self, code: str, state: Path) -> subprocess.CompletedProcess:
        code = code.replace("/data/state.sqlite", str(state))
        env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1"}
        return subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True)

    def test_the_state_snippets_still_match_the_store_api(self):
        snippets = [x for x in self.snippets() if "remindabot.store" in x]   # (the endpoint probe does not use it)
        self.assertEqual(len(snippets), 2, "the script's Python snippets changed shape; update this test")
        with tempfile.TemporaryDirectory() as d:
            state = Path(d) / "state.sqlite"
            first = self.run_snippet(snippets[0], state)           # a claim: must print exactly "new"
            self.assertEqual((first.returncode, first.stdout.strip()), (0, "new"), first.stderr)
            second = self.run_snippet(snippets[1], state)           # marks today's pass as done
            self.assertEqual(second.returncode, 0, second.stderr)
            from remindabot.store import Store
            today = datetime.now(ZoneInfo("Europe/Lisbon")).date().isoformat()
            store = Store(state, readonly=True)
            self.addCleanup(store.close)
            self.assertEqual(store.last_run(), today)

    def test_it_runs_the_module_that_exists(self):
        text = (SCRIPTS / "check-image.sh").read_text(encoding="utf-8")
        self.assertIn("python -m remindabot", text)
        self.assertNotIn("python -m app", text)


@unittest.skipUnless(HAVE_TOOLS, "needs bash, curl and jq")
class DevSeed(unittest.TestCase):
    """scripts/dev-seed.sh against the stand-in: the order of the calls and what it leaves behind."""

    def setUp(self):
        self.stub = StubMattermost().__enter__()
        self.addCleanup(self.stub.__exit__)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        tmp = Path(self.tmp.name)
        self.bundle = tmp / "boards.tar.gz"
        self.bundle.write_bytes(b"x" * 100)                  # a stand-in plugin bundle: never downloaded
        self.bot_dir, self.admin_dir = tmp / "bot", tmp / "admin"

    def seed(self, password="dev-password") -> subprocess.CompletedProcess:
        env = {**os.environ, "MM_URL": self.stub.url, "SECRETS_BOT": str(self.bot_dir),
               "SECRETS_ADMIN": str(self.admin_dir), "BOARDS_BUNDLE": str(self.bundle),
               "DEV_ADMIN_PASSWORD": password}
        return subprocess.run(["bash", str(SCRIPTS / "dev-seed.sh")], env=env, capture_output=True, text=True,
                              timeout=120)

    def test_a_first_run_prepares_everything_and_a_second_run_does_nothing(self):
        st = self.stub.state
        done = self.seed()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual((self.bot_dir / "token").read_text(), BOT_TOKEN)
        self.assertEqual((self.admin_dir / "token").read_text(), ADMIN_TOKEN)
        self.assertEqual(sorted(u for u in st.users if u not in ("devadmin", "remindabot")),
                         ["alice", "bob", "carol"])
        self.assertEqual(len(st.boards), 6)
        # every board got a view (so card links are full links) and the bot joined only the Posts board
        self.assertEqual(sorted(st.views), sorted(st.boards))
        posts = next(i for i, b in st.boards.items() if b["title"] == "Posts")
        self.assertEqual(st.joined, [posts])
        # the file the tests use, and the mark of "already prepared"
        state = json.loads((self.admin_dir / "dev-state.json").read_text())
        self.assertEqual(state["boards"]["text"], posts)
        self.assertEqual(sorted(state["users"]), ["alice", "bob", "carol", "devadmin"])
        before = len(st.calls)
        again = self.seed()
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(len(st.calls), before)               # nothing was called again

    def test_the_seeded_boards_match_what_the_docs_promise(self):
        """Posts is correct; the two wrong-type boards use exactly the types the bot must refuse."""
        self.assertEqual(self.seed().returncode, 0)
        boards = {b["title"]: b for b in self.stub.state.boards.values()}
        remind_types = {title: next(p["type"] for p in b["cardProperties"] if p["name"] == "Remind Before")
                        for title, b in boards.items() if any(p["name"] == "Remind Before" for p in b["cardProperties"])}
        self.assertEqual(remind_types["Posts"], "text")
        self.assertEqual(sorted(t for t in remind_types.values() if t != "text"), ["multiSelect", "number"])
        self.assertEqual(boards["Quadro privado"]["type"], "P")
        self.assertEqual(len(boards), 6)
        posts = [c for c in self.stub.state.cards.values() if c["boardId"] == boards["Posts"]["id"]]
        self.assertEqual(len(posts), 16)
        written = {c["title"].split(" (")[0]: next(iter(
            v for k, v in c["properties"].items() if isinstance(v, str) and not v.startswith("{")), None)
            for c in posts}
        self.assertEqual(written["Workshop de Python"], "7;3;1")
        self.assertEqual(written["Com virgulas"], "7,3,1")
        self.assertEqual(written["Cartaz do evento"], "7; 3; 1")
        # the multi select stores option ids, in the order the demo gives them
        multi = next(c for c in self.stub.state.cards.values()
                     if c["boardId"] == boards["Remind Before como Multipla escolha (tipo errado)"]["id"])
        options = [p for p in boards["Remind Before como Multipla escolha (tipo errado)"]["cardProperties"]
                   if p["name"] == "Remind Before"][0]["options"]
        self.assertEqual(next(v for v in multi["properties"].values() if isinstance(v, list)
                              and v and v[0] in {o["id"] for o in options}),
                         [options[2]["id"], options[0]["id"]])

    def test_the_account_a_person_can_log_in_as_is_a_notifyee_of_a_demo_card(self):
        """Regression: the demo once had only alice, bob and carol, whom nobody can log in as, so the
        person trying the bot saw "nothing arrives". devadmin must be told about one card."""
        self.assertEqual(self.seed().returncode, 0)
        admin_id = self.stub.state.users["devadmin"]["id"]
        cards = self.stub.state.cards.values()
        mine = [c for c in cards if admin_id in next((v for v in c["properties"].values()
                                                     if isinstance(v, list)), [])]
        self.assertEqual([c["title"].split(" (")[0] for c in mine], ["Mensagem para o devadmin"])
        remind = next(v for v in mine[0]["properties"].values() if isinstance(v, str) and not v.startswith("{"))
        self.assertEqual(remind, "7;3;1")
        due = json.loads(next(v for v in mine[0]["properties"].values()
                              if isinstance(v, str) and v.startswith("{")))["from"]
        self.assertTrue(due > 0 and due % 86400000 == 12 * 3600000, "a due date is noon UTC of its day")

    def test_the_due_dates_count_from_today_in_lisbon(self):
        self.assertEqual(self.seed().returncode, 0)
        by_title = {c["title"].split(" (")[0]: c for c in self.stub.state.cards.values()}
        today = datetime.now(ZoneInfo("Europe/Lisbon")).date()

        def due_days(title):
            raw = next(v for v in by_title[title]["properties"].values() if isinstance(v, str) and v.startswith("{"))
            day = datetime.fromtimestamp(json.loads(raw)["from"] / 1000, ZoneInfo("UTC")).date()
            return (day - today).days
        self.assertEqual(due_days("Sessao de fotos"), 3)
        self.assertEqual(due_days("Muito cedo"), 40)
        self.assertEqual(due_days("Ja passou"), -2)

    def test_an_old_stack_with_unknown_admin_gives_instructions_not_a_traceback(self):
        self.stub.state.users["someoneelse"] = {"id": "x" * 26, "username": "someoneelse", "password": "p",
                                                "roles": "system_admin"}
        done = self.seed()
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("docker compose down -v", done.stderr)
        self.assertFalse((self.admin_dir / "dev-state.json").exists())     # not marked as prepared
        self.assertFalse((self.bot_dir / "token").exists())

    def test_a_run_that_stopped_half_way_does_not_duplicate_the_demo_boards(self):
        self.assertEqual(self.seed().returncode, 0)
        (self.admin_dir / "dev-state.json").unlink()                         # as if the last step never ran
        again = self.seed()
        self.assertNotEqual(again.returncode, 0)
        self.assertIn("docker compose down -v", again.stderr)
        self.assertEqual(len(self.stub.state.boards), 6)                     # still one copy

    def test_the_users_the_team_and_the_bot_are_not_created_twice(self):
        self.assertEqual(self.seed().returncode, 0)
        users, teams = dict(self.stub.state.users), dict(self.stub.state.teams)
        (self.admin_dir / "dev-state.json").unlink()
        self.seed()                                                          # refuses at the boards, after those
        self.assertEqual(self.stub.state.users.keys(), users.keys())
        self.assertEqual(self.stub.state.teams.keys(), teams.keys())


@unittest.skipUnless(HAVE_TOOLS, "needs bash, curl and jq")
class SandboxSetup(unittest.TestCase):
    def test_it_creates_the_bot_adds_it_to_every_team_and_prints_only_the_token(self):
        with StubMattermost() as stub, tempfile.TemporaryDirectory() as tmp:
            stub.state.users["sandboxadmin"] = {"id": "s" * 26, "username": "sandboxadmin", "password": "pw",
                                                "roles": "system_admin system_user"}
            stub.state.teams["one"] = {"id": "t" * 26, "name": "one"}
            stub.state.teams["two"] = {"id": "u" * 26, "name": "two"}
            bundle = Path(tmp) / "boards.tar.gz"
            bundle.write_bytes(b"x" * 100)
            env = {**os.environ, "MM_URL": stub.url, "SANDBOX_ADMIN_PASSWORD": "pw", "BOARDS_BUNDLE": str(bundle)}
            done = subprocess.run(["bash", str(SCRIPTS / "sandbox-setup.sh")], env=env, capture_output=True,
                                  text=True, timeout=60)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual(done.stdout.strip(), BOT_TOKEN)         # stdout is the token and nothing else
            members = [p for m, p in stub.state.calls if m == "POST" and p.endswith("/members")]
            self.assertEqual(len(members), 2)                         # the bot joined both teams
            self.assertIn("remindabot", stub.state.users)

    def test_a_wrong_password_stops_with_a_clear_message(self):
        with StubMattermost() as stub:
            stub.state.users["sandboxadmin"] = {"id": "s" * 26, "username": "sandboxadmin", "password": "pw",
                                                "roles": "system_admin"}
            env = {**os.environ, "MM_URL": stub.url, "SANDBOX_ADMIN_PASSWORD": "wrong", "BOARDS_BUNDLE": "/nonexistent"}
            done = subprocess.run(["bash", str(SCRIPTS / "sandbox-setup.sh")], env=env, capture_output=True,
                                  text=True, timeout=60)
            self.assertNotEqual(done.returncode, 0)
            self.assertIn("cannot log in as sandboxadmin", done.stderr)
            self.assertEqual(done.stdout, "")


@unittest.skipUnless(HAVE_TOOLS, "needs bash, curl and jq")
class MakeBotReadOnly(unittest.TestCase):
    def setUp(self):
        self.stub = StubMattermost().__enter__()
        self.addCleanup(self.stub.__exit__)
        st = self.stub.state
        st.users["admin"] = {"id": "a" * 26, "username": "admin", "password": "x", "roles": "system_admin system_user"}
        st.users["remindabot"] = {"id": "b" * 26, "username": "remindabot", "password": "x", "roles": "system_user"}
        st.tokens[ADMIN_TOKEN] = "a" * 26
        st.tokens[BOT_TOKEN] = "b" * 26
        st.plugin = True
        st.teams["nei"] = {"id": "t" * 26, "name": "nei"}
        editor = {"schemeAdmin": False, "schemeEditor": True, "schemeCommenter": False, "schemeViewer": False}
        viewer = {"schemeAdmin": False, "schemeEditor": False, "schemeCommenter": False, "schemeViewer": True}
        for board_id, title, kind, scheme in (("o" * 26, "Open", "O", editor), ("p" * 26, "Private", "P", editor),
                                              ("v" * 26, "Already", "O", viewer), ("n" * 26, "Not mine", "O", None)):
            st.boards[board_id] = {"id": board_id, "title": title, "type": kind, "teamId": "t" * 26}
            st.members[board_id] = {"b" * 26: scheme} if scheme else {}

    def run_script(self, *args, admin_token=ADMIN_TOKEN) -> subprocess.CompletedProcess:
        env = {**os.environ, "REMINDER_BOT_TOKEN": BOT_TOKEN, "ADMIN_TOKEN": admin_token}
        return subprocess.run(["bash", str(SCRIPTS / "make-bot-read-only.sh"), self.stub.url, *args], env=env,
                              capture_output=True, text=True, timeout=60)

    def test_the_plan_lists_editor_boards_and_changes_nothing(self):
        done = self.run_script()
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("member of 3 board(s); 2 still need", done.stdout)
        self.assertIn("EDITOR RIGHTS", done.stdout)
        self.assertIn("Nothing was changed", done.stdout)
        self.assertNotIn("Not mine", done.stdout)                    # a board the bot is not in is not touched
        self.assertFalse([c for c in self.stub.state.calls if c[0] in ("PUT",) or c[1].endswith("/join")])

    def test_apply_joins_as_admin_and_sets_viewer_including_the_private_board(self):
        done = self.run_script("--apply")
        self.assertEqual(done.returncode, 0, done.stderr + done.stdout)
        self.assertIn("2 board(s) changed, 0 failed", done.stdout)
        for board_id in ("o" * 26, "p" * 26):
            scheme = self.stub.state.members[board_id]["b" * 26]
            self.assertEqual((scheme["schemeViewer"], scheme["schemeEditor"], scheme["schemeAdmin"]),
                             (True, False, False))
        self.assertIn("allow_admin=true", " ".join(p for m, p in self.stub.state.calls if m == "POST" and "/join" in p))
        again = self.run_script()
        self.assertIn("0 still need to be made read-only", again.stdout)

    def test_a_board_the_admin_cannot_join_is_reported_and_the_exit_code_says_so(self):
        self.stub.state.deny_join.add("o" * 26)
        done = self.run_script("--apply")
        self.assertEqual(done.returncode, 1)
        self.assertIn("FAILED  Open", done.stdout)
        self.assertIn("1 board(s) changed, 1 failed", done.stdout)

    def test_a_token_that_is_not_an_administrator_stops_at_once(self):
        self.stub.state.tokens["plain-user-token"] = "b" * 26      # the bot's own account: not an admin
        done = self.run_script("--apply", admin_token="plain-user-token")
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("system administrator", done.stderr)
        self.assertFalse([c for c in self.stub.state.calls if c[0] == "PUT"])

    def test_missing_tokens_or_url_is_a_usage_error(self):
        env = {k: v for k, v in os.environ.items() if k not in ("REMINDER_BOT_TOKEN", "ADMIN_TOKEN")}
        no_tokens = subprocess.run(["bash", str(SCRIPTS / "make-bot-read-only.sh"), self.stub.url], env=env,
                                   capture_output=True, text=True, timeout=30)
        self.assertEqual(no_tokens.returncode, 2)
        no_url = subprocess.run(["bash", str(SCRIPTS / "make-bot-read-only.sh")], env=env, capture_output=True,
                                text=True, timeout=30)
        self.assertEqual(no_url.returncode, 2)


class ComposeFiles(unittest.TestCase):
    def test_nothing_in_a_helper_stack_is_published_beyond_this_machine(self):
        """These stacks have throwaway passwords: their ports may only ever listen on 127.0.0.1."""
        for compose in COMPOSE_FILES:
            text = compose.read_text(encoding="utf-8")
            published = re.findall(r'^\s*-\s*"?([0-9a-z.:]+:\d+:\d+|\d+:\d+)"?\s*(?:#.*)?$', text, flags=re.MULTILINE)
            self.assertTrue(published, f"{compose.name}: expected a published port")
            for mapping in published:
                with self.subTest(file=str(compose.relative_to(ROOT)), ports=mapping):
                    self.assertTrue(mapping.startswith("127.0.0.1:"), mapping)

    def test_the_dev_stack_has_the_names_the_documents_and_scripts_rely_on(self):
        compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
        self.assertRegex(compose, r"(?m)^name: chat-dev$")             # makes the network chat-dev_default
        for service in ("postgres", "mattermost", "seed", "register", "reminder", "unit", "e2e"):
            self.assertRegex(compose, rf"(?m)^  {service}:\s*(?:#.*)?$", service)
        self.assertRegex(compose, r"ALLOWEDUNTRUSTEDINTERNALCONNECTIONS=reminder")   # how Mattermost may call the bot
        checker = (SCRIPTS / "check-image.sh").read_text(encoding="utf-8")
        self.assertIn("chat-dev_default", checker)
        self.assertIn("http://mattermost:8065", checker)
        self.assertIn("secret-bot", checker)
        self.assertRegex(compose, r"(?m)^  secret-bot:$")

    def test_only_settings_the_bot_reads_are_set(self):
        """A typo in a REMINDER_* name is silently ignored by the bot, so check the compose files."""
        config = (ROOT / "src" / "remindabot" / "config.py").read_text(encoding="utf-8")
        known = set(re.findall(r'get\("([A-Z_]+)"', config)) | {"ADMIN_TOKEN", "LOG_LEVEL"}
        known |= {name + "_FILE" for name in known}
        for compose in COMPOSE_FILES:
            for name in set(re.findall(r"\bREMINDER_([A-Z_]+)\b", compose.read_text(encoding="utf-8"))):
                with self.subTest(file=compose.name, setting=name):
                    self.assertIn(name, known)

    def test_the_bot_in_the_dev_stack_never_holds_the_admin_token(self):
        compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")
        service = compose.split("\n  reminder:\n")[1].split("\n  # --- tests")[0]
        self.assertIn('REMINDER_ADMIN_TOKEN_FILE: ""', service)
        self.assertNotIn("secret-admin", service)

    def test_the_bot_image_contains_only_the_package_and_the_default_message(self):
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("COPY src ./src", dockerfile)
        self.assertIn("COPY config/message.txt /config/message.txt", dockerfile)
        self.assertIn('CMD ["python", "-m", "remindabot"]', dockerfile)
        ignored = (ROOT / ".dockerignore").read_text(encoding="utf-8").split()
        for path in ("tests", "dev", "scripts", "docs"):
            self.assertIn(path, ignored, f"{path} must not be sent to the image build")


class Documentation(unittest.TestCase):
    readme = property(lambda self: doc_text())

    def test_every_path_the_documents_name_exists(self):
        names = set(re.findall(r"(?<![\w/.-])((?:scripts|dev|docs|src|tests|config)/[A-Za-z0-9_./-]*[A-Za-z0-9_])",
                               self.readme))
        # files the stack creates when it runs (tokens, passwords) are not in a checkout
        names = {n for n in names if not re.search(r"/\.(dev|sandbox)", n)}
        self.assertTrue(names)
        for name in sorted(names):
            with self.subTest(path=name):
                self.assertTrue((ROOT / name).exists(), f"the documents mention {name}, which does not exist")

    def test_every_setting_the_code_reads_is_documented(self):
        config = (ROOT / "src" / "remindabot" / "config.py").read_text(encoding="utf-8")
        names = set(re.findall(r'get\("([A-Z_]+)"', config))
        self.assertGreaterEqual(len(names), 15)
        text = (ROOT / "docs" / "configuration.md").read_text(encoding="utf-8")
        for name in sorted(names):
            with self.subTest(setting=f"REMINDER_{name}"):
                short = f"_{name.split('PROP_')[1]}" if name.startswith("PROP_") else None   # "`_NOTIFYEES`" form
                self.assertTrue(f"REMINDER_{name}" in text or (short and f"`{short}`" in text),
                                f"REMINDER_{name} is read by the code but not documented")
        self.assertIn("REMINDER_BOT_TOKEN_FILE", text)
        self.assertIn("REMINDER_ADMIN_TOKEN", text)

    def test_every_chat_command_is_documented_and_registered_with_a_summary(self):
        from remindabot.commands import SPECS, SPEC_BY_TRIGGER
        self.assertEqual(len(SPECS), len(SPEC_BY_TRIGGER), "two commands share a trigger")
        text = (ROOT / "docs" / "chat-commands.md").read_text(encoding="utf-8")
        for spec in SPECS:
            with self.subTest(command=spec.trigger):
                self.assertIn("/" + spec.trigger, text, f"/{spec.trigger} is not in docs/chat-commands.md")
                self.assertTrue(spec.summary.strip())
                self.assertTrue(re.fullmatch(r"remind_[a-z]+", spec.trigger))
                self.assertTrue(callable(spec.handler))

    def test_the_test_classes_that_security_md_points_to_exist_where_it_says(self):
        """docs/security.md names the test that proves each promise; a renamed test must not leave it lying."""
        text = (ROOT / "docs" / "security.md").read_text(encoding="utf-8")
        checked = 0
        for chunk in re.split(r"\n\s*\n|\n(?=\|)", text):
            files = re.findall(r"`(tests/[\w/]+\.py)`", chunk)
            names = [n for n in re.findall(r"`([A-Z][A-Za-z]*[a-z][A-Za-z]*)`", chunk)]    # CamelCase, not SIGKILL
            for name in names:
                if not files:
                    continue
                with self.subTest(test_class=name):
                    sources = [(ROOT / f).read_text(encoding="utf-8") for f in files]
                    self.assertTrue(any(re.search(rf"(?m)^class {name}\b", src) for src in sources),
                                    f"docs/security.md names {name}, which is not a class of {files}")
                    checked += 1
        self.assertGreaterEqual(checked, 12)

    def test_the_license_and_the_contributing_guide_exist_and_the_readme_links_them(self):
        license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
        self.assertTrue(license_text.startswith("MIT License"))
        self.assertIn("Copyright (c) 2026 NEI/AAC", license_text)
        readme = (ROOT / "README.md").read_text(encoding="utf-8")     # the README itself, not all the documents
        self.assertIn("(CONTRIBUTING.md)", readme)
        self.assertIn("MIT", readme)
        contributing = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
        self.assertIn("https://github.com/NEIAAC/remindabot/issues", contributing)
        self.assertIn("(LICENSE)", contributing)

    def test_the_documents_do_not_describe_things_that_were_removed(self):
        for stale in ("Multi select or Number", "Text, Multi select", "REMINDER_PROP_ASSIGNEES", "python -m app",
                      "tools/dev-stack", "tools/prod-copy", "make_bot_read_only.py", "run_bot.py", "verify.py",
                      "check_container.sh"):
            self.assertNotIn(stale, self.readme)
        for old_script in ("up.sh", "down.sh", "teardown.sh", "bootstrap.py"):        # (not "setup.sh" & co.)
            self.assertIsNone(re.search(rf"(?<![\w-]){re.escape(old_script)}", self.readme), old_script)

    def test_the_readme_example_table_agrees_with_the_rule(self):
        """The day-by-day table under 'Remind Before = 7;3;1' is checked against the real rule."""
        from datetime import date
        from remindabot.boards import Card
        from remindabot.logic import evaluate
        text = (ROOT / "README.md").read_text(encoding="utf-8")
        section = text.split("`Remind Before = 7;3;1`:")[1].split("\n\n")[1]
        rows = [r for r in section.splitlines() if r.startswith("|") and "---" not in r][1:]
        self.assertGreaterEqual(len(rows), 6)
        card = Card(id="c", title="t", due=date(2026, 10, 25), remind_before=(7, 3, 1), notifyees=("u",),
                    is_template=False, deleted=False)
        announced: set[int] = set()
        checked = 0
        for row in rows:
            today_text, _left, what = (cell.strip() for cell in row.strip("|").split("|"))
            numbers = [int(n) for n in re.findall(r"\d+", today_text)]
            days = range(numbers[0], numbers[-1] + 1) if len(numbers) == 2 else numbers[:1]
            for day_of_month in days:
                decision = evaluate(card, date(2026, 10, day_of_month))
                message = decision.send and not any(s <= decision.stage for s in announced)
                if message:
                    announced.add(decision.stage)
                self.assertEqual(message, what.startswith("**Message**"), (today_text, what))
                checked += 1
        self.assertGreaterEqual(checked, 9)


if __name__ == "__main__":
    unittest.main()
