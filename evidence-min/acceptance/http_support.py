from __future__ import annotations
import asyncio,contextlib,http.client,io,json,socket,threading
from http.cookies import SimpleCookie
from typing import Any,Generator
from tornado.httpclient import HTTPRequest,HTTPResponse
from tornado.httpserver import HTTPServer
from tornado.httputil import HTTPHeaders
from tornado.ioloop import IOLoop
from tornado.testing import bind_unused_port
COOKIE="corridor_web"


class Served:
    """``make_app(settings)`` on 127.0.0.1:<free port>, served by its own event loop thread."""

    def __init__(self, settings: dict[str, Any]) -> None:
        from corridor.web.app import SERVER_OPTIONS, make_app

        sock, port = bind_unused_port()
        self.app = make_app({**settings, "port": port})
        self.loop = asyncio.new_event_loop()
        self._options = SERVER_OPTIONS
        started = threading.Event()
        failed: list[BaseException] = []

        def run() -> None:
            asyncio.set_event_loop(self.loop)
            try:
                self.server, self.io_loop = self.loop.run_until_complete(self._start(sock))
            except BaseException as exc:
                failed.append(exc)
                started.set()
                return
            started.set()
            self.loop.run_forever()
            with contextlib.suppress(Exception):
                self.io_loop.close(all_fds=True)

        self.thread = threading.Thread(target=run, name="web-test-server", daemon=True)
        self.thread.start()
        started.wait(30)
        if failed:
            raise failed[0]

    async def _start(self, sock: socket.socket) -> tuple[HTTPServer, IOLoop]:
        server = HTTPServer(self.app, **self._options)
        server.add_sockets([sock])
        return server, IOLoop.current()

    def call(self, function: Any, *args: Any) -> Any:
        """Run ``function(*args)`` on the server's loop thread and return its result."""

        async def call() -> Any:
            return function(*args)

        return asyncio.run_coroutine_threadsafe(call(), self.loop).result(30)

    def close(self) -> None:
        async def stop() -> None:
            self.server.stop()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.server.close_all_connections(), 5)

        with contextlib.suppress(Exception):
            asyncio.run_coroutine_threadsafe(stop(), self.loop).result(10)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(10)
        self.app.settings["executor"].shutdown()

@contextlib.contextmanager
def serve(settings: dict[str, Any] | None = None) -> Generator[Any, None, None]:
    served = Served(dict(settings or {}))
    served.app.settings["served"] = served
    try:
        yield served.app
    finally:
        served.close()

def _merge(base: dict[str, Any], extra: dict[str, Any] | None) -> list[tuple[str, str]]:
    """Header lines: ``extra`` replaces by name (any case), None drops, a list repeats."""
    merged = dict(base)
    for name, value in (extra or {}).items():
        for existing in [key for key in merged if key.lower() == name.lower()]:
            del merged[existing]
        merged[name] = value
    lines: list[tuple[str, str]] = []
    for name, value in merged.items():
        if value is None:
            continue
        for item in value if isinstance(value, (list, tuple)) else [value]:
            lines.append((name, str(item)))
    return lines

