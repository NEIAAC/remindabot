"""The deployment rehearsal (dev/rehearsal): the SCRIPT and the isolation layer, without a Docker daemon.

These tests cannot prove the rehearsal works on real Docker (only running it does), but they prove what
can go wrong in the script itself:

  * Isolation          the copy cannot reach the internet, contact anyone, or publish a port to the network;
  * Up                 the data is restored, scrubbed and CHECKED before Mattermost ever starts, the
                       production file is copied byte for byte, no secret is on a command line;
  * Deploy             the new file is put in place and the bot's build address is checked the way the
                       server will use it;
  * SnapshotAndCompare what changed between two snapshots is reported, and what must not change fails;
  * Down               only a folder the script made itself is ever removed.

docker, curl and git are replaced by small shell stand-ins that log every call.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REHEARSAL = ROOT / "dev" / "rehearsal"
SCRIPT = REHEARSAL / "rehearse.sh"
HAVE_BASH = bool(shutil.which("bash"))

# A production compose file in the shape of NEIAAC/chat's (the same service names and keys).
PRODUCTION = """name: chat
services:
  mattermost:
    image: mattermost/mattermost-team-edition:10.9.1
    container_name: chat-web
    env_file:
    - .env
    ports:
      - ${APP_PORT}:8065
    environment:
      - MM_SERVICESETTINGS_ENABLEINCOMINGWEBHOOKS=true
    depends_on:
      postgres:
        condition: service_healthy
  postgres:
    image: postgres:13-alpine
    container_name: chat-db
    environment:
      - POSTGRES_USER=${DB_USER}
"""
WITH_BOT = PRODUCTION.replace("name: chat\n", "name: chat\n") + """  reminder:
    build: https://github.com/NEIAAC/remindabot.git#main
    container_name: chat-reminder
    environment:
      - REMINDER_BOT_TOKEN=${REMINDER_BOT_TOKEN}
"""

WITH_IMAGE = PRODUCTION + """  reminder:
    image: ghcr.io/neiaac/remindabot:1.0.0
    container_name: chat-reminder
    environment:
      - REMINDER_BOT_TOKEN=${REMINDER_BOT_TOKEN}
