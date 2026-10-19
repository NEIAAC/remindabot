"""The bot itself: its configuration, its connections and its state, and what it can be told to do.

This is the composition root, the role `bot.py` has in other Mattermost bots: `Bot(config)` wires the
pieces together and starts them, and everything else is one of those pieces:

    driver.py          talks to Mattermost            boards.py     talks to the Boards plugin
    scheduler.py       runs the daily pass on time    runner.py     what ONE pass does
    webhook_server.py  receives the slash commands    commands/     what each command does
    store.py           the SQLite state               overrides.py  what administrators changed from chat

The command line (`__main__.py`) only chooses which of the methods below to call:

    Bot.run()                     the daemon: the scheduler plus the webhook server, until stopped
    Bot.once() / .check() / .register_commands()    one-off actions for the person setting it up
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from datetime import datetime
from typing import Callable

from . import render
from .boards import Boards
from .commands import CommandRouter
from .config import Config
from .driver import ApiError, Driver
from .overrides import Overrides
from .register import register_commands, unregister_commands
from .runner import StateError, run_once
from .scheduler import serve
from .store import Store
from .webhook_server import start_server

log = logging.getLogger("reminder")


def heartbeat(cfg: Config) -> Callable[[], None]:
    """A function that touches the heartbeat file; the Docker healthcheck looks at its age."""
    def beat() -> None:
        try:
            cfg.heartbeat_file.write_text(str(time.time()))
        except OSError:
            pass   # a heartbeat that cannot be written must never stop the bot
    return beat


class Bot:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.beat = heartbeat(cfg)
        self.driver = Driver(cfg.mm_url, cfg.bot_token, beat=self.beat)
        self.boards = Boards(cfg.mm_url, cfg.bot_token, beat=self.beat)
        self.lock = threading.Lock()   # held while a pass runs: the daily pass and the chat commands share it

    def new_clients(self) -> tuple[Driver, Boards]:
        """A fresh pair of clients: their caches are per pass, and threads must not share them."""
        return (Driver(self.cfg.mm_url, self.cfg.bot_token, beat=self.beat),
                Boards(self.cfg.mm_url, self.cfg.bot_token, beat=self.beat))

    def open_store(self) -> Store:
        """A fresh connection to the state file: a SQLite connection belongs to one thread."""
        return Store(self.cfg.state_file)

    # -- the daemon ---------------------------------------------------------------------------------

    def run(self) -> int:
        """Run until stopped (SIGTERM): answer chat commands, and run the daily pass on time."""
        cfg = self.cfg
        store = None
        try:
            store = self.open_store()
            effective = Overrides(store).effective(cfg)
        except (sqlite3.Error, OSError) as exc:
            log.error("cannot open the state file %s: %s. Nothing was changed; restore it from a backup, or "
                      "move it away to start afresh (everyone still inside a window is then reminded once more).",
                      cfg.state_file, exc)
            if store is not None:
                store.close()
            return 1
        log.info("started: daily run %s, template %s",
                 f"at {effective.send_at.strftime('%H:%M')} {cfg.tz}" if cfg.daily_pass
                 else "OFF (REMINDER_DAILY_PASS=false: reminders go out only by /remind_send or --once)",
                 cfg.template_file)
        server = self._start_webhook_server()
        try:
            serve(cfg, self.driver, self.boards, store, beat=self.beat, lock=self.lock)
        finally:
            if server is not None:
                server.shutdown()          # stop answering ...
                server.server_close()      # ... and let go of the port
            store.close()                  # the one connection the daemon kept for its whole life
        return 0

    def _start_webhook_server(self):
        """Start the chat-command endpoint, or return None (switched off, or the port is taken)."""
        cfg = self.cfg
        if not cfg.commands_port:
            return None
        router = CommandRouter(cfg, lock=self.lock, clients=self.new_clients, open_store=self.open_store)
        try:
            server = start_server(router, cfg.commands_bind, cfg.commands_port)
        except OSError as exc:   # a busy port must not stop the reminders
            log.error("chat commands are NOT available: cannot listen on %s:%d: %s",
                      cfg.commands_bind, cfg.commands_port, exc)
            return None
        log.info("chat commands: listening on %s:%d (Mattermost calls %s)",
                 cfg.commands_bind, cfg.commands_port, cfg.commands_url)
        return server

    # -- one-off actions: each returns the exit code of the command line ---------------------------------

    def check(self) -> int:
        """Is the token valid, what can the bot see, and is everything it needs in place?"""
        cfg = self.cfg
        try:
            me = self.driver.me()
            teams = self.driver.my_teams()
        except ApiError as exc:
            log.error("cannot talk to Mattermost: %s", exc)
            return 1
        log.info("OK: logged in as @%s, member of %d team(s): %s", me["username"], len(teams),
                 ", ".join(t["name"] for t in teams) or "-")
        store = None
        try:
            try:
                store = Store(cfg.state_file, readonly=True)
                overrides = Overrides(store)
                effective = overrides.effective(cfg)
                paused, tokens = overrides.paused(), overrides.command_tokens()
            except (sqlite3.Error, OSError) as exc:
                log.error("cannot read the state file %s: %s", cfg.state_file, exc)
                return 1
            try:
                _, source = overrides.template(cfg)
            except render.TemplateError as exc:
                log.error("message template problem: %s", exc)
                return 2
        finally:
            if store is not None:
                store.close()
        log.info("message OK (%s), timezone %s, daily run %s%s", source, cfg.tz,
                 f"at {effective.send_at.strftime('%H:%M')}" if cfg.daily_pass else "OFF (REMINDER_DAILY_PASS=false)",
                 " [PAUSED]" if paused else "")
        if cfg.commands_port:
            log.info("chat commands: endpoint on port %d, registered in %d team(s)%s", cfg.commands_port,
                     len(tokens), "" if tokens else " (run --register-commands)")
        else:
            log.info("chat commands: switched off (REMINDER_COMMANDS_PORT=0)")
        return 0

    def once(self, *, dry_run: bool = False, join_only: bool = False) -> int:
        """A single pass now. In a dry run the state file is opened read-only (it is never created)."""
        cfg = self.cfg
        store = None
        try:
            store = Store(cfg.state_file, readonly=dry_run)
            overrides = Overrides(store)
            paused = overrides.paused()
            if paused and not dry_run and not join_only:
                log.warning("sending is PAUSED (by @%s): nothing was sent. /remind_resume lifts it.", paused.get("by"))
                return 0
            template, _ = overrides.template(cfg)    # the message set from chat wins over the file
            summary = run_once(overrides.effective(cfg), self.driver, self.boards, store, template,
                               datetime.now(cfg.tz).date(), dry_run=dry_run, join_only=join_only, beat=self.beat)
        except sqlite3.Error as exc:
            log.error("cannot use the state file %s: %s. Nothing was sent or changed.", cfg.state_file, exc)
            return 1
        except (ApiError, StateError, OSError, render.TemplateError) as exc:
            log.error("run failed: %s", exc)
            return 1
        finally:
            if store is not None:
                store.close()
        log.info("%s", summary.line())
        return 1 if summary.failed else 0

    def register_commands(self, admin_token: str, *, remove: bool = False) -> int:
        """Create (or with `remove`, delete) the slash commands in every team. The administrator's
        token is used for this one call; the bot never keeps it."""
        store = None
        try:
            action = unregister_commands if remove else register_commands
            store = Store(self.cfg.state_file)
            report, ok = action(self.cfg, admin_token, store)
        except (ApiError, sqlite3.Error, OSError) as exc:
            log.error("could not register the commands: %s", exc)
            return 1
        finally:
            if store is not None:
                store.close()
        for line in report:
            log.info("%s", line)
        if not ok:
            log.error("finished with problems, see above")
        elif not remove:
            log.info("done. Try /remind_help in Mattermost.")
        return 0 if ok else 1