class WebClient:
    """One browser. ``post`` and ``get`` carry what the page's own requests carry.

    POST: Host, Origin, Content-Type, the cookie, and ``X-Corridor-Web`` for
    ``/api/session`` or the tab and generation headers otherwise; GET under ``/api/``
    (except the phone) carries the tab and generation. The generation is adopted as the
    page does (contract §1.5): from ``/api/session`` and reset successes and from a
    ``stale_generation`` answer.
    """

    def __init__(self, app: Any, *, host: str | None = None) -> None:
        self.app = app
        self.port: int = app.settings["port"]
        self.host = host or f"127.0.0.1:{self.port}"
        self.browser: str | None = None
        self.tab: str | None = None
        self.generation: str | None = None
        self.revision: int | None = None
        self.timeout = 60
        self._lock = threading.Lock()

    @property
    def origin(self) -> str:
        return "http://" + self.host

    def open(self, *, view: str | None = None, locale: str | None = None) -> dict[str, Any]:
        body = {
            name: value
            for name, value in (("tab", self.tab), ("view", view), ("locale", locale))
            if value is not None
        }
        response = self.post("/api/session", body)
        data = self.json(response)
        assert response.code == 200, (response.code, data)
        return data

    def session_headers(self) -> dict[str, Any]:
        return {"X-Corridor-Tab": self.tab, "X-Corridor-Generation": self.generation}

    def cookie(self) -> dict[str, Any]:
        return {"Cookie": None if self.browser is None else f"{COOKIE}={self.browser}"}

    def post(
        self,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        headers: dict[str, Any] | None = None,
        raw: bytes | None = None,
    ) -> HTTPResponse:
        payload = (
            raw
            if raw is not None
            else json.dumps({} if body is None else body, ensure_ascii=False).encode("utf-8")
        )
        base: dict[str, Any] = {
            "Host": self.host,
            "Origin": self.origin,
            "Content-Type": "application/json",
            **self.cookie(),
        }
        if path == "/api/session":
            base["X-Corridor-Web"] = "1"
        else:
            base.update(self.session_headers())
        return self.request("POST", path, payload, _merge(base, headers))

    def get(self, path: str, *, headers: dict[str, Any] | None = None) -> HTTPResponse:
        base: dict[str, Any] = {"Host": self.host, **self.cookie()}
        if path.startswith("/api/") and not path.startswith("/api/phone/"):
            base.update(self.session_headers())
        return self.request("GET", path, None, _merge(base, headers))

    def json(self, response: HTTPResponse) -> dict[str, Any]:
        return json.loads(response.body.decode("utf-8"))

    def abandon(self, path: str, body: dict[str, Any] | None = None) -> None:
        """Send a POST and hang up before the answer (a closed tab, a lost connection)."""
        payload = json.dumps({} if body is None else body).encode("utf-8")
        base = {"Host": self.host, "Origin": self.origin, "Content-Type": "application/json"}
        base.update(self.session_headers())
        base.update(self.cookie())
        self.request("POST", path, payload, _merge(base, None), read=False)

    def request(
        self,
        method: str,
        path: str,
        body: bytes | None,
        lines: list[tuple[str, str]],
        *,
        read: bool = True,
    ) -> HTTPResponse:
        """Exactly the given header lines; ``post`` and ``get`` add the defaults."""
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=self.timeout)
        try:
            connection.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            for name, value in lines:
                connection.putheader(name, value)
            if body is not None:
                connection.putheader("Content-Length", str(len(body)))
            connection.endheaders(body)
            if not read:
                return None  # type: ignore[return-value]
            answer = connection.getresponse()
            data = answer.read()
        finally:
            connection.close()
        headers = HTTPHeaders()
        for name, value in answer.getheaders():
            headers.add(name, value)
        url = f"http://{self.host}{path}"
        response = HTTPResponse(
            HTTPRequest(url, method=method),
            answer.status,
            headers=headers,
            buffer=io.BytesIO(data),
            effective_url=url,
            reason=answer.reason,
        )
        self._adopt(path, response)
        return response

    def _adopt(self, path: str, response: HTTPResponse) -> None:
        with self._lock:
            for line in response.headers.get_list("Set-Cookie"):
                jar: SimpleCookie = SimpleCookie()
                jar.load(line)
                if COOKIE in jar:
                    self.browser = jar[COOKIE].value
            if "application/json" not in response.headers.get("Content-Type", ""):
                return
            data = json.loads(response.body.decode("utf-8"))
            if not isinstance(data, dict) or "generation" not in data:
                return
            stale = not data.get("ok") and data["error"]["code"] == "stale_generation"
            fresh = data.get("ok") and path in ("/api/session", "/api/session/reset")
            if fresh and path == "/api/session":
                self.tab = data["tab"]
            if fresh or stale:
                self.generation, self.revision = data["generation"], data["revision"]
            elif data["generation"] == self.generation and data["revision"] >= (self.revision or 0):
                self.revision = data["revision"]

def ws_of(app: Any, client: WebClient) -> Any:
    """The server-side session of ``client`` (does not count as a use for eviction)."""
    return app.settings["store"].peek(client.browser, client.tab)
import re
PAIR=re.compile(r"/phone/([A-Za-z0-9_-]{16,64})")
CODE=re.compile(r"(?<![0-9])[0-9]{6}(?![0-9])")

