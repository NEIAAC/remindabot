"""Everything that knows how Mattermost Boards (the Focalboard plugin) stores data.

This is the ONLY module that depends on the Boards data model. The Boards plugin is in
maintenance mode and Mattermost plans a native replacement, so when that happens this is
the file to rewrite; `logic.py` and the rest only see the plain `Card` / `Schema` types.
"""
from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Iterable, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .driver import ApiError, Client, is_valid_id

log = logging.getLogger("reminder")

API_PREFIX = "/plugins/focalboard/api/v2"

# Property types we accept for each role (Boards type names).
DUE_TYPES = {"date"}
NOTIFYEE_TYPES = {"multiPerson", "person"}
# "Remind Before" is a TEXT property holding a list of day counts separated by ";" (for example
# "7;3;1"). Other types (Number, Select, Multi select...) are refused, so there is one way to do it.
REMIND_TYPES = {"text"}

# The cards endpoint defaults to 100 per page and has no ORDER BY, so several small pages are not
# guaranteed to be consistent. Ask for one big page; paging is only a safety net (with de-duplication).
PAGE_SIZE = 1000
MAX_PAGES = 50

# Epoch-millisecond range we consider a real date (1970 .. year 2200). Anything else is garbage and
# must not reach datetime arithmetic, where huge values raise OverflowError.
_MIN_MS = 0
_MAX_MS = 7_258_118_400_000


@dataclass(frozen=True)
class Schema:
    """Property ids for one board (ids differ per board, only the labels are shared)."""
    due_id: str
    notifyees_id: str
    remind_id: str


@dataclass(frozen=True)
class Card:
    """One board card, reduced to the few things the reminder needs (already decoded and typed)."""
    id: str
    title: str
    due: Optional[date]
    remind_before: tuple[int, ...]   # every whole number written in "Remind Before", in the order written
    notifyees: tuple[str, ...]       # user ids to notify
    is_template: bool
    deleted: bool
    remind_unreadable: tuple[str, ...] = ()   # entries of "Remind Before" that are not whole numbers


def _norm(label: object) -> str:
    """A property name as compared: trimmed and case-insensitive ("due date" == " Due Date ")."""
    return str(label or "").strip().casefold()


def _props(board: dict) -> list[dict]:
    """The board's property definitions, ignoring anything that is not a well-formed definition."""
    return [p for p in (board.get("cardProperties") or []) if isinstance(p, dict) and p.get("id")]


def _first_usable(props: Iterable[dict], label: str, types: set[str]) -> Optional[dict]:
    """The first property named `label` whose type is one of `types` (two may share a name)."""
    wanted = _norm(label)
    for prop in props:
        kind = prop.get("type")
        if _norm(prop.get("name")) == wanted and isinstance(kind, str) and kind in types:
            return prop
    return None


def find_schema(board: dict, prop_due: str, prop_notifyees: str, prop_remind: str) -> Optional[Schema]:
    """Return the property ids if the board has all three properties with a usable type.

    Two properties may share a label (e.g. a text "Due Date" and a date "Due Date"); the first one
    with an acceptable type wins.
    """
    props = _props(board)
    due = _first_usable(props, prop_due, DUE_TYPES)
    notifyees = _first_usable(props, prop_notifyees, NOTIFYEE_TYPES)
    remind = _first_usable(props, prop_remind, REMIND_TYPES)
    if not (due and notifyees and remind):
        return None
    return Schema(due["id"], notifyees["id"], remind["id"])


