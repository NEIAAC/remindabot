"""Configuration: every setting comes from an environment variable named REMINDER_<NAME>.

`Config.from_env()` reads them once at startup and checks them, so a typo is reported immediately
(with the name of the variable) instead of at 14:00 one afternoon. docs/configuration.md has the list.

Any setting can also be given as REMINDER_<NAME>_FILE, the path of a file holding the value: the
convention of Docker secrets, so a token does not have to sit in the environment (and in
`docker inspect`). The plain variable wins when both are set.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import time
from pathlib import Path
from typing import Mapping, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class ConfigError(Exception):
    """Raised when the environment does not describe a usable configuration."""


# Accepted spellings of a yes/no setting (compared in lower case).
_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def env_or_file(env: Mapping[str, str], name: str) -> str:
    """The value of REMINDER_<name>, else the content of the file named by REMINDER_<name>_FILE,
    else "". Blank counts as unset (docker compose passes `VAR=` through for a missing `${VAR}`).
    A file that cannot be read is an error, not a silent "unset": the token would just be "missing".
    """
    value = (env.get(f"REMINDER_{name}") or "").strip()
    if value:
        return value
    path = (env.get(f"REMINDER_{name}_FILE") or "").strip()
    if not path:
        return ""
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ConfigError(f"REMINDER_{name}_FILE={path!r} cannot be read: {exc.strerror or exc}") from exc


def _positive_int(raw: str, name: str) -> int:
    """A whole number >= 1, or a ConfigError that names the variable."""
    try:
        value = int(raw)
        if value < 1:
            raise ValueError
        return value
    except ValueError as exc:
        raise ConfigError(f"{name}={raw!r} must be a positive whole number") from exc


def _http_url(raw: str, name: str) -> str:
    """An http(s) URL with a host, without a trailing slash, or a ConfigError."""
    url = raw.strip().rstrip("/")
    scheme, _, rest = url.partition("://")
    if scheme not in ("http", "https") or not rest or rest.startswith("/"):
        raise ConfigError(f"{name}={raw!r} must look like https://chat.example.com")
    return url


@dataclass(frozen=True)
class Config:
    """All settings, validated. Immutable: nothing changes it after startup."""

    mm_url: str            # where the bot reaches Mattermost (the internal Docker network address)
    site_url: str          # the public address; only used to build links inside messages
    bot_token: str         # the bot account's access token
    tz: ZoneInfo           # the timezone in which "today" and the daily send time are understood
    send_at: time          # time of day of the daily pass, in `tz`
    daily_pass: bool       # False: no daily pass at all; reminders go out only by --once or /remind_send
    prop_due: str          # name of the board property holding the due date
    prop_notifyees: str    # name of the property holding the people to notify
    prop_remind: str       # name of the property holding the list of "remind N days before"
    auto_join: bool        # join *open* boards by itself so their cards can be read
    max_per_day: int       # safety valve: never send more than this many messages in one day
    max_per_user: int      # ... and never more than this many to the same person in one day
    commands_bind: str     # address the chat-command endpoint listens on, inside the container
    commands_port: int     # its port; 0 switches the chat commands off
    commands_url: str      # how MATTERMOST reaches that endpoint (used when registering the commands)
    template_file: Path    # the message text
    state_file: Path       # the SQLite file remembering what was already sent
    heartbeat_file: Path   # touched regularly so the container healthcheck can see we are alive

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "Config":
        """Build a Config from the environment (or from `env`, which tests use)."""
        env = os.environ if env is None else env

        def get(name: str, default: Optional[str] = None) -> str:
            """Value of REMINDER_<name> (or of the file REMINDER_<name>_FILE); a blank value counts as unset."""
            value = env_or_file(env, name)
            if value:
                return value
            if default is not None and default.strip():
                return default.strip()
            raise ConfigError(f"REMINDER_{name} is required")

        # Timezone: REMINDER_TZ, else the container's TZ, else Lisbon.
        tz_name = get("TZ", env.get("TZ") or "Europe/Lisbon")
        try:
            tz = ZoneInfo(tz_name)
        except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
            raise ConfigError(f"REMINDER_TZ={tz_name!r} is not a valid timezone") from exc

        # Daily send time, "HH:MM".
        send_at_raw = get("SEND_AT", "14:00")
        try:
            hour, minute = send_at_raw.split(":")
            send_at = time(int(hour), int(minute))
        except ValueError as exc:
            raise ConfigError(f"REMINDER_SEND_AT={send_at_raw!r} must look like HH:MM") from exc

        auto_join_raw = get("AUTO_JOIN", "true").lower()
        if auto_join_raw not in _TRUE | _FALSE:
            raise ConfigError(f"REMINDER_AUTO_JOIN={auto_join_raw!r} must be true or false")
        daily_raw = get("DAILY_PASS", "true").lower()
        if daily_raw not in _TRUE | _FALSE:
            raise ConfigError(f"REMINDER_DAILY_PASS={daily_raw!r} must be true or false")

        # Safety caps.
        max_per_day = _positive_int(get("MAX_MESSAGES_PER_DAY", "100"), "REMINDER_MAX_MESSAGES_PER_DAY")
        max_per_user = _positive_int(get("MAX_MESSAGES_PER_USER_PER_DAY", "20"),
                                     "REMINDER_MAX_MESSAGES_PER_USER_PER_DAY")

        # Addresses.
        site_url = _http_url(get("SITE_URL"), "REMINDER_SITE_URL")
        mm_url = _http_url(get("MM_URL", "http://mattermost:8065"), "REMINDER_MM_URL")

        # The chat commands: a small HTTP endpoint that Mattermost calls (never published outside the
        # Docker network). Port 0 = off.
        port_raw = get("COMMANDS_PORT", "8080")
        try:
            commands_port = int(port_raw)
            if not 0 <= commands_port <= 65535:
                raise ValueError
        except ValueError:
            raise ConfigError(f"REMINDER_COMMANDS_PORT={port_raw!r} must be a port number, or 0 to switch "
                              "the chat commands off") from None
        commands_url = _http_url(get("COMMANDS_URL", f"http://reminder:{commands_port or 8080}"),
                                 "REMINDER_COMMANDS_URL")

        return cls(
            mm_url=mm_url,
            site_url=site_url,
            bot_token=get("BOT_TOKEN"),
            tz=tz,
            send_at=send_at,
            daily_pass=daily_raw in _TRUE,
            prop_due=get("PROP_DUE", "Due Date"),
            prop_notifyees=get("PROP_NOTIFYEES", "Notifyees"),
            prop_remind=get("PROP_REMIND", "Remind Before"),
            auto_join=auto_join_raw in _TRUE,
            max_per_day=max_per_day,
            max_per_user=max_per_user,
            commands_bind=get("COMMANDS_BIND", "0.0.0.0"),
            commands_port=commands_port,
            commands_url=commands_url,
            template_file=Path(get("TEMPLATE_FILE", "/config/message.txt")),
            state_file=Path(get("STATE_FILE", "/data/state.sqlite")),
            heartbeat_file=Path(get("HEARTBEAT_FILE", "/tmp/heartbeat")),
        )
