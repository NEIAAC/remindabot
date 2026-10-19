"""The driver: how the bot talks to Mattermost (the role `driver.py` has in other Mattermost bots).

Everything here is plain `urllib` (no third-party packages). There are two layers:

* `Client`  - one JSON-over-HTTP connection with bearer-token auth, retries and safety limits.
              It is shared by the core API (this file) and the Boards plugin API (boards.py).
* `Driver`  - the handful of core calls: who am I, my teams, look up a user, send a direct message,
              open a pop-up form, answer a slash command late.
"""
from __future__ import annotations

import http.client
import json
import re
import socket
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Optional


class ApiError(Exception):
    """A call to Mattermost failed.

    status     HTTP status, or 0 when no HTTP answer was received (connection problem, timeout).
    ambiguous  True when the request MAY have reached the server and been acted on (we only know the
               answer was lost). Matters for "send a message": an ambiguous failure must not be
               blindly repeated, because the message might already exist.
    """

    def __init__(self, status: int, message: str, url: str = "", ambiguous: bool = False):
        super().__init__(f"HTTP {status} from {url}: {message}")
        self.status = status
        self.ambiguous = ambiguous


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow redirects.

    urllib would forward the Authorization header to wherever it is sent and silently turn a
    redirected POST into a GET (so a "successful" DM would have sent nothing). A redirect here
    means REMINDER_MM_URL is wrong, which should be a loud error.
    """

    def redirect_request(self, *args, **kwargs):  # noqa: D401
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)

# No legitimate answer from Mattermost is anywhere near this big; refuse to read more into memory.
MAX_RESPONSE_BYTES = 32 * 1024 * 1024

# Transient conditions worth retrying (status 0 = could not connect / timed out).
_RETRY_STATUSES = {0, 429, 500, 502, 503, 504}

# A Mattermost id is exactly 26 characters of [a-z0-9]. Anything else (a card's Notifyees value is
# typed by users!) must never be put into a URL path: "me", "../x" or "username/bob" would make the
# bot call endpoints nobody intended.
_ID_RE = re.compile(r"^[a-z0-9]{26}$")


def is_valid_id(value: object) -> bool:
    return isinstance(value, str) and _ID_RE.fullmatch(value) is not None


class Client:
    """JSON-over-HTTP with bearer auth. Shared by the core API and the Boards plugin API."""

    def __init__(self, base_url: str, token: str, prefix: str = "", timeout: float = 30,
                 retries: int = 2, sleep: Callable[[float], None] = time.sleep,
                 beat: Callable[[], None] = lambda: None):
        self.base_url = base_url
        self.token = token
        self.prefix = prefix
        self.timeout = timeout
        self.retries = retries
        self._sleep = sleep
        self._beat = beat   # called before every attempt so the container healthcheck stays fresh

    def request(self, method: str, path: str, body: Any = None, *, idempotent: Optional[bool] = None) -> Any:
        """Retries transient failures, but only for requests that are safe to repeat.

        GETs are always safe. A POST that could create something twice (sending a message) must
        pass idempotent=False (the default for non-GET): a timeout does not tell us whether the
        server already acted, so it is not hammered again within seconds. A failed message is
        retried later by the scheduler instead (see scheduler.serve).
        """
        if idempotent is None:
            idempotent = method == "GET"
        attempts = 1 + (self.retries if idempotent else 0)
        for attempt in range(attempts):
            self._beat()
            try:
                return self._once(method, path, body)
            except ApiError as exc:
                if attempt + 1 >= attempts or exc.status not in _RETRY_STATUSES:
                    raise
                self._sleep(min(2 ** attempt, 10))
        raise AssertionError("unreachable")

    def _once(self, method: str, path: str, body: Any) -> Any:
        """One HTTP attempt. Every failure becomes an ApiError; nothing else escapes."""
        url = f"{self.base_url}{self.prefix}{path}"
        try:
            req = urllib.request.Request(url, method=method)
            if self.token:
                req.add_header("Authorization", f"Bearer {self.token}")
            # Required by the Boards plugin's CSRF check; harmless for the core API.
            req.add_header("X-Requested-With", "XMLHttpRequest")
            data = None
            if body is not None:
                req.add_header("Content-Type", "application/json")
                data = json.dumps(body).encode()
            with _OPENER.open(req, data, timeout=self.timeout) as resp:
                payload = resp.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                if 300 <= exc.code < 400:
                    detail = f"redirected to {exc.headers.get('Location')!r}; check REMINDER_MM_URL"
                else:
                    try:
                        detail = exc.read(300).decode("utf-8", errors="replace")
                    except Exception:  # noqa: BLE001 - the body is only for the log line
                        detail = "(error body unreadable)"
            finally:
                exc.close()    # an error answer holds the connection open until it is closed
            # The detail goes into the log. A proxy's error page may repeat our headers: never the token.
            if len(self.token) >= 8:
                detail = detail.replace(self.token, "***")
            raise ApiError(exc.code, detail, url) from exc
        except ValueError as exc:  # e.g. "unknown url type" for a base URL without a scheme
            raise ApiError(0, f"invalid request: {exc}", url) from exc
        except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
            reason = getattr(exc, "reason", exc)
            # Refused connections and DNS failures never reached the server, so nothing was done.
            # Timeouts, resets and truncated answers might have been acted on.
            never_delivered = isinstance(reason, (ConnectionRefusedError, socket.gaierror))
            raise ApiError(0, f"connection failed: {reason!r}", url, ambiguous=not never_delivered) from exc
        if len(payload) > MAX_RESPONSE_BYTES:
            raise ApiError(200, f"response larger than {MAX_RESPONSE_BYTES} bytes", url)
        raw = payload.decode("utf-8", errors="replace")
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError as exc:
            # Boards answers 200 + its web page for routes it does not know; never treat that as data.
            raise ApiError(200, "response was not JSON (wrong endpoint?)", url) from exc


_CLOSING_ANGLE = re.compile(r"(?m)(?<!^)>")


def for_command_reply(text: str) -> str:
    """The text of a slash command's reply, made safe from a Mattermost rewrite.

    Mattermost turns every `<address|words>` in a command reply into the link `[words](address)`
    (a Slack-compatibility feature, always on). It does not know about tables or code spans, so a
    `<` in one table cell and the next `>` in another swallow everything in between: that is how a
    help line such as `/remind_card <card link or title>` once ate its own explanation. Only slash
    command replies are rewritten (posts and direct messages made through the API are not), so
    this is applied to those and nothing else.

    Every `<` becomes the look-alike `‹` and every `>` the look-alike `›` (a `>` that starts a line
    is a quotation mark and stays), which Mattermost leaves alone. Nothing here needs a real one.
    A `>` that render.sanitize_inline already escaped (`\\>`) is replaced together with its backslash.
    """
    return _CLOSING_ANGLE.sub("\u203a", text.replace("<", "\u2039").replace("\\>", "\u203a"))


def display_name(user: dict) -> str:
    """Same preference as the server's TeammateNameDisplay=nickname_full_name; a person with neither
    is greeted by their @username ("colega" only if even that is missing)."""
    nickname = (user.get("nickname") or "").strip()
    if nickname:
        return nickname
    full = f"{user.get('first_name') or ''} {user.get('last_name') or ''}".strip()
    if full:
        return full
    username = user.get("username")
    return f"@{username}" if username else "colega"


class Driver:
    """The Mattermost core-API calls the bot needs, with small per-pass caches."""

    def __init__(self, base_url: str, token: str, timeout: float = 30,
                 beat: Callable[[], None] = lambda: None):
        self.api = Client(base_url, token, prefix="/api/v4", timeout=timeout, beat=beat)
        # delayed answers to slash commands go to /hooks/..., outside /api/v4, and carry no token
        self._hooks = Client(base_url, "", prefix="", timeout=timeout, beat=beat)
        self._users: dict[str, dict] = {}
        self._team_members: dict[tuple[str, str], bool] = {}
        self._me: Optional[dict] = None

    def reset_caches(self) -> None:
        """Forget cached lookups. Called at the start of every pass: the daemon lives for months,
        and people join teams, leave them or get deactivated in between."""
        self._users.clear()
        self._team_members.clear()
        self._me = None

    def me(self) -> dict:
        """The bot's own user (its id is needed to open DM channels)."""
        if self._me is None:
            self._me = self.api.request("GET", "/users/me")
        return self._me

    def my_teams(self) -> list[dict]:
        return self.api.request("GET", "/users/me/teams") or []

    def user(self, user_id: str) -> dict:
        """Look a user up by id. The id must look like a Mattermost id (see _ID_RE)."""
        if not is_valid_id(user_id):
            raise ApiError(404, f"{user_id!r} is not a valid user id", "")
        if user_id not in self._users:
            self._users[user_id] = self.api.request("GET", f"/users/{user_id}")
        return self._users[user_id]

    def is_team_member(self, team_id: str, user_id: str) -> bool:
        """True if the user is an active member of the team. Errors other than 404 propagate (cannot verify)."""
        key = (team_id, user_id)
        if key not in self._team_members:
            try:
                member = self.api.request("GET", f"/teams/{team_id}/members/{user_id}")
                self._team_members[key] = bool(member) and not member.get("delete_at")
            except ApiError as exc:
                if exc.status != 404:
                    raise
                self._team_members[key] = False
        return self._team_members[key]

    def _dm_channel(self, user_id: str) -> str:
        # Opening a DM channel is idempotent (returns the existing one), so it may be retried.
        channel = self.api.request("POST", "/channels/direct", [self.me()["id"], user_id], idempotent=True)
        return channel["id"]

    def has_recent_dm(self, user_id: str, message: str, limit: int = 60) -> bool:
        """Did the bot already post exactly this message in its DM with the user (recently)?

        Used before re-sending a message whose earlier attempt has an unknown outcome (the process
        died or the answer was lost), so that "unknown" never turns into a duplicate.
        """
        channel_id = self._dm_channel(user_id)
        data = self.api.request("GET", f"/channels/{channel_id}/posts?per_page={int(limit)}") or {}
        posts = data.get("posts") or {}
        me = self.me()["id"]
        return any(isinstance(p, dict) and p.get("user_id") == me and p.get("message") == message
                   for p in posts.values())

    def send_dm(self, user_id: str, message: str) -> None:
        """Send a direct message. Not retried here (see Client.request)."""
        channel_id = self._dm_channel(user_id)
        self.api.request("POST", "/posts", {"channel_id": channel_id, "message": message}, idempotent=False)

    # -- what the chat commands need (commands/) --------------------------------------------

    def fresh_user(self, user_id: str) -> dict:
        """A user looked up NOW, bypassing the per-pass cache (an administrator who was just
        demoted must not keep working commands for the rest of the day)."""
        if not is_valid_id(user_id):
            raise ApiError(404, f"{user_id!r} is not a valid user id", "")
        return self.api.request("GET", f"/users/{user_id}")

    def is_system_admin(self, user_id: str) -> bool:
        """True if this active user has the system_admin role right now."""
        user = self.fresh_user(user_id)
        roles = str(user.get("roles") or "").split() if isinstance(user, dict) else []
        return "system_admin" in roles and not user.get("delete_at")

    def open_dialog(self, trigger_id: str, callback_url: str, dialog: dict) -> None:
        """Open a pop-up form for the person who just typed a slash command (the trigger id is only
        valid for a few seconds)."""
        self.api.request("POST", "/actions/dialogs/open",
                         {"trigger_id": trigger_id, "url": callback_url, "dialog": dialog}, idempotent=False)

    def answer_later(self, response_url: str, text: str) -> None:
        """Deliver the late answer of a slash command (only its author sees it).

        `response_url` comes from the request, so it is not trusted: only its PATH is used, and only
        if it is a command-response path; it is sent to OUR Mattermost address (the URL in the
        request carries the public site address, which this container may not be able to reach).
        """
        import urllib.parse
        path = urllib.parse.urlsplit(response_url or "").path
        if not re.fullmatch(r"/hooks/commands/[A-Za-z0-9]{10,40}", path):
            raise ApiError(0, f"not a command response address: {path[:60]!r}", "")
        try:
            self._hooks.request("POST", path,
                                {"response_type": "ephemeral", "text": for_command_reply(text)[:15000]},
                                idempotent=False)
        except ApiError as exc:
            if exc.status != 200:   # Mattermost answers this call with the plain text "ok"
                raise
