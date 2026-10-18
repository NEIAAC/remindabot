"""The commands that only LOOK: help, status, next, boards, card, history, getmessage.

None of them sends anything or changes a setting. Those that need the boards (status, next, boards,
card) do a dry run (`ctx.scan()`), so what they show is exactly what a real pass would decide.
"""
from __future__ import annotations

from datetime import date, datetime

from ..driver import ApiError
from ..logic import plan
from ..overrides import MAX_MESSAGE_CHARS, SettingError
from . import markup as md
from .cards import card_from_report, find_one
from .context import Ctx
from .registry import command, commands

MAX_NEXT_DAYS = 30
MAX_HISTORY = 50
MAX_PEOPLE_SHOWN = 10          # per line of /remind_next
MAX_NOTIFYEES_LISTED = 25      # in /remind_card
MAX_MESSAGE_SHOWN = MAX_MESSAGE_CHARS   # of the message in /remind_getmessage


@command("remind_help", summary="List these commands.")
def help_(ctx: Ctx) -> str:
    # a `|` inside a table cell would split it, and md.code drops it: show "or" choices with a slash
    rows = [[md.code("/" + s.trigger + (" " + s.hint.replace("|", "/") if s.hint else "")), s.summary]
            for s in commands()]
    return ("### Reminder bot commands\nOnly system administrators can use them. Replies are visible "
            "only to you.\n\n" + md.table(["Command", "What it does"], rows, limit=len(rows)))


def _next_run(ctx: Ctx) -> str:
    at = ctx.cfg.send_at.strftime("%H:%M")
    if not ctx.cfg.daily_pass:
        return "never: the daily pass is off (REMINDER_DAILY_PASS=false); `/remind_send` sends"
    if ctx.overrides.paused():
        return "not while paused"
    if ctx.store.last_run() == ctx.today.isoformat():
        return f"tomorrow {at}"
    if ctx.now.time() >= ctx.cfg.send_at:
        return "within a minute (the time has passed and today's pass has not completed)"
    return f"today {at}"


@command("remind_status", summary="Settings, what was sent today and what is waiting to be sent.", slow=True)
def status(ctx: Ctx) -> str:
    cfg, overrides, store = ctx.cfg, ctx.overrides, ctx.store
    active = overrides.active()
    _, message_source = overrides.template(cfg)
    scan = ctx.scan()

    def src(name: str, default: str) -> str:
        return f"set {md.who_when(active[name], cfg)}" if name in active else default

    paused = overrides.paused()
    day_start = datetime.combine(ctx.today, datetime.min.time(), tzinfo=cfg.tz).timestamp()
    sent_today = store.recent_history(md.MAX_ROWS * 4, since=day_start)
    sent_total = store.sent_since(day_start)
    waiting = [i for i in scan.items if i.state == "to_send"]
    capped = [i for i in scan.items if i.state == "capped"]
    deferred = [i for i in scan.items if i.state == "deferred"]

    out = ["### Reminder bot status\n"]
    out.append(md.table(["Setting", "Value"], [
        ["Sending", (f":pause_button: **PAUSED** {md.who_when(paused, cfg)}" if paused else ":arrow_forward: active")],
        ["Daily pass", f"{cfg.send_at.strftime('%H:%M')} {cfg.tz} ({src('time', 'from the environment')})"
                       + ("" if cfg.daily_pass else " — **switched off** (`REMINDER_DAILY_PASS=false`): "
                                                    "only `/remind_send` sends")],
        ["Next pass", _next_run(ctx)],
        ["Last completed pass", store.last_run() or "never"],
        ["Message", ("custom, " + src("message", "") if message_source == "custom"
                     else f"file {md.code(cfg.template_file)}") + " (`/remind_getmessage`)"],
        ["Safety caps", f"{cfg.max_per_day} per day, {cfg.max_per_user} per person "
                        f"({src('caps', 'from the environment')})"],
        ["Board properties", f"{md.code(cfg.prop_due)}, {md.code(cfg.prop_notifyees)}, {md.code(cfg.prop_remind)}"],
        ["Joins open boards", "yes" if cfg.auto_join else "no"],
        ["Alerts to", f"{len(overrides.alert_users())} administrator(s) (`/remind_alerts`)"],
        ["Mattermost", f"{md.code(cfg.mm_url)}; links use {md.code(cfg.site_url)}"],
        ["Bot", md.person(ctx.driver.me().get("username"))],
    ], limit=20))

    out.append(f"\n**Today, {ctx.today.isoformat()}**\n")
    out.append(f":white_check_mark: **Sent today ({sent_total})**")
    rows = [[datetime.fromtimestamp(h["sent_at"], cfg.tz).strftime("%H:%M"), md.person(h["username"], h["user_id"]),
             md.text(h["card_title"]), md.text(h["board_title"], 40), md.stage(h["days"])] for h in sent_today]
    out.append(md.table(["Time", "To", "Card", "Board", "Reminder"], rows))
    if sent_total > len(sent_today):
        out.append(f"_{sent_total - len(sent_today)} more were sent today without details (older records)._")

    out.append(f"\n:hourglass: **Waiting for the next pass ({len(waiting)})**")
    rows = [[md.person(i.username, i.user_id), md.text(i.card_title), md.text(i.board_title, 40), i.due.isoformat(),
             f"{md.stage(i.stage)} ({i.days_left} left)"] for i in waiting]
    out.append(md.table(["To", "Card", "Board", "Due", "Reminder"], rows))

    notes = []
    if capped:
        notes.append(f":stop_sign: {len(capped)} held back by the daily safety cap (`/remind_setcap`).")
    if deferred:
        notes.append(f":arrows_counterclockwise: {len(deferred)} are being sent by another process right now.")
    if scan.no_notifyees:
        notes.append(f":warning: {scan.no_notifyees} card(s) are due a reminder but nobody is in "
                     f"{md.code(cfg.prop_notifyees)}.")
    for problem in scan.problems[:5]:
        notes.append(f":warning: {md.text(problem, 200)}")
    if scan.boards_unreadable:
        notes.append(f":lock: {len(scan.boards_unreadable)} board(s) cannot be read (`/remind_boards`).")
    if notes:
        out.append("\n**Needs attention**\n" + "\n".join("- " + n for n in notes))
    return "\n".join(out)


