"""One reminder pass: look at every board the bot can see and send what is due today.

The pass has two phases:

  1. COLLECT  - for every team and board, read the cards and keep the ones that need a reminder today.
  2. SEND     - sort them (earliest due date first) and message each person in the card's Notifyees.

Sorting matters because of the safety caps: if a cap is ever reached, the reminders that were held
back are the ones with the most time left.

Besides sending, a pass produces a REPORT (`Summary.boards`, `.cards`, `.items`): what it saw on every
board and card, and what it did or would do for every person. The chat commands (commands/) show
it; the daily loop ignores it.

Failures are sorted into two kinds, because the daily loop reacts differently:
  * transient  (server hiccup, timeout)  -> counted in `Summary.failed`, retried later the same day;
  * permanent  (unknown user, no access) -> logged, never retried, never counted as a failure.
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime
from string import Template
from typing import Callable, Optional

from . import render
from .boards import Boards, Card, decode_card, describe_schema_problem, find_schema
from .config import Config
from .logic import MAX_REMIND_BEFORE_DAYS, evaluate, ignored_values
from .driver import ApiError, Driver, display_name
from .store import RECLAIMED, Store

log = logging.getLogger("reminder")

# At most this many "other people notified" names are written into one message.
MAX_NAMES_LISTED = 10


class StateError(Exception):
    """The reminder state could not be read or written.

    The bot stops sending when this happens: without the state it could no longer promise that
    nobody gets the same reminder twice.
    """


@dataclass
class BoardReport:
    """What a pass found out about one board."""
    title: str
    board_id: str
    team_id: str
    kind: str                 # "O" open board, "P" private board
    state: str = "ok"         # ok | not-used | misconfigured | not-joined | unreadable | error
    detail: str = ""
    cards: int = 0            # cards on the board
    in_window: int = 0        # ... that have a reminder due today


@dataclass
class CardReport:
    """What a pass decided about one card (all cards, not only those with a reminder due)."""
    board_title: str
    board_id: str
    team_id: str
    card_id: str
    title: str
    due: Optional[date]
    remind: tuple[int, ...]            # the whole numbers read from "Remind Before"
    unreadable: tuple[str, ...]        # the entries of it that could not be read
    is_template: bool
    deleted: bool
    send: bool                         # a reminder is due today
    stage: Optional[int]               # the "N days before" reminder that applies today
    reason: str                        # the rule's explanation (logic.Decision.reason)
    nobody: bool                       # a reminder is due but Notifyees is empty
    notifyees: tuple[str, ...]


@dataclass
class ItemReport:
    """One person's reminder for one card today, and what became of it."""
    board_title: str
    card_id: str
    card_title: str
    due: date
    stage: int
    days_left: int
    user_id: str
    username: str
    # to_send (dry run) | sent | already (told before) | deferred (being sent elsewhere)
    # | capped (daily safety cap) | failed
    state: str


@dataclass
class Summary:
    """Counters for one pass, printed as a single log line at the end."""
    boards_seen: int = 0          # boards the bot could list
    boards_relevant: int = 0      # ... of which have the three properties
    boards_joined: int = 0        # open boards the bot joined during this pass
    boards_unreadable: list[str] = field(default_factory=list)
    cards_checked: int = 0
    cards_in_window: int = 0      # cards that have a reminder to send today (some stage reached)
    no_notifyees: int = 0         # cards that are due a reminder but have nobody in Notifyees
    sent: int = 0                 # messages sent (or, in a dry-run, that would be sent)
    already_sent: int = 0         # nothing to do: this person was reminded already
    deferred: int = 0             # skipped because another process is sending it right now: retry later
    failed: int = 0               # transient problems: retry later
    capped: int = 0               # held back by a daily safety cap
    dry_run: bool = False
    # the report (see the module docstring); problems = things somebody should fix on a board
    boards: list[BoardReport] = field(default_factory=list)
    cards: list[CardReport] = field(default_factory=list)
    items: list[ItemReport] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    def line(self) -> str:
        sent = f"{self.sent} would be sent (DRY-RUN, nothing was sent)" if self.dry_run else f"{self.sent} sent"
        extras = ""
        if self.deferred:
            extras += f", {self.deferred} in progress elsewhere"
        if self.capped:
            extras += f", {self.capped} held back by the daily safety cap"
        if self.no_notifyees:
            extras += f", {self.no_notifyees} card(s) due a reminder but with nobody in Notifyees"
        return (
            f"boards: {self.boards_seen} seen, {self.boards_relevant} with the required properties, "
            f"{self.boards_joined} joined, {len(self.boards_unreadable)} unreadable | "
            f"cards: {self.cards_checked} checked, {self.cards_in_window} in reminder window | "
            f"messages: {sent}, {self.already_sent} already sent, {self.failed} failed{extras}"
        )


