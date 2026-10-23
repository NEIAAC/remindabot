"""The commands that DO something: send, resend, test, pause, resume.

Sending goes through `run_once`, the same function as the daily pass, and under the same lock, so
a command can never do something the daily pass could not (a duplicate, a cap bypass).
"""
from __future__ import annotations

import logging
from datetime import timedelta

from .. import render
from ..driver import ApiError, display_name
from ..overrides import SettingError
from ..runner import Summary, run_once
from . import markup as md
from .cards import find_one
from .context import Ctx
from .registry import command

log = logging.getLogger("reminder")


def _send_report(title: str, summary: Summary) -> str:
    lines = [f":white_check_mark: **{title}.** {summary.sent} sent, {summary.already_sent} already sent before, "
             f"{summary.failed} failed."]
    if summary.capped:
        lines.append(f"- :stop_sign: {summary.capped} held back by the daily safety cap.")
    if summary.deferred:
        lines.append(f"- :arrows_counterclockwise: {summary.deferred} are being sent elsewhere; try again later.")
    if summary.no_notifyees:
        lines.append(f"- :warning: {summary.no_notifyees} card(s) due a reminder have nobody in Notifyees.")
    if summary.failed:
        lines.append("- :warning: Failures are retried by the next pass; see the log for the reason.")
    sent = [i for i in summary.items if i.state == "sent"]
    if sent:
        lines.append("\n" + md.table(["To", "Card", "Board", "Reminder"],
                                     [[md.person(i.username, i.user_id), md.text(i.card_title),
                                       md.text(i.board_title, 40), md.stage(i.stage)] for i in sent]))
    return "\n".join(lines)


@command("remind_send", slow=True, summary="Run a reminder pass now: check every card and send what is due.")
def send(ctx: Ctx) -> str:
    paused = ctx.paused_text()
    if paused:
        return paused
    if not ctx.lock.acquire(timeout=2):
        return ":hourglass: A reminder pass is already running. Try again in a minute."
    try:
        summary = run_once(ctx.cfg, ctx.driver, ctx.boards, ctx.store, ctx.template(), ctx.today)
    finally:
        ctx.lock.release()
    return _send_report("Reminder pass finished", summary)


@command("remind_resend", hint="<card link or title> [@user]", slow=True,
         summary="Send today's reminder of one card again.")
def resend(ctx: Ctx) -> str:
    paused = ctx.paused_text()
    if paused:
        return paused
    words = ctx.args.split()
    only_user = None
    if words and words[-1].startswith("@"):
        only_user = words.pop()[1:].casefold()
    scan = ctx.scan()
    report = find_one(scan, " ".join(words))
    if not report.send or report.stage is None or report.due is None:
        raise SettingError(f"No reminder is due today for {md.text(report.title)}: {md.text(report.reason, 150)} "
                           "`/remind_resend` only repeats the reminder that is due today.")
    targets = []
    for user_id in dict.fromkeys(report.notifyees):
        try:
            user = ctx.driver.user(user_id)
        except ApiError:
            continue
        if only_user and (user.get("username") or "").casefold() != only_user:
            continue
        targets.append(user_id)
    if not targets:
        raise SettingError("That person is not in the card's Notifyees." if only_user
                           else "The card has nobody to remind.")
    if not ctx.lock.acquire(timeout=2):
        return ":hourglass: A reminder pass is already running. Try again in a minute."
    try:
        for user_id in targets:
            ctx.store.forget(report.card_id, user_id, report.due.isoformat(), report.stage)
        summary = run_once(ctx.cfg, ctx.driver, ctx.boards, ctx.store, ctx.template(), ctx.today,
                           only_cards={report.card_id})
    finally:
        ctx.lock.release()
    log.warning("reminder of %r re-sent by @%s", report.title, ctx.username)
    return _send_report(f"Resent the reminder of {md.text(report.title)}", summary)


@command("remind_test", summary="Send yourself a sample reminder, to preview the wording and the link.")
def test(ctx: Ctx) -> str:
    due = ctx.today + timedelta(days=7)
    message = render.render(ctx.template(), {
        "responsible_name": render.sanitize_inline(display_name(ctx.driver.fresh_user(ctx.user_id)), 80,
                                                   fallback=render.UNKNOWN),
        "co_responsibles": "Ana, Rui", "card_title": "Example card", "board_title": "Example board",
        **render.date_values(due), "days_left": "7", "remind_before": "7",
        "card_url": f"{ctx.cfg.site_url}/boards",
    })
    ctx.driver.send_dm(ctx.user_id,
                       ":test_tube: _Preview: this is not a real reminder and nobody else got it._\n\n" + message)
    return ":test_tube: A preview was sent to you as a direct message from the bot."


@command("remind_pause", summary="Stop all sending until /remind_resume.")
def pause(ctx: Ctx) -> str:
    if ctx.overrides.paused():
        return ":pause_button: Sending was already paused."
    ctx.overrides.pause(ctx.username)
    log.warning("sending PAUSED by @%s", ctx.username)
    return (":pause_button: Sending is **paused**: the daily pass, `/remind_send`, `/remind_resend` and "
            "`--once` will not send until you run `/remind_resume`. Previews and status still work.")


@command("remind_resume", summary="Start sending again.")
def resume(ctx: Ctx) -> str:
    if not ctx.overrides.resume():
        return ":arrow_forward: Sending was not paused."
    log.warning("sending RESUMED by @%s", ctx.username)
    return (":arrow_forward: Sending is **active** again. If today's pass is overdue it runs within a minute "
            "(`/remind_status` shows when).")
