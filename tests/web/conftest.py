"""Shared fixtures of the web tests (contract §6.8).

``web_app`` serves ``make_app`` on a loopback port from a background thread; ``WebClient``
is one browser with its own cookie jar, server-issued tab and adopted generation. A
module overrides ``web_settings`` to pass settings or module doubles to ``make_app``.
Every test runs with the network closed except loopback, with no live-model or client
variables in the environment, and with ``corridor`` imported from this checkout.
"""

import asyncio
import contextlib
import http.client
import io
import ipaddress
import json
import os
import re
import socket
import sys
import threading
from collections.abc import Generator, Iterator
from dataclasses import dataclass, field
from http.cookies import SimpleCookie
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from tornado.httpclient import HTTPRequest, HTTPResponse
from tornado.httpserver import HTTPServer
from tornado.httputil import HTTPHeaders
from tornado.ioloop import IOLoop
from tornado.testing import bind_unused_port

ROOT = Path(__file__).resolve().parents[2]
COOKIE = "corridor_web"
# A catalog key where its text belongs (RW-9 item 3): the page and ``wtr`` both fall back to
# the key for a missing string, so a person would read it as it stands.
RAW_KEY = re.compile(r"(?<![A-Za-z0-9_.])(?:web\.(?:ui|vm)|polish)\.[a-z0-9_]+")


# --- the server -----------------------------------------------------------------------------


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


@pytest.fixture
def web_settings() -> dict[str, Any]:
    """Override in a test module to pass settings or module doubles to ``make_app``."""
    return {}


@pytest.fixture
def web_app(web_settings: dict[str, Any]) -> Iterator[Any]:
    with serve(web_settings) as app:
        yield app


# --- the browser ----------------------------------------------------------------------------


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


def raw_keys(path: str, response: HTTPResponse) -> list[str]:
    """The catalog keys one JSON answer shows. ``/api/i18n`` is the catalog itself, so only
    its values count there; pages and scripts name keys by design and are not scanned."""
    if "application/json" not in response.headers.get("Content-Type", ""):
        return []
    text = response.body.decode("utf-8", "replace")
    if path.partition("?")[0] == "/api/i18n":
        strings = json.loads(text).get("strings") or {}
        text = json.dumps(list(strings.values()), ensure_ascii=False)
    return RAW_KEY.findall(text)


class WebClient:
    """One browser. ``post`` and ``get`` carry what the page's own requests carry.

    POST: Host, Origin, Content-Type, the cookie, and ``X-Corridor-Web`` for
    ``/api/session`` or the tab and generation headers otherwise; GET under ``/api/``
    (except the phone) carries the tab and generation. The generation is adopted as the
    page does (contract §1.5): from ``/api/session`` and reset successes and from a
    ``stale_generation`` answer.
    """

    def __init__(
        self,
        app: Any,
        *,
        host: str | None = None,
        scheme: str = "http",
        cookie_name: str = COOKIE,
        forwarded_for: str | None = None,
    ) -> None:
        self.app = app
        self.port: int = app.settings["port"]
        self.host = host or f"127.0.0.1:{self.port}"
        # Public mode's browser: the public name as Host, ``https`` in the Origin, the
        # ``__Host-`` cookie and the address the proxy would forward.
        self.scheme = scheme
        self.cookie_name = cookie_name
        self.forwarded_for = forwarded_for
        self.browser: str | None = None
        self.tab: str | None = None
        self.generation: str | None = None
        self.revision: int | None = None
        self.timeout = 60
        self._lock = threading.Lock()

    @property
    def origin(self) -> str:
        return f"{self.scheme}://{self.host}"

    def forwarded(self) -> dict[str, Any]:
        return {} if self.forwarded_for is None else {"X-Forwarded-For": self.forwarded_for}

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
        value = None if self.browser is None else f"{self.cookie_name}={self.browser}"
        return {"Cookie": value}

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
            **self.forwarded(),
            **self.cookie(),
        }
        if path == "/api/session":
            base["X-Corridor-Web"] = "1"
        else:
            base.update(self.session_headers())
        return self.request("POST", path, payload, _merge(base, headers))

    def get(self, path: str, *, headers: dict[str, Any] | None = None) -> HTTPResponse:
        base: dict[str, Any] = {"Host": self.host, **self.forwarded(), **self.cookie()}
        if path.startswith("/api/") and not path.startswith("/api/phone/"):
            base.update(self.session_headers())
        return self.request("GET", path, None, _merge(base, headers))

    def json(self, response: HTTPResponse) -> dict[str, Any]:
        return json.loads(response.body.decode("utf-8"))

    def abandon(self, path: str, body: dict[str, Any] | None = None) -> None:
        """Send a POST and hang up before the answer (a closed tab, a lost connection)."""
        payload = json.dumps({} if body is None else body).encode("utf-8")
        base = {"Host": self.host, "Origin": self.origin, "Content-Type": "application/json"}
        base.update(self.forwarded())
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
        url = f"{self.scheme}://{self.host}{path}"
        response = HTTPResponse(
            HTTPRequest(url, method=method),
            answer.status,
            headers=headers,
            buffer=io.BytesIO(data),
            effective_url=url,
            reason=answer.reason,
        )
        self._adopt(path, response)
        assert not raw_keys(path, response), (path, raw_keys(path, response))
        return response

    def _adopt(self, path: str, response: HTTPResponse) -> None:
        with self._lock:
            for line in response.headers.get_list("Set-Cookie"):
                jar: SimpleCookie = SimpleCookie()
                jar.load(line)
                if self.cookie_name in jar:
                    self.browser = jar[self.cookie_name].value
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