@command("remind_next", hint="[days]", slow=True,
         summary="What will be sent in the next days (default 7), day by day.")
def next_(ctx: Ctx) -> str:
    days = 7
    if ctx.args:
        try:
            days = int(ctx.args.split()[0])
        except ValueError:
            raise SettingError("Write the number of days, for example `/remind_next 14`.") from None
        if not 1 <= days <= MAX_NEXT_DAYS:
            raise SettingError(f"The number of days must be between 1 and {MAX_NEXT_DAYS}.")
    scan = ctx.scan()
    driver = ctx.driver
    by_day: dict[date, list[tuple[str, str, int, list[str]]]] = {}
    for report in scan.cards:
        if report.due is None or not report.notifyees:
            continue
        card = card_from_report(report)
        per_event: dict[tuple[date, int], list[str]] = {}
        for user_id in dict.fromkeys(card.notifyees):
            sent = ctx.store.sent_stages(card.id, user_id, report.due.isoformat())
            for event in plan(card, ctx.today, sent, days):
                try:
                    user = driver.user(user_id)
                except ApiError:
                    continue
                if user.get("delete_at") or user.get("id") == driver.me().get("id"):
                    continue
                per_event.setdefault(event, []).append(user.get("username") or user_id[:8])
        for (day, stage), people in per_event.items():
            by_day.setdefault(day, []).append((report.title, report.board_title, stage, people))
    if not by_day:
        return f"Nothing is scheduled for the next {days} day(s)."
    runs = (f"runs every day at {ctx.cfg.send_at.strftime('%H:%M')}" if ctx.cfg.daily_pass
            else "is run every day (it is switched off here: someone has to use /remind_send)")
    out = [f"### Scheduled reminders, next {days} day(s)\n"
           f"_Assumes the daily pass {runs} and that everybody in Notifyees may see the board._\n"]
    shown = 0
    for day in sorted(by_day):
        label = "today" if day == ctx.today else day.strftime("%a %d %b")
        out.append(f"**{label}**")
        for title, board, stage, people in sorted(by_day[day]):
            shown += 1
            if shown > md.MAX_ROWS * 2:
                break
            names = sorted(people)
            more = f" and {len(names) - MAX_PEOPLE_SHOWN} more" if len(names) > MAX_PEOPLE_SHOWN else ""
            out.append(f"- {md.text(title)} _({md.text(board, 40)})_ — {md.stage(stage)} → "
                       + ", ".join(md.person(p) for p in names[:MAX_PEOPLE_SHOWN]) + more)
        if shown > md.MAX_ROWS * 2:
            out.append("_… the list was cut_")
            break
    return "\n".join(out)


@command("remind_boards", slow=True, summary="Which boards are read or skipped, and why.")
def boards(ctx: Ctx) -> str:
    scan = ctx.scan()
    label = {"ok": ":white_check_mark: read", "not-used": ":white_circle: not used",
             "misconfigured": ":warning: set up wrongly", "not-joined": ":heavy_plus_sign: open, not joined",
             "unreadable": ":lock: private, no access", "error": ":x: error"}
    rows = []
    for board in sorted(scan.boards, key=lambda b: (b.state == "not-used", b.title.casefold())):
        detail = board.detail
        if board.state == "ok":
            detail = f"{board.cards} cards, {board.in_window} with a reminder due today"
        elif board.state == "not-used":
            detail = f"has no {ctx.cfg.prop_remind!r} property"
        rows.append([md.text(board.title, 50), "private" if board.kind == "P" else "open",
                     label.get(board.state, board.state), md.text(detail, 140)])
    header = (f"### Boards the bot can see ({len(scan.boards)})\n"
              f"{scan.boards_relevant} use the reminder properties. Only boards with a "
              f"{md.code(ctx.cfg.prop_remind)} property are considered; the bot joins open boards by "
              f"itself: {'yes' if ctx.cfg.auto_join else 'no'}.\n")
    return header + md.table(["Board", "Kind", "State", "Detail"], rows, limit=40)