class Browser:
    """One browser tab on the demo: the page's own requests and the state it adopted."""

    def __init__(self, app, *, view="judge", locale=None):
        self.app = app
        self.client = WebClient(app)
        self.bodies = []
        opened = self.client.open(view=view, locale=locale)
        self.state = opened["state"]
        self._ids = 0

    @property
    def ws(self):
        return ws_of(self.app, self.client)

    def post(self, path, body=None, *, expect=200, headers=None, raw=None):
        response = self.client.post(path, body, headers=headers, raw=raw)
        data = self.client.json(response)
        self.bodies.append((path, response.body.decode("utf-8")))
        assert response.code == expect, (path, response.code, data)
        if "state" in data:
            assert data["state"]["generation"] == data["generation"]
            assert data["state"]["revision"] == data["revision"]
            self.state = data["state"]
        return data

    def get(self, path, *, expect=200):
        response = self.client.get(path)
        data = self.client.json(response)
        self.bodies.append((path, response.body.decode("utf-8")))
        assert response.code == expect, (path, response.code, data)
        return data

    def fail(self, path, body, code, status=409):
        data = self.post(path, body, expect=status)
        assert data["ok"] is False and data["error"]["code"] == code, data
        return data

    def login(self):
        return self.post("/api/session/login", {})

    def send(self, text, files=0):
        self._ids += 1
        body = {"text": text, "external_files": files, "client_msg_id": f"message-{self._ids:04d}"}
        return self.post("/api/chat/message", body)["rkey"]

    def shortcut(self, key, *, expect=200):
        self._ids += 1
        body = {"shortcut": key, "client_msg_id": f"message-{self._ids:04d}"}
        return self.post("/api/chat/shortcut", body, expect=expect)

    def response(self, rkey):
        return self.state["chat"]["responses"][rkey]

    def task(self, tkey):
        return next(task for task in self.state["device"]["tasks"] if task["key"] == tkey)

    def gesture(self, tkey):
        gesture = self.task(tkey)["gesture"]
        assert gesture is not None, self.task(tkey)["step"]
        return gesture

    def device(self, tkey, op, **extra):
        gesture = self.gesture(tkey)
        assert gesture["op"] == op
        path = {
            "confirm": "confirm",
            "mfa_request": "mfa/request",
            "mfa_verify": "mfa/verify",
        }[op]
        body = {"gesture": gesture["token"], "card_version": gesture["card_version"], **extra}
        return self.post(f"/api/device/tasks/{tkey}/{path}", body)

    def gate(self, rkey):
        return self.response(rkey)["gate"]

    def answer(self, rkey, answers, *, expect=200):
        body = {"gate_version": self.gate(rkey)["version"], "answers": answers}
        return self.post(f"/api/chat/responses/{rkey}/gate/answer", body, expect=expect)

    def pair(self):
        return PAIR.search(self.state["session"]["phone_url"]).group(1)

    def sms_code(self):
        """Read the code where a person would: the simulated phone page's own API."""
        response = self.client.get(f"/api/phone/{self.pair()}?lang=zh-CN&view=customer")
        data = self.client.json(response)
        for message in data["messages"]:
            if message["status"] == "active":
                return CODE.search(message["text"]).group(0)
        return None

    def red(self, tkey):
        self.device(tkey, "confirm")
        self.device(tkey, "mfa_request")
        self.device(tkey, "mfa_verify", code=self.sms_code())
        return self.device(tkey, "confirm")

    def tab(self, name):
        return self.get(f"/api/tabs/{name}")["tab"]["vm"]

    def listed(self, kind):
        """The version of the list a business choice indexes, as the page shows it."""
        if kind == "subscriptions":
            return (self.tab("accounts")["subscriptions"] or {}).get("cancel_version", "0" * 32)
        vm = self.tab("wealth")
        if kind == "wealth":
            return (vm["comparison"] or {}).get("candidates_version", "0" * 32)
        return vm["positions_version"]

    def pick(self, path, choice, *, expect=200):
        kind = {"/api/subscriptions/cancel": "subscriptions", "/api/wealth/purchase": "wealth"}
        body = {"choice": choice, "list_version": self.listed(kind.get(path, "positions"))}
        return self.post(path, body, expect=expect)

    def compensate(self, *, expect=200):
        version = self.tab("operator")["compensation"]["compensate_version"]
        return self.post("/api/chat/compensate", {"plan_version": version}, expect=expect)
