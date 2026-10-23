"""Hardening: what the repository ships must not be a threat to the machine it runs on.

The other safety tests look at the bot's behaviour. These look at everything around it:

  * ScriptHygiene     no secret on a command line (visible in `ps`), nothing dangerous in a script;
  * ProcessList       the same, measured on a live process list while the scripts run;
  * CheckImage        scripts/check-image.sh, run against a fake `docker`: right order, right arguments;
  * ComposeHardening  nothing privileged, nothing shared with the host, the bot run the way it is deployed;
  * SandboxIsolation  the copy of a production database cannot reach the internet, and is scrubbed first;
  * ImageHardening    the bot's image: unprivileged, minimal, no build-time downloads;
  * RepoHygiene       no key, token, e-mail address or data file is committed.
"""
import os
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

from .support.stubserver import ADMIN_TOKEN, BOT_TOKEN, StubMattermost

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
COMPOSE = {"dev": ROOT / "compose.yaml", "sandbox": ROOT / "dev" / "sandbox" / "compose.yaml"}
HAVE_TOOLS = all(shutil.which(p) for p in ("bash", "curl", "jq"))


def shell_scripts() -> list[Path]:
    return sorted([*SCRIPTS.rglob("*.sh"), *(ROOT / "dev").rglob("*.sh")])


def services(path: Path) -> dict[str, str]:
    """The text of each service of a compose file (a small reader: the files are written in one style)."""
    text = path.read_text(encoding="utf-8")
    section = re.search(r"(?ms)^services:\n(.*?)(?=^\S)", text + "\nend:")
    found: dict[str, str] = {}
    current = None
    for line in section.group(1).splitlines():
        match = re.match(r"^  ([a-z0-9_-]+):\s*(?:#.*)?$", line)
        if match:
            current = match.group(1)
            found[current] = ""
        elif current:
            found[current] += line + "\n"
    return found


class ScriptHygiene(unittest.TestCase):
    def test_no_secret_is_ever_an_argument_of_a_program(self):
        """A token on a command line is readable by every user of the machine, in `ps`, while it runs."""
        for path in shell_scripts():
            text = path.read_text(encoding="utf-8")
            for number, line in enumerate(text.splitlines(), 1):
                code = line.split("#", 1)[0]
                literal = re.sub(r"'[^']*'", "''", code)           # inside '...' a $VAR is text for ANOTHER shell
                with self.subTest(script=path.name, line=number):
                    if "Bearer" in code:
                        self.assertIn("printf", code, "the bearer token must be fed to curl's stdin (-K -)")
                    self.assertNotRegex(code, r'-e\s+"?REMINDER_(BOT|ADMIN)_TOKEN=\$', "use --env-file")
                    self.assertNotRegex(literal, r"--password\s+\"?\$", "a password argument shows in ps")
                    self.assertNotRegex(code, r"--arg\s+(p|pw|pass|password|token|secret)\b", "jq arguments show in ps")

    def test_nothing_dangerous_is_written_in_any_script(self):
        bad = {
            r"curl[^\n|]*\s(-k|--insecure)\b": "TLS checks switched off",
            r"\|\s*(sudo\s+)?(ba|z)?sh\b": "a download piped into a shell",
            r"\beval\b": "eval",
            r"chmod\s+(-R\s+)?(0?777|0?666|a\+w|o\+w)": "world-writable files",
            r"--privileged": "a privileged container",
            r"set\s+-[a-z]*x": "xtrace prints every secret",
            r"rm\s+-[a-z]*r[a-z]*f?\s+(/|~|\$HOME|\$\w+\s)": "a recursive delete of a variable or of the root",
            r"docker\.sock": "the Docker socket",
        }
        for path in shell_scripts():
            text = re.sub(r"(?m)^\s*#.*$", "", path.read_text(encoding="utf-8"))
            for pattern, why in bad.items():
                with self.subTest(script=path.name, danger=why):
                    self.assertIsNone(re.search(pattern, text), f"{path.name}: {why}")

    def test_scripts_use_unix_line_endings_and_a_shebang(self):
        for path in shell_scripts():
            raw = path.read_bytes()
            self.assertNotIn(b"\r", raw, f"{path.name}: CRLF would break bash")
            if path.parent.name != "lib":
                self.assertTrue(raw.startswith(b"#!/usr/bin/env bash\n"), path.name)

    def test_the_http_helpers_keep_their_temporary_files_private_and_clean_them_up(self):
        text = (SCRIPTS / "lib" / "mm.sh").read_text(encoding="utf-8")
        self.assertIn("mktemp", text)
        self.assertRegex(text, r"trap 'rm -f [^']*API_REQUEST[^']*' EXIT")
        self.assertNotRegex(text, r"/tmp/[a-z]")

    def test_the_scripts_say_where_their_secrets_come_from(self):
        text = (SCRIPTS / "make-bot-read-only.sh").read_text(encoding="utf-8")
        self.assertIn("environment", text)
        self.assertNotIn("--token", text)