@pytest.fixture
def client(web_app: Any) -> WebClient:
    return WebClient(web_app)


@pytest.fixture
def other_browser(web_app: Any) -> WebClient:
    return WebClient(web_app)


# --- guards ---------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def tree_guard() -> None:
    import corridor

    location = Path(corridor.__file__).resolve()
    assert location.is_relative_to(ROOT / "src"), f"corridor imported from {location}"


def _loopback(host: Any) -> bool:
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    if not isinstance(host, str):
        return False
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.split("%")[0]).is_loopback
    except ValueError:
        return False


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith(("LLM_", "CORRIDOR_LIVE", "LIVE_")):
            monkeypatch.delenv(name)
    monkeypatch.setenv("CORRIDOR_LOCALE", "zh-CN")

    def refused(target: Any) -> RuntimeError:
        return RuntimeError(f"test: network disabled (non-loopback {target!r})")

    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_create = socket.create_connection
    real_getaddrinfo = socket.getaddrinfo

    def connect(self: socket.socket, address: Any) -> Any:
        if isinstance(address, tuple) and not _loopback(address[0]):
            raise refused(address[0])
        return real_connect(self, address)

    def connect_ex(self: socket.socket, address: Any) -> Any:
        if isinstance(address, tuple) and not _loopback(address[0]):
            raise refused(address[0])
        return real_connect_ex(self, address)

    def create_connection(address: Any, *args: Any, **kwargs: Any) -> Any:
        if not _loopback(address[0]):
            raise refused(address[0])
        return real_create(address, *args, **kwargs)

    def getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        if not _loopback(host):
            raise refused(host)
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "create_connection", create_connection)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)