class _BoardContext:
    """What we need to know about one board while sending: who may be told, and where its link points.

    Who may be told: a card's Notifyees is just a list of user ids that anyone with edit rights can
    type in, so it must not decide who gets to read the board's contents (the card title would leak
    to someone who cannot see it). So we require:
      * private board -> the person is a member of that board;
      * open board    -> the person is a member of the board's team.
    """

    def __init__(self, mm: Driver, boards: Boards, team_id: str, board: dict):
        self.mm, self.boards, self.team_id, self.board = mm, boards, team_id, board
        self.title: str = board.get("title") or board["id"]
        self._members: Optional[set[str]] = None
        self._view_id: Optional[str] = None
        self._view_loaded = False

    def allows(self, user_id: str) -> bool:
        """May this person be told about this board's cards? Raises ApiError if it cannot be verified."""
        if self.board.get("type") == "P":
            if self._members is None:
                self._members = self.boards.member_ids(self.board["id"])
            return user_id in self._members
        return self.mm.is_team_member(self.team_id, user_id)

    def card_url(self, site_url: str, card_id: str) -> str:
        """Link that opens the card inside Mattermost Boards (any view of the board will do)."""
        if not self._view_loaded:   # looked up only when a reminder is actually sent
            self._view_id = self.boards.first_view_id(self.board["id"])
            self._view_loaded = True
        base = f"{site_url}/boards/team/{self.team_id}/{self.board['id']}"
        return f"{base}/{self._view_id}/{card_id}" if self._view_id else base


@dataclass(frozen=True)
class _Due:
    """One card that needs a reminder today, together with the board it is on."""
    ctx: _BoardContext
    card: Card
    stage: int   # which "N days before" reminder this is (see logic.py)


class _Budget:
    """The daily safety caps: how many messages may still go out today, in total and per person.

    "Today" counts what earlier passes of the same day already sent (the state file knows), so
    retrying a pass never multiplies the allowance.
    """

    def __init__(self, cfg: Config, store: Optional[Store]) -> None:
        self.cfg, self.store = cfg, store
        self.day_start = 0.0
        self.total = 0
        self.per_user: dict[str, int] = {}
        self.warned: set[str] = set()
        self.reported_total = False
        if store is not None:
            now = datetime.fromtimestamp(store.now(), cfg.tz)
            self.day_start = datetime.combine(now.date(), dtime.min, tzinfo=cfg.tz).timestamp()
            self.total = store.sent_since(self.day_start)

    def _user_count(self, user_id: str) -> int:
        if user_id not in self.per_user:
            self.per_user[user_id] = self.store.sent_since(self.day_start, user_id) if self.store else 0
        return self.per_user[user_id]

    def allows(self, user_id: str, username: Optional[str]) -> bool:
        """May one more message go to this person today? Logs (once) when a cap is hit."""
        if self.total >= self.cfg.max_per_day:
            if not self.reported_total:
                self.reported_total = True
                log.error("daily safety cap reached: %d messages today (REMINDER_MAX_MESSAGES_PER_DAY). "
                          "The remaining reminders are held back until tomorrow; check that this is expected.",
                          self.cfg.max_per_day)
            return False
        if self._user_count(user_id) >= self.cfg.max_per_user:
            if user_id not in self.warned:
                self.warned.add(user_id)
                log.error("daily safety cap: @%s already got %d messages today "
                          "(REMINDER_MAX_MESSAGES_PER_USER_PER_DAY); the rest are held back until tomorrow",
                          username, self.cfg.max_per_user)
            return False
        return True

    def spent(self, user_id: str) -> None:
        """Record that one message was sent (or would be, in a dry-run)."""
        self.total += 1
        self.per_user[user_id] = self._user_count(user_id) + 1