@unittest.skipUnless(HAVE_TOOLS and os.path.isdir("/proc"), "needs bash, curl, jq and /proc")
class ProcessList(unittest.TestCase):
    """What `ps` would show while the scripts run, sampled for real."""

    def sample(self, run, secrets: list[str]):
        """Run `run()` while another thread reads every process's command line; returns (leaks, curls seen)."""
        stop, leaks, curls = threading.Event(), [], [0]
        me = os.getpid()

        def watch():
            while not stop.is_set():
                for entry in os.listdir("/proc"):
                    if not entry.isdigit() or int(entry) == me:
                        continue
                    try:
                        with open(f"/proc/{entry}/cmdline", "rb") as handle:
                            command = handle.read().replace(b"\0", b" ").decode("utf-8", "replace")
                    except OSError:
                        continue
                    if command.startswith(("curl ", "jq ", "bash ", "/usr/bin/curl", "/usr/bin/jq")) or "curl" in command[:40]:
                        curls[0] += command.startswith(("curl ", "/usr/bin/curl"))
                        leaks.extend((secret, command[:120]) for secret in secrets if secret in command)
                time.sleep(0.004)
        watcher = threading.Thread(target=watch, daemon=True)
        watcher.start()
        try:
            run()
        finally:
            stop.set()
            watcher.join()
        return leaks, curls[0]

    def test_make_bot_read_only_never_shows_a_token_in_the_process_list(self):
        with StubMattermost() as stub:
            st = stub.state
            st.users["admin"] = {"id": "a" * 26, "username": "admin", "password": "x", "roles": "system_admin"}
            st.users["remindabot"] = {"id": "b" * 26, "username": "remindabot", "password": "x", "roles": "system_user"}
            st.tokens[ADMIN_TOKEN], st.tokens[BOT_TOKEN] = "a" * 26, "b" * 26
            st.plugin = True
            st.teams["nei"] = {"id": "t" * 26, "name": "nei"}
            st.boards["o" * 26] = {"id": "o" * 26, "title": "Open", "type": "O", "teamId": "t" * 26}
            st.members["o" * 26] = {"b" * 26: {"schemeEditor": True, "schemeAdmin": False,
                                                "schemeCommenter": False, "schemeViewer": False}}
            st.slow.update({"/api/v4/users/me": 0.4, "/api/v4/users/me/teams": 0.4, "/members": 0.4})
            env = {**os.environ, "REMINDER_BOT_TOKEN": BOT_TOKEN, "ADMIN_TOKEN": ADMIN_TOKEN}
            done = []
            leaks, curls = self.sample(
                lambda: done.append(subprocess.run(["bash", str(SCRIPTS / "make-bot-read-only.sh"), stub.url, "--apply"],
                                                   env=env, capture_output=True, text=True, timeout=120)),
                [BOT_TOKEN, ADMIN_TOKEN])
            self.assertEqual(done[0].returncode, 0, done[0].stdout + done[0].stderr)
            self.assertGreater(curls, 2, "the sampler never saw curl run: this test would prove nothing")
            self.assertEqual(leaks, [], "a token was visible in the process list")

    def test_the_dev_seed_never_shows_a_password_or_token_in_the_process_list(self):
        with StubMattermost() as stub, tempfile.TemporaryDirectory() as tmp:
            stub.state.slow.update({"/api/v4/users/login": 0.3, "/api/v4/users": 0.2, "/api/v4/bots": 0.2})
            bundle = Path(tmp) / "boards.tar.gz"
            bundle.write_bytes(b"x" * 100)
            password = "correct-horse-battery-staple-7731"
            env = {**os.environ, "MM_URL": stub.url, "DEV_ADMIN_PASSWORD": password, "BOARDS_BUNDLE": str(bundle),
                   "SECRETS_BOT": f"{tmp}/bot", "SECRETS_ADMIN": f"{tmp}/admin"}
            done = []
            leaks, curls = self.sample(
                lambda: done.append(subprocess.run(["bash", str(SCRIPTS / "dev-seed.sh")], env=env, capture_output=True,
                                                   text=True, timeout=180)),
                [password, BOT_TOKEN, ADMIN_TOKEN])
            self.assertEqual(done[0].returncode, 0, done[0].stdout + done[0].stderr)
            self.assertGreater(curls, 5)
            self.assertEqual(leaks, [], "a password or token was visible in the process list")

    def test_the_sandbox_setup_never_shows_the_admin_password_in_the_process_list(self):
        with StubMattermost() as stub, tempfile.TemporaryDirectory() as tmp:
            stub.state.users["sandboxadmin"] = {"id": "s" * 26, "username": "sandboxadmin", "password": "pw-from-restore-91827364",
                                                "roles": "system_admin"}
            stub.state.slow.update({"/api/v4/users/login": 0.4, "/api/v4/teams": 0.4})
            bundle = Path(tmp) / "boards.tar.gz"
            bundle.write_bytes(b"x" * 100)
            env = {**os.environ, "MM_URL": stub.url, "SANDBOX_ADMIN_PASSWORD": "pw-from-restore-91827364",
                   "BOARDS_BUNDLE": str(bundle)}
            done = []
            leaks, _ = self.sample(
                lambda: done.append(subprocess.run(["bash", str(SCRIPTS / "sandbox-setup.sh")], env=env,
                                                   capture_output=True, text=True, timeout=60)),
                ["pw-from-restore-91827364", BOT_TOKEN])
            self.assertEqual(done[0].returncode, 0, done[0].stderr)
            self.assertEqual(leaks, [])


