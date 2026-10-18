"""The webhook server: the small HTTP endpoint that Mattermost calls for the chat commands.

It runs inside the bot's own process, on a thread, and only translates HTTP into calls of
`CommandRouter` (commands/router.py), where everything that matters happens:

    POST /command   a slash command (form-encoded, as Mattermost sends it)
    POST /dialog    the submission of the pop-up form of /remind_setmessage (JSON)
    GET  /healthz   "ok", for a container health probe or a quick `curl`

It must only ever be reachable from Mattermost, i.e. on the Docker network: the compose file does
not publish its port, and every request is authenticated again by the router (token + role). It is
plain HTTP because it never leaves the Docker network.
"""
from __future__ import annotations

import json
import logging
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .commands import CommandRouter

log = logging.getLogger("reminder")

# A slash command or a pop-up form is a few hundred bytes. Refuse anything near this size.
MAX_BODY_BYTES = 256 * 1024
# A client that stops talking must not keep a thread (and the socket) forever.
REQUEST_TIMEOUT_S = 30
# At most this many connections are served at once (one thread each); the rest are hung up on at once.
# Mattermost makes a handful of calls a day, so this only ever refuses something that is not Mattermost.
MAX_CONNECTIONS = 64


def make_handler(router: CommandRouter):
    class Handler(BaseHTTPRequestHandler):
        server_version = "remindabot"
        protocol_version = "HTTP/1.1"
        timeout = REQUEST_TIMEOUT_S

        def log_message(self, fmt, *args):   # the default prints to stderr for every request
            log.debug("chat endpoint: " + fmt, *args)

        def _send(self, status: int, payload) -> None:
            body = json.dumps(payload).encode() if not isinstance(payload, bytes) else payload
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            if self.path.split("?")[0] == "/healthz":
                return self._send(200, b'"ok"')
            self._send(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802
            path = self.path.split("?")[0]
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                return self._send(400, {"error": "bad length"})
            if length < 0 or length > MAX_BODY_BYTES:
                return self._send(413, {"error": "too large"})
            try:
                raw = self.rfile.read(length)
            except OSError:            # it stopped sending (timeout) or hung up: nothing to answer, nothing to log loudly
                log.debug("chat endpoint: the client went away before the end of its request")
                self.close_connection = True
                return
            try:
                if path == "/command":
                    parsed = urllib.parse.parse_qs(raw.decode("utf-8", "replace"), keep_blank_values=True)
                    form = {key: values[0] for key, values in parsed.items()}
                    return self._send(200, router.handle_command(form))
                if path == "/dialog":
                    payload = json.loads(raw.decode("utf-8", "replace") or "{}")
                    if not isinstance(payload, dict):
                        return self._send(400, {"error": "bad payload"})
                    return self._send(200, router.handle_dialog(payload))
            except ValueError:
                return self._send(400, {"error": "bad payload"})
            except Exception:  # noqa: BLE001 - one bad request must never take the endpoint down
                log.exception("chat endpoint: unexpected error on %s", path)
                return self._send(200, {"response_type": "ephemeral",
                                        "text": ":rotating_light: Unexpected error in the bot; it was logged."})
            self._send(404, {"error": "not found"})

    return Handler


class _Server(ThreadingHTTPServer):
    daemon_threads = True          # a stuck request must not keep the process from stopping
    allow_reuse_address = True
    request_queue_size = 128       # the default (5) makes a burst wait a second for the kernel's SYN retry

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._slots = threading.BoundedSemaphore(MAX_CONNECTIONS)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)          # too many at once: hang up instead of starting another thread
            return
        super().process_request(request, client_address)

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


def start_server(router: CommandRouter, bind: str, port: int) -> ThreadingHTTPServer:
    """Start listening on a background thread and return the server (`.shutdown()` stops it).
    Raises OSError if the port cannot be used; the caller decides whether that is fatal."""
    server = _Server((bind, port), make_handler(router))
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="remind-chat-endpoint")
    thread.start()
    return server
