"""The bot's memory: which reminders were already sent, and a guard against sending any twice.

One SQLite file, one row per (card, person, due date, stage). A "stage" is one number of the
card's "Remind Before" list, i.e. "the 7 days before reminder". The life of a row:

    (no row) --claim--> pending --confirm--> sent
                          |
                          +--release--> (no row)       the send definitely failed, try again later

* `claim()` is atomic across processes, so two runs at the same time (say the daily service and a
  manual `compose run ... --once`) can never both send the same reminder.
* A claim that is neither confirmed nor released (the process died, or the answer to the send was
  lost) is "stale" after STALE_AFTER_S. The next pass may take it over, but it first checks whether
  the message was in fact delivered (see runner.py), so a crash costs a delayed reminder, not a
  duplicate.
* A stage is also silenced by any SMALLER stage already sent for the same card, person and due
  date: once somebody was told "3 days left" there is no point in telling them "7 days" afterwards,
  even if an editor removed the 3 from the list in between. `status()` implements this.
* The table also serves as the audit log for the daily caps: "how many did we send today" is just a
  query on `updated_at`.
* Two more small tables live in the same file: `meta` (key/value: the last completed daily pass and
  the settings an administrator changed from chat, see overrides.py) and `history` (what was sent,
  with the card title and the person's name, so that chat commands can show it).
"""
from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path
from typing import Callable, Optional

# Sending one message takes two HTTP calls of at most ~30 s each. After 5 minutes an unconfirmed
# claim certainly belongs to a dead or lost attempt.
STALE_AFTER_S = 300

# Version 1 of the table had no `days` column (one reminder per card). Its rows are carried over
# with days = 0, the smallest stage, so they silence every stage of that due date.
_CREATE_REMINDERS = """CREATE TABLE IF NOT EXISTS reminders (
                   card_id    TEXT NOT NULL,
                   user_id    TEXT NOT NULL,
                   due_date   TEXT NOT NULL,   -- part of the key: a changed due date = new reminders
                   days       INTEGER NOT NULL, -- the stage: this is the "N days before" reminder
                   status     TEXT NOT NULL,   -- 'pending' (claimed, being sent) | 'sent'
                   updated_at REAL NOT NULL,   -- unix time of the last change
                   PRIMARY KEY (card_id, user_id, due_date, days)
               )"""

# A readable log of every message sent, for the `/remind_history` and `/remind_status` commands.
# It is NOT what prevents duplicates (the reminders table is); losing it would only lose the list.
_CREATE_HISTORY = """CREATE TABLE IF NOT EXISTS history (
                   id         INTEGER PRIMARY KEY AUTOINCREMENT,
                   sent_at    REAL NOT NULL,
                   card_id    TEXT NOT NULL,
                   card_title TEXT NOT NULL,
                   board_title TEXT NOT NULL,
                   user_id    TEXT NOT NULL,
                   username   TEXT NOT NULL,
                   due_date   TEXT NOT NULL,
                   days       INTEGER NOT NULL
               )"""

# Results of claim():
NEW = "new"              # nobody had tried: send it
RECLAIMED = "reclaimed"  # an earlier attempt was orphaned: check whether it was delivered, then send