FAKE_DOCKER = r'''#!/usr/bin/env bash
# A stand-in for the docker CLI that records every call (and the content of any --env-file, at the
# moment it is used) and answers what a healthy installation would.
log=$FAKE_DOCKER_LOG
printf '%s\n' "ARGV:$*" >> "$log"
args=" $* "
prev=""
for a in "$@"; do
  [ "$prev" = "--env-file" ] && printf 'ENVFILE %s: %s\n' "$a" "$(cat "$a" 2>/dev/null | tr '\n' ' ')" >> "$log"
  prev=$a
done
case "$1" in
  build) exit 0 ;;
  image|network|volume|rm|stop|logs) [ "$1" = logs ] && echo "started: daily run at 14:00"; exit 0 ;;
  inspect)
    case "$args" in
      *State.Running*) echo true ;; *Health.Status*) echo healthy ;; *ExitCode*) echo 0 ;;
    esac; exit 0 ;;
  run)
    case "$args" in
      *"--entrypoint cat"*"/s/token"*) printf '%s' "$FAKE_DOCKER_TOKEN"; exit 0 ;;
      *"--entrypoint id"*) echo 10001; exit 0 ;;
      *"--entrypoint touch"*) exit 1 ;;
      *"--entrypoint test"*) exit 0 ;;
      *"set_last_run"*) exit 0 ;;
      *"s.claim"*) echo new; exit 0 ;;
      *urlopen*healthz*) echo '"ok"'; exit 0 ;;
      *"--entrypoint python"*) exit 0 ;;
      *" -d "*) echo container-id; exit 0 ;;
      *"--once --dry-run"*) echo "boards: 1 seen (DRY-RUN, nothing was sent)"; exit 0 ;;
      *"--env-file"*"--check"*) echo "OK: logged in"; exit 0 ;;
      *"--check"*) exit 2 ;;
    esac
    exit 0 ;;
esac
exit 0
'''


