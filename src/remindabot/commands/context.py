"""What a command handler is given: everything one invocation needs, and nothing global."""
from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

from ..boards import Boards
from ..config import Config
from ..driver import Driver
from ..overrides import Overrides
from ..runner import Summary, run_once
from ..store import Store
from . import markup as md


@dataclass
class Ctx:
    """One slash command, as seen by its handler."""
    cfg: Config               # the EFFECTIVE configuration (what was changed from chat applied)
    overrides: Overrides      # what an administrator changed from chat, and the pause
    store: Store              # this invocation's own connection to the state file
    driver: Driver            # Mattermost, for this invocation (its caches are not shared)
    boards: Boards
    lock: threading.Lock      # held while a pass runs, so a command and the daily pass never overlap
    user_id: str              # who typed it (already checked to be a system administrator)
    username: str
    args: str                 # what came after the command, stripped
    trigger_id: str           # needed to open a pop-up form, valid for a few seconds
    now: datetime             # in the configured timezone

    @property
    def today(self) -> date:
        return self.now.date()

    def template(self):
        """The message template in use (the one set from chat, else the file)."""
        return self.overrides.template(self.cfg)[0]

    def scan(self) -> Summary:
        """Look at everything, change nothing (a dry run): the basis of status, next, boards, card."""
        return run_once(self.cfg, self.driver, self.boards, self.store, self.template(), self.today, dry_run=True)

    def paused_text(self) -> Optional[str]:
        """The refusal to show when sending is paused, or None when it is not."""
        info = self.overrides.paused()
        if info is None:
            return None
        return (f":pause_button: Sending is **paused** ({md.who_when(info, self.cfg)}). "
                "Use `/remind_resume` first.")


@dataclass
class DialogCtx:
    """The submission of a pop-up form, as seen by its handler (already authenticated)."""
    cfg: Config
    overrides: Overrides
    driver: Driver
    user: dict                # the person's own Mattermost account, read just now
    user_id: str
    payload: dict             # what Mattermost posted: `submission` holds the fields