@command("remind_card", hint="<card link or title>", slow=True,
         summary="Explain what the bot sees and does for one card.")
def card(ctx: Ctx) -> str:
    scan = ctx.scan()
    report = find_one(scan, ctx.args)
    cfg = ctx.cfg
    lines = [f"### {md.text(report.title, 100)}", f"Board: {md.text(report.board_title, 60)}"]
    if report.is_template or report.deleted:
        lines.append(f"This is a {'template' if report.is_template else 'deleted'} card: never reminded.")
    lines.append("Due date: " + (f"{report.due.isoformat()} ({(report.due - ctx.today).days} days left)"
                                  if report.due else "**none** (no reminder without it)"))
    read = ", ".join(str(n) for n in report.remind) or "nothing"
    lines.append(f"{md.code(cfg.prop_remind)} was read as: **{read}**")
    if report.unreadable:
        lines.append(f":warning: Ignored, not whole numbers: {', '.join(md.code(u) for u in report.unreadable)} "
                     "(separate numbers with `;`)")
    today = (f"**a reminder is due** ({md.stage(report.stage)})" if report.send else md.text(report.reason, 120))
    lines.append(f"Today: {today}")
    people = []
    states = {i.user_id: i for i in scan.items if i.card_id == report.card_id}
    state_text = {"to_send": "will be told at the next pass", "sent": "told", "already": "was already told",
                  "deferred": "being sent right now", "capped": "held back by a daily cap",
                  "failed": "sending failed, will be retried"}
    for user_id in report.notifyees[:MAX_NOTIFYEES_LISTED]:
        try:
            user = ctx.driver.user(user_id)
            name = md.person(user.get("username"), user_id)
        except ApiError:
            name = md.code(user_id[:8] + "…")
        item = states.get(user_id)
        people.append(f"- {name}: {state_text.get(item.state, item.state) if item else 'no reminder today'}")
    lines.append(f"{md.code(cfg.prop_notifyees)}: " + ("" if people else "**nobody**"))
    lines += people
    if len(report.notifyees) > MAX_NOTIFYEES_LISTED:
        lines.append(f"- … and {len(report.notifyees) - MAX_NOTIFYEES_LISTED} more")
    if report.due:
        upcoming = []
        for user_id in report.notifyees[:1]:
            sent = ctx.store.sent_stages(report.card_id, user_id, report.due.isoformat())
            upcoming = plan(card_from_report(report), ctx.today, sent, 60)
        if upcoming:
            lines.append("Coming up: " + "; ".join(f"{d.strftime('%d %b')} ({md.stage(s)})" for d, s in upcoming[:8]))
    return "\n".join(lines)


@command("remind_history", hint="[how many]", summary="The last messages that were sent (default 10).")
def history(ctx: Ctx) -> str:
    count = 10
    if ctx.args:
        try:
            count = int(ctx.args.split()[0])
        except ValueError:
            raise SettingError("Write how many to show, for example `/remind_history 20`.") from None
        if not 1 <= count <= MAX_HISTORY:
            raise SettingError(f"Choose between 1 and {MAX_HISTORY}.")
    rows = ctx.store.recent_history(count)
    if not rows:
        return "No message has been sent since the bot started recording history."
    table = md.table(["When", "To", "Card", "Board", "Reminder"],
                     [[md.when(h["sent_at"], ctx.cfg), md.person(h["username"], h["user_id"]), md.text(h["card_title"]),
                       md.text(h["board_title"], 40), md.stage(h["days"])] for h in rows], limit=MAX_HISTORY)
    return f"### The last {len(rows)} message(s) sent\n" + table


@command("remind_getmessage", summary="Show the message that is sent now, and where it comes from.")
def getmessage(ctx: Ctx) -> str:
    _, source = ctx.overrides.template(ctx.cfg)
    where = (f"custom message {md.who_when(ctx.overrides.active().get('message', {}), ctx.cfg)}"
             if source == "custom" else f"the file {md.code(ctx.cfg.template_file)}")
    body = ctx.overrides.message_body(ctx.cfg).replace("```", "'''")
    if len(body) > MAX_MESSAGE_SHOWN:      # a file can be of any size; the form and the reply cannot
        body = body[:MAX_MESSAGE_SHOWN].rsplit("\n", 1)[0] + f"\n… (cut: the message is {len(body)} characters long)"
    return f"Message in use: {where}.\n```\n{body}\n```\n`/remind_setmessage` edits it, `/remind_test` previews it."
