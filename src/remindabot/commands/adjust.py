"""The commands that CHANGE a setting: setmessage, setsend, setcap, reset, alerts.

What they change is stored by `Overrides` (overrides.py) in the state file, on top of the
environment and the message file; `/remind_reset` removes the change again.
"""
from __future__ import annotations

import logging

from .. import render
from ..driver import ApiError
from ..overrides import MAX_MESSAGE_CHARS, SettingError, parse_cap
from . import markup as md
from .context import Ctx, DialogCtx
from .registry import command, dialog

log = logging.getLogger("reminder")


@command("remind_setmessage", summary="Edit the reminder message in a pop-up form.")
def setmessage(ctx: Ctx) -> dict:
    """Opens the form. The answer to the command itself is empty: the form is the answer."""
    body = ctx.overrides.message_body(ctx.cfg)
    if len(body) > MAX_MESSAGE_CHARS:
        return md.ephemeral(f":warning: The current message is longer than the pop-up form can hold "
                            f"({MAX_MESSAGE_CHARS} characters). Shorten the file, or use `/remind_reset message`.")
    variables = ", ".join(f"`${n}`" for n in render.PLACEHOLDERS)
    form = {
        "callback_id": "setmessage",
        "title": "Reminder message",
        "submit_label": "Save",
        "notify_on_cancel": False,
        "state": ctx.overrides.sign_state("setmessage", ctx.user_id),
        "introduction_text": f"Variables you can use: {variables}. Use `$$` for a literal dollar sign. "
                             "Markdown works. `/remind_test` previews it.",
        "elements": [{"display_name": "Message", "name": "message", "type": "textarea",
                      "default": body, "max_length": MAX_MESSAGE_CHARS, "optional": False,
                      "help_text": "Sent to each notifyee of a card."}],
    }
    try:
        ctx.driver.open_dialog(ctx.trigger_id, ctx.cfg.commands_url.rstrip("/") + "/dialog", form)
    except ApiError as exc:
        log.error("could not open the message form: %s", exc)
        return md.ephemeral(f":warning: Could not open the form: {md.text(exc, 200)}")
    return {}


@dialog("setmessage", again="/remind_setmessage")
def save_message(form: DialogCtx) -> dict:
    """The form was submitted by a system administrator (the router checked): validate and save."""
    text = str((form.payload.get("submission") or {}).get("message") or "")
    who = form.user.get("username") or form.user_id
    try:
        form.overrides.set_message(text, who)
    except SettingError as exc:
        return {"errors": {"message": str(exc)[:500]}}      # shown next to the field, inside the form
    log.info("the message was changed by @%s (%d characters)", who, len(text))
    try:    # a normal message, so the confirmation stays in the person's direct messages
        form.driver.send_dm(form.user_id, ":white_check_mark: The reminder message was updated. "
                                          "`/remind_test` sends you a preview, `/remind_reset message` goes back "
                                          "to the file.")
    except ApiError as exc:
        log.warning("could not confirm the new message by direct message: %s", exc)
    return {}


@command("remind_setsend", hint="HH:MM", summary="Set the time of day of the daily pass.")
def setsend(ctx: Ctx) -> str:
    if not ctx.args:
        raise SettingError("Give the time as HH:MM, for example `/remind_setsend 09:30`.")
    value = ctx.overrides.set_send_at(ctx.args.split()[0], ctx.username)
    log.warning("daily send time set to %s by @%s", value.strftime("%H:%M"), ctx.username)
    if not ctx.cfg.daily_pass:
        when = "The daily pass is switched off here (`REMINDER_DAILY_PASS=false`), so nothing runs by itself."
    elif ctx.store.last_run() == ctx.today.isoformat():
        when = "Today's pass already ran, so this takes effect tomorrow."
    elif ctx.now.time() < value:
        when = f"The next pass is today at {value.strftime('%H:%M')}."
    else:
        when = "That time has already passed today, so the pass runs within a minute."
    return f":alarm_clock: The daily pass now runs at **{value.strftime('%H:%M')}** ({ctx.cfg.tz}). {when}"


@command("remind_setcap", hint="<total> [per person]  or  total=N person=N", summary="Set the daily safety caps.")
def setcap(ctx: Ctx) -> str:
    if not ctx.args:
        raise SettingError("Write for example `/remind_setcap 200 30` (200 per day, 30 per person) "
                           "or `/remind_setcap person=10`.")
    total = person = None
    bare = []
    for word in ctx.args.replace(",", " ").split():
        key, _, value = word.partition("=")
        if value:
            if key.lower() in ("total", "day", "per_day"):
                total = parse_cap(value, "The daily total")
            elif key.lower() in ("person", "user", "per_person"):
                person = parse_cap(value, "The per-person limit")
            else:
                raise SettingError(f"Unknown name `{md.text(key, 20)}`: use `total=` or `person=`.")
        else:
            bare.append(word)
    if len(bare) > 2:
        raise SettingError("Too many numbers: give the daily total, then the per-person limit.")
    if bare:
        total = parse_cap(bare[0], "The daily total")
    if len(bare) == 2:
        person = parse_cap(bare[1], "The per-person limit")
    ctx.overrides.set_caps(total, person, ctx.username)
    now = ctx.overrides.effective(ctx.cfg)
    log.warning("daily caps set to %d/%d by @%s", now.max_per_day, now.max_per_user, ctx.username)
    return (f":shield: Daily safety caps are now **{now.max_per_day}** messages in total and "
            f"**{now.max_per_user}** per person.")


@command("remind_reset", hint="message | time | caps | all",
         summary="Go back to the defaults of the file / environment.")
def reset(ctx: Ctx) -> str:
    what = (ctx.args.split() or [""])[0].lower()
    done = ctx.overrides.reset(what)
    if not done:
        return "Nothing to reset: those settings were already the defaults."
    log.warning("settings reset (%s) by @%s", ", ".join(done), ctx.username)
    return f":leftwards_arrow_with_hook: Back to the defaults: {', '.join(done)}."


@command("remind_alerts", hint="on | off | status",
         summary="Get a direct message from the bot when something needs attention.")
def alerts(ctx: Ctx) -> str:
    word = (ctx.args.split() or ["status"])[0].lower()
    users = ctx.overrides.alert_users()
    if word == "status":
        return (f"You are {'**getting**' if ctx.user_id in users else '**not** getting'} alerts. "
                f"{len(users)} administrator(s) in total. `/remind_alerts on` or `off` changes yours.")
    if word not in ("on", "off"):
        raise SettingError("Write `/remind_alerts on`, `off` or `status`.")
    ctx.overrides.set_alerts(ctx.user_id, word == "on")
    return (":bell: You will get a direct message from the bot when a pass fails, a board is set up "
            "wrongly or a daily cap is hit (at most once a day for each problem)."
            if word == "on" else ":no_bell: You will not get alerts any more.")