class StreamlitSentinel:
    """Stands in for ``st`` in every ``corridor.ui`` module: any use fails the test."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"web code touched st.{name}")

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("web code called st")


@pytest.fixture
def st_sentinel(monkeypatch: pytest.MonkeyPatch) -> StreamlitSentinel:
    """Replace ``st`` in loaded ``corridor.ui`` modules (design §6.7 item 3).

    The web package's own imports are loaded first, so every module it reaches is covered.
    """
    with contextlib.suppress(ImportError):
        import corridor.web.compat  # noqa: F401
    sentinel = StreamlitSentinel()
    for name, module in list(sys.modules.items()):
        if name.startswith("corridor.ui.") and module is not None and "st" in vars(module):
            monkeypatch.setattr(module, "st", sentinel)
    return sentinel


# --- doubles for the shell's own tests (line A) --------------------------------------------

SERVICE = (
    "login",
    "set_view",
    "set_locale",
    "send_message",
    "send_shortcut",
    "make_current",
    "select_recipient",
    "gate_answer",
    "gate_retry",
    "choose_birthday",
    "complete_aa",
    "chat_stop",
    "handoff",
    "compensate_latest",
    "select_nodes",
    "device_confirm",
    "device_mfa_request",
    "device_mfa_verify",
    "device_cancel",
    "stop_all",
    "subscriptions_query",
    "subscriptions_cancel",
    "wealth_compare",
    "wealth_purchase",
    "wealth_redeem",
    "preset_r4",
    "billing_analyze",
)
REPLIES = ("send_message", "send_shortcut", "complete_aa", "chat_stop", "handoff")
RESULTS = ("login", "device_confirm", "device_mfa_request", "device_mfa_verify")
SMS_CODE = "246810"


def _effects() -> Any:
    from corridor.web.state import Effects

    return Effects()


@dataclass(eq=False)
class FakeSession:
    """The fields of ``WebSession`` that the shell reads or writes (contract §6.2).

    The count is the real ``state.Effects``, and the three product fields are the real
    proxies over stand-in objects, bound to that count as ``WebSession`` binds them: the
    handler trusts the count only while that binding holds (RW-6 item 2)."""

    browser_id: str
    tab_id: str
    view: str
    locale: str
    generation: str = "unset-by-A"
    revision: int = 0
    busy: int = 0
    pair: str = field(default_factory=lambda: os.urandom(12).hex())
    controller: object = field(default_factory=object)
    session: object = field(default_factory=object)
    wealth: object = field(default_factory=object)
    rkeys: dict[str, str] = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    effects: Any = field(default_factory=_effects)
    public: bool = False
    public_deployments: Any = None

    def __post_init__(self) -> None:
        from corridor.web.state import Product, unwrap

        for name in ("controller", "session", "wealth"):
            setattr(self, name, Product(unwrap(getattr(self, name)), self.effects, frozenset()))


@dataclass
class Record:
    name: str
    ws: Any
    args: tuple
    locale: str
    audience: str
    thread: str


class Doubles:
    """Module doubles for ``make_app`` (contract §6.1): each call is recorded with the
    locale and audience it ran under; any service function can be made to block or fail.
    """

    def __init__(self) -> None:
        self.records: list[Record] = []
        self.sessions: list[FakeSession] = []
        self.live_sessions: set[int] = set()
        self.gates: dict[str, threading.Event] = {}
        self.entered: dict[str, threading.Semaphore] = {}
        self.raises: dict[str, BaseException] = {}
        self._lock = threading.Lock()
        self.service = SimpleNamespace(**{name: self._service(name) for name in SERVICE})
        self.refresh = SimpleNamespace(refresh=lambda ws: self.record("refresh", ws))
        self.vm = SimpleNamespace(
            app=SimpleNamespace(app_state=self.app_state, i18n_strings=self.i18n_strings),
            tabs=SimpleNamespace(tab=self.tab),
            evidence=SimpleNamespace(technical=self.technical),
            phone=SimpleNamespace(payload=self.phone),
        )
        self.live = SimpleNamespace(
            on_new=lambda ws: self.record("live.on_new", ws),
            on_reset=lambda old, new: self.record("live.on_reset", old, new),
            close=lambda ws: self.record("live.close", ws),
            sync=self.sync,
            may_call=lambda ws: id(ws) in self.live_sessions,
            view=lambda ws: {"enabled": False, "rendered": self._context()},
            challenge=lambda ws: self.record("live.challenge", ws) or "0" * 32,
            unlock=lambda ws, nonce, proof: self.record("live.unlock", ws) or "invalid",
            select=lambda ws, deployment: self.record("live.select", ws, deployment),
            toggle=lambda ws, on: self.record("live.toggle", ws, on),
        )
        self.state = SimpleNamespace(
            new_web_session=self.new_web_session,
            reserve_pairs=lambda count: self.record("reserve_pairs", None, count),
            retire_pair=lambda ws: self.record("retire_pair", ws),
            forget_pair=lambda ws: self.record("forget_pair", ws),
        )

    def settings(self, **extra: Any) -> dict[str, Any]:
        modules = {
            "service": self.service,
            "refresh": self.refresh,
            "vm": self.vm,
            "live": self.live,
            "state": self.state,
        }
        return {**modules, **extra}

    @staticmethod
    def _context() -> dict[str, str]:
        from corridor.i18n import get_locale
        from corridor.ui.audience import get_audience

        return {"locale": get_locale(), "audience": get_audience()}

    def record(self, name: str, ws: Any = None, *args: Any) -> None:
        context = self._context()
        with self._lock:
            self.records.append(
                Record(
                    name,
                    ws,
                    args,
                    context["locale"],
                    context["audience"],
                    threading.current_thread().name,
                )
            )

    def names(self, ws: Any = None) -> list[str]:
        with self._lock:
            return [item.name for item in self.records if ws is None or item.ws is ws]

    def block(self, name: str) -> threading.Event:
        """Calls of ``name`` wait until the returned event is set (at most 30 s)."""
        self.gates[name] = threading.Event()
        self.entered[name] = threading.Semaphore(0)
        return self.gates[name]

    def wait_entered(self, name: str, count: int = 1, timeout: float = 10) -> bool:
        return all(self.entered[name].acquire(timeout=timeout) for _ in range(count))

    def _service(self, name: str) -> Any:
        def call(ws: Any, *args: Any) -> Any:
            self.record(name, ws, *args)
            if name in self.entered:
                self.entered[name].release()
                self.gates[name].wait(30)
            if name in self.raises:
                raise self.raises[name]
            if name == "set_view":
                ws.view = args[0]
            if name == "set_locale":
                ws.locale = args[0]
            if name in REPLIES:
                rkey = f"R{len(ws.rkeys) + 1}"
                ws.rkeys[rkey] = f"rid-{rkey}"
                return rkey
            if name in RESULTS:
                return {"status": "settled", "pending_id": "internal-id-never-shown"}
            return None

        return call

    def sync(self, ws: Any, *, apply: bool) -> dict[str, Any]:
        self.record("live.sync", ws, apply)
        return {"state": "OFF", "enabled": False, "invalid": False, "boundary": True}

    def new_web_session(
        self, view: str, locale: str, *, browser_id: str, tab_id: str
    ) -> FakeSession:
        ws = FakeSession(browser_id=browser_id, tab_id=tab_id, view=view, locale=locale)
        with self._lock:
            self.sessions.append(ws)
        return ws

    def app_state(self, ws: Any) -> dict[str, Any]:
        self.record("app_state", ws)
        return {
            "schema": 1,
            "contract": "web-contract/1.1",
            "generation": ws.generation,
            "revision": ws.revision,
            "view": ws.view,
            "locale": ws.locale,
            "rendered": self._context(),
        }

    def i18n_strings(self, ws: Any) -> dict[str, str]:
        context = self._context()
        return {"web.ui.probe": f"{context['locale']}/{context['audience']}"}

    def tab(self, ws: Any, name: str) -> dict[str, Any]:
        self.record("tab", ws, name)
        return {"name": name, "rendered": self._context()}

    def technical(self, ws: Any, rkey: str) -> dict[str, Any]:
        return {"envelope": {"rkey": rkey}, "cards": [], "kernel": None, "gate_record": None}

    def phone(self, pair: str, *, allowed: bool, public: bool = False) -> dict[str, Any]:
        self.record("phone", None, pair, allowed)
        messages = [{"text": f"code {SMS_CODE}", "status": "active", "latest": True}]
        payload = {
            "ok": True,
            "version": "v1",
            "messages": messages if allowed else [],
            "unpaired": None if allowed else "unpaired",
            "rendered": self._context(),
        }
        if public:
            payload["banner"] = "public-banner"
        return payload


@pytest.fixture
def doubles() -> Doubles:
    return Doubles()