def run_once(
    cfg: Config,
    mm: Driver,
    boards: Boards,
    store: Optional[Store],
    template: Template,
    today: date,
    *,
    dry_run: bool = False,
    join_only: bool = False,
    only_cards: Optional[set[str]] = None,
    beat: Callable[[], None] = lambda: None,
) -> Summary:
    """Run one pass and return its counters.

    Raises ApiError only when Mattermost itself is unusable (bad token, unreachable), and
    StateError when the reminder state cannot be used. Everything else (one bad board, one bad
    card, one undeliverable message) is logged, counted, and skipped.

    dry_run    print what would be sent; send nothing, join nothing, write nothing.
    join_only  only join the relevant open boards (so their cards can be read), then stop.
    only_cards only handle these card ids (used to resend one card); the rest is still read.
    """
    summary = Summary(dry_run=dry_run)
    mm.reset_caches()   # the daemon lives for months: never trust a lookup from an earlier pass
    mm.me()             # fails fast on a bad token / unreachable server
    teams = mm.my_teams()
    if not teams:
        log.warning("the bot is not a member of any team, so it can see no boards")

    # ---- phase 1: collect ---------------------------------------------------------------
    due: list[_Due] = []
    for team in teams:
        try:
            team_boards = boards.list_boards(team["id"])
        except ApiError as exc:
            log.error("team %r: cannot list boards: %s", team.get("name"), exc)
            summary.failed += 1
            continue
        for board in team_boards:
            beat()
            summary.boards_seen += 1
            try:
                due.extend(_collect_board(cfg, mm, boards, today, summary, team["id"], board,
                                          dry_run=dry_run, join_only=join_only))
            except Exception:  # noqa: BLE001 - one broken board must never stop the others
                log.exception("board %r: unexpected error, skipping it", board.get("title"))
                summary.failed += 1

    if join_only:
        return summary

    # ---- phase 2: send, most urgent first ------------------------------------------------
    if only_cards is not None:
        due = [item for item in due if item.card.id in only_cards]
    due.sort(key=lambda item: (item.card.due, item.ctx.title, item.card.title, item.card.id))
    try:
        budget = _Budget(cfg, store)
    except sqlite3.Error as exc:
        raise StateError(f"cannot read reminder state: {exc}") from exc
    for item in due:
        beat()
        try:
            _remind_card(cfg, mm, store, template, summary, budget, today, item, dry_run)
        except sqlite3.Error as exc:
            raise StateError(f"reminder state problem: {exc}") from exc
        except StateError:
            raise
        except Exception:  # noqa: BLE001 - one broken card must never stop the others
            log.exception("card %r: unexpected error, skipping it", item.card.id)
            summary.failed += 1
    return summary


def _collect_board(cfg, mm, boards, today, summary, team_id, board, *, dry_run, join_only) -> list[_Due]:
    """Phase 1 for one board: check it can be used, read its cards, return those due a reminder today."""
    title = board.get("title") or board["id"]
    report = BoardReport(title=title, board_id=board["id"], team_id=team_id, kind=board.get("type") or "O")
    summary.boards.append(report)
    schema = find_schema(board, cfg.prop_due, cfg.prop_notifyees, cfg.prop_remind)
    if schema is None:
        problem = describe_schema_problem(board, cfg.prop_due, cfg.prop_notifyees, cfg.prop_remind)
        if problem:
            log.warning("board %r is set up for reminders but cannot be used: %s", title, problem)
            report.state, report.detail = "misconfigured", problem
            summary.problems.append(f"board {title!r}: {problem}")
        else:
            log.debug("skip board %r: it does not use the reminder properties", title)
            report.state = "not-used"
        return []
    summary.boards_relevant += 1

    allow_join = (cfg.auto_join or join_only) and not dry_run
    cards = _load_cards(boards, board, title, summary, report, allow_join=allow_join)
    if cards is None or join_only:
        return []

    ctx = _BoardContext(mm, boards, team_id, board)
    found: list[_Due] = []
    for raw in cards:
        summary.cards_checked += 1
        report.cards += 1
        try:
            card = decode_card(raw, schema, cfg.tz)
            decision = evaluate(card, today)
            log.debug("card %r: %s (%s)", card.title, "REMIND" if decision.send else "skip", decision.reason)
            summary.cards.append(CardReport(
                board_title=title, board_id=board["id"], team_id=team_id, card_id=card.id, title=card.title,
                due=card.due, remind=card.remind_before, unreadable=card.remind_unreadable,
                is_template=card.is_template, deleted=card.deleted, send=decision.send, stage=decision.stage,
                reason=decision.reason, nobody=decision.nobody_to_tell, notifyees=card.notifyees))
            if decision.send:
                summary.cards_in_window += 1
                report.in_window += 1
                found.append(_Due(ctx, card, decision.stage))
            elif decision.nobody_to_tell:
                summary.no_notifyees += 1
                log.warning("card %r on board %r is due a reminder but nobody is in %r; nobody was told",
                            card.title, title, cfg.prop_notifyees)
            _warn_ignored_values(cfg, title, card, today)
        except Exception:  # noqa: BLE001 - one broken card must never stop the others
            log.exception("board %r: unexpected error on card %r, skipping it", title, raw.get("id"))
            summary.failed += 1
    return found