def describe_schema_problem(board: dict, prop_due: str, prop_notifyees: str, prop_remind: str) -> Optional[str]:
    """Explain why a board that clearly *tries* to use the reminders cannot be used.

    "Due Date" is a label of the standard Boards templates, so plenty of unrelated boards have it.
    "Remind Before" is the opt-in: only a board that has that label is considered to be trying to
    use the reminders. For such a board a missing property or a wrong type (say "Remind Before"
    as a Select) is almost certainly a mistake that would otherwise fail silently.
    Anything else gives None.
    """
    props = _props(board)
    roles = ((prop_due, DUE_TYPES), (prop_notifyees, NOTIFYEE_TYPES), (prop_remind, REMIND_TYPES))
    if not any(_norm(p.get("name")) == _norm(prop_remind) for p in props):
        return None
    problems: list[str] = []
    for label, types in roles:
        same_label = [p for p in props if _norm(p.get("name")) == _norm(label)]
        if _first_usable(props, label, types):
            continue
        if same_label:
            problems.append(f"property {label!r} has type {same_label[0].get('type')!r} "
                            f"but must be one of {sorted(types)}")
        else:
            problems.append(f"property {label!r} is missing")
    return "; ".join(problems) if problems else None


def decode_date(raw: object, default_tz: ZoneInfo) -> Optional[date]:
    """Decode a Boards Date property into a calendar date.

    Stored as a JSON string such as {"from": <epoch ms>, "includeTime": false}. For date-only
    values the Boards UI (date picker and calendar view alike) shifts the timestamp so it is
    always 12:00 UTC of the chosen day, so the UTC date is the date the user picked, whatever
    timezone their browser was in. When a time of day is included the value is a real instant
    and is read in its own `timeZone` (else ours). Ranges are read from their start (`from`).
    Never raises: anything unusable is None.
    """
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError):
        return None
    if isinstance(data, (int, float)) and not isinstance(data, bool):  # legacy: bare epoch ms
        data = {"from": data}
    if not isinstance(data, dict):
        return None
    millis = data.get("from")
    if isinstance(millis, bool) or not isinstance(millis, (int, float)):
        return None
    if isinstance(millis, float) and not math.isfinite(millis):
        return None
    if not (_MIN_MS < millis <= _MAX_MS):
        return None
    try:
        instant = datetime.fromtimestamp(millis / 1000, tz=timezone.utc)
        if not data.get("includeTime"):
            return instant.date()
        tz = default_tz
        tz_name = data.get("timeZone")
        if isinstance(tz_name, str) and tz_name:
            try:
                tz = ZoneInfo(tz_name)
            except (ZoneInfoNotFoundError, ValueError, OSError):
                tz = default_tz
        return instant.astimezone(tz).date()
    except (OverflowError, OSError, ValueError):
        return None


# One entry of the list: a whole number, with an optional minus sign ("0" and "-1" mean "never").
_WHOLE_NUMBER = re.compile(r"-?[0-9]+")
# A text longer than this is garbage, not a list of reminders (and a huge text must stay cheap).
_MAX_REMIND_TEXT = 500
# How many unreadable entries are remembered for the warning in the log.
_MAX_UNREADABLE = 5


def decode_remind_list(raw: object) -> tuple[tuple[int, ...], tuple[str, ...]]:
    """Decode "Remind Before" ("7;3;1") into (whole numbers, unreadable entries).

    The rule is one line: whole numbers separated by ";", spaces around them are ignored.
    Anything between two ";" that is not a whole number ("tres", "1.5", "7 dias", "7,3") is
    unreadable AS A WHOLE and is returned separately, so the runner can warn about it instead of
    silently dropping a reminder somebody meant to set. The numbers keep the order written, without
    repeats; any size and sign is returned (`logic.usable_days` applies the range). Never raises.
    """
    if raw is None:
        return (), ()
    if not isinstance(raw, str):    # a list or number cannot have been typed into a Text property
        return (), ("(not text)",)
    numbers: dict[int, None] = {}
    unreadable: dict[str, None] = {}
    for entry in raw[:_MAX_REMIND_TEXT].split(";"):
        entry = entry.strip()
        if not entry:                # "7;3;" and ";;" are harmless
            continue
        if _WHOLE_NUMBER.fullmatch(entry):
            numbers[int(entry)] = None
        elif len(unreadable) < _MAX_UNREADABLE:
            unreadable[" ".join(entry.split())[:30]] = None
    return tuple(numbers), tuple(unreadable)


