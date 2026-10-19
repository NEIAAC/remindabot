"""The shape of the bot: how it is put together, not what it does.

Other Mattermost bots are recognisable by a few pieces (a Bot that starts everything, a driver, a
scheduler, a webhook server, handlers declared by decorator), and a bot stays maintainable only while
those pieces keep to their jobs. These tests pin that down:

  * the command registry: a command is declared once, with its handler, and nothing else needs touching;
  * the Bot: it wires the scheduler and the webhook server to ONE shared lock, and stops cleanly;
  * the direction of the dependencies (a handler never touches HTTP, the engine knows nothing of chat);
  * docs/development.md lists exactly the files that exist.
"""
import ast
import re
import socket
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

from remindabot import bot as bot_mod
from remindabot.bot import Bot, heartbeat
from remindabot.commands import HANDLER_MODULES, SPEC_BY_TRIGGER, SPECS, command, registry
from remindabot.config import Config
from remindabot.overrides import Overrides
from remindabot.store import Store

from . import test_integration as ti
from .test_commands import CommandsBase

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "src" / "remindabot"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Registry(unittest.TestCase):
    def test_every_command_is_declared_with_a_handler_a_summary_and_the_prefix(self):
        self.assertGreaterEqual(len(SPECS), 15)
        for spec in SPECS:
            with self.subTest(command=spec.trigger):
                self.assertTrue(spec.trigger.startswith("remind_"))
                self.assertTrue(callable(spec.handler))
                self.assertTrue(spec.summary.endswith("."), "one sentence, for /remind_help")
                self.assertIs(SPEC_BY_TRIGGER[spec.trigger], spec)

    def test_a_command_lives_in_one_of_the_handler_modules(self):
        modules = {module.__name__ for module in HANDLER_MODULES}
        for spec in SPECS:
            with self.subTest(command=spec.trigger):
                self.assertIn(spec.handler.__module__, modules)

    def test_help_comes_first_and_the_groups_are_together(self):
        order = [spec.handler.__module__.rsplit(".", 1)[1] for spec in SPECS]
        self.assertEqual(SPECS[0].trigger, "remind_help")
        self.assertEqual(order, sorted(order, key=lambda name: ("show", "control", "adjust").index(name)),
                         "commands are listed group by group: look, act, change")

    def test_declaring_a_trigger_twice_is_refused(self):
        with mock.patch.object(registry, "_commands", []):
            @command("remind_x", summary="One.")
            def first(ctx):
                return "1"
            with self.assertRaises(ValueError):
                command("remind_x", summary="Two.")(first)

    def test_the_slow_ones_are_those_that_read_every_board(self):
        slow = {spec.trigger for spec in SPECS if spec.slow}
        self.assertEqual(slow, {"remind_status", "remind_next", "remind_boards", "remind_card",
                                "remind_send", "remind_resend"})


class RegistryServesANewCommand(CommandsBase):
    def test_a_declared_command_is_served_by_the_router_without_touching_it(self):
        """What 'add a command' in docs/development.md promises: declare it, and it works."""
        with mock.patch.object(registry, "_commands", list(registry.commands())):
            @command("remind_echo", summary="Say it back.", hint="[words]")
            def echo(ctx):
                return f"{ctx.username} said {ctx.args!r} on {ctx.today}"
            store = self.store()
            Overrides(store).set_command_tokens({"team1": {"remind_echo": "tok-echo"}})
            store.close()
            reply = self.commands.handle_command(self.form("remind_echo", "hello", token="tok-echo"))
            self.assertEqual(reply["response_type"], "ephemeral")
            self.assertEqual(reply["text"], f"root said 'hello' on {ti.TODAY}")
            denied = self.commands.handle_command(self.form("remind_echo", "hello", token="tok-echo",
                                                            user=ti.U1, username="alice"))
            self.assertIn("Only system administrators", denied["text"])     # the router's checks come with it


