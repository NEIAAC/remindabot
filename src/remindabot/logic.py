"""The reminder rule, as pure functions with no I/O so it is trivial to test and to change.

A card lists SEVERAL reminders in "Remind Before" (say "7;3;1" = "a week, three days and one day
before"). Each one is a STAGE. The rule, in one sentence: on any day, the card's current stage is
the SMALLEST listed number that is still >= the days left, and that stage is announced once.

    Remind Before = 7;3;1     and the card is due on the 20th
    on the 12th (8 days left)   no stage yet                     -> nothing
    on the 13th (7 days left)   stage 7                          -> one message
    on the 14th-16th            still stage 7, already announced -> nothing
    on the 17th (3 days left)   stage 3                          -> one message
    on the 19th (1 day left)    stage 1                          -> one message
    on the 20th (due today)     still stage 1, already announced -> nothing

If the bot sees the card late (added on the 18th, or it was off for a few days) it sends ONE message
for the stage that applies now, not one per missed stage.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterable, Optional

from .boards import Card

# A reminder more than ~10 years ahead is a typo (and huge values would overflow date arithmetic).
MAX_REMIND_BEFORE_DAYS = 3660


@dataclass(frozen=True)
class Decision:
    """The answer of `evaluate`: whether to remind, and a human-readable reason (shown with DEBUG logs)."""
    send: bool
    reason: str
    stage: Optional[int] = None      # the "N days before" reminder that applies today (None = none yet)
    nobody_to_tell: bool = False     # a reminder applies today, but the card has no Notifyees


def usable_days(values: Iterable[int]) -> tuple[int, ...]:
    """The numbers that count as reminders: 1 .. MAX_REMIND_BEFORE_DAYS, largest first, no repeats.

    Zero and negative numbers are the agreed way to say "never" (they also mark template cards),
    so they are simply not reminders.
    """
    return tuple(sorted({v for v in values if 1 <= v <= MAX_REMIND_BEFORE_DAYS}, reverse=True))


def stage_for(days: tuple[int, ...], days_left: int) -> Optional[int]:
    """The reminder that applies when `days_left` days remain: the smallest listed number that is
    still >= days_left, or None when even the biggest one has not been reached yet.
    `days` is what `usable_days` returns."""
    reached = [n for n in days if n >= days_left]
    return min(reached) if reached else None


def ignored_values(card: Card) -> list[str]:
    """What was written in "Remind Before" but cannot be a reminder, for a warning in the log.

    Zero and negative numbers are NOT listed: they are deliberate (see `usable_days`).
    """
    return list(card.remind_unreadable) + [str(v) for v in card.remind_before if v > MAX_REMIND_BEFORE_DAYS]


def evaluate(card: Card, today: date) -> Decision:
    """Should this card's Notifyees be reminded today, and for which stage?

    Yes when ALL of these hold:
      * it is a real card (not a template, not deleted);
      * "Remind Before" holds at least one whole number between 1 and MAX_REMIND_BEFORE_DAYS
        (empty, 0 and -1 mean "never remind");
      * it has a Due Date that is today or later;
      * the due date is at most as far away as the largest listed number (a stage has been reached);
      * it has at least one Notifyee.
    Whether a given person was already reminded for that stage is decided separately (see store.py).
    """
    if card.is_template:
        return Decision(False, "template card")
    if card.deleted:
        return Decision(False, "deleted card")
    days = usable_days(card.remind_before)
    if not days:
        return Decision(False, "no valid Remind Before (needs whole numbers > 0)")
    if card.due is None:
        return Decision(False, "no Due Date")
    if card.due < today:
        return Decision(False, "due date already passed")
    left = (card.due - today).days
    stage = stage_for(days, left)
    if stage is None:
        return Decision(False, f"too early (the first reminder is {days[0]} days before the due date)")
    if not card.notifyees:
        return Decision(False, "no Notifyees", stage=stage, nobody_to_tell=True)
    return Decision(True, f"due {card.due.isoformat()}, {left} day(s) left, stage {stage}", stage=stage)


def plan(card: Card, today: date, already_sent: Iterable[int], horizon_days: int) -> list[tuple[date, int]]:
    """On which days will this card's reminders go out, if the bot runs every day from `today`?

    Returns (day, stage) pairs, for at most `horizon_days` days ahead (today included). A stage in
    `already_sent` (or any bigger stage than one already sent) is not announced again, exactly like
    the real pass: the reminders a person was already told are the ones in `already_sent`.
    It describes ONE person; everybody in Notifyees gets the same plan unless their history differs.
    """
    if card.is_template or card.deleted or card.due is None:
        return []
    days = usable_days(card.remind_before)
    if not days:
        return []
    sent = set(already_sent)
    out: list[tuple[date, int]] = []
    for offset in range(horizon_days + 1):
        day = today + timedelta(days=offset)
        left = (card.due - day).days
        if left < 0:
            break
        stage = stage_for(days, left)
        if stage is None or any(s <= stage for s in sent):
            continue
        sent.add(stage)
        out.append((day, stage))
    return out
