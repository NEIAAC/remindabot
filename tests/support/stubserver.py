"""A small stand-in for the parts of Mattermost and Boards that the SHELL scripts use.

tests/support/fakeserver.py is a faithful fake for what the bot does (reading cards, sending
messages). This one is for scripts/dev-seed.sh, scripts/sandbox-setup.sh and
scripts/make-bot-read-only.sh: creating users, teams, a bot, boards, cards and changing memberships.
It is strict about the details that were once wrong (the CSRF header, a multipart plugin upload,
26-character ids), and answers any route it does not know with 404 so a typo in a script fails.
"""
from __future__ import annotations

import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PREFIX = "/plugins/focalboard/api/v2"
BOT_TOKEN = "bot-token-xyz"
ADMIN_TOKEN = "admin-token-abc"


class State:
    def __init__(self):
        self.users: dict[str, dict] = {}        # username -> {id, username, password, roles}
        self.teams: dict[str, dict] = {}        # name -> {id, name}
        self.boards: dict[str, dict] = {}       # id -> board (as created, plus type)
        self.members: dict[str, dict] = {}      # board id -> {user id -> scheme dict}
        self.cards: dict[str, dict] = {}
        self.views: list[str] = []
        self.joined: list[str] = []             # board ids the BOT joined
        self.plugin = False
        self.counter = 0
        self.calls: list[tuple[str, str]] = []
        self.deny_join: set[str] = set()        # board ids the admin may not join (a test of failures)
        self.slow: dict[str, float] = {}        # path suffix -> seconds to wait before answering (to catch a process mid-call)
        self.tokens: dict[str, str] = {BOT_TOKEN: "", ADMIN_TOKEN: ""}   # access token -> user id

    def new_id(self) -> str:
        self.counter += 1
        return f"{self.counter:026d}".replace("0", "a")