class Store:
    def __init__(self, path: Path, *, readonly: bool = False, stale_after: int = STALE_AFTER_S,
                 clock: Callable[[], float] = time.time):
        self.stale_after = stale_after
        self._clock = clock
        self.readonly = readonly
        self.db: Optional[sqlite3.Connection] = None   # None = "empty and read-only" (dry-run, no file yet)
        self._days = "days"   # the SQL for the stage column; "0" when reading a version-1 file read-only

        if readonly:
            # --dry-run: look at what was already sent, but never create or change anything.
            if path.exists():
                # as_uri() percent-encodes the path, so characters such as '#' or '?' in it cannot
                # change what SQLite opens.
                db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=10,
                                     isolation_level=None)
                try:
                    has_table = db.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='reminders'").fetchone()
                    if has_table:
                        self.db = db
                        columns = [row[1] for row in db.execute("PRAGMA table_info(reminders)")]
                        if "days" not in columns:
                            self._days = "0"
                    else:
                        db.close()   # a file without our table is the same as no history
                except BaseException:
                    db.close()       # a damaged file: do not keep it open behind the error
                    self.db = None
                    raise
            return

        path.parent.mkdir(parents=True, exist_ok=True)
        # The file holds names, card titles and the key that signs the pop-up forms: it is the bot's
        # alone. Create it private (no window in which it is readable), and tighten an older one.
        if not path.exists():
            os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))
        # isolation_level=None = autocommit; claim() opens its transaction explicitly.
        self.db = sqlite3.connect(path, timeout=10, isolation_level=None)
        try:
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass   # a file owned by somebody else (a bind mount): not ours to change
            self.db.execute(_CREATE_REMINDERS)
            self._upgrade_from_v1()
            self.db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            self.db.execute(_CREATE_HISTORY)
        except BaseException:
            self.close()     # a damaged file: do not keep it open behind the error
            raise

    def _upgrade_from_v1(self) -> None:
        """Rebuild an old-format table (no `days` column) in place, keeping what was sent."""
        assert self.db is not None

        def has_days() -> bool:
            return "days" in [row[1] for row in self.db.execute("PRAGMA table_info(reminders)")]

        if has_days():
            return
        self.db.execute("BEGIN IMMEDIATE")   # one process upgrades; a second one waits, then sees it done
        try:
            if not has_days():
                self.db.execute("ALTER TABLE reminders RENAME TO reminders_v1")
                self.db.execute(_CREATE_REMINDERS)
                self.db.execute("INSERT INTO reminders (card_id, user_id, due_date, days, status, updated_at) "
                                "SELECT card_id, user_id, due_date, 0, status, updated_at FROM reminders_v1")
                self.db.execute("DROP TABLE reminders_v1")
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def now(self) -> float:
        """The current unix time, by this store's clock (tests substitute a fake clock)."""
        return self._clock()

    def close(self) -> None:
        """Release the file. The daemon keeps one open for its whole life; the chat commands open one per use."""
        if self.db is not None:
            self.db.close()
            self.db = None

    # -- reads -------------------------------------------------------------------------------

    def status(self, card_id: str, user_id: str, due_date: str, days: int) -> str:
        """Where does this person stand for the `days` stage of this card?

        'sent'    this stage, or a smaller one, was already sent (so nothing more to say);
        'pending' one of those was claimed recently, so someone may be sending it right now;
        'stale'   one was claimed long ago and never finished;
        'none'    nothing was tried.
        """
        if self.db is None:
            return "none"
        now = self._clock()
        result = "none"
        for _, status, updated_at in self._rows_up_to(card_id, user_id, due_date, days):
            if status == "sent":
                return "sent"
            if (now - updated_at) <= self.stale_after:
                result = "pending"
            elif result == "none":
                result = "stale"
        return result

    def _rows_up_to(self, card_id: str, user_id: str, due_date: str, days: int) -> list[tuple[int, str, float]]:
        """(stage, status, updated_at) of every row of this card+person+due date for stage `days`
        or a smaller one: exactly the rows that decide whether `days` still needs sending."""
        assert self.db is not None
        # `self._days` is the literal text "days" or "0", never user input.
        return self.db.execute(
            f"SELECT {self._days}, status, updated_at FROM reminders "
            f"WHERE card_id=? AND user_id=? AND due_date=? AND {self._days} <= ?",
            (card_id, user_id, due_date, days),
        ).fetchall()

    def sent_since(self, since: float, user_id: Optional[str] = None) -> int:
        """How many messages were sent at or after `since` (optionally to one person).

        This is what the daily safety caps count. Two records each give a lower bound, and the
        larger one is the best estimate: the reminder records (one per card, person and stage; these
        also cover what an older version sent) and the history (one per delivered message, which
        still counts a reminder that was deliberately sent again after `forget`).
        """
        if self.db is None:
            return 0
        extra, args = "", [since]
        if user_id is not None:
            extra = " AND user_id = ?"
            args.append(user_id)
        sent = self.db.execute(
            f"SELECT COUNT(*) FROM reminders WHERE status='sent' AND updated_at >= ?{extra}", args).fetchone()[0]
        try:
            logged = self.db.execute(
                f"SELECT COUNT(*) FROM history WHERE sent_at >= ?{extra}", args).fetchone()[0]
        except sqlite3.OperationalError:     # a file that never had the history table
            logged = 0
        return max(sent, logged)

    def last_run(self) -> Optional[str]:
        """The date (ISO) of the last completed daily pass, or None."""
        return self.get_meta("last_run")

    def get_meta(self, key: str) -> Optional[str]:
        """One value of the key/value area, or None. Safe on a read-only store and on an old file."""
        if self.db is None:
            return None
        try:
            row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        except sqlite3.OperationalError:   # a read-only look at a file that never had the table
            return None
        return row[0] if row else None

    def sent_stages(self, card_id: str, user_id: str, due_date: str) -> set[int]:
        """The stages already sent to this person for this card and due date (a v1 row counts as 0)."""
        if self.db is None:
            return set()
        rows = self.db.execute(
            f"SELECT {self._days} FROM reminders WHERE card_id=? AND user_id=? AND due_date=? AND status='sent'",
            (card_id, user_id, due_date)).fetchall()
        return {r[0] for r in rows}

    def recent_history(self, limit: int = 10, since: float = 0.0) -> list[dict]:
        """The latest sent messages, newest first (at most `limit`, none older than `since`)."""
        if self.db is None:
            return []
        try:
            rows = self.db.execute(
                "SELECT sent_at, card_id, card_title, board_title, user_id, username, due_date, days "
                "FROM history WHERE sent_at >= ? ORDER BY sent_at DESC, id DESC LIMIT ?",
                (since, int(limit))).fetchall()
        except sqlite3.OperationalError:     # an old file read-only: no history yet
            return []
        keys = ("sent_at", "card_id", "card_title", "board_title", "user_id", "username", "due_date", "days")
        return [dict(zip(keys, row)) for row in rows]

    # -- writes ------------------------------------------------------------------------------

    def claim(self, card_id: str, user_id: str, due_date: str, days: int) -> Optional[str]:
        """Atomically take the right to send this reminder (stage `days`).

        Returns NEW or RECLAIMED when the caller now owns it, or None when it is already sent
        (this stage or a smaller one) or another process holds a recent claim on it.
        """
        assert self.db is not None and not self.readonly
        now = self._clock()
        self.db.execute("BEGIN IMMEDIATE")   # take the write lock first: check-and-set is one step
        try:
            blocked = False       # sent already, or somebody holds a recent claim
            orphaned = False      # an old unfinished claim on exactly this stage
            for row_days, status, updated_at in self._rows_up_to(card_id, user_id, due_date, days):
                if status == "sent" or (now - updated_at) <= self.stale_after:
                    blocked = True
                elif row_days == days:
                    orphaned = True
            result: Optional[str]
            if blocked:
                result = None
            elif orphaned:
                self.db.execute(
                    "UPDATE reminders SET updated_at=? WHERE card_id=? AND user_id=? AND due_date=? AND days=?",
                    (now, card_id, user_id, due_date, days))
                result = RECLAIMED
            else:
                # (the columns are named: a file that a NEWER version gave an extra column must still work
                # after a rollback, and positional values would break on it)
                self.db.execute("INSERT INTO reminders (card_id, user_id, due_date, days, status, updated_at) "
                                "VALUES (?, ?, ?, ?, 'pending', ?)", (card_id, user_id, due_date, days, now))
                result = NEW
            self.db.execute("COMMIT")
            return result
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def confirm(self, card_id: str, user_id: str, due_date: str, days: int) -> None:
        """The message is delivered."""
        assert self.db is not None and not self.readonly
        self.db.execute(
            "UPDATE reminders SET status='sent', updated_at=? "
            "WHERE card_id=? AND user_id=? AND due_date=? AND days=?",
            (self._clock(), card_id, user_id, due_date, days))

    def release(self, card_id: str, user_id: str, due_date: str, days: int) -> None:
        """The send definitely failed: give the claim back so the next pass retries.
        A 'sent' row is never touched."""
        assert self.db is not None and not self.readonly
        self.db.execute(
            "DELETE FROM reminders WHERE card_id=? AND user_id=? AND due_date=? AND days=? AND status='pending'",
            (card_id, user_id, due_date, days))

    def set_last_run(self, day: str) -> None:
        self.set_meta("last_run", day)

    def set_meta(self, key: str, value: str) -> None:
        assert self.db is not None and not self.readonly
        self.db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))

    def delete_meta(self, key: str) -> None:
        assert self.db is not None and not self.readonly
        self.db.execute("DELETE FROM meta WHERE key=?", (key,))

    def record_history(self, *, card_id: str, card_title: str, board_title: str, user_id: str,
                       username: str, due_date: str, days: int) -> None:
        """Remember one delivered message for the chat commands. Never allowed to break a send."""
        assert self.db is not None and not self.readonly
        self.db.execute(
            "INSERT INTO history (sent_at, card_id, card_title, board_title, user_id, username, due_date, days) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (self._clock(), card_id, card_title[:300], board_title[:300], user_id, username[:100], due_date, days))

    def forget(self, card_id: str, user_id: str, due_date: str, days: int) -> int:
        """Make the `days` stage sendable again for one person (an administrator asked to resend it).

        Removes this stage and every smaller one, because a smaller stage already sent is exactly
        what silences a bigger one. Returns how many rows were removed.
        """
        assert self.db is not None and not self.readonly
        cur = self.db.execute(
            "DELETE FROM reminders WHERE card_id=? AND user_id=? AND due_date=? AND days <= ?",
            (card_id, user_id, due_date, days))
        return cur.rowcount

    def prune(self, keep_days: int = 400) -> int:
        """Forget very old entries so the file never grows without bound."""
        assert self.db is not None and not self.readonly
        cur = self.db.execute("DELETE FROM reminders WHERE due_date < date('now', ?)", (f"-{keep_days} days",))
        self.db.execute("DELETE FROM history WHERE sent_at < ?", (self._clock() - keep_days * 86400,))
        return cur.rowcount