class CheckImage(unittest.TestCase):
    """scripts/check-image.sh against a fake docker: it cannot prove the image, but it proves the SCRIPT."""

    TOKEN = "shim-token-1234567890"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bin = Path(self.tmp.name) / "bin"
        self.bin.mkdir()
        shim = self.bin / "docker"
        shim.write_text(FAKE_DOCKER)
        shim.chmod(0o755)
        self.log = Path(self.tmp.name) / "calls.log"
        self.log.write_text("")

    def run_script(self, **extra) -> subprocess.CompletedProcess:
        env = {**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}", "FAKE_DOCKER_LOG": str(self.log),
               "FAKE_DOCKER_TOKEN": self.TOKEN, "TMPDIR": self.tmp.name}
        env.update(extra)
        return subprocess.run(["bash", str(SCRIPTS / "check-image.sh")], env=env, capture_output=True, text=True,
                              timeout=120)

    @unittest.skipUnless(shutil.which("bash"), "needs bash")
    def test_every_check_runs_with_the_token_and_none_of_them_shows_it(self):
        done = self.run_script()
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertRegex(done.stdout, r"RESULT: \d+ passed, 0 failed")
        calls = self.log.read_text()
        argv = [line[5:] for line in calls.splitlines() if line.startswith("ARGV:")]
        self.assertFalse([a for a in argv if self.TOKEN in a], "the token was on a docker command line")
        with_env = [line for line in calls.splitlines() if line.startswith("ENVFILE")]
        self.assertGreaterEqual(len(with_env), 4, "the checks that talk to Mattermost must carry the token file")
        for line in with_env:
            self.assertIn(f"REMINDER_BOT_TOKEN={self.TOKEN}", line, "the container would have run without the token")
        paths = {line.split()[1].rstrip(":") for line in with_env}
        for path in paths:
            self.assertFalse(Path(path).exists(), "the token file was left behind")

    def test_a_token_file_is_used_instead_of_reading_the_stack(self):
        token_file = Path(self.tmp.name) / "token"
        token_file.write_text("from-a-file-98765432\n")
        done = self.run_script(TOKEN_FILE=str(token_file), FAKE_DOCKER_TOKEN="")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("REMINDER_BOT_TOKEN=from-a-file-98765432", self.log.read_text())
        self.assertNotIn("/s/token", self.log.read_text(), "it must not read the stack's volume when given a file")

    def test_an_empty_token_stops_the_script_before_anything_runs_with_it(self):
        done = self.run_script(FAKE_DOCKER_TOKEN="")
        self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
        self.assertIn("empty", done.stdout)
        self.assertNotIn("ENVFILE", self.log.read_text())

    def test_a_missing_network_stops_it_with_a_hint(self):
        (self.bin / "docker").write_text(FAKE_DOCKER.replace("image|network|volume", "image|volume").replace(
            "esac\nexit 0", "esac\nexit 0"))
        # a fake that fails `docker network inspect`
        script = (self.bin / "docker").read_text().replace('case "$1" in', 'case "$1" in\n  network) exit 1 ;;', 1)
        (self.bin / "docker").write_text(script)
        done = self.run_script()
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("docker compose up", done.stdout)


