"""What an administrator changed from chat, layered over the environment configuration.

(config.py is the static configuration, read from the environment once at startup. This module is
the part that people change while the bot runs, so it lives in the state file instead.)

The environment (compose file) and the message file are the DEFAULTS. A system administrator can
override a few of them with slash commands (see commands/); those overrides live in the same
state file as the "already sent" records, in its `meta` table, so there is still just one volume
to back up and nothing else to keep in sync:

    what            default comes from               overridden by
    --------------  -------------------------------  --------------------
    message text    /config/message.txt              /remind_setmessage
    daily send time REMINDER_SEND_AT                 /remind_setsend
    daily caps      REMINDER_MAX_MESSAGES_...        /remind_setcap

Resetting an override (`/remind_reset`) goes back to the default. Nothing here changes the
default files or the environment, so a redeploy with a new compose file still means what it says
until somebody overrides it again.

Two non-configuration bits of state also live here because they too must survive restarts:
whether sending is paused, and which administrators asked to be alerted about problems.
"""
from __future__ import annotations

import dataclasses
import hashlib
import hmac
import json
import re
import secrets
from datetime import time as dtime
from string import Template
from typing import Optional

from . import render
from .config import Config
from .store import Store

# The pop-up form of /remind_setmessage cannot hold more than this.
MAX_MESSAGE_CHARS = 3000
# A cap above this is a typo, not a safety valve.
MAX_CAP = 10000
# How long a pop-up form that was opened stays valid.
DIALOG_VALID_S = 15 * 60

_HHMM = re.compile(r"^([01]?\d|2[0-3])[:h]([0-5]\d)$")

# meta keys
_MESSAGE, _MESSAGE_BY, _MESSAGE_AT = "set.message", "set.message_by", "set.message_at"
_SEND_AT, _SEND_AT_BY, _SEND_AT_AT = "set.send_at", "set.send_at_by", "set.send_at_at"
_CAP_DAY, _CAP_USER, _CAPS_BY, _CAPS_AT = "set.max_per_day", "set.max_per_user", "set.caps_by", "set.caps_at"
_PAUSED, _PAUSED_BY, _PAUSED_AT = "paused", "paused_by", "paused_at"
_ALERTS, _SECRET, _TOKENS = "alerts", "secret", "command_tokens"


class SettingError(ValueError):
    """A value an administrator typed is not acceptable. The text is shown to them as it is."""


def parse_send_at(text: str) -> dtime:
    """'9:00', '09:00' or '9h00' -> a time of day. Seconds are not offered: the loop wakes up once a minute."""
    match = _HHMM.match((text or "").strip().lower())
    if not match:
        raise SettingError(f"`{(text or '').strip()[:30]}` is not a time of day. Write it as HH:MM, for example 14:00.")
    return dtime(int(match.group(1)), int(match.group(2)))


def parse_cap(text: str, what: str) -> int:
    try:
        value = int((text or "").strip())
    except ValueError:
        raise SettingError(f"{what} must be a whole number, not `{(text or '').strip()[:30]}`.") from None
    if not 1 <= value <= MAX_CAP:
        raise SettingError(f"{what} must be between 1 and {MAX_CAP}.")
    return value