class BotLifecycle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.port = free_port()

    def config(self, **env) -> Config:
        base = {"REMINDER_BOT_TOKEN": "t", "REMINDER_SITE_URL": "https://x.example",
                "REMINDER_MM_URL": "http://127.0.0.1:9", "REMINDER_TEMPLATE_FILE": str(ti.SHIPPED_TEMPLATE),
                "REMINDER_STATE_FILE": str(Path(self.tmp.name) / "state.sqlite"),
                "REMINDER_HEARTBEAT_FILE": str(Path(self.tmp.name) / "heartbeat"),
                "REMINDER_COMMANDS_BIND": "127.0.0.1", "REMINDER_COMMANDS_PORT": str(self.port)}
        return Config.from_env({**base, **env})

    def test_the_chat_endpoint_runs_while_the_scheduler_runs_and_is_stopped_afterwards(self):
        bot = Bot(self.config())
        seen = {}

        def scheduler_stand_in(cfg, driver, boards, store, **kwargs):
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/healthz", timeout=5) as reply:
                seen["health"] = reply.read()
            seen["lock"] = kwargs["lock"]

        with mock.patch.object(bot_mod, "serve", scheduler_stand_in):
            self.assertEqual(bot.run(), 0)
        self.assertEqual(seen["health"], b'"ok"')
        self.assertIs(seen["lock"], bot.lock)
        with self.assertRaises(OSError):                      # the endpoint is gone once the bot stops
            urllib.request.urlopen(f"http://127.0.0.1:{self.port}/healthz", timeout=2)

    def test_the_endpoint_stops_even_when_the_scheduler_dies(self):
        bot = Bot(self.config())
        with mock.patch.object(bot_mod, "serve", side_effect=SystemExit(0)):     # what SIGTERM does
            with self.assertRaises(SystemExit):
                bot.run()
        with self.assertRaises(OSError):
            urllib.request.urlopen(f"http://127.0.0.1:{self.port}/healthz", timeout=2)

    def test_the_commands_and_the_daily_pass_share_one_lock(self):
        """A manual /remind_send and the daily pass must never run at the same moment."""
        bot = Bot(self.config())
        captured = {}
        with mock.patch.object(bot_mod, "start_server", lambda router, bind, port: captured.update(router=router)):
            bot._start_webhook_server()
        self.assertIs(captured["router"].lock, bot.lock)
        self.assertIs(captured["router"].cfg, bot.cfg)

    def test_a_busy_port_does_not_stop_the_reminders(self):
        with socket.socket() as taken:
            taken.bind(("127.0.0.1", 0))
            taken.listen()
            bot = Bot(self.config(REMINDER_COMMANDS_PORT=str(taken.getsockname()[1])))
            with self.assertLogs("reminder", level="ERROR") as logs:
                self.assertIsNone(bot._start_webhook_server())
            self.assertIn("NOT available", "\n".join(logs.output))
            with mock.patch.object(bot_mod, "serve") as serve:
                self.assertEqual(bot.run(), 0)
            serve.assert_called_once()

    def test_port_zero_switches_the_chat_commands_off(self):
        self.assertIsNone(Bot(self.config(REMINDER_COMMANDS_PORT="0"))._start_webhook_server())

    def test_each_invocation_gets_clients_of_its_own(self):
        bot = Bot(self.config())
        first, second = bot.new_clients(), bot.new_clients()
        for a, b in zip(first, second):
            self.assertIsNot(a, b)
        self.assertIsNot(first[0], bot.driver)               # not the daemon's, whose caches belong to one pass

    def count_state_files(self):
        """Patch Store so that every connection opened and every one closed is counted. CPython closes a
        forgotten SQLite connection by itself (Python 3.13 and newer even warn about it), which would
        hide a leak, so the count is the check."""
        count = {"opened": 0, "closed": 0}
        init, close = Store.__init__, Store.close

        def counting_init(store, *args, **kwargs):
            init(store, *args, **kwargs)
            count["opened"] += store.db is not None

        def counting_close(store):
            count["closed"] += store.db is not None
            close(store)
        patches = (mock.patch.object(Store, "__init__", counting_init), mock.patch.object(Store, "close", counting_close))
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        return count

    def test_every_action_lets_go_of_the_state_file_when_it_ends(self):
        cfg = self.config()
        Store(cfg.state_file).close()                                  # a state file that exists
        bot = Bot(cfg)
        bot.driver, bot.boards = mock.Mock(), mock.Mock()
        bot.driver.me.return_value = {"username": "remindabot"}
        bot.driver.my_teams.return_value = [{"name": "nei"}]
        summary = mock.Mock(failed=0, **{"line.return_value": "summary"})
        count = self.count_state_files()
        with mock.patch.object(bot_mod, "run_once", return_value=summary), \
                mock.patch.object(bot_mod, "register_commands", return_value=([], True)), \
                mock.patch.object(bot_mod, "unregister_commands", return_value=([], True)), \
                mock.patch.object(bot_mod, "serve"):
            actions = {"check": bot.check, "once": bot.once, "once --dry-run": lambda: bot.once(dry_run=True),
                       "join-boards": lambda: bot.once(join_only=True),
                       "register-commands": lambda: bot.register_commands("admin-token"),
                       "unregister-commands": lambda: bot.register_commands("admin-token", remove=True),
                       "run (the daemon)": bot.run}
            for name, action in actions.items():
                with self.subTest(action=name):
                    before = dict(count)
                    self.assertEqual(action(), 0)
                    opened, closed = count["opened"] - before["opened"], count["closed"] - before["closed"]
                    self.assertGreater(opened, 0, "the action never opened the state file")
                    self.assertEqual(closed, opened, "a connection to the state file was left open")

    def test_the_heartbeat_is_written_and_a_failure_to_write_it_is_harmless(self):
        cfg = self.config()
        heartbeat(cfg)()
        self.assertTrue(cfg.heartbeat_file.exists())
        heartbeat(self.config(REMINDER_HEARTBEAT_FILE="/nonexistent-dir/heartbeat"))()   # must not raise