class ComposeHardening(unittest.TestCase):
    def test_nothing_is_privileged_and_nothing_is_shared_with_the_host(self):
        forbidden = [r"privileged:\s*true", r"network_mode:\s*[\"']?host", r"\bpid:\s*[\"']?host", r"\bipc:\s*[\"']?host",
                     r"docker\.sock", r"\bcap_add\b", r"\bdevices:", r"unconfined", r"userns_mode", r"\bsysctls:"]
        for name, path in COMPOSE.items():
            text = path.read_text(encoding="utf-8")
            for pattern in forbidden:
                with self.subTest(file=name, pattern=pattern):
                    self.assertIsNone(re.search(pattern, text))

    def test_the_bot_runs_in_the_stack_the_way_it_is_deployed(self):
        reminder = services(COMPOSE["dev"])["reminder"]
        self.assertIn("read_only: true", reminder)
        self.assertRegex(reminder, r"cap_drop:\s*\n\s*- ALL")
        self.assertIn("no-new-privileges:true", reminder)
        self.assertRegex(reminder, r"tmpfs:\s*\n\s*- /tmp")
        self.assertNotIn("secret-admin", reminder, "the bot must never hold an administrator's token")
        self.assertIn('REMINDER_ADMIN_TOKEN_FILE: ""', reminder)

    def test_every_secret_volume_is_read_only_for_whoever_does_not_make_it(self):
        for name, block in services(COMPOSE["dev"]).items():
            for mount in re.findall(r"- (secret-(?:bot|admin):/secrets/\w+)(:ro)?", block):
                with self.subTest(service=name, mount=mount[0]):
                    if name != "seed":
                        self.assertEqual(mount[1], ":ro", "only `seed` may write the secrets")

    def test_the_bot_and_its_one_off_step_publish_nothing(self):
        blocks = services(COMPOSE["dev"])
        for name in ("reminder", "register", "seed", "unit", "e2e"):
            with self.subTest(service=name):
                self.assertNotRegex(blocks[name], r"\bports:")
        self.assertRegex(blocks["mattermost"], r'"127\.0\.0\.1:8066:8065"')

    def test_the_secret_volumes_exist_apart_from_each_other(self):
        text = COMPOSE["dev"].read_text(encoding="utf-8")
        self.assertRegex(text, r"(?m)^  secret-bot:$")
        self.assertRegex(text, r"(?m)^  secret-admin:$")

    def test_the_test_services_only_run_when_asked_for(self):
        blocks = services(COMPOSE["dev"])
        for name in ("unit", "e2e"):
            self.assertIn("profiles: [test]", blocks[name])