def _warn_ignored_values(cfg: Config, board_title: str, card: Card, today: date) -> None:
    """Say so when somebody wrote something into "Remind Before" that is not a reminder ("7 dias",
    "7,3", "1.5", 99999), because otherwise that reminder would silently never come.
    Finished cards, templates and deleted cards are not worth a warning."""
    if card.is_template or card.deleted or card.due is None or card.due < today:
        return
    ignored = ignored_values(card)
    if ignored:
        log.warning("card %r on board %r: %r contains values that are not a whole number of days "
                    "between 1 and %d, ignored: %s (write whole numbers separated by ';', e.g. 7;3;1)",
                    card.title, board_title, cfg.prop_remind, MAX_REMIND_BEFORE_DAYS, "; ".join(ignored))


def _load_cards(boards: Boards, board: dict, title: str, summary: Summary, report: BoardReport,
                *, allow_join: bool) -> Optional[list[dict]]:
    """Cards of a board, joining it first when that is needed and allowed.

    Reading cards requires board membership. Open boards can be joined by the bot itself;
    private boards must have the bot added by a board admin. Returns None when unreadable.
    """
    try:
        try:
            return boards.cards(board["id"])
        except ApiError as exc:
            if exc.status != 403 or board.get("type") != "O":
                raise
            if not allow_join:
                log.warning("open board %r: the bot is not a member and may not join now; skipped", title)
                summary.boards_unreadable.append(title)
                report.state, report.detail = "not-joined", "open board, the bot is not a member (and may not join now)"
                return None
            boards.join(board["id"])
            summary.boards_joined += 1
            log.info("joined open board %r so its cards can be read", title)
            return boards.cards(board["id"])
    except ApiError as exc:
        log.warning("cannot read board %r (private boards need the bot added as a member): %s", title, exc)
        summary.boards_unreadable.append(title)
        if exc.status != 403:  # 403 is a permanent setup matter; anything else may be transient: retry
            summary.failed += 1
            report.state, report.detail = "error", f"cannot read it: {exc}"
        else:
            report.state, report.detail = "unreadable", "private board: the bot must be added as a member"
        return None


def _recipients(mm: Driver, summary: Summary, item: _Due) -> list[dict]:
    """The people to message about a card: its Notifyees, minus everyone who must not get it."""
    card, ctx = item.card, item.ctx
    me = mm.me()["id"]
    people: list[dict] = []
    for user_id in card.notifyees:
        try:
            user = mm.user(user_id)
        except ApiError as exc:
            if exc.status == 404:   # permanent: that user does not exist (any more)
                log.warning("card %r: notifyee %s does not exist; skipped", card.title, user_id)
            else:                   # transient: try again later
                log.warning("card %r: cannot look up user %s: %s", card.title, user_id, exc)
                summary.failed += 1
            continue
        if not isinstance(user, dict) or not user.get("id"):
            log.warning("card %r: unexpected answer for user %s; skipped", card.title, user_id)
            continue
        if user["id"] == me:
            continue   # never message ourselves
        if user.get("delete_at"):
            log.info("card %r: skipping deactivated user @%s", card.title, user.get("username"))
            continue
        try:
            allowed = ctx.allows(user["id"])
        except ApiError as exc:
            log.warning("card %r: cannot verify that @%s may see board %r (%s); not messaging now",
                        card.title, user.get("username"), ctx.title, exc)
            summary.failed += 1
            continue
        if not allowed:
            log.warning("card %r: @%s is a notifyee but not a member of board %r or its team; not messaging",
                        card.title, user.get("username"), ctx.title)
            continue
        people.append(user)
    return people