class Layering(unittest.TestCase):
    """The direction of the dependencies, read from the imports themselves."""

    @staticmethod
    def imports(path: Path) -> set[str]:
        """Every module `path` imports, as a name relative to the package ("driver", "commands.router",
        "urllib.request"...)."""
        found: set[str] = set()
        depth = len(path.relative_to(PACKAGE).parts) - 1            # 0 for a module of the package itself
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.level:
                base = path.relative_to(PACKAGE).parts[:depth]
                base = base[:len(base) - (node.level - 1)] if node.level > 1 else base
                module = ".".join([*base, *(node.module.split(".") if node.module else [])])
                if node.module:
                    found.add(module)
                else:                                                # `from . import render, boards`
                    found.update(".".join([*base, alias.name]) if base or alias.name else alias.name
                                 for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                found.add(node.module)
            elif isinstance(node, ast.Import):
                found.update(alias.name for alias in node.names)
        return found

    def modules(self, *names: str) -> list[Path]:
        return [PACKAGE / name for name in names]

    def test_the_leaves_import_nothing_of_the_bot(self):
        for path in self.modules("driver.py", "config.py", "render.py", "store.py"):
            with self.subTest(module=path.name):
                local = {m for m in self.imports(path) if (PACKAGE / (m.split(".")[0] + ".py")).exists()
                         or (PACKAGE / m.split(".")[0]).is_dir()}
                self.assertEqual(local, set())

    def test_the_engine_knows_nothing_of_chat_or_http_endpoints(self):
        chat = {"commands", "webhook_server", "bot", "register", "__main__"}
        for path in self.modules("boards.py", "logic.py", "runner.py", "scheduler.py", "alerts.py", "overrides.py"):
            with self.subTest(module=path.name):
                self.assertFalse({m.split(".")[0] for m in self.imports(path)} & chat)

    def test_commands_do_not_reach_up_into_the_things_that_start_them(self):
        up = {"webhook_server", "scheduler", "bot", "register", "__main__"}
        for path in sorted((PACKAGE / "commands").glob("*.py")):
            with self.subTest(module=path.name):
                self.assertFalse({m.split(".")[0] for m in self.imports(path)} & up)

    def test_a_command_handler_never_touches_http(self):
        for module in HANDLER_MODULES:
            path = Path(module.__file__)
            with self.subTest(module=path.name):
                names = {m.split(".")[0] for m in self.imports(path)}
                self.assertFalse(names & {"urllib", "http", "socket", "requests"},
                                 "a handler talks to Mattermost through ctx.driver")

    def test_the_webhook_server_only_knows_the_router(self):
        local = {m for m in self.imports(PACKAGE / "webhook_server.py") if m.startswith("commands")}
        self.assertEqual(local, {"commands"})

    def test_only_the_command_line_starts_the_bot(self):
        for path in sorted(PACKAGE.rglob("*.py")):
            if path.name in ("__main__.py", "bot.py"):
                continue
            with self.subTest(module=str(path.relative_to(PACKAGE))):
                self.assertNotIn("bot", {m.split(".")[0] for m in self.imports(path)})


class LayoutIsDocumented(unittest.TestCase):
    """docs/development.md lists the files; it must list the files that exist, and only those."""

    def documented(self) -> tuple[set[str], set[str]]:
        text = (ROOT / "docs" / "development.md").read_text(encoding="utf-8")
        block = text.split("## The layout")[1].split("```")[1]
        top, nested = set(), set()
        for line in block.splitlines():
            match = re.match(r"^( {2}| {4})([a-z_]+\.py)\s", line)
            if match:
                (top if len(match.group(1)) == 2 else nested).add(match.group(2))
        return top, nested

    def test_every_module_is_in_the_list_and_every_listed_module_exists(self):
        top, nested = self.documented()
        real_top = {p.name for p in PACKAGE.glob("*.py") if p.name != "__init__.py"}
        real_nested = {p.name for p in (PACKAGE / "commands").glob("*.py") if p.name != "__init__.py"}
        self.assertEqual(top, real_top)
        self.assertEqual(nested, real_nested)


if __name__ == "__main__":
    unittest.main()
