"""The scheduler: decides WHEN the daily pass runs, and what to do when it fails.

`serve()` is the loop the daemon runs (see bot.py): once a minute it looks at the clock, and once it
is past the send time and today's pass is not done, it runs one pass (runner.py). It never exits on
its own, whatever goes wrong inside a pass. What a pass DOES is in runner.py, the reminder rule is in
logic.py, and everything about Boards is in boards.py.
"""
from __future__ import annotations

import contextlib
import logging
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta
from typing import Callable, Optional

from . import alerts, render
from .boards import Boards
from .config import Config
from .driver import ApiError, Driver
from .overrides import Overrides
from .runner import StateError, run_once
from .store import Store

log = logging.getLogger("reminder")

# How often the daemon wakes up to look at the clock.
POLL_S = 60
# How long to wait before trying a failed pass again.
RETRY_AFTER_S = 600
# The first pass of a day plus up to three retries of messages that failed.
MAX_ATTEMPTS_PER_DAY = 4


def serve(
    cfg: Config,
    driver: Driver,
    boards: Boards,
    store: Store,
    *,
    now_fn: Optional[Callable[[], datetime]] = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    beat: Callable[[], None] = lambda: None,
    max_iterations: Optional[int] = None,
    lock: Optional[threading.Lock] = None,
) -> None:
    """The daemon: run one pass per day at/after the send time, catching up after a restart.

    The send time, the daily caps and the message can be overridden from chat (overrides.py), and
    sending can be paused; all of that is read again every iteration, so a change made from chat
    takes effect within a minute without a restart. `lock` is shared with the chat commands so that
    a manual `/remind_send` and the daily pass never run at the same moment.

    It never exits on its own, whatever goes wrong inside a pass:
      * Mattermost unreachable / bad template / unexpected error -> try again in RETRY_AFTER_S;
      * some messages failed or are in progress elsewhere -> try those again (the state file skips
        everything already sent), at most MAX_ATTEMPTS_PER_DAY times, then wait for tomorrow;
      * the state file cannot be written -> stop for the rest of the day (duplicates could no
        longer be prevented).

    The clock, the sleep and the iteration limit are parameters only so that tests can run a
    simulated week in a second; the daemon uses the defaults.
    """
    now_fn = now_fn or (lambda: datetime.now(cfg.tz))
    overrides = Overrides(store)
    pass_lock = lock or contextlib.nullcontext()

    # Bookkeeping, all "for today" and reset when the date changes:
    attempts_day: Optional[date] = None        # the date `attempts` and `next_try_at` belong to
    attempts = 0                               # passes started today
    next_try_at: Optional[datetime] = None     # do not start another pass before this moment
    done_day: Optional[date] = None            # the date whose pass is complete (or given up)
    halted_day: Optional[date] = None          # the date we stopped because the state is broken
    pause_logged_day: Optional[date] = None    # the date we already said "paused"
    iteration = 0

    while max_iterations is None or iteration < max_iterations:
        iteration += 1
        beat()                                 # tells the container healthcheck we are alive
        now = now_fn()
        today = now.date()
        if attempts_day != today:              # a new day: forget yesterday's retries
            attempts_day, attempts, next_try_at = today, 0, None

        try:    # what an administrator may have changed from chat since the last look
            effective, paused, settings_ok = overrides.effective(cfg), overrides.paused(), True
        except sqlite3.Error as exc:
            # "paused" is unknown, so this minute must not send anything (a later minute will)
            log.error("cannot read the settings from the state file; not sending this minute: %s", exc)
            effective, paused, settings_ok = cfg, None, False

        # Is it time? After the send time, not already done today, not halted, not paused, not
        # waiting to retry.
        due_time = (effective.daily_pass and now.time() >= effective.send_at
                    and done_day != today and halted_day != today)
        if paused and due_time and pause_logged_day != today:
            pause_logged_day = today
            log.warning("sending is PAUSED (by @%s): today's pass waits for /remind_resume", paused.get("by"))
        ready = (
            due_time
            and settings_ok
            and not paused
            and (next_try_at is None or now >= next_try_at)
        )
        if ready:
            # A previous process may already have finished today's pass (e.g. before a restart).
            ready = _last_run(store) != today.isoformat()
            if not ready:
                done_day = today

        if ready:
            attempts += 1

            def retry_at() -> datetime:
                """Ten minutes from the moment the pass ENDED (a slow pass must not retry at once)."""
                return now_fn() + timedelta(seconds=RETRY_AFTER_S)

            def alert(kind: str, text: str) -> None:
                alerts.notify(driver, overrides, kind, text, today.isoformat())

            try:
                # Re-read the message every pass (the file, or the one set from chat), so edits take
                # effect without a restart.
                template, _ = overrides.template(cfg)
                with pass_lock:
                    summary = run_once(effective, driver, boards, store, template, today, beat=beat)
            except render.TemplateError as exc:
                log.error("message template problem, nothing sent (retrying in %d min): %s",
                          RETRY_AFTER_S // 60, exc)
                next_try_at = retry_at()
                alert("template", f"The message template has a problem, so nothing was sent:\n{exc}")
            except StateError as exc:
                log.critical("%s. Stopping for today to avoid duplicate messages; fix the state volume.", exc)
                halted_day = today
                alert("state", f"The reminder state file cannot be used, so sending STOPPED for today to avoid "
                               f"duplicates:\n{exc}")
            except ApiError as exc:
                log.error("pass failed (retrying in %d min): %s", RETRY_AFTER_S // 60, exc)
                next_try_at = retry_at()
                alert("api", f"Today's pass failed (it is retried every {RETRY_AFTER_S // 60} minutes):\n{exc}")
            except Exception:  # noqa: BLE001 - the loop must survive anything
                log.exception("unexpected error in the daily pass (retrying in %d min)", RETRY_AFTER_S // 60)
                next_try_at = retry_at()
                alert("error", "Today's pass hit an unexpected error (see the bot's log); it is retried.")
            else:
                log.info("daily pass finished: %s", summary.line())
                setup = list(summary.problems)
                if summary.no_notifyees:
                    setup.append(f"{summary.no_notifyees} card(s) are due a reminder but have nobody in "
                                 f"{cfg.prop_notifyees!r}")
                if setup:
                    alert("setup", "Something on the boards needs a person:\n" + "\n".join(f"- {t}" for t in setup))
                if summary.capped:
                    alert("cap", f"A daily safety cap held back {summary.capped} reminder(s) today "
                                 f"(limits now {effective.max_per_day} per day, {effective.max_per_user} per "
                                 "person; `/remind_setcap` changes them).")
                # "failed" = transient problems; "deferred" = another process was mid-send. Both
                # deserve another look later today. Held-back (capped) items do not: the cap is
                # per day, so retrying cannot help.
                unfinished = summary.failed + summary.deferred
                if unfinished and attempts < MAX_ATTEMPTS_PER_DAY:
                    log.warning("%d item(s) not finished (%d failed, %d in progress elsewhere), "
                                "retrying in %d min (attempt %d of %d)", unfinished, summary.failed,
                                summary.deferred, RETRY_AFTER_S // 60, attempts, MAX_ATTEMPTS_PER_DAY)
                    next_try_at = retry_at()
                else:
                    if unfinished:
                        log.error("giving up for today with %d unfinished item(s); they are retried tomorrow",
                                  unfinished)
                        alert("unfinished", f"{unfinished} reminder(s) could not be sent today after "
                                            f"{attempts} attempts; they are retried tomorrow.")
                    done_day = today   # in memory, so even a broken state file cannot make us loop
                    try:
                        store.set_last_run(today.isoformat())   # ... and on disk, for restarts
                        store.prune()
                    except sqlite3.Error as exc:
                        log.error("could not record the run in the state file: %s", exc)

        _sleep_with_beat(sleep_fn, beat, POLL_S)


def _last_run(store: Store) -> Optional[str]:
    """Date of the last completed pass, or None (also when the state cannot be read)."""
    try:
        return store.last_run()
    except sqlite3.Error as exc:
        log.error("cannot read the state file: %s", exc)
        return None


def _sleep_with_beat(sleep_fn: Callable[[float], None], beat: Callable[[], None], seconds: int) -> None:
    """Sleep in short slices so the healthcheck heartbeat stays fresh."""
    remaining = seconds
    while remaining > 0:
        step = min(30, remaining)
        sleep_fn(step)
        remaining -= step
        beat()