class Handler(BaseHTTPRequestHandler):
    state: State
    log_message = lambda *args: None   # noqa: E731  (silence)

    def reply(self, code, obj, headers=None):
        raw = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(raw)

    def caller(self):
        """The user id behind the Authorization header, or None."""
        auth = self.headers.get("Authorization", "")
        token = auth[7:] if auth.startswith("Bearer ") else ""
        if token in self.state.tokens:
            return self.state.tokens[token]
        if token.startswith("session-"):
            return token[len("session-"):]
        return None

    def handle_any(self, method):
        st = self.state
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        ctype = self.headers.get("Content-Type", "")
        data = json.loads(body) if body and "json" in ctype else None
        path = self.path.split("?")[0]
        query = self.path.partition("?")[2]
        st.calls.append((method, self.path))
        if self.headers.get("X-Requested-With") != "XMLHttpRequest":
            return self.reply(400, {"message": "missing X-Requested-With"})
        for suffix, seconds in st.slow.items():
            if path.endswith(suffix):
                time.sleep(seconds)
        me = self.caller()

        if path == "/api/v4/system/ping":
            return self.reply(200, {"status": "OK"})
        if path == "/api/v4/users/login":
            user = st.users.get(data["login_id"])
            if not user or user.get("password") != data["password"]:
                return self.reply(401, {"message": "bad login"})
            return self.reply(200, user, {"Token": "session-" + user["id"]})
        if path == "/api/v4/users/me":
            user = next((u for u in st.users.values() if u["id"] == me), None)
            return self.reply(200, user) if user else self.reply(401, {"message": "no such session"})
        if path == "/api/v4/users/me/teams":
            return self.reply(200, list(st.teams.values()))
        if path == "/api/v4/users" and method == "POST":
            if st.users and not me:
                return self.reply(403, {"id": "api.user.create_user.no_open_server"})   # as the real server
            if data["username"] in st.users:
                return self.reply(400, {"message": "exists"})
            roles = "system_user" if st.users else "system_admin system_user"          # the first user is the admin
            user = {"id": st.new_id(), "username": data["username"], "password": data["password"], "roles": roles}
            st.users[data["username"]] = user
            return self.reply(201, user)
        match = re.fullmatch(r"/api/v4/users/username/(\w+)", path)
        if match:
            user = st.users.get(match.group(1))
            return self.reply(200, user) if user else self.reply(404, {})
        if path == "/api/v4/plugins" and method == "GET":
            return self.reply(200, {"active": [{"id": "focalboard"}] if st.plugin else [], "inactive": []})
        if path == "/api/v4/plugins" and method == "POST":
            if "multipart/form-data" not in ctype or b'name="plugin"; filename="' not in body:
                return self.reply(400, {"message": "not a multipart upload of a file named plugin"})
            return self.reply(201, {"id": "focalboard", "version": "9.1.3"})
        if path == "/api/v4/plugins/focalboard/enable":
            st.plugin = True
            return self.reply(200, {})
        match = re.fullmatch(r"/api/v4/teams/name/(\w+)", path)
        if match:
            team = st.teams.get(match.group(1))
            return self.reply(200, team) if team else self.reply(404, {})
        if path == "/api/v4/teams" and method == "POST":
            team = {"id": st.new_id(), "name": data["name"]}
            st.teams[data["name"]] = team
            return self.reply(201, team)
        if path == "/api/v4/teams" and method == "GET":
            return self.reply(200, list(st.teams.values()))
        if re.fullmatch(r"/api/v4/teams/\w+/members", path):
            return self.reply(201, {})
        if path == "/api/v4/bots" and method == "POST":
            if data["username"] in st.users:
                return self.reply(400, {"message": "exists"})
            user = {"id": st.new_id(), "username": data["username"], "password": "x", "roles": "system_user"}
            st.users[data["username"]] = user
            st.tokens[BOT_TOKEN] = user["id"]
            return self.reply(201, {"user_id": user["id"]})
        match = re.fullmatch(r"/api/v4/users/(\w+)/tokens", path)
        if match:
            owner = match.group(1)
            if owner == st.tokens.get(BOT_TOKEN):
                return self.reply(200, {"token": BOT_TOKEN})
            st.tokens[ADMIN_TOKEN] = owner
            return self.reply(200, {"token": ADMIN_TOKEN})

        if path.startswith(PREFIX):
            if not st.plugin:
                return self.reply(404, {})
            return self.boards(method, path[len(PREFIX):], query, data, me)
        return self.reply(404, {"message": f"the stand-in has no route {method} {self.path}"})

    def boards(self, method, rest, query, data, me):
        st = self.state
        if re.fullmatch(r"/teams/\w+/boards", rest):
            return self.reply(200, list(st.boards.values()))
        if rest == "/boards" and method == "POST":
            if not {"teamId", "type", "title", "cardProperties"} <= set(data):
                return self.reply(400, {"message": "incomplete board"})
            board = {"id": st.new_id(), **data}
            st.boards[board["id"]] = board
            st.members[board["id"]] = {}
            return self.reply(200, board)
        match = re.fullmatch(r"/boards/(\w+)/cards", rest)
        if match and method == "POST":
            if "disable_notify=true" not in query or match.group(1) not in st.boards:
                return self.reply(400, {"message": "bad card request"})
            card = {"id": st.new_id(), "boardId": match.group(1), **data}
            st.cards[card["id"]] = card
            return self.reply(200, card)
        match = re.fullmatch(r"/boards/(\w+)/blocks", rest)
        if match and method == "POST":
            block = data[0]
            if block["type"] != "view" or block["boardId"] != match.group(1) or len(block["id"]) != 26:
                return self.reply(400, {"message": "bad view"})
            st.views.append(match.group(1))
            return self.reply(200, data)
        match = re.fullmatch(r"/boards/(\w+)/join", rest)
        if match and method == "POST":
            board_id = match.group(1)
            board = st.boards.get(board_id)
            if board is None:
                return self.reply(404, {})
            who = next((u for u in st.users.values() if u["id"] == me), None)
            is_admin = bool(who) and "system_admin" in who.get("roles", "")
            if board["type"] == "P" and not (is_admin and "allow_admin=true" in query):
                return self.reply(403, {"message": "private board"})
            if board_id in st.deny_join and is_admin:
                return self.reply(403, {"message": "denied by the test"})
            if me == st.tokens.get(BOT_TOKEN):
                st.joined.append(board_id)
                st.members[board_id][me] = {"schemeAdmin": False, "schemeEditor": True,
                                            "schemeCommenter": False, "schemeViewer": False}
            else:
                st.members[board_id][me] = {"schemeAdmin": True, "schemeEditor": False,
                                            "schemeCommenter": False, "schemeViewer": False}
            return self.reply(200, {"boardId": board_id, "userId": me})
        match = re.fullmatch(r"/boards/(\w+)/members", rest)
        if match and method == "GET":
            members = st.members.get(match.group(1), {})
            if me not in members:
                return self.reply(403, {"message": "not a member"})
            return self.reply(200, [{"boardId": match.group(1), "userId": uid, **scheme}
                                    for uid, scheme in members.items()])
        match = re.fullmatch(r"/boards/(\w+)/members/(\w+)", rest)
        if match and method == "PUT":
            board_id, target = match.groups()
            if not st.members.get(board_id, {}).get(me, {}).get("schemeAdmin"):
                return self.reply(403, {"message": "only a board admin can change roles"})
            st.members[board_id][target] = {k: bool(data.get(k)) for k in
                                            ("schemeAdmin", "schemeEditor", "schemeCommenter", "schemeViewer")}
            return self.reply(200, {})
        return self.reply(404, {"message": f"the stand-in has no route {method} {PREFIX}{rest}"})

    def do_GET(self):
        self.handle_any("GET")

    def do_POST(self):
        self.handle_any("POST")

    def do_PUT(self):
        self.handle_any("PUT")


class StubMattermost:
    """`with StubMattermost() as stub:` -> stub.url, stub.state."""

    def __init__(self):
        self.state = State()
        handler = type("BoundHandler", (Handler,), {"state": self.state})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def __enter__(self):
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