def decode_people(raw: object) -> tuple[str, ...]:
    """Multi person -> list of user ids, Person -> a single id string.

    The value is typed by users (anyone with edit rights can store any text in it), so only
    well-formed Mattermost ids are kept. Anything else would end up inside a URL path.
    """
    if isinstance(raw, str):
        items: list = [raw]
    elif isinstance(raw, list):
        items = raw
    else:
        return ()
    return tuple(dict.fromkeys(item for item in items if is_valid_id(item)))


def decode_card(card: dict, schema: Schema, default_tz: ZoneInfo) -> Card:
    props = card.get("properties")
    if not isinstance(props, dict):
        props = {}
    title = card.get("title")
    remind, unreadable = decode_remind_list(props.get(schema.remind_id))
    return Card(
        id=str(card["id"]),
        title=title if isinstance(title, str) and title.strip() else "(sem título)",
        due=decode_date(props.get(schema.due_id), default_tz),
        remind_before=remind,
        notifyees=decode_people(props.get(schema.notifyees_id)),
        is_template=bool(card.get("isTemplate")),
        deleted=bool(card.get("deleteAt")),
        remind_unreadable=unreadable,
    )


class Boards:
    """The Boards plugin REST API (the part this bot needs: list boards, read cards, join, members)."""

    def __init__(self, base_url: str, token: str, timeout: float = 30, beat=lambda: None):
        self.api = Client(base_url, token, prefix=API_PREFIX, timeout=timeout, beat=beat)

    def list_boards(self, team_id: str) -> list[dict]:
        """Boards of a team that the bot may see: its own boards plus the team's open boards.
        Deleted boards and board templates are left out."""
        boards = self.api.request("GET", f"/teams/{team_id}/boards") or []
        if not isinstance(boards, list):
            raise ApiError(200, "unexpected boards payload", f"team {team_id}")
        return [b for b in boards if isinstance(b, dict) and b.get("id")
                and not b.get("deleteAt") and not b.get("isTemplate")]

    def cards(self, board_id: str) -> list[dict]:
        """All cards of a board (templates included), without duplicates."""
        seen: dict[str, dict] = {}
        for page in range(MAX_PAGES):
            batch = self.api.request("GET", f"/boards/{board_id}/cards?page={page}&per_page={PAGE_SIZE}") or []
            if not isinstance(batch, list):
                raise ApiError(200, "unexpected cards payload", f"board {board_id}")
            before = len(seen)
            for card in batch:
                if isinstance(card, dict) and card.get("id"):
                    seen[str(card["id"])] = card
            # Keep going until a page adds nothing new (empty, or only repeats). Stopping on a
            # "short" page would silently truncate if the server ever caps per_page below ours.
            if len(seen) == before:
                break
            if page == 1:
                log.warning("board %s has more than one page of cards, reading it in several pages", board_id)
        else:
            log.error("board %s has more than %d cards: the rest is NOT being checked", board_id,
                      MAX_PAGES * PAGE_SIZE)
        return list(seen.values())

    def member_ids(self, board_id: str) -> set[str]:
        """User ids of the board's members (the bot must itself be a member to ask)."""
        members = self.api.request("GET", f"/boards/{board_id}/members") or []
        if not isinstance(members, list):
            raise ApiError(200, "unexpected members payload", f"board {board_id}")
        return {m["userId"] for m in members if isinstance(m, dict) and isinstance(m.get("userId"), str)}

    def join(self, board_id: str) -> None:
        """Become a member of an OPEN board (private boards refuse with 403). Safe to repeat."""
        self.api.request("POST", f"/boards/{board_id}/join", idempotent=True)

    def first_view_id(self, board_id: str) -> Optional[str]:
        """Id of any view of the board (a card link needs one), or None if it cannot be found."""
        try:
            views = self.api.request("GET", f"/boards/{board_id}/blocks?type=view") or []
        except ApiError:
            return None
        if isinstance(views, list) and views and isinstance(views[0], dict):
            return views[0].get("id")
        return None