class SandboxIsolation(unittest.TestCase):
    """A copy of production data: it must not be able to reach out, and nobody must be able to log in."""

    compose = COMPOSE["sandbox"].read_text(encoding="utf-8")
    restore = (ROOT / "dev" / "sandbox" / "restore.sh").read_text(encoding="utf-8")

    def test_the_network_has_no_route_to_the_internet(self):
        self.assertRegex(self.compose, r"(?ms)^networks:\n  internal:\n    internal: true")

    def test_only_the_gate_is_on_the_outside_network_and_only_it_publishes_a_port(self):
        blocks = services(COMPOSE["sandbox"])
        for name, block in blocks.items():
            with self.subTest(service=name):
                if name == "gate":
                    self.assertRegex(block, r"networks: \[internal, outside\]")
                    self.assertRegex(block, r'"127\.0\.0\.1:8067:8065"')
                else:
                    self.assertRegex(block, r"networks: \[internal\]")
                    self.assertNotIn("outside", block)
                    self.assertNotRegex(block, r"\bports:")

    def test_the_chat_server_cannot_contact_anyone(self):
        mattermost = services(COMPOSE["sandbox"])["mattermost"]
        for setting in ("SENDEMAILNOTIFICATIONS", "SENDPUSHNOTIFICATIONS", "ENABLEINCOMINGWEBHOOKS",
                        "ENABLEOUTGOINGWEBHOOKS", "ENABLECOMMANDS", "ENABLEDIAGNOSTICS", "ENABLEMARKETPLACE",
                        "ENABLEREMOTEMARKETPLACE", "ENABLESECURITYFIXALERT"):
            with self.subTest(setting=setting):
                self.assertRegex(mattermost, rf'{setting}: "false"')
        self.assertRegex(mattermost, r'SMTPSERVER: ""')
        self.assertRegex(mattermost, r'PUSHNOTIFICATIONSERVER: ""')

    def test_the_data_is_restored_and_scrubbed_before_the_chat_server_ever_starts(self):
        order = [self.restore.index(marker) for marker in
                 ("up -d db", "pg_restore", "UPDATE users SET password", "LEFT=$(", "--profile app up -d mattermost")]
        self.assertEqual(order, sorted(order), "the order of restore.sh is the safety of the sandbox")
        refusal = self.restore.index('if [ "$LEFT" != "0" ]')
        self.assertLess(refusal, self.restore.index("--profile app up -d mattermost"))
        self.assertIn("exit 1", self.restore[refusal:refusal + 400])

    def test_the_scrub_removes_every_way_to_log_in_to_or_to_contact_a_real_person(self):
        for statement in ("password = ''", "authdata = NULL", "authservice = ''", "mfasecret = ''", "email = id ||",
                          "DELETE FROM sessions", "DELETE FROM useraccesstokens", "DELETE FROM tokens",
                          "DELETE FROM outgoingwebhooks", "DELETE FROM incomingwebhooks", "DELETE FROM commands",
                          "DELETE FROM oauthapps", "DELETE FROM oauthaccessdata", "DELETE FROM oauthauthdata",
                          "push_notifications"):
            with self.subTest(statement=statement):
                self.assertIn(statement, self.restore)
        self.assertIn("firstname = ''", self.restore)
        self.assertIn("nickname = ''", self.restore)
        self.assertIn("NOT LIKE '%@sandbox.invalid'", self.restore)       # the check, not just the scrub

    def test_the_dump_instructions_leave_out_what_people_wrote(self):
        readme = (ROOT / "dev" / "sandbox" / "README.md").read_text(encoding="utf-8")
        for table in ("posts", "fileinfo", "reactions", "drafts", "sessions", "useraccesstokens", "tokens",
                      "outgoingwebhooks", "incomingwebhooks", "commands"):
            with self.subTest(table=table):
                self.assertIn(f"--exclude-table-data='{table}'", readme)

    def test_its_secrets_are_not_world_readable(self):
        self.assertIn("umask 077", self.restore)


class ImageHardening(unittest.TestCase):
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    def test_it_runs_as_an_unprivileged_user_with_a_fixed_id(self):
        self.assertRegex(self.dockerfile, r"useradd --system --uid 100\d\d")
        self.assertRegex(self.dockerfile, r"(?m)^USER reminder$")
        self.assertLess(self.dockerfile.index("useradd"), self.dockerfile.index("USER reminder"))

    def test_it_is_built_from_a_pinned_slim_base_and_downloads_nothing_else(self):
        self.assertRegex(self.dockerfile, r"(?m)^FROM python:3\.\d+-slim$")
        self.assertNotIn(":latest", self.dockerfile)
        for word in ("pip ", "apt", "curl", "wget", "ADD ", "git clone"):
            self.assertNotIn(word, self.dockerfile.replace("# ", "#"), f"{word!r}: nothing is fetched at build time")

    def test_it_copies_only_what_it_runs(self):
        copies = re.findall(r"(?m)^COPY (.+)$", self.dockerfile)
        self.assertEqual(sorted(copies), ["config/message.txt /config/message.txt", "src ./src"])

    def test_it_exposes_one_port_and_has_a_health_check_and_no_secrets(self):
        self.assertEqual(re.findall(r"(?m)^EXPOSE (\d+)", self.dockerfile), ["8080"])
        self.assertIn("HEALTHCHECK", self.dockerfile)
        self.assertNotRegex(self.dockerfile, r"(?i)ENV[^\n]*(token|password|secret)")

    def test_the_build_context_leaves_out_credentials_and_history(self):
        ignored = (ROOT / ".dockerignore").read_text(encoding="utf-8").split()
        for pattern in (".git", "*.env*", "tests", "dev", "scripts", "docs"):
            self.assertIn(pattern, ignored)


