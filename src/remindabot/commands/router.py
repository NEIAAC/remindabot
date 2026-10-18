"""Receives a slash command (or a pop-up form) from the HTTP endpoint, checks it, runs its handler.

HOW A COMMAND REACHES A HANDLER
  An administrator types `/remind_status` in Mattermost. Mattermost posts the command to the bot's
  small HTTP endpoint (webhook_server.py) with the user's id and a secret token.
  `CommandRouter.handle_command` then:

    1. checks the token (so only Mattermost can call it, see register.py),
    2. asks Mattermost, NOW, whether that user is a system administrator,
    3. runs the handler of the command and returns its reply, which only that person sees.

  Commands that need to read every board (status, send, ...) take a few seconds, longer than
  Mattermost waits, so they answer "working..." at once and deliver the result a moment later
  through the `response_url` Mattermost provided.

SAFETY
  * Every command re-checks the caller's role. Nothing is cached about who is an administrator.
  * A handler gets a context (context.py) and returns text; it never touches HTTP, and nothing it
    returns reaches Mattermost without going through `markup.ephemeral`.
"""
from __future__ import annotations

import hmac
import logging
import sqlite3
import threading
from datetime import datetime
from typing import Callable, Optional

from .. import render
from ..boards import Boards
from ..config import Config
from ..driver import ApiError, Driver
from ..overrides import Overrides, SettingError
from ..runner import StateError
from ..store import Store
from . import markup as md
from .context import Ctx, DialogCtx
from .registry import Reply, Spec, find_command, find_dialog

log = logging.getLogger("reminder")

# Commands that read every board each cost Mattermost a few hundred requests. However often they are
# typed, no more than this many run at once; the next one is told to wait (a flood must not become a
# load on the chat server).
MAX_SLOW_COMMANDS = 3


