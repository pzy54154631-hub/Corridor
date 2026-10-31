"""Loopback-only exact JSON task endpoint; no real bank or external IM access."""

from http.server import BaseHTTPRequestHandler, HTTPServer

from corridor.bench.domestic_runner import run_task
from corridor.crypto.jcs import jcs_bytes
from corridor.schemas.base import loads_exact

MAX_REQUEST_BYTES = 65536


class TaskHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        """Do not print requests or user-provided fields."""

    def _reply(self, status, value):
        content = jcs_bytes(value)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self):
        self._reply(
            200 if self.path == "/health" else 404,
            {"status": "ready" if self.path == "/health" else "not_found", "synthetic": True},
        )

    def do_POST(self):
        if self.path != "/run-task":
            self._reply(404, {"error": "not_found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_REQUEST_BYTES or self.headers.get("Transfer-Encoding"):
                raise ValueError
            data = self.rfile.read(length)
            if len(data) != length:
                raise ValueError
            request = loads_exact(data)
            result = run_task(request)
        except (ValueError, TypeError, KeyError):
            self._reply(400, {"error": "invalid_task", "synthetic": True})
            return
        self._reply(200, result)


def make_server(port=8765):
    if type(port) is not int or not 0 <= port <= 65535:
        raise ValueError("Invalid local port")
    return HTTPServer(("127.0.0.1", port), TaskHandler)
