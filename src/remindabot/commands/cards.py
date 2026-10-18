"""Finding a card from what an administrator typed (a link, an id or part of a title)."""
from __future__ import annotations

import re

from ..boards import Card
from ..driver import is_valid_id
from ..overrides import SettingError
from ..runner import Summary
from . import markup as md

_LINK = re.compile(r"/boards/team/([a-z0-9]{26})/([a-z0-9]{26})(?:/([a-z0-9]{26})(?:/([a-z0-9]{26}))?)?")


def find_cards(reports: list, query: str) -> list:
    """Cards matching a link, an id, or (part of) a title. A link or id is exact; text is a
    case-insensitive substring, and an exact title match wins over partial ones."""
    query = (query or "").strip()
    if not query:
        return []
    link = _LINK.search(query)
    card_id = link.group(4) if link and link.group(4) else (query if is_valid_id(query) else None)
    if card_id:
        return [r for r in reports if r.card_id == card_id]
    wanted = query.casefold()
    exact = [r for r in reports if r.title.casefold() == wanted]
    return exact or [r for r in reports if wanted in r.title.casefold()]


def find_one(scan: Summary, query: str):
    """Exactly one card for `query`, or raises SettingError saying why not."""
    if not query:
        raise SettingError("Give the card as a link (copy it from the message) or part of its title.")
    found = find_cards(scan.cards, query)
    if not found:
        raise SettingError(f"No card matches `{md.text(query, 60)}` on a board the bot can read. "
                           "`/remind_boards` shows which boards it reads.")
    if len(found) > 1:
        names = "\n".join(f"- {md.text(c.title)} _({md.text(c.board_title, 40)}, due "
                          f"{c.due.isoformat() if c.due else 'no date'})_" for c in found[:8])
        raise SettingError(f"{len(found)} cards match; use the card's link to pick one:\n{names}")
    return found[0]


def card_from_report(report) -> Card:
    """The card as the reminder rule sees it, rebuilt from the report a scan made of it."""
    return Card(id=report.card_id, title=report.title, due=report.due, remind_before=report.remind,
                notifyees=report.notifyees, is_template=report.is_template, deleted=report.deleted,
                remind_unreadable=report.unreadable)