class CommandRouter:
    """Runs the chat commands. `handle_command` and `handle_dialog` are what the HTTP layer calls."""

    def __init__(
        self,
        cfg: Config,
        *,
        clients: Callable[[], tuple[Driver, Boards]],
        open_store: Callable[[], Store],
        lock: Optional[threading.Lock] = None,
        now_fn: Optional[Callable[[], datetime]] = None,
        background: Optional[Callable[[Callable[[], None]], None]] = None,
    ):
        self.cfg = cfg
        self.clients = clients            # a fresh (Driver, Boards) pair: caches are per invocation
        self.open_store = open_store      # a fresh Store: a SQLite connection belongs to one thread
        self.lock = lock or threading.Lock()   # the same lock the daily loop holds while it runs a pass
        self.now_fn = now_fn or (lambda: datetime.now(cfg.tz))
        self.background = background or self._in_thread
        self._slow_slots = threading.BoundedSemaphore(MAX_SLOW_COMMANDS)

    @staticmethod
    def _in_thread(fn: Callable[[], None]) -> None:
        threading.Thread(target=fn, daemon=True, name="remind-command").start()

    # -- entry points ----------------------------------------------------------------------------

    def handle_command(self, form: dict) -> dict:
        """One slash command. `form` is what Mattermost posted. Returns the JSON to answer with."""
        trigger = (form.get("command") or "").lstrip("/").strip().lower()
        spec = find_command(trigger)
        if spec is None:
            return md.ephemeral(f"Unknown command `/{trigger[:40]}`. Try `/remind_help`.")
        store = self.open_store()
        try:
            overrides = Overrides(store)
            if not self._token_ok(overrides, form, trigger):
                log.warning("command /%s from %r rejected: wrong or missing token", trigger, form.get("user_name"))
                return md.ephemeral(":no_entry: This command is not registered with the bot (wrong token). "
                                    "An administrator must run `python -m remindabot --register-commands`.")
            driver, boards = self.clients()
            user_id = form.get("user_id") or ""
            try:
                admin = driver.is_system_admin(user_id)
            except ApiError as exc:
                log.error("command /%s: cannot verify the caller's role: %s", trigger, exc)
                return md.ephemeral(":warning: Could not verify that you are a system administrator "
                                    "(Mattermost did not answer). Nothing was done; try again.")
            if not admin:
                log.info("command /%s refused for @%s: not a system administrator", trigger, form.get("user_name"))
                return md.ephemeral(":no_entry: Only system administrators can use the reminder commands.")

            ctx = self._context(overrides, store, driver, boards, form)
            log.info("command /%s %r by @%s", trigger, ctx.args[:100], ctx.username)
            if not spec.slow:
                return self._reply(self._run(spec, ctx))
            if not self._slow_slots.acquire(blocking=False):
                log.warning("command /%s refused for @%s: %d slow commands are already running", trigger,
                            ctx.username, MAX_SLOW_COMMANDS)
                return md.ephemeral(f":hourglass: {MAX_SLOW_COMMANDS} commands that read every board are already "
                                    "running. Try again in a minute.")
            response_url = form.get("response_url") or ""
            try:
                self.background(lambda: self._run_later(spec, form, response_url))
            except BaseException:
                self._slow_slots.release()
                raise
            return md.ephemeral(f":hourglass_flowing_sand: Working on `/{trigger}` …")
        finally:
            store.close()

    def handle_dialog(self, payload: dict) -> dict:
        """The submission of a pop-up form (today only /remind_setmessage's)."""
        if payload.get("cancelled"):
            return {}
        spec = find_dialog(payload.get("callback_id"))
        store = self.open_store()
        try:
            overrides = Overrides(store)
            user_id = payload.get("user_id") or ""
            if spec is None or not overrides.verify_state(payload.get("state"), spec.callback_id, user_id):
                log.warning("dialog submission rejected: bad or expired state (user %s)", user_id[:8])
                return {"error": f"This form has expired. Type {spec.again if spec else 'the command'} again."}
            driver, _ = self.clients()
            try:
                if not driver.is_system_admin(user_id):
                    return {"error": "Only system administrators can use this form."}
                user = driver.fresh_user(user_id)
            except ApiError:
                return {"error": "Could not verify your role; try again."}
            return spec.handler(DialogCtx(cfg=overrides.effective(self.cfg), overrides=overrides, driver=driver,
                                          user=user, user_id=user_id, payload=payload))
        finally:
            store.close()

    # -- plumbing ----------------------------------------------------------------------------------------

    def _token_ok(self, overrides: Overrides, form: dict, trigger: str) -> bool:
        expected = overrides.command_tokens().get(form.get("team_id") or "", {}).get(trigger)
        given = form.get("token") or ""
        return bool(expected) and hmac.compare_digest(str(expected), str(given))

    def _context(self, overrides: Overrides, store: Store, driver: Driver, boards: Boards, form: dict) -> Ctx:
        return Ctx(cfg=overrides.effective(self.cfg), overrides=overrides, store=store, driver=driver,
                   boards=boards, lock=self.lock, user_id=form.get("user_id") or "",
                   username=form.get("user_name") or "", args=(form.get("text") or "").strip(),
                   trigger_id=form.get("trigger_id") or "", now=self.now_fn())

    @staticmethod
    def _reply(result: Reply) -> dict:
        """A handler returns text (shown to its author) or the complete answer (a dict)."""
        return result if isinstance(result, dict) else md.ephemeral(result)

    def _run(self, spec: Spec, ctx: Ctx) -> Reply:
        """Run one handler. Never raises: a failure becomes a message."""
        try:
            return spec.handler(ctx)
        except SettingError as exc:
            return f":warning: {exc}"
        except render.TemplateError as exc:
            return f":warning: The message template has a problem, nothing was done: {md.text(exc, 300)}"
        except StateError as exc:
            log.error("command %s: %s", spec.trigger, exc)
            return f":rotating_light: The reminder state file has a problem, nothing was done: {exc}"
        except ApiError as exc:
            log.error("command %s: Mattermost error: %s", spec.trigger, exc)
            return f":warning: Mattermost returned an error, nothing more was done: {md.text(exc, 200)}"
        except sqlite3.Error as exc:
            log.error("command %s: state file error: %s", spec.trigger, exc)
            return f":rotating_light: The reminder state file could not be used: {md.text(exc, 200)}"
        except Exception:  # noqa: BLE001 - a command must never leave the person without an answer
            log.exception("command %s failed", spec.trigger)
            return ":rotating_light: Unexpected error; it was logged. Nothing more was done."

    def _run_later(self, spec: Spec, form: dict, response_url: str) -> None:
        """The slow half of a command: its own store and clients (this runs in another thread)."""
        try:
            store = self.open_store()
            try:
                driver, boards = self.clients()
                ctx = self._context(Overrides(store), store, driver, boards, form)
                result = self._run(spec, ctx)
                try:
                    driver.answer_later(response_url,
                                        md.fit(result if isinstance(result, str) else str(result.get("text", ""))))
                except ApiError as exc:
                    log.error("could not deliver the answer of /%s: %s", spec.trigger, exc)
            finally:
                store.close()
        finally:
            self._slow_slots.release()
