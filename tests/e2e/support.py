"""Helpers of the live-stack tests: where things are, a small REST client, a websocket listener.

Everything is configured by environment variables that compose.yaml sets for the `e2e` service;
the defaults are what that service uses.
"""
from __future__ import annotations

import base64
import json
import os
import socket
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parents[2]
MM_URL = os.environ.get("MM_URL", "http://mattermost:8065").rstrip("/")        # Mattermost, from the stack
SITE_URL = os.environ.get("SITE_URL", "http://localhost:8066").rstrip("/")     # the address in card links
BOT_URL = os.environ.get("BOT_URL", "http://reminder:8080").rstrip("/")        # the running bot, as the tests reach it
# ... and as MATTERMOST reaches it, which is what the slash commands are registered with. The same
# thing in the compose stack; they differ only when the bot runs outside Docker.
COMMANDS_URL = os.environ.get("COMMANDS_URL", BOT_URL).rstrip("/")
SECRETS_BOT = Path(os.environ.get("SECRETS_BOT", "/secrets/bot"))
SECRETS_ADMIN = Path(os.environ.get("SECRETS_ADMIN", "/secrets/admin"))

BOARDS_API = "/plugins/focalboard/api/v2"


def stack_files_present() -> bool:
    return all(path.exists() for path in (SECRETS_BOT / "token", SECRETS_ADMIN / "token",
                                          SECRETS_ADMIN / "dev-state.json"))


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip()


def call(path: str, token: str, method: str = "GET", body=None, timeout: float = 30):
    """One JSON call; returns (status, parsed body or text). Never raises on an HTTP error."""
    req = urllib.request.Request(MM_URL + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("X-Requested-With", "XMLHttpRequest")      # the Boards plugin insists on it
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status, text = resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        status, text = exc.code, exc.read().decode("utf-8", "replace")
    try:
        return status, json.loads(text or "null")
    except ValueError:
        return status, text          # e.g. the Boards plugin answers an unknown route with its HTML page


def post_raw(url: str, data: bytes, content_type: str = "application/x-www-form-urlencoded",
             method: str = "POST", timeout: float = 15):
    """A request to the BOT's own endpoint (not Mattermost): returns (status, text), status 0 if the
    connection was refused or cut. Never raises on an HTTP error."""
    request = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": content_type})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except OSError:
        return 0, ""


def noon_utc_ms(days_from_today: int) -> int:
    """A date as the Boards date picker stores it: 12:00 UTC of the chosen day (Lisbon calendar)."""
    day = datetime.now(ZoneInfo("Europe/Lisbon")).date() + timedelta(days=days_from_today)
    return int(datetime(day.year, day.month, day.day, 12, tzinfo=timezone.utc).timestamp() * 1000)


class Events:
    """Everything Mattermost pushes to one logged-in user over its websocket: the late answers of
    slash commands (event `ephemeral_message`) and pop-up forms (event `open_dialog`).

    A minimal RFC 6455 client, because the standard library has none and the web app is the only
    place these events can be seen: they are not available through the REST API.
    """

    def __init__(self, token: str):
        parts = urllib.parse.urlsplit(MM_URL)
        self.sock = socket.create_connection((parts.hostname, parts.port), timeout=10)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((f"GET /api/v4/websocket HTTP/1.1\r\nHost: {parts.netloc}\r\nUpgrade: websocket\r\n"
                           f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
                           f"Authorization: Bearer {token}\r\n\r\n").encode())
        head = b""
        while b"\r\n\r\n" not in head:
            head += self.sock.recv(1)
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise OSError(f"websocket refused: {head[:80]!r}")
        self.events: list[dict] = []
        self._lock = threading.Lock()
        self._alive = True
        threading.Thread(target=self._read, daemon=True).start()

    def _recv(self, n: int) -> bytes:
        data = b""
        while len(data) < n:
            chunk = self.sock.recv(n - len(data))
            if not chunk:
                raise OSError("closed")
            data += chunk
        return data

    def _read(self) -> None:
        self.sock.settimeout(None)
        message = b""
        try:
            while self._alive:
                b1, b2 = self._recv(2)
                opcode, length = b1 & 0x0F, b2 & 0x7F
                if length == 126:
                    length = struct.unpack(">H", self._recv(2))[0]
                elif length == 127:
                    length = struct.unpack(">Q", self._recv(8))[0]
                payload = self._recv(length)           # the server never masks
                if opcode == 0x9:                       # ping -> pong (client frames must be masked)
                    mask = os.urandom(4)
                    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
                    self.sock.sendall(bytes([0x8A, 0x80 | len(payload)]) + mask + masked)
                    continue
                if opcode == 0x8:
                    return
                message += payload
                if b1 & 0x80:                           # FIN: the message is complete
                    try:
                        event = json.loads(message.decode("utf-8", "replace"))
                        if isinstance(event, dict) and event.get("event"):
                            with self._lock:
                                self.events.append(event)
                    except ValueError:
                        pass
                    message = b""
        except OSError:
            return

    def wait(self, kind: str, contains: str = "", after: int = 0, seconds: float = 25.0):
        """The first event of this kind (after the first `after` events) whose payload holds `contains`."""
        deadline = time.time() + seconds
        while time.time() < deadline:
            with self._lock:
                for event in self.events[after:]:
                    if event.get("event") == kind and contains in json.dumps(event.get("data", {})):
                        return event
            time.sleep(0.1)
        return None

    def count(self) -> int:
        with self._lock:
            return len(self.events)

    def close(self) -> None:
        self._alive = False
        try:
            self.sock.close()
        except OSError:
            pass


def ephemeral_text(event) -> str:
    """The message text of an `ephemeral_message` event."""
    return json.loads(event["data"]["post"])["message"] if event else ""
