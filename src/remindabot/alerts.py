"""Direct messages to the administrators who asked for them (`/remind_alerts on`).

The daily loop calls `notify` when something needs a person: a pass failed, a board is set up
wrongly, a daily cap was hit. Each kind of problem is sent at most once a day, so a pass that
fails every ten minutes does not fill anybody's inbox. Failing to alert is never a problem of
its own: it is logged, and the bot carries on.
"""
from __future__ import annotations

import logging

from .driver import ApiError, Driver
from .overrides import Overrides

log = logging.getLogger("reminder")


def notify(driver: Driver, overrides: Overrides, kind: str, text: str, day: str) -> int:
    """Send `text` to every administrator who opted in, unless this `kind` was already sent for `day`.
    Returns how many people were messaged."""
    try:
        users = overrides.alert_users()
        if not users or overrides.alert_already_sent_today(kind, day):
            return 0
        overrides.mark_alert_sent(kind, day)    # first: even if sending fails we do not retry every ten minutes
    except Exception as exc:  # noqa: BLE001 - alerts are best effort (the state file may be the problem)
        log.warning("cannot prepare an alert (%s): %s", kind, exc)
        return 0
    sent = 0
    for user_id in users:
        try:
            driver.send_dm(user_id, f":rotating_light: **Reminder bot**\n\n{text}")
            sent += 1
        except ApiError as exc:
            log.warning("could not send an alert to %s: %s", user_id[:8], exc)
    return sent