class RepoHygiene(unittest.TestCase):
    SKIP = {".git", "tools", "__pycache__", ".pytest_cache", "node_modules"}
    EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})")
    ALLOWED_DOMAINS = re.compile(r"(\.invalid|\.local|\.test|(^|\.)example\.(com|org|net)|^x\.example)$")
    SECRET = re.compile(r"(?i)\b[\w-]*(token|secret|passw(?:or)?d|api[_-]?key)[\w-]*\s*[:=]\s*[\"']?([A-Za-z0-9+/=_\-]{16,})[\"']?")
    # values that are not secrets: paths of secret FILES, the documented dev passwords, and test fixtures
    KNOWN = ("/secrets/", "/run/secrets/", "devadmin-password", "sandbox_only_password", "rehearsal_only_password", "tok-remind_", "plain-user-token",
             "REMINDER_", "mmuser_dev", "bot-token-xyz", "admin-token-abc", "shim-token", "from-a-file", "pw-from-restore",
             "correct-horse", "secret-bot", "secret-admin")

    def files(self):
        for path in sorted(ROOT.rglob("*")):
            if path.is_file() and not self.SKIP & set(path.relative_to(ROOT).parts):
                raw = path.read_bytes()
                if b"\0" not in raw:
                    yield path, raw.decode("utf-8", "replace")

    def test_no_private_key_is_committed(self):
        for path, text in self.files():
            self.assertNotRegex(text, r"-----BEGIN [A-Z ]*PRIVATE KEY-----", str(path.relative_to(ROOT)))

    def test_no_real_person_email_address_is_committed(self):
        for path, text in self.files():
            for domain in self.EMAIL.findall(text):
                with self.subTest(file=str(path.relative_to(ROOT)), domain=domain):
                    self.assertRegex(domain, self.ALLOWED_DOMAINS, "only addresses that cannot exist belong here")

    def test_no_token_or_password_value_is_committed(self):
        for path, text in self.files():
            if path.parts[-2] == "tests" or path.suffix == ".md":
                continue                                           # fixtures and prose are checked by the lists above
            for match in self.SECRET.finditer(text):
                value = match.group(0)
                with self.subTest(file=str(path.relative_to(ROOT)), value=value[:50]):
                    self.assertTrue(any(known in value for known in self.KNOWN) or "$" in value or "{" in value,
                                    "a secret-looking value: if it is not one, add it to KNOWN, with a reason")

    def test_no_data_or_secret_file_is_committed(self):
        names = re.compile(r"(\.env($|\.)|\.pem$|\.key$|\.sqlite3?$|\.dump$|\.sql$|^\.sandbox-|^\.dev-|id_rsa|\.p12$)")
        for path, _ in self.files():
            with self.subTest(file=str(path.relative_to(ROOT))):
                self.assertIsNone(names.search(path.name) if path.name != ".env.example" else None)

    def test_only_scripts_are_executable(self):
        for path in sorted(ROOT.rglob("*")):
            if path.is_file() and not self.SKIP & set(path.relative_to(ROOT).parts):
                if path.stat().st_mode & stat.S_IXUSR:
                    with self.subTest(file=str(path.relative_to(ROOT))):
                        self.assertEqual(path.suffix, ".sh")

    def test_the_gitignore_keeps_secrets_and_state_out(self):
        text = (ROOT / ".gitignore").read_text(encoding="utf-8")
        for pattern in ("*.env*", "dev/sandbox/.sandbox-*", "__pycache__/"):
            self.assertIn(pattern, text)


if __name__ == "__main__":
    unittest.main()