class Overrides:
    """Reads and writes the overrides. Reading works on a read-only store; writing needs a writable one."""

    def __init__(self, store: Store):
        self.store = store

    # -- the effective configuration -----------------------------------------------------------

    def effective(self, cfg: Config) -> Config:
        """`cfg` with every override applied. A damaged override value is ignored, never fatal."""
        changes: dict = {}
        raw = self.store.get_meta(_SEND_AT)
        if raw:
            try:
                changes["send_at"] = parse_send_at(raw)
            except SettingError:
                pass
        for key, field in ((_CAP_DAY, "max_per_day"), (_CAP_USER, "max_per_user")):
            raw = self.store.get_meta(key)
            if raw:
                try:
                    changes[field] = parse_cap(raw, field)
                except SettingError:
                    pass
        return dataclasses.replace(cfg, **changes) if changes else cfg

    def active(self) -> dict[str, dict]:
        """Which of the three overridable things are overridden, with who/when (for /remind_status)."""
        out: dict[str, dict] = {}
        if self.store.get_meta(_MESSAGE):
            out["message"] = {"by": self.store.get_meta(_MESSAGE_BY), "at": self._at(_MESSAGE_AT)}
        if self.store.get_meta(_SEND_AT):
            out["time"] = {"by": self.store.get_meta(_SEND_AT_BY), "at": self._at(_SEND_AT_AT)}
        if self.store.get_meta(_CAP_DAY) or self.store.get_meta(_CAP_USER):
            out["caps"] = {"by": self.store.get_meta(_CAPS_BY), "at": self._at(_CAPS_AT)}
        return out

    def _at(self, key: str) -> Optional[float]:
        raw = self.store.get_meta(key)
        try:
            return float(raw) if raw else None
        except ValueError:
            return None

    # -- the message ---------------------------------------------------------------------------

    def custom_message(self) -> Optional[str]:
        return self.store.get_meta(_MESSAGE) or None

    def template(self, cfg: Config) -> tuple[Template, str]:
        """The template to use now, and where it comes from ('file' or 'custom').

        A custom message that no longer parses (it cannot happen through the commands, but the
        file is just a database) falls back to the file instead of stopping the daily pass.
        """
        text = self.custom_message()
        if text:
            try:
                return render.parse_template(text, "the custom message"), "custom"
            except render.TemplateError:
                pass
        return render.load_template(cfg.template_file), "file"

    def message_body(self, cfg: Config) -> str:
        """The current message text as plain text (custom, or the file's body without its header)."""
        text = self.custom_message()
        if text:
            return text
        return render.template_body(cfg.template_file.read_text(encoding="utf-8"))

    def set_message(self, text: str, who: str) -> None:
        """Validate and store a new message. Raises SettingError (readable) or nothing."""
        text = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip("\n")
        if not text.strip():
            raise SettingError("The message is empty.")
        if len(text) > MAX_MESSAGE_CHARS:
            raise SettingError(f"The message is {len(text)} characters long; the limit is {MAX_MESSAGE_CHARS}.")
        try:
            render.parse_template(text, "The message")
        except render.TemplateError as exc:
            raise SettingError(str(exc)) from exc
        self._write(_MESSAGE, text, _MESSAGE_BY, _MESSAGE_AT, who)

    # -- time and caps --------------------------------------------------------------------------

    def set_send_at(self, text: str, who: str) -> dtime:
        value = parse_send_at(text)
        self._write(_SEND_AT, value.strftime("%H:%M"), _SEND_AT_BY, _SEND_AT_AT, who)
        return value

    def set_caps(self, total: Optional[int], per_person: Optional[int], who: str) -> None:
        if total is None and per_person is None:
            raise SettingError("Give at least one number.")
        if total is not None:
            self.store.set_meta(_CAP_DAY, str(total))
        if per_person is not None:
            self.store.set_meta(_CAP_USER, str(per_person))
        self.store.set_meta(_CAPS_BY, who)
        self.store.set_meta(_CAPS_AT, str(self.store.now()))

    def reset(self, what: str) -> list[str]:
        """Go back to the defaults. `what` is message | time | caps | all. Returns what was reset."""
        groups = {
            "message": (_MESSAGE, _MESSAGE_BY, _MESSAGE_AT),
            "time": (_SEND_AT, _SEND_AT_BY, _SEND_AT_AT),
            "caps": (_CAP_DAY, _CAP_USER, _CAPS_BY, _CAPS_AT),
        }
        if what != "all" and what not in groups:
            raise SettingError("Say what to reset: `message`, `time`, `caps` or `all`.")
        done = []
        for name in (groups if what == "all" else [what]):
            if any(self.store.get_meta(key) for key in groups[name]):
                done.append(name)
            for key in groups[name]:
                self.store.delete_meta(key)
        return done

    def _write(self, key: str, value: str, by_key: str, at_key: str, who: str) -> None:
        self.store.set_meta(key, value)
        self.store.set_meta(by_key, who)
        self.store.set_meta(at_key, str(self.store.now()))

    # -- pause ----------------------------------------------------------------------------------

    def paused(self) -> Optional[dict]:
        """None when sending is on; otherwise who paused it and when."""
        if self.store.get_meta(_PAUSED) != "1":
            return None
        return {"by": self.store.get_meta(_PAUSED_BY), "at": self._at(_PAUSED_AT)}

    def pause(self, who: str) -> None:
        self._write(_PAUSED, "1", _PAUSED_BY, _PAUSED_AT, who)

    def resume(self) -> bool:
        """Returns True if it was paused."""
        was = self.paused() is not None
        for key in (_PAUSED, _PAUSED_BY, _PAUSED_AT):
            self.store.delete_meta(key)
        return was

    # -- alerts ---------------------------------------------------------------------------------

    def alert_users(self) -> list[str]:
        try:
            data = json.loads(self.store.get_meta(_ALERTS) or "[]")
        except ValueError:
            return []
        return [u for u in data if isinstance(u, str)]

    def set_alerts(self, user_id: str, on: bool) -> None:
        users = [u for u in self.alert_users() if u != user_id]
        if on:
            users.append(user_id)
        self.store.set_meta(_ALERTS, json.dumps(users))

    def alert_already_sent_today(self, kind: str, day: str) -> bool:
        return self.store.get_meta(f"alert.{kind}") == day

    def mark_alert_sent(self, kind: str, day: str) -> None:
        self.store.set_meta(f"alert.{kind}", day)

    # -- command registration and the pop-up form's signature ----------------------------------

    def command_tokens(self) -> dict[str, dict[str, str]]:
        """{team id: {trigger: token}} as registered by `--register-commands`."""
        try:
            data = json.loads(self.store.get_meta(_TOKENS) or "{}")
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def set_command_tokens(self, tokens: dict[str, dict[str, str]]) -> None:
        self.store.set_meta(_TOKENS, json.dumps(tokens))

    def _secret(self) -> bytes:
        value = self.store.get_meta(_SECRET)
        if not value:
            value = secrets.token_hex(32)
            self.store.set_meta(_SECRET, value)
        return value.encode()

    def sign_state(self, purpose: str, user_id: str) -> str:
        """A value that only this bot could have produced for this person, valid for DIALOG_VALID_S.
        The pop-up form hands it back when it is submitted: Mattermost does not sign submissions."""
        expires = int(self.store.now()) + DIALOG_VALID_S
        nonce = secrets.token_hex(8)
        mac = hmac.new(self._secret(), f"{purpose}|{user_id}|{expires}|{nonce}".encode(), hashlib.sha256).hexdigest()
        return f"{expires}.{nonce}.{mac}"

    def verify_state(self, state: object, purpose: str, user_id: str) -> bool:
        if not isinstance(state, str):
            return False
        try:
            expires_text, nonce, mac = state.split(".")
            expires = int(expires_text)
        except ValueError:
            return False
        if expires < self.store.now():
            return False
        expected = hmac.new(self._secret(), f"{purpose}|{user_id}|{expires}|{nonce}".encode(),
                            hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, mac)
