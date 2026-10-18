"""Small helpers that turn data into Markdown for a reply, safely.

Everything a person typed (a card title, a name) is made inert first, and the two things Mattermost
does to a slash command's reply that the text does not say are dealt with here: tables need blank
lines around them, and `<a|b>` is rewritten into a link (driver.for_command_reply).
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

from .. import render
from ..config import Config
from ..driver import for_command_reply

# Lists in a reply are cut to this many rows (Mattermost refuses very long posts, and nobody reads them).
MAX_ROWS = 25
# Mattermost refuses a post longer than 16383 characters; stay well below that.
MAX_REPLY_CHARS = 14000


def text(value: object, limit: int = 60) -> str:
    """User-controlled text (a title, a name) made inert for a Mattermost reply."""
    return render.sanitize_inline(value, limit, fallback="-")


def code(value: object) -> str:
    """Short literal text in a code span (a username, a setting), where nothing is interpreted."""
    return "`" + re.sub(r"[`\n\r|]", "", str(value))[:80] + "`"


def person(username: Optional[str], user_id: str = "") -> str:
    return code("@" + username) if username else code(user_id[:8] or "?")


def table(headers: list[str], rows: list[list[str]], limit: int = MAX_ROWS) -> str:
    """A Markdown table with a blank line before and after it.

    Mattermost only draws a table that starts a paragraph: with a text line right above it the
    `| a | b |` lines are shown as they are, and a text line right below becomes one more row.
    Hence the blank lines, so callers can just append the result to their lines.
    """
    if not rows:
        return "_none_"
    lines = ["", "| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    lines += ["| " + " | ".join(row) + " |" for row in rows[:limit]]
    lines.append("")
    if len(rows) > limit:
        lines.append(f"_… and {len(rows) - limit} more_")
    return "\n".join(lines)


def when(ts: Optional[float], cfg: Config) -> str:
    if not ts:
        return "?"
    return datetime.fromtimestamp(ts, cfg.tz).strftime("%Y-%m-%d %H:%M")


def who_when(info: dict, cfg: Config) -> str:
    who = f"by {person(info.get('by'))}" if info.get("by") else ""
    return f"{who} on {when(info.get('at'), cfg)}".strip()


def stage(days: int) -> str:
    return f"{days} day{'s' if days != 1 else ''} before"


def fit(message: str) -> str:
    """`message`, cut at a line boundary (and with any code block closed) if Mattermost would refuse
    it for being too long. The handlers already keep their lists short; this is the net under them."""
    if len(message) <= MAX_REPLY_CHARS:
        return message
    cut = message[:MAX_REPLY_CHARS].rsplit("\n", 1)[0]
    if cut.count("```") % 2:
        cut += "\n```"
    return cut + "\n\n_… the reply was cut here: Mattermost does not accept longer messages._"


def ephemeral(message: str) -> dict:
    """A reply only its author sees. Every direct answer to a slash command goes through here."""
    return {"response_type": "ephemeral", "text": for_command_reply(fit(message))}