def _remind_card(cfg, mm, store, template, summary, budget: _Budget, today, item: _Due, dry_run: bool) -> None:
    """Phase 2 for one card: send each Notifyee the reminder for the card's current stage, at most once."""
    card, ctx, stage = item.card, item.ctx, item.stage
    assert card.due is not None
    due_key = card.due.isoformat()   # part of the state key: a new due date means new reminders
    days_left = (card.due - today).days
    people = _recipients(mm, summary, item)

    def note(user: dict, state: str) -> None:
        """Record what happened to this person's reminder in the pass report."""
        summary.items.append(ItemReport(
            board_title=ctx.title, card_id=card.id, card_title=card.title, due=card.due, stage=stage,
            days_left=days_left, user_id=user["id"], username=user.get("username") or "", state=state))

    # Display names, made safe to put into a message (see render.sanitize_inline).
    names = {u["id"]: render.sanitize_inline(display_name(u), 80, fallback=render.UNKNOWN) for u in people}

    for user in people:
        username = user.get("username")
        state = store.status(card.id, user["id"], due_key, stage) if store is not None else "none"
        if state == "sent":
            summary.already_sent += 1
            note(user, "already")
            continue
        if state == "pending":    # another process is sending it right now: look again later
            summary.deferred += 1
            note(user, "deferred")
            continue
        if not budget.allows(user["id"], username):
            summary.capped += 1
            note(user, "capped")
            continue

        # The other people told about this card, for templates that mention them (capped to keep it short).
        others = [name for uid, name in names.items() if uid != user["id"]]
        if len(others) > MAX_NAMES_LISTED:
            others = others[:MAX_NAMES_LISTED] + [f"+{len(others) - MAX_NAMES_LISTED}"]
        message = render.render(template, {
            "responsible_name": names[user["id"]],
            "co_responsibles": ", ".join(others),
            "card_title": render.sanitize_inline(card.title, fallback=render.UNKNOWN),
            "board_title": render.sanitize_inline(ctx.title),
            **render.date_values(card.due),
            "days_left": str(days_left),
            "remind_before": str(stage),
            "card_url": ctx.card_url(cfg.site_url, card.id),
        })

        if dry_run:
            log.info("DRY-RUN would send to @%s about %r (the %d-days-before reminder):\n%s",
                     username, card.title, stage, message)
            summary.sent += 1
            budget.spent(user["id"])
            note(user, "to_send")
            continue

        assert store is not None
        # 1. claim: from now on nobody else may send this one
        claimed = store.claim(card.id, user["id"], due_key, stage)
        if claimed is None:
            summary.deferred += 1
            note(user, "deferred")
            continue
        # 2. an orphaned claim means an earlier attempt died or lost its answer. It may have been
        #    delivered after all, so look before sending a second copy.
        if claimed == RECLAIMED:
            try:
                delivered = mm.has_recent_dm(user["id"], message)
            except ApiError as exc:
                log.warning("card %r: cannot check whether @%s already got it (%s); will look again later",
                            card.title, username, exc)
                summary.failed += 1
                note(user, "failed")
                continue   # the claim stays; it goes stale again and is picked up next pass
            if delivered:
                store.confirm(card.id, user["id"], due_key, stage)
                summary.already_sent += 1
                note(user, "already")
                log.info("card %r: @%s had already received it; recorded", card.title, username)
                continue
        # 3. send
        try:
            mm.send_dm(user["id"], message)
        except Exception as exc:  # noqa: BLE001
            summary.failed += 1
            if isinstance(exc, ApiError) and exc.ambiguous:
                # Might have been delivered. Keep the claim: the next pass checks before re-sending.
                log.error("card %r: sending to @%s has an unknown outcome (%s); will verify before retrying",
                          card.title, username, exc)
            else:
                log.error("card %r: could not message @%s: %s", card.title, username, exc)
                store.release(card.id, user["id"], due_key, stage)   # definitely not sent: retry later
            note(user, "failed")
            continue
        # 4. record
        store.confirm(card.id, user["id"], due_key, stage)
        summary.sent += 1
        budget.spent(user["id"])
        note(user, "sent")
        try:   # the readable log for the chat commands; it must never turn a delivered message into an error
            store.record_history(card_id=card.id, card_title=card.title, board_title=ctx.title,
                                 user_id=user["id"], username=username or "", due_date=due_key, days=stage)
        except sqlite3.Error as exc:
            log.warning("could not add the message to the history (%s); it was sent", exc)
        log.info("sent reminder to @%s about %r (due %s, the %d-days-before reminder)",
                 username, card.title, due_key, stage)
