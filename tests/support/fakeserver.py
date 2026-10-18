"""A fake Mattermost + Boards server for tests, with scriptable faults.

It reproduces the behaviours of the real server that the bot depends on (verified against
Mattermost 10.9.1 / Boards 9.1.3):
  * reading a board's cards requires board membership (403 otherwise); open boards can be joined,
    private boards cannot;
  * the cards endpoint pages with a default of 100 per page;
  * unknown Boards routes answer 200 with an HTML page.
"""
from __future__ import annotations

import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional
from urllib.parse import parse_qs, urlparse



def uid(name: str) -> str:
    """A Mattermost-style 26-character id derived from a readable name (the bot rejects any other shape)."""
    return (name + "z" * 26)[:26]   # padding with a letter keeps "x1" and "x10" distinct


BOT_ID = uid("bot")
ADMIN_ID = uid("root")
BOT_TOKEN = "good-token"


class Fault:
    """Make matching requests misbehave `times` times (None = always)."""

    def __init__(self, match: Callable[[str, str], bool], kind: str, *, times: Optional[int] = None,
                 status: int = 500, body: str = "boom", delay: float = 0.0):
        self.match, self.kind, self.times = match, kind, times
        self.status, self.body, self.delay = status, body, delay
        self.hits = 0


class FakeMattermost:
    def __init__(self):
        self.teams: list[dict] = [{"id": "team1", "name": "nei"}]
        self.users: dict[str, dict] = {}
        self.boards: dict[str, list[dict]] = {"team1": []}   # team id -> board dicts
        self.cards: dict[str, list[dict]] = {}                # board id -> card dicts
        self.views: dict[str, list[dict]] = {}
        self.members: set[str] = set()                        # board ids the bot belongs to
        self.posts: list[dict] = []                           # {"channel_id", "message"}
        self.dm_channels: dict[str, str] = {}                 # channel id -> user id
        self.faults: list[Fault] = []
        self.requests: list[tuple[str, str]] = []
        self.token = BOT_TOKEN
        self.team_members: Optional[set[str]] = None          # None = every known user is a team member
        self.board_members: dict[str, set[str]] = {}          # board id -> user ids (default: everyone)
        self.server_max_per_page: Optional[int] = None        # emulate a server-side cap
        # what the chat commands need (commands/, register.py)
        self.admin_token = "admin-token"                      # a second valid token: a system administrator's
        self.dialogs: list[dict] = []                         # bodies posted to /actions/dialogs/open
        self.delayed: list[dict] = []                         # bodies posted to /hooks/commands/<id> (late answers)
        self.commands: dict[str, list[dict]] = {}             # team id -> custom slash commands
        self.mm_config = {"ServiceSettings": {"EnableCommands": True,
                                              "AllowedUntrustedInternalConnections": "reminder"}}
        self._lock = threading.Lock()
        self._httpd: Optional[ThreadingHTTPServer] = None

    # -- helpers to build data -------------------------------------------------------------

    def add_user(self, user_id: str, username: str, **extra) -> dict:
        user = {"id": user_id, "username": username, "first_name": "", "last_name": "",
                "nickname": "", "delete_at": 0, **extra}
        self.users[user_id] = user
        return user

    def add_board(self, board_id: str, title: str, *, type_: str = "O", member: bool = False,
                  team: str = "team1", props: Optional[list[dict]] = None) -> dict:
        board = {"id": board_id, "teamId": team, "title": title, "type": type_, "deleteAt": 0,
                 "isTemplate": False, "cardProperties": props if props is not None else standard_props()}
        self.boards.setdefault(team, []).append(board)
        self.cards.setdefault(board_id, [])
        self.views.setdefault(board_id, [{"id": f"view-{board_id}"}])
        if member:
            self.members.add(board_id)
        return board

    def add_card(self, board_id: str, card_id: str, title: str, *, due_ms=None, notifyees=None,
                 remind=None, **extra) -> dict:
        """`remind` is stored the way Boards stores a Text property: a string ("12", "7;3;1").
        A non-string (an int, say) is turned into one; pass a list only to simulate a damaged card."""
        props: dict = {}
        if due_ms is not None:
            props["p_due"] = json.dumps({"from": due_ms})
        if notifyees is not None:
            props["p_who"] = notifyees
        if remind is not None:
            props["p_rem"] = remind if isinstance(remind, (list, dict)) else str(remind)
        card = {"id": card_id, "boardId": board_id, "title": title, "isTemplate": False,
                "deleteAt": 0, "properties": props, **extra}
        self.cards[board_id].append(card)
        return card

    # -- lifecycle ---------------------------------------------------------------------------

    def start(self) -> str:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # silence
                pass

            def _handle(self, method: str):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length)) if length else None
                fake._dispatch(self, method, body)

            def do_GET(self):
                self._handle("GET")

            def do_POST(self):
                self._handle("POST")

            def do_PUT(self):
                self._handle("PUT")

            def do_DELETE(self):
                self._handle("DELETE")

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._httpd.daemon_threads = True
        self._httpd.handle_error = lambda *args: None  # clients that hang up on purpose are expected
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{self._httpd.server_address[1]}"

    def stop(self):
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()

    # -- request handling -------------------------------------------------------------------

    @staticmethod
    def _send(h, status: int, payload=None, *, raw: Optional[str] = None, ctype="application/json",
              headers: Optional[dict] = None):
        data = (raw if raw is not None else (json.dumps(payload) if payload is not None else "")).encode()
        h.send_response(status)
        h.send_header("Content-Type", ctype)
        h.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            h.send_header(k, v)
        h.end_headers()
        h.wfile.write(data)

    def _dispatch(self, h, method: str, body):
        parsed = urlparse(h.path)
        path, query = parsed.path, parse_qs(parsed.query)
        with self._lock:
            self.requests.append((method, h.path))

        for fault in self.faults:
            if fault.match(method, h.path) and (fault.times is None or fault.hits < fault.times):
                fault.hits += 1
                if fault.kind == "status":
                    return self._send(h, fault.status, raw=fault.body, ctype="text/plain")
                if fault.kind == "html":
                    return self._send(h, 200, raw="<!doctype html><html></html>", ctype="text/html")
                if fault.kind == "redirect":
                    return self._send(h, 302, raw="", headers={"Location": "http://elsewhere.invalid/"})
                if fault.kind == "echo_auth":
                    # a misbehaving proxy that repeats the request's credentials in its error page
                    return self._send(h, fault.status, raw="bad request, headers were: "
                                      + str(h.headers.get("Authorization")), ctype="text/plain")
                if fault.kind == "slow":
                    time.sleep(fault.delay)
                if fault.kind == "hang":
                    # accepts the request, does nothing for a while, then dies without having acted
                    time.sleep(fault.delay)
                    h.close_connection = True
                    h.connection.close()
                    return
                if fault.kind == "drop":
                    h.close_connection = True
                    h.connection.close()
                    return
                if fault.kind in ("commit_then_drop", "commit_then_slow"):
                    # the server DID create the post, but the answer never arrives: an ambiguous failure
                    if method == "POST" and body is not None:
                        with self._lock:
                            self.posts.append({"channel_id": body["channel_id"], "message": body["message"],
                                               "user_id": self.dm_channels.get(body["channel_id"])})
                    if fault.kind == "commit_then_slow":
                        time.sleep(fault.delay)        # the post exists; the answer is slow (the client may die first)
                    h.close_connection = True
                    h.connection.close()
                    return

        if path.startswith("/hooks/commands/"):    # the late answer of a slash command: no token, as in Mattermost
            with self._lock:
                self.delayed.append(body)
            return self._send(h, 200, raw="ok", ctype="text/plain")

        if h.headers.get("Authorization") not in (f"Bearer {self.token}", f"Bearer {self.admin_token}"):
            return self._send(h, 401, {"message": "bad token"})

        if path.startswith("/api/v4"):
            return self._core(h, method, path[len("/api/v4"):], body)
        if path.startswith("/plugins/focalboard/api/v2"):
            if h.headers.get("X-Requested-With") != "XMLHttpRequest":
                return self._send(h, 400, {"error": "CSRF"})
            return self._boards(h, method, path[len("/plugins/focalboard/api/v2"):], query)
        return self._send(h, 404, {"message": "no route"})

    def _core(self, h, method, path, body):
        if method == "GET" and path == "/users/me":
            if h.headers.get("Authorization") == f"Bearer {self.admin_token}":
                return self._send(h, 200, {"id": ADMIN_ID, "username": "root", "roles": "system_user system_admin"})
            return self._send(h, 200, {"id": BOT_ID, "username": "remindabot", "roles": "system_user"})
        if method == "GET" and path == "/teams":
            return self._send(h, 200, self.teams)
        if method == "GET" and path == "/commands":
            return self._send(h, 200, self.commands.get(parse_qs(urlparse(h.path).query).get("team_id", [""])[0], []))
        if method == "POST" and path == "/commands":
            command = {**body, "id": f"cmd{sum(len(v) for v in self.commands.values()) + 1:023d}x",
                       "token": f"tok-{body['team_id']}-{body['trigger']}"}
            self.commands.setdefault(body["team_id"], []).append(command)
            return self._send(h, 201, command)
        m = re.fullmatch(r"/commands/([^/]+)", path)
        if method == "PUT" and m:
            for commands in self.commands.values():
                for i, command in enumerate(commands):
                    if command["id"] == m.group(1):
                        commands[i] = {**command, **body, "id": command["id"], "token": command["token"]}
                        return self._send(h, 200, commands[i])
            return self._send(h, 404, {"message": "no such command"})
        if method == "DELETE" and m:
            for commands in self.commands.values():
                for command in commands:
                    if command["id"] == m.group(1):
                        commands.remove(command)
                        return self._send(h, 200, {"status": "OK"})
            return self._send(h, 404, {"message": "no such command"})
        if method == "GET" and path == "/config":
            return self._send(h, 200, self.mm_config)
        if method == "POST" and path == "/actions/dialogs/open":
            with self._lock:
                self.dialogs.append(body)
            return self._send(h, 200, {})
        if method == "GET" and path == "/users/me/teams":
            return self._send(h, 200, self.teams)
        m = re.fullmatch(r"/teams/([^/]+)/members/([a-z0-9]+)", path)
        if method == "GET" and m:
            uid = m.group(2)
            ok = uid in self.users and (self.team_members is None or uid in self.team_members)
            if ok:
                return self._send(h, 200, {"team_id": m.group(1), "user_id": uid, "delete_at": 0})
            return self._send(h, 404, {"message": "not a member"})
        m = re.fullmatch(r"/users/([a-z0-9]+)", path)
        if method == "GET" and m:
            user = self.users.get(m.group(1))
            return self._send(h, 200, user) if user else self._send(h, 404, {"message": "no user"})
        if method == "POST" and path == "/channels/direct":
            other = [u for u in body if u != BOT_ID][0]
            channel_id = f"dm-{other}"
            self.dm_channels[channel_id] = other
            return self._send(h, 201, {"id": channel_id})
        m = re.fullmatch(r"/channels/([^/]+)/posts", path)
        if method == "GET" and m:
            mine = [(i, p) for i, p in enumerate(self.posts) if p["channel_id"] == m.group(1)]
            posts = {f"post{i}": {"id": f"post{i}", "user_id": BOT_ID, "message": p["message"],
                                  "channel_id": p["channel_id"]} for i, p in mine}
            return self._send(h, 200, {"order": list(posts), "posts": posts})
        if method == "POST" and path == "/posts":
            with self._lock:
                self.posts.append({"channel_id": body["channel_id"], "message": body["message"],
                                   "user_id": self.dm_channels.get(body["channel_id"])})
            return self._send(h, 201, {"id": f"post{len(self.posts)}"})
        return self._send(h, 404, {"message": f"no core route {method} {path}"})

    def _boards(self, h, method, path, query):
        m = re.fullmatch(r"/teams/([^/]+)/boards", path)
        if method == "GET" and m:
            return self._send(h, 200, self.boards.get(m.group(1), []))
        m = re.fullmatch(r"/boards/([^/]+)/cards", path)
        if method == "GET" and m:
            return self._cards(h, m.group(1), query)
        m = re.fullmatch(r"/boards/([^/]+)/members", path)
        if method == "GET" and m:
            if m.group(1) not in self.members:
                return self._send(h, 403, {"error": "access denied to board members"})
            ids = self.board_members.get(m.group(1), set(self.users) | {BOT_ID})
            return self._send(h, 200, [{"userId": uid, "boardId": m.group(1)} for uid in sorted(ids)])
        m = re.fullmatch(r"/boards/([^/]+)/join", path)
        if method == "POST" and m:
            board = self._find_board(m.group(1))
            if board is None:
                return self._send(h, 404, {"error": "not found"})
            if board["type"] != "O":
                return self._send(h, 403, {"error": "cannot join a non Open board"})
            self.members.add(board["id"])
            return self._send(h, 200, {"boardId": board["id"], "userId": BOT_ID, "schemeEditor": True})
        m = re.fullmatch(r"/boards/([^/]+)/blocks", path)
        if method == "GET" and m:
            return self._send(h, 200, self.views.get(m.group(1), []))
        # what the real plugin does for routes it does not know: 200 and its web page
        return self._send(h, 200, raw="<!doctype html><html><body>Boards</body></html>", ctype="text/html")

    def _find_board(self, board_id):
        for boards in self.boards.values():
            for board in boards:
                if board["id"] == board_id:
                    return board
        return None

    def _cards(self, h, board_id, query):
        if board_id not in self.members:
            return self._send(h, 403, {"error": "access denied to fetch cards"})
        per_page = int(query.get("per_page", ["100"])[0])          # real default: 100
        if self.server_max_per_page:
            per_page = min(per_page, self.server_max_per_page)
        page = int(query.get("page", ["0"])[0])
        cards = self.cards.get(board_id, [])
        return self._send(h, 200, cards[page * per_page:(page + 1) * per_page])


def standard_props() -> list[dict]:
    return [
        {"id": "p_due", "name": "Due Date", "type": "date", "options": []},
        {"id": "p_who", "name": "Notifyees", "type": "multiPerson", "options": []},
        {"id": "p_rem", "name": "Remind Before", "type": "text", "options": []},
    ]


def noon_utc_ms(d) -> int:
    """How the Boards UI stores a date-only value: 12:00 UTC of the chosen day."""
    from datetime import datetime, timezone
    return int(datetime(d.year, d.month, d.day, 12, tzinfo=timezone.utc).timestamp() * 1000)