"""

# Stand-ins. Each logs one line per call: "<program> <cwd-name> <arguments>".
FAKE_DOCKER = r'''#!/usr/bin/env bash
printf 'docker in=%s: %s\n' "$(basename "$PWD")" "$*" >> "$FAKE_LOG"
case "$1 $2" in
  "info "*|"info") [ "${FAKE_NO_DAEMON:-}" = yes ] && { echo "permission denied" >&2; exit 1; }; exit 0 ;;
  "compose version") echo "5.6.0"; exit 0 ;;
  "ps -a") printf '%s' "${FAKE_PS:-}"; exit 0 ;;
esac
if [ "$1" = compose ]; then
  args=" $* "
  case "$args" in
    *" config --quiet "*) exit 0 ;;
    *" ps -a -q "*) printf 'aaaaaaaaaaaa1\nbbbbbbbbbbbb2\n'; exit 0 ;;
    *" ps -a --services "*) printf 'postgres\nmattermost\n'; exit 0 ;;
    *" logs "*) printf '{"level":"error"}\nfine\n'; exit 0 ;;
    *" psql "*)
      sql=""; prev=""
      for a in "$@"; do [ "$prev" = "-c" ] && sql=$a; prev=$a; done
      case "$sql" in
        *"NOT LIKE '%@sandbox.invalid'"*) echo "${FAKE_LEFT:-0}" ;;
        *query_to_xml*) printf 'users\t10\nposts\t0\nteams\t2\nfocalboard_blocks\t5\n' ;;
        *md5*) echo "0123456789abcdef" ;;
        "") cat > /dev/null ;;
      esac
      exit 0 ;;
    *" pg_restore "*) cat > /dev/null; exit 0 ;;
  esac
  exit 0
fi
case "$1" in
  manifest)
    printf 'manifest empty-config=%s: %s\n' "$([ -z "$(ls -A "${DOCKER_CONFIG:-/nonexistent}" 2>/dev/null)" ] && echo yes || echo no)" "$*" >> "$FAKE_LOG"
    [ "${FAKE_MANIFEST_FAILS:-}" = yes ] && exit 1
    exit 0 ;;
  inspect) printf 'C\tsvc\taaaaaaaaaaaa\t2026-10-19T10:00:00Z\t0\timage:1\n'; exit 0 ;;
esac
exit 0
'''
FAKE_CURL = "#!/usr/bin/env bash\nprintf 'curl %s\\n' \"$*\" >> \"$FAKE_LOG\"\nexit 0\n"
FAKE_GIT = r'''#!/usr/bin/env bash
printf 'git %s\n' "$*" >> "$FAKE_LOG"
[ "${FAKE_GIT_FAILS:-}" = yes ] && exit 128
exit 0
'''


def script_sql(path: Path) -> str:
    """The SQL of the scrub: what sits between <<'SQL' and the closing SQL of a script."""
    return re.search(r"<<'SQL'\n(.*?)\nSQL\n", path.read_text(encoding="utf-8"), re.S).group(1)


class Isolation(unittest.TestCase):
    layer = (REHEARSAL / "isolation.yaml").read_text(encoding="utf-8")

    def test_the_network_has_no_route_to_the_internet(self):
        self.assertRegex(self.layer, r"(?ms)^networks:\n  default:\n    internal: true")

    def test_only_the_gate_publishes_a_port_and_only_on_this_machine(self):
        self.assertEqual(re.findall(r'- "([^"]+:\d+:\d+)"', self.layer), ["127.0.0.1:8068:8065"])
        self.assertRegex(self.layer, r"mattermost:\n    ports: !reset \[\]")

    def test_the_chat_server_cannot_contact_anyone(self):
        for setting in ("SENDEMAILNOTIFICATIONS=false", "SENDPUSHNOTIFICATIONS=false", "PUSHNOTIFICATIONSERVER=\n",
                        "SMTPSERVER=\n"):
            with self.subTest(setting=setting):
                self.assertIn("MM_EMAILSETTINGS_" + setting, self.layer + "\n")

    def test_it_leaves_container_names_and_the_bot_service_to_the_production_file(self):
        """The commands of the production documents (docker exec chat-db ...) must work unchanged, and a
        layer that named `reminder` would invent a service when the production file has none yet."""
        self.assertNotIn("container_name", self.layer)
        self.assertNotRegex(self.layer, r"(?m)^  reminder:")

    def test_nothing_is_privileged(self):
        for pattern in (r"privileged", r"network_mode", r"docker\.sock", r"cap_add"):
            self.assertIsNone(re.search(pattern, self.layer))

    @unittest.skipUnless(shutil.which("docker"), "needs the docker CLI (no daemon is needed)")
    def test_docker_compose_accepts_it_over_a_production_style_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            (work / "docker-compose.yml").write_text(WITH_BOT)
            (work / "isolation.yaml").write_text(self.layer)
            (work / ".env").write_text("COMPOSE_PROJECT_NAME=t\nCOMPOSE_FILE=docker-compose.yml:isolation.yaml\n"
                                       "APP_PORT=8068\nDB_USER=u\nREMINDER_BOT_TOKEN=\n")
            done = subprocess.run(["docker", "compose", "config", "--format", "json"], cwd=work, capture_output=True,
                                  text=True, timeout=60)
            if done.returncode != 0 and "unknown" in done.stderr:
                self.skipTest("this docker CLI has no compose plugin")
            self.assertEqual(done.returncode, 0, done.stderr)
            import json
            config = json.loads(done.stdout)
            self.assertTrue(config["networks"]["default"]["internal"])
            self.assertNotIn("ports", config["services"]["mattermost"])
            self.assertEqual(config["services"]["gate"]["ports"][0]["host_ip"], "127.0.0.1")
            self.assertEqual(config["services"]["mattermost"]["container_name"], "chat-web")
            self.assertIn("reminder", config["services"])

    @unittest.skipUnless(shutil.which("docker"), "needs the docker CLI (no daemon is needed)")
    def test_a_local_build_context_can_be_added_to_a_service_that_names_a_published_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            (work / "docker-compose.yml").write_text(WITH_IMAGE)
            (work / "isolation.yaml").write_text(self.layer)
            (work / "local-source.yaml").write_text(
                "services:\n  reminder:\n    build:\n      context: " + str(ROOT) + "\n")
            (work / ".env").write_text("COMPOSE_PROJECT_NAME=t\nCOMPOSE_FILE=docker-compose.yml:isolation.yaml:local-source.yaml\n"
                                       "APP_PORT=8068\nDB_USER=u\nREMINDER_BOT_TOKEN=\n")
            done = subprocess.run(["docker", "compose", "config", "--format", "json"], cwd=work, capture_output=True,
                                  text=True, timeout=60)
            if done.returncode != 0 and "unknown" in done.stderr:
                self.skipTest("this docker CLI has no compose plugin")
            self.assertEqual(done.returncode, 0, done.stderr)
            import json
            reminder = json.loads(done.stdout)["services"]["reminder"]
            self.assertEqual(reminder["image"], "ghcr.io/neiaac/remindabot:1.0.0")
            self.assertEqual(reminder["build"]["context"], str(ROOT))


@unittest.skipUnless(HAVE_BASH, "needs bash")
class Script(unittest.TestCase):
    """rehearse.sh against stand-ins for docker, curl and git."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        for name, body in (("docker", FAKE_DOCKER), ("curl", FAKE_CURL), ("git", FAKE_GIT)):
            (self.bin / name).write_text(body)
            (self.bin / name).chmod(0o755)
        self.log = self.root / "calls.log"
        self.log.write_text("")
        self.work = self.root / "work"
        self.before = self.root / "infra" / "docker-compose.yml"
        self.before.parent.mkdir()
        self.before.write_text(PRODUCTION)
        self.after = self.root / "infra-new" / "docker-compose.yml"
        self.after.parent.mkdir()
        self.after.write_text(WITH_BOT)
        self.dump = self.root / "mattermost.dump"
        self.dump.write_bytes(b"PGDMP" + b"x" * 100)

    def run_script(self, *args, **env) -> subprocess.CompletedProcess:
        full = {**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}", "FAKE_LOG": str(self.log),
                "REHEARSE_WORK": str(self.work), "BOARDS_VERSION": "9.1.3"}
        full.update(env)
        return subprocess.run(["bash", str(SCRIPT), *args], env=full, capture_output=True, text=True, timeout=120)

    def calls(self) -> list[str]:
        return self.log.read_text().splitlines()

    def up(self, **env) -> subprocess.CompletedProcess:
        return self.run_script("up", str(self.before), str(self.dump), **env)

    # -- up ---------------------------------------------------------------------------------------

    def test_up_builds_the_copy_and_leaves_the_production_file_untouched(self):
        done = self.up()
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual((self.work / "docker-compose.yml").read_bytes(), self.before.read_bytes())
        self.assertEqual((self.work / "isolation.yaml").read_bytes(), (REHEARSAL / "isolation.yaml").read_bytes())
        env = (self.work / ".env").read_text()
        self.assertIn("COMPOSE_PROJECT_NAME=remindabot-rehearsal", env)
        self.assertIn("COMPOSE_FILE=docker-compose.yml:isolation.yaml", env)
        self.assertIn("MM_EMAILSETTINGS_SMTPSERVER=\n", env)
        self.assertNotIn("password-smtp", env)
        self.assertTrue((self.work / "snapshots" / "00-start.txt").exists())
        self.assertEqual((self.work / ".admin-password").stat().st_mode & 0o077, 0, "the admin password is private")

    def test_the_data_is_restored_scrubbed_and_checked_before_mattermost_starts(self):
        self.assertEqual(self.up().returncode, 0)
        calls = self.calls()

        def first(marker):
            return next(i for i, line in enumerate(calls) if marker in line)
        order = [first("up -d postgres"), first("pg_restore"), first("psql -U mmuser -d mattermost -v ON_ERROR_STOP=0"),
                 first("NOT LIKE '%@sandbox.invalid'"), first("up -d mattermost gate")]
        self.assertEqual(order, sorted(order), "the order of rehearse.sh is the safety of the copy")
        self.assertEqual(len(set(order)), 5)

    def test_an_incomplete_scrub_stops_before_mattermost_starts(self):
        done = self.up(FAKE_LEFT="3")
        self.assertEqual(done.returncode, 1)
        self.assertIn("scrub is incomplete", done.stderr)
        self.assertIn("stopped early", done.stderr, "the person must be told how to start again")
        self.assertFalse([line for line in self.calls() if "up -d mattermost" in line])

    def test_the_scrub_is_the_one_of_the_sandbox(self):
        """One list of what makes a copy safe: the two scripts must not drift apart."""
        self.assertEqual(script_sql(SCRIPT), script_sql(ROOT / "dev" / "sandbox" / "restore.sh"))

    def test_no_password_or_token_is_ever_on_a_command_line(self):
        self.assertEqual(self.up().returncode, 0)
        password = (self.work / ".admin-password").read_text()
        self.assertGreater(len(password), 12)
        self.assertFalse([line for line in self.calls() if password in line], "the admin password was on a command line")

    def test_it_will_not_run_where_production_runs(self):
        done = self.up(FAKE_PS="chat-web\nother\n")
        self.assertEqual(done.returncode, 1)
        self.assertIn("must not run on the server", done.stderr)
        self.assertFalse(self.work.exists(), "nothing may be made before this check")

    def test_a_second_up_refuses_instead_of_mixing_two_copies(self):
        self.assertEqual(self.up().returncode, 0)
        again = self.up()
        self.assertEqual(again.returncode, 1)
        self.assertIn("exists already", again.stderr)

    def test_a_missing_docker_permission_is_explained_before_anything_is_made(self):
        done = self.up(FAKE_NO_DAEMON="yes")
        self.assertEqual(done.returncode, 1)
        self.assertIn("run this with sudo", done.stderr)
        self.assertFalse(self.work.exists())

    def test_an_old_docker_compose_is_refused(self):
        script = (self.bin / "docker").read_text().replace('echo "5.6.0"', 'echo "2.20.1"')
        (self.bin / "docker").write_text(script)
        done = self.up()
        self.assertEqual(done.returncode, 1)
        self.assertIn("2.24", done.stderr)

    def test_missing_files_are_reported_before_anything_is_made(self):
        self.assertEqual(self.run_script("up", str(self.before), str(self.root / "nope.dump")).returncode, 1)
        self.assertEqual(self.run_script("up", str(self.root / "nope.yml"), str(self.dump)).returncode, 1)
        self.assertFalse(self.work.exists())

    def test_the_commands_run_in_the_work_folder_where_production_file_and_env_are(self):
        self.assertEqual(self.up().returncode, 0)
        compose_calls = [line for line in self.calls() if line.startswith("docker in=") and " compose " in line + " "]
        self.assertTrue(compose_calls)
        for line in compose_calls:
            if "version" not in line:
                self.assertTrue(line.startswith("docker in=work: compose") or line.startswith("docker in=work:  compose"), line)

    # -- deploy -----------------------------------------------------------------------------------

    def deploy(self, mode="local", **env) -> subprocess.CompletedProcess:
        return self.run_script("deploy", str(self.after), mode, **env)

    def test_deploy_puts_the_new_file_in_place_and_shows_what_changes(self):
        self.up()
        done = self.deploy()
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual((self.work / "docker-compose.yml").read_bytes(), self.after.read_bytes())
        self.assertEqual((self.work / "docker-compose.before.yml").read_bytes(), self.before.read_bytes())
        self.assertIn("+  reminder:", done.stdout)
        self.assertIn("docker compose up -d --build", done.stdout, "local mode builds the bot here")
        again = self.deploy("published")
        self.assertRegex(again.stdout, r"(?m)^  docker compose up -d$", "the server's own command")

    def test_deploy_builds_the_bot_from_this_checkout_unless_told_to_use_the_published_one(self):
        self.up()
        self.assertEqual(self.deploy("local").returncode, 0)
        self.assertIn(f"context: {ROOT}", (self.work / "local-source.yaml").read_text())
        self.assertIn("COMPOSE_FILE=docker-compose.yml:isolation.yaml:local-source.yaml", (self.work / ".env").read_text())
        (self.work / "local-source.yaml").unlink()
        self.assertEqual(self.deploy("published").returncode, 0)
        self.assertNotIn("local-source", (self.work / ".env").read_text().split("COMPOSE_FILE=")[1].splitlines()[0])
        self.assertFalse((self.work / "local-source.yaml").exists())

    def test_deploy_says_so_loudly_when_the_server_could_not_read_the_bots_address(self):
        self.up()
        done = self.deploy(FAKE_GIT_FAILS="yes")
        self.assertEqual(done.returncode, 0)
        self.assertIn("NO.", done.stdout)
        self.assertIn("would fail", done.stdout)
        ok = self.deploy()
        self.assertIn("it can be read without credentials", ok.stdout)
        asked = [line for line in self.calls() if line.startswith("git ls-remote")]
        self.assertIn("https://github.com/NEIAAC/remindabot.git", asked[-1])
        self.assertIn("main", asked[-1])

    def test_deploy_checks_that_the_server_could_pull_the_published_image(self):
        self.after.write_text(WITH_IMAGE)
        self.up()
        ok = self.deploy("published")
        self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)
        self.assertIn("yes: it can be pulled without a login", ok.stdout)
        asked = [line for line in self.calls() if line.startswith("manifest")]
        self.assertIn("manifest inspect ghcr.io/neiaac/remindabot:1.0.0", asked[-1])
        self.assertIn("empty-config=yes", asked[-1], "the check must not use the person's own registry login")
        failed = self.deploy("published", FAKE_MANIFEST_FAILS="yes")
        self.assertEqual(failed.returncode, 0)
        self.assertIn("NO. That image cannot be pulled without a login", failed.stdout)
        self.assertNotIn("local-source", (self.work / ".env").read_text())

    def test_with_a_published_image_local_mode_builds_it_here_under_the_same_name(self):
        self.after.write_text(WITH_IMAGE)
        self.up()
        self.assertEqual(self.deploy("local").returncode, 0)
        self.assertIn("COMPOSE_FILE=docker-compose.yml:isolation.yaml:local-source.yaml", (self.work / ".env").read_text())

    def test_deploy_needs_a_rehearsal_and_a_valid_mode(self):
        self.assertEqual(self.deploy().returncode, 1)
        self.up()
        self.assertEqual(self.deploy("elsewhere").returncode, 1)
        self.assertEqual(self.deploy("git").returncode, 1, "the old name of the mode is gone")

    # -- snapshot and compare ---------------------------------------------------------------------

    def test_a_snapshot_records_rows_fingerprints_containers_and_log_errors(self):
        self.up()
        text = (self.work / "snapshots" / "00-start.txt").read_text()
        self.assertRegex(text, r"(?m)^T\tusers\t10$")
        self.assertRegex(text, r"(?m)^H\tfocalboard_blocks\t0123456789abcdef$")
        self.assertRegex(text, r"(?m)^H\tteams\t0123456789abcdef$")
        self.assertNotIn("focalboard_boards", [line.split("\t")[1] for line in text.splitlines() if line.startswith("H")],
                         "a table that does not exist is not fingerprinted")
        self.assertRegex(text, r"(?m)^C\tsvc\t")
        self.assertRegex(text, r"(?m)^L\tmattermost\t1$")

    def test_snapshot_names_are_checked_and_never_overwritten(self):
        self.up()
        self.assertEqual(self.run_script("snapshot", "00-start").returncode, 1)
        self.assertEqual(self.run_script("snapshot", "../evil").returncode, 1)
        self.assertEqual(self.run_script("snapshot", "ready").returncode, 0)

    def write_snapshots(self, before: str, after: str):
        (self.work / "snapshots").mkdir(parents=True, exist_ok=True)
        (self.work / ".rehearsal").write_text("")
        (self.work / "snapshots" / "a.txt").write_text(before)
        (self.work / "snapshots" / "b.txt").write_text(after)

    BEFORE = ("T\tusers\t100\nT\tposts\t0\nT\tteams\t3\nT\tsessions\t9\nT\tfocalboard_blocks\t50\n"
              "H\tfocalboard_blocks\taaa\nH\tteams\tccc\n"
              "C\tmattermost\tid1\ts1\t0\tmm\nC\tpostgres\tpg\ts0\t0\tpg\nL\tmattermost\t2\n")

    def test_what_the_bot_is_expected_to_change_is_reported_as_expected(self):
        after = (self.BEFORE.replace("users\t100", "users\t101").replace("posts\t0", "posts\t12")
                 .replace("sessions\t9", "sessions\t4").replace("id1", "id2") + "C\treminder\tr1\ts2\t0\tbot\n")
        self.write_snapshots(self.BEFORE, after)
        done = self.run_script("compare", "a", "b")
        self.assertEqual(done.returncode, 0, done.stdout)
        self.assertIn("expected: the bot account", done.stdout)
        self.assertIn("expected: the reminders", done.stdout)
        self.assertRegex(done.stdout, r"mattermost\s+RECREATED")
        self.assertRegex(done.stdout, r"reminder\s+NEW")
        self.assertRegex(done.stdout, r"postgres\s+unchanged")
        self.assertRegex(done.stdout, r"focalboard_blocks\s+identical")
        self.assertIn("VERDICT OK", done.stdout)

    def test_a_table_nobody_expected_to_change_is_flagged_for_review_not_failed(self):
        after = self.BEFORE + "T\tmystery\t4\n"
        self.write_snapshots(self.BEFORE + "T\tmystery\t1\n", after)
        done = self.run_script("compare", "a", "b")
        self.assertEqual(done.returncode, 0)
        self.assertRegex(done.stdout, r"mystery\s+1 -> 4 \(\+3\)\s+REVIEW")
        self.assertIn("VERDICT REVIEW", done.stdout)

    def test_changed_cards_lost_rows_or_a_vanished_container_fail(self):
        for name, after in (
                ("a card was edited", self.BEFORE.replace("blocks\taaa", "blocks\tzzz")),
                ("a board was added", self.BEFORE.replace("focalboard_blocks\t50", "focalboard_blocks\t51")),
                ("users were lost", self.BEFORE.replace("users\t100", "users\t99")),
                ("a container vanished", self.BEFORE.replace("C\tpostgres\tpg\ts0\t0\tpg\n", ""))):
            with self.subTest(change=name):
                self.write_snapshots(self.BEFORE, after)
                done = self.run_script("compare", "a", "b")
                self.assertEqual(done.returncode, 1, done.stdout)
                self.assertIn("VERDICT FAIL", done.stdout)

    def test_new_errors_in_the_logs_are_reported(self):
        self.write_snapshots(self.BEFORE, self.BEFORE.replace("L\tmattermost\t2", "L\tmattermost\t9"))
        done = self.run_script("compare", "a", "b")
        self.assertIn("REVIEW: 7 new", done.stdout)

    def test_compare_needs_two_existing_snapshots(self):
        self.write_snapshots(self.BEFORE, self.BEFORE)
        self.assertEqual(self.run_script("compare", "a", "missing").returncode, 1)
        self.assertEqual(self.run_script("compare", "a").returncode, 2)

    # -- down -------------------------------------------------------------------------------------

    def test_down_removes_the_copy_and_nothing_else(self):
        self.up()
        done = self.run_script("down")
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertFalse(self.work.exists())
        self.assertTrue([line for line in self.calls() if "down -v --remove-orphans" in line])
        self.assertTrue(self.before.exists() and self.dump.exists(), "the inputs are never touched")

    def test_down_refuses_a_folder_the_script_did_not_make(self):
        self.work.mkdir()
        (self.work / "precious.txt").write_text("mine")
        done = self.run_script("down")
        self.assertEqual(done.returncode, 1)
        self.assertTrue((self.work / "precious.txt").exists())

    def test_without_a_command_it_prints_the_usage(self):
        done = self.run_script()
        self.assertEqual(done.returncode, 2)
        self.assertIn("rehearse.sh up BEFORE_COMPOSE DUMP", done.stdout)
        self.assertNotIn("(end of usage)", done.stdout)


class Documents(unittest.TestCase):
    def test_the_dump_command_lives_in_one_place_and_the_readme_points_to_it(self):
        readme = (REHEARSAL / "README.md").read_text(encoding="utf-8")
        sandbox = (ROOT / "dev" / "sandbox" / "README.md").read_text(encoding="utf-8")
        anchor = "../sandbox/README.md#1-make-the-dump-on-the-production-server"
        self.assertIn(anchor, readme)
        self.assertIn("### 1. Make the dump (on the production server)", sandbox, "the anchor must exist")
        self.assertIn("--exclude-table-data='posts'", sandbox)

    def test_the_readme_says_how_to_practise_and_what_it_cannot_prove(self):
        readme = (REHEARSAL / "README.md").read_text(encoding="utf-8")
        for needle in ("rehearse.sh up", "rehearse.sh deploy", "rehearse.sh snapshot", "rehearse.sh compare",
                       "rehearse.sh down", "What the rehearsal does not prove", "docker compose up -d"):
            with self.subTest(needle=needle):
                self.assertIn(needle, readme)

    def test_the_work_folder_and_the_dump_are_kept_out_of_git(self):
        text = (ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("dev/rehearsal/work/", text)


if __name__ == "__main__":
    unittest.main()
