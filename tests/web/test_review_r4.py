"""Regression tests for the fourth review round (R4) of the web front end.

Each test names the item it closes: item 1 (E-15: one
session's jobs in the live and the main pool never overlap) and item 2 (E-3: any
exception from the birthday or the compensation preparer, a ``WebError`` of any status
included, is an unknown outcome; the closed authorization lab answers 404 before
anything else; E-14: every open read route, a reachable success and a reachable refusal,
counted apart). The real handlers, service functions, controllers, gateway and kernel run
throughout, except where a test injects the fault it names or uses the
shell's module doubles. This module runs the submission build: ``FEATURES`` as shipped.
"""

import asyncio
import contextvars
import dataclasses
import http.client
import io
import threading
from collections import Counter
from dataclasses import dataclass
from typing import Any

import pytest
from tornado.httpclient import HTTPRequest, HTTPResponse
from tornado.httputil import HTTPHeaders

import corridor.web as web_package
from corridor.ui.controller import DemoController
from corridor.web import app as web_app_module
from corridor.web import errors, handlers, lab, live, security, tasks
from corridor.web import refresh as refresh_module
from corridor.web import state as web_state
from corridor.web.executor import Executor
from tests.web.conftest import WebClient, serve, ws_of
from tests.web.test_api_chat import Browser
from tests.web.test_executor import MESSAGE, eventually, in_thread
from tests.web.test_review_r1 import spied
from tests.web.test_review_r3 import (
    COUNTED,
    LAB_POSTS,
    STALE,
    Walk,
    dispatched_plan,
    open_query,
)
from tests.web.test_tasks import RED, YELLOW

FOREIGN_ORIGIN = "http://evil.example"
FOREIGN_HOST = "evil.example"
LAB_GET = "/api/lab"
# Paths below the lab that are no route of it: in an open build they are plain 404s.
LAB_OTHER_PATHS = ("/api/lab/", "/api/lab/phone", "/api/lab/start/again")


@pytest.fixture
def browser(web_app):
    page = Browser(web_app)
    page.login()
    return page


# --- RW-4''' item 1 (E-15): the live and the main pool share one session lock ----------------
# RW-4'''' item 3: the order is read from the lock's own record (``RecordedLock``), never
# from a timeout; a variant with a lock that does not exclude proves the check would fail
# (``test_review_r5.py::test_e15_the_two_pool_check_fails_when_the_lock_does_not_exclude``).

# The job a lock call belongs to: tasks copy the context, so ``Executor.run``'s own task
# carries the name its caller set.
JOB: contextvars.ContextVar[str | None] = contextvars.ContextVar("job", default=None)


class RecordedLock:
    """The session's own ``asyncio.Lock`` behind a wrapper that records each call made to
    it: ``("acquire", job, held)`` when a job asks for it, with whether it was already held
    at that moment; ``("acquired", job, lock)`` when it is granted; ``("released", job,
    lock)``. Every ``async with ws.lock`` of ``Executor.run`` goes through it, so the order
    of the record is the lock's own order of events."""

    def __init__(self, lock: asyncio.Lock, log: list) -> None:
        self.lock = lock
        self.log = log

    def locked(self) -> bool:
        return self.lock.locked()

    async def __aenter__(self) -> None:
        self.log.append(("acquire", JOB.get(), self.lock.locked()))
        await self.lock.acquire()
        self.log.append(("acquired", JOB.get(), id(self.lock)))

    async def __aexit__(self, *exc: Any) -> bool:
        self.lock.release()
        self.log.append(("released", JOB.get(), id(self.lock)))
        return False


def two_pool_record(ws, first, lock=None):
    """Two jobs of ``ws``: job "one" in pool ``first``, held inside its pool thread until
    the record is taken; then job "two" in the other pool. The loop runs until job two has
    asked the lock for itself (its ``acquire`` call is in the record); only then is job one
    let go. What either side saw is returned: the lock's record when job two asked and at
    the end, what the jobs did and in which threads, the pools' admission counts."""
    other = "main" if first == "live" else "live"
    original = ws.lock
    log: list = []
    ws.lock = (lock or RecordedLock)(original, log)
    executor = Executor(pool_main=2, pool_live=2, queue_main=2, queue_live=2)
    jobs: list = []
    guard = threading.Lock()
    entered, release = threading.Event(), threading.Event()

    def note(*entry):
        with guard:
            jobs.append(entry)

    def first_job():
        note("start", "one", threading.current_thread().name)
        entered.set()
        # Held until the record is taken: no timeout decides anything below.
        assert release.wait(30)
        note("return", "one", threading.current_thread().name)
        return "one"

    def second_job():
        note("start", "two", threading.current_thread().name)
        return "two"

    async def named(name, job, pool):
        JOB.set(name)
        return await executor.run(ws, job, pool=pool)

    async def scenario():
        loop = asyncio.get_running_loop()
        one = asyncio.ensure_future(named("one", first_job, first))
        assert await loop.run_in_executor(None, entered.wait, 30)
        two = asyncio.ensure_future(named("two", second_job, other))
        for _ in range(100_000):
            if any(entry[:2] == ("acquire", "two") for entry in log):
                break
            await asyncio.sleep(0)
        with guard:
            seen = {
                "log": list(log),
                "jobs": list(jobs),
                "busy": ws.busy,
                "admitted": {name: lane.admitted for name, lane in executor.lanes.items()},
            }
        release.set()
        seen["results"] = await asyncio.gather(one, two)
        return seen

    try:
        seen = asyncio.run(scenario())
    finally:
        release.set()
        executor.shutdown()
        ws.lock = original
    seen.update(final=list(log), jobs_final=list(jobs), lock=id(original), first=first)
    seen["other"] = other
    return seen


def assert_one_lock_serializes(seen):
    """Job two asked for the very lock job one held, was not granted it while job one ran,
    and started only after job one had returned and released it."""
    first, other, lock = seen["first"], seen["other"], seen["lock"]
    # When job two asked: job one held the lock, job two was admitted to its own pool, had
    # asked for the same lock while it was held, and had not been granted it.
    assert seen["log"] == [("acquire", "one", False), ("acquired", "one", lock)] + [
        ("acquire", "two", True)
    ], seen["log"]
    assert [entry[:2] for entry in seen["jobs"]] == [("start", "one")], seen["jobs"]
    assert seen["busy"] == 2 and seen["admitted"] == {first: 1, other: 1}
    # At the end: one grant at a time, job two's only after job one's release.
    assert seen["final"] == [
        ("acquire", "one", False),
        ("acquired", "one", lock),
        ("acquire", "two", True),
        ("released", "one", lock),
        ("acquired", "two", lock),
        ("released", "two", lock),
    ], seen["final"]
    assert [entry[:2] for entry in seen["jobs_final"]] == [
        ("start", "one"),
        ("return", "one"),
        ("start", "two"),
    ], seen["jobs_final"]
    threads = {name: thread for _, name, thread in seen["jobs_final"]}
    assert threads["one"].startswith(f"web-{first}") and threads["two"].startswith(f"web-{other}")
    assert seen["results"] == ["one", "two"]


@pytest.mark.parametrize("first", ["live", "main"])
def test_e15_one_sessions_jobs_in_the_two_pools_never_overlap(first):
    """A session holds one ``asyncio.Lock`` (``WebSession.lock``), and ``Executor.run``
    takes it before it hands the whole job to either pool. A job of the same session sent
    to the other pool while the first runs is admitted to its own pool and asks for that
    same lock while it is held; it is granted the lock, and starts, only after the first
    job returned and released it. The lock's own record decides this, not a timeout."""
    ws = web_state.new_web_session("customer", "zh-CN", browser_id="b" * 43, tab_id="t" * 32)
    held = [
        item.name
        for item in dataclasses.fields(ws)
        if isinstance(getattr(ws, item.name), asyncio.Lock)
    ]
    assert held == ["lock"]
    assert_one_lock_serializes(two_pool_record(ws, first))
    assert ws.busy == 0 and not ws.lock.locked() and isinstance(ws.lock, asyncio.Lock)


def test_e15_a_live_pool_call_holds_the_same_sessions_main_pool_requests(doubles):
    """The same through the handlers, where every session request reaches ``Executor.run``
    by ``Api.locked``: while one session's business POST runs in the live pool, a shell
    POST and a GET of that session (main pool) are admitted and ask for its lock while it
    is held (the lock's own record); they are granted it, and reach the service and the
    view model, only after the live call returned and released it."""
    with serve(doubles.settings()) as app:
        client = WebClient(app)
        client.open()
        ws = ws_of(app, client)
        log: list = []
        original = ws.lock
        ws.lock = RecordedLock(original, log)
        doubles.live_sessions.add(id(ws))
        gate = doubles.block("send_message")
        message = in_thread(client.post, "/api/chat/message", MESSAGE)
        assert doubles.wait_entered("send_message")
        before = len(doubles.names(ws))
        view = in_thread(client.post, "/api/session/view", {"view": "judge"})
        read = in_thread(client.get, "/api/state")
        lanes = app.settings["executor"].lanes
        assert eventually(lambda: [entry[0] for entry in log].count("acquire") == 3)
        lock = id(original)
        # The live call holds the lock; both main-pool requests asked for it while it was
        # held and neither was granted it.
        assert log == [
            ("acquire", None, False),
            ("acquired", None, lock),
            ("acquire", None, True),
            ("acquire", None, True),
        ], log
        assert ws.busy == 3 and lanes["main"].admitted == 2 and lanes["live"].admitted == 1
        assert doubles.names(ws)[before:] == []
        gate.set()
        for thread, answers in (message, view, read):
            thread.join(10)
            assert answers["value"].code == 200
        ws.lock = original
    # One grant at a time: each later grant follows the release before it.
    assert [entry[0] for entry in log[4:]] == ["released", "acquired"] * 2 + ["released"]
    records = [item for item in doubles.records if item.ws is ws][before - 1 :]
    names = [item.name for item in records]
    # The live call finished, recorded and refreshed before either waiting request began.
    assert names[:4] == ["send_message", "live.sync", "refresh", "app_state"]
    assert set(names[4:]) == {"set_view", "refresh", "app_state"}
    assert names[4:].count("app_state") == 2
    assert all(item.thread.startswith("web-live") for item in records[:4])
    assert all(item.thread.startswith("web-main") for item in records[4:])


# --- RW-4''' item 2, E-3: a WebError of any status after the preparer changed state ---------

# A WebError of each status class the preparers could raise (a 4xx refusal, with or
# without state; the expected failure; the lab's contained unknown result).
WEB_ERRORS = {
    "bad_json-400": lambda: errors.error("bad_json"),
    "no_session-401": lambda: errors.error("no_session"),
    "not_found-404": lambda: errors.error("not_found"),
    "not_applicable-409": lambda: errors.error("not_applicable"),
    "invalid_input-422": lambda: errors.error("invalid_input"),
    "operation_failed-422": errors.WebOperationFailed,
    "contained-500": errors.WebChildOutcomeUnknown,
}


def distrusted(page, data, tkey, saved, monkeypatch):
    assert data["error"]["code"] == "outcome_unknown" and page.ws.untrusted
    assert page.ws.gestures == {}
    assert all(task["gesture"] is None for task in data["state"]["device"]["tasks"])
    counts = spied(monkeypatch, "approve", "confirm_input", "request_mfa", "submit_mfa", "dispatch")
    page.fail(f"/api/device/tasks/{tkey}/confirm", saved, "not_applicable")
    assert counts == Counter()


@pytest.mark.parametrize("raised", list(WEB_ERRORS.values()), ids=list(WEB_ERRORS))
def test_e3_a_web_error_after_the_birthday_preparer_changed_state_distrusts_the_session(
    browser, monkeypatch, raised
):
    """The preparer records the request and registers the plan, then raises a WebError:
    whatever its status, the answer is ``outcome_unknown`` and the session is distrusted;
    no refusal, no page error and no draft is shown."""
    tkey, saved = open_query(browser)
    rkey = browser.send("帮我准备生日")
    choice = next(
        item["key"] for item in browser.response(rkey)["birthday"]["choices"] if item["enabled"]
    )
    c = browser.ws.controller
    plans, claimed = len(c._plans), len(c._request_facts)
    original = DemoController.preview_birthday

    def prepared_then_raised(self, *args, **kwargs):
        original(self, *args, **kwargs)
        raise raised()

    monkeypatch.setattr(DemoController, "preview_birthday", prepared_then_raised)
    data = browser.post(f"/api/chat/responses/{rkey}/birthday", {"choice": choice}, expect=500)
    # The state did change before the error: a claimed request and a registered plan.
    assert len(c._plans) == plans + 1 and len(c._request_facts) == claimed + 1
    assert "birthday_choice_error" not in browser.ws.business
    assert "birthday_draft" not in browser.ws.business
    distrusted(browser, data, tkey, saved, monkeypatch)


@pytest.mark.parametrize("raised", list(WEB_ERRORS.values()), ids=list(WEB_ERRORS))
def test_e3_a_web_error_after_compensation_changed_state_distrusts_the_session(
    browser, monkeypatch, raised
):
    """The gateway audits the compensation request and the controller prepares the unlock
    card, then a WebError is raised: an unknown outcome, the session distrusted, and the
    prepared card gets no task and no token."""
    dispatched_plan(browser)
    tkey, saved = open_query(browser)
    c = browser.ws.controller
    pending = set(c._pending)
    original = DemoController.compensate

    def prepared_then_raised(self, plan_id):
        original(self, plan_id)
        raise raised()

    monkeypatch.setattr(DemoController, "compensate", prepared_then_raised)
    version = browser.tab("operator")["compensation"]["compensate_version"]
    data = browser.post("/api/chat/compensate", {"plan_version": version}, expect=500)
    # The state did change before the error: a new unlock card is pending.
    (prepared,) = set(c._pending) - pending
    assert c._pending[prepared]["compensation_plan_id"]
    assert "compensation" not in browser.ws.business
    assert not any(task.source == "compensation" for task in browser.ws.tasks.values())
    distrusted(browser, data, tkey, saved, monkeypatch)


# --- RW-4''' item 2: the closed lab answers 404 before anything else ------------------------


def traced(monkeypatch):
    """Records each step a request passes on its way to a route's work: the transport
    check, the cookie, the body parser and field check, the session lookup, the lock and
    the executor, the generation check, the lab's capability sync, the call context and
    the action. A request that never gets past routing records nothing."""
    seen: list[str] = []

    def plain(owner, name):
        original = getattr(owner, name)

        def wrapped(*args, **kwargs):
            seen.append(name)
            return original(*args, **kwargs)

        monkeypatch.setattr(owner, name, wrapped)

    def awaited(owner, name):
        original = getattr(owner, name)

        async def wrapped(*args, **kwargs):
            seen.append(name)
            return await original(*args, **kwargs)

        monkeypatch.setattr(owner, name, wrapped)

    for name in ("check", "browser_id", "parse", "fields"):
        plain(security, name)
    for name in ("session", "guarded", "context"):
        plain(handlers.Api, name)
    plain(handlers.Write, "perform")
    plain(lab, "sync_capability")
    awaited(handlers.Api, "locked")
    awaited(Executor, "run")
    return seen


def lab_requests(client):
    """Every request variant to the lab's paths, as ``(label, response)``: well formed; a
    malformed, empty, mistyped, unknown-field or oversized body; a stale or malformed
    generation; no tab, no cookie, no or a foreign origin, a foreign host, a cross-site
    fetch, the wrong content type; and every other method."""
    gets = {
        "as sent": {},
        "stale generation": STALE,
        "malformed generation": {"X-Corridor-Generation": "!" * 12},
        "no tab": {"X-Corridor-Tab": None},
        "no cookie": {"Cookie": None},
        "foreign origin": {"Origin": FOREIGN_ORIGIN},
        "foreign host": {"Host": FOREIGN_HOST},
        "cross-site": {"Sec-Fetch-Site": "cross-site"},
    }
    for path in (LAB_GET, *LAB_OTHER_PATHS):
        for label, headers in gets.items():
            yield f"GET {path} {label}", client.get(path, headers=headers)
    for path, body in {**LAB_POSTS, **dict.fromkeys(LAB_OTHER_PATHS, {})}.items():
        posts = {
            "as sent": (body, None, {}),
            "malformed json": (None, b"{", {}),
            "empty body": (None, b"", {}),
            "not an object": (None, b"[]", {}),
            "unknown field": ({**body, "extra": 1}, None, {}),
            "wrong types": ({key: 7 for key in body} or {"x": 7}, None, {}),
            "above the parse limit": ({**body, "pad": "x" * (64 * 1024)}, None, {}),
            "stale generation": (body, None, STALE),
            "malformed generation": (body, None, {"X-Corridor-Generation": "!" * 12}),
            "no tab": (body, None, {"X-Corridor-Tab": None}),
            "no cookie": (body, None, {"Cookie": None}),
            "no origin": (body, None, {"Origin": None}),
            "foreign origin": (body, None, {"Origin": FOREIGN_ORIGIN}),
            "foreign host": (body, None, {"Host": FOREIGN_HOST}),
            "text content type": (body, None, {"Content-Type": "text/plain"}),
        }
        for label, (value, raw, headers) in posts.items():
            yield f"POST {path} {label}", client.post(path, value, headers=headers, raw=raw)
    lines = [("Host", client.host), ("Origin", client.origin), *client.cookie().items()]
    lines = [(name, value) for name, value in lines if value is not None]
    for path in (LAB_GET, *LAB_POSTS, *LAB_OTHER_PATHS):
        for method in ("PUT", "DELETE", "PATCH", "OPTIONS"):
            yield f"{method} {path}", client.request(method, path, b"{}", lines)
        yield f"HEAD {path}", head(client, path, lines)
        if path == LAB_GET:
            yield f"POST {path}", client.post(path, {})
        elif path in LAB_POSTS:
            yield f"GET {path}", client.get(path)


def head(client, path, lines):
    """A HEAD request: its answer has no body, so it skips the client's JSON adoption."""
    connection = http.client.HTTPConnection("127.0.0.1", client.port, timeout=client.timeout)
    try:
        connection.putrequest("HEAD", path, skip_host=True, skip_accept_encoding=True)
        for name, value in lines:
            connection.putheader(name, value)
        connection.endheaders()
        answer = connection.getresponse()
        answer.read()
    finally:
        connection.close()
    headers = HTTPHeaders()
    for name, value in answer.getheaders():
        headers.add(name, value)
    request = HTTPRequest(f"http://{client.host}{path}", method="HEAD")
    return HTTPResponse(request, answer.status, headers=headers, buffer=io.BytesIO(b""))


def assert_plain_404(label, response, client):
    assert response.code == 404, (label, response.code)
    assert not response.headers.get_list("Set-Cookie"), label
    assert response.headers["Cache-Control"] == "no-store", label
    assert response.headers["X-Content-Type-Options"] == "nosniff", label
    assert "Content-Security-Policy" in response.headers, label
    if response.request.method == "HEAD":
        return
    data = client.json(response)
    assert set(data) == {"ok", "error"} and data["ok"] is False, (label, data)
    assert set(data["error"]) == {"code", "message", "retry"}, (label, data)
    assert data["error"]["code"] == "not_found" and data["error"]["retry"] is False, label


def test_rw4_every_lab_path_answers_404_before_anything_else_in_the_submission_build(
    web_app, monkeypatch
):
    """RW-4''' item 2: with ``lab`` closed (the shipped ``FEATURES``), every request to a
    lab path answers a plain 404 at routing, in both views, before and after login and
    without a session at all: before its method, transport headers, body, session or
    generation is looked at. Nothing past routing runs (no step is recorded), no session
    changes revision, no lock or pool slot is taken, no session is built and no lab code
    runs. The same tracer records every step of an ordinary request afterwards."""
    assert lab.FEATURE not in web_package.FEATURES
    assert web_app.settings["features"] is None
    reached = []
    for name in ("open_to", "view", "start", "change", "try_previous", "gesture"):
        monkeypatch.setattr(lab, name, lambda *a, _name=name, **k: reached.append(_name))
    pages = {view: Browser(web_app, view=view) for view in ("customer", "judge")}
    built = []
    original = web_state.new_web_session

    def counted(*args, **kwargs):
        built.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(web_state, "new_web_session", counted)
    seen = traced(monkeypatch)
    lanes = web_app.settings["executor"].lanes
    tried = 0
    for page in pages.values():
        for logged_in_now in (False, True):
            if logged_in_now:
                page.login()
            revision = page.ws.revision
            seen.clear()
            for label, response in lab_requests(page.client):
                assert_plain_404(label, response, page.client)
                assert seen == [], (label, seen)
                tried += 1
            assert page.ws.revision == revision and page.ws.busy == 0
            assert all(lane.admitted == 0 for lane in lanes.values())
    anonymous = WebClient(web_app)
    for label, response in lab_requests(anonymous):
        assert_plain_404(label, response, anonymous)
        assert seen == [], (label, seen)
        tried += 1
    # Per caller: 4 paths x 8 GETs, 7 paths x 15 POSTs, 8 paths x 5 other methods, and the
    # other one of GET and POST on the 5 lab routes.
    assert tried == 5 * (4 * 8 + 7 * 15 + 8 * 5 + 5)
    assert reached == [] and built == []
    for page in pages.values():
        assert page.ws.demos == {} and not page.ws.untrusted
    allowed = web_app.settings["store"].pairs._allowed
    assert allowed == {page.pair() for page in pages.values()}
    # The tracer is live: an ordinary read and an ordinary write record every step.
    page = pages["judge"]
    page.get("/api/state")
    assert seen == [
        "check",
        "session",
        "browser_id",
        "locked",
        "run",
        "guarded",
        "sync_capability",
        "context",
    ]
    seen.clear()
    page.post("/api/session/view", {"view": "judge"})
    for step in ("check", "parse", "fields", "session", "guarded", "perform", "context"):
        assert step in seen, (step, seen)


def test_rw4_only_the_closed_lab_capability_answers_404_first():
    """With ``lab`` opened explicitly (the lab's own tests), the same requests meet the
    usual pipeline again: transport refusals, body refusals and the generation check
    answer first, and a path below the lab that is no route is a plain 404 only after the
    transport check."""
    with serve({"features": web_package.FEATURES | {lab.FEATURE}}) as app:
        page = Browser(app)
        page.login()
        body = LAB_POSTS["/api/lab/change"]
        assert page.client.post("/api/lab/change", body, headers=STALE).code == 409
        assert page.client.post("/api/lab/change", raw=b"{").code == 400
        assert page.client.post("/api/lab/change", {**body, "extra": 1}).code == 400
        assert page.client.post("/api/lab/change", body, headers={"Origin": None}).code == 403
        assert page.client.get(LAB_GET, headers={"Origin": FOREIGN_ORIGIN}).code == 403
        assert page.client.get(LAB_GET, headers={"X-Corridor-Tab": None}).code == 401
        assert page.client.get(LAB_GET).code == 200
        assert page.client.get("/api/lab/phone").code == 404
        assert page.client.get("/api/lab/phone", headers={"Host": FOREIGN_HOST}).code == 403
        lines = [("Host", page.client.host)]
        assert page.client.request("PUT", "/api/lab/change", b"{}", lines).code == 405


# --- RW-4''' item 2, E-14: every open read route, a success and a refusal, counted apart ----

# Besides the device's and the gateway's entry points (``COUNTED``): refreshes, token
# issues and live syncs, and the session's revision and held tokens before and after.
READ_COUNTED = (*COUNTED, "refresh", "issue", "live_sync", "revision", "tokens")
# The read routes that check a capability inside their handler rather than by ``needs``.
READ_NEEDS = {"/vendor/corridor-hmac.js": ("live",)}


@dataclass
class ReadWalk(Walk):
    """One read route's walk: the setup requests are not counted, the measured reads are,
    each state ("positive", "negative") apart."""

    def __post_init__(self):
        super().__post_init__()
        for owner, name, key in (
            (refresh_module, "refresh", "refresh"),
            (tasks, "issue", "issue"),
            (live, "sync", "live_sync"),
        ):
            original = getattr(owner, name)

            def wrapped(*args, _key=key, _original=original, **kwargs):
                if self.now is not None:
                    self.counts[self.now][_key] += 1
                return _original(*args, **kwargs)

            self.monkeypatch.setattr(owner, name, wrapped)

    def read(self, state, page, path, *, headers=None, body=None):
        """One measured GET; with ``body``, the same bookkeeping around a POST instead (the
        counters' positive calibration, ``test_review_r5.py``)."""
        ws = page.ws
        tokens = {key: held.token for key, held in ws.gestures.items()}
        revision = ws.revision
        counts = self.counts.setdefault(state, Counter())
        self.now = state
        try:
            if body is None:
                response = page.client.get(path, headers=headers)
            else:
                response = page.client.post(path, body, headers=headers)
        finally:
            self.now = None
        counts["revision"] += ws.revision - revision
        after = {key: held.token for key, held in ws.gestures.items()}
        counts["tokens"] += len(set(tokens.items()) ^ set(after.items()))
        self.results.setdefault(state, []).append(response)
        return response


def read_state(w):
    page = w.page()
    w.read("positive", page, "/api/state")
    w.read("negative", page, "/api/state", headers=STALE)


def read_i18n(w):
    page = w.page()
    w.read("positive", page, "/api/i18n")
    w.read("negative", page, "/api/i18n", headers={"X-Corridor-Tab": None})


def read_tab(w):
    page = w.page()
    for name in handlers.TAB_NAMES:
        w.read("positive", page, f"/api/tabs/{name}")
    w.read("negative", page, "/api/tabs/nobody")


def read_evidence(w):
    page = w.page()
    rkey = page.send(YELLOW)
    w.read("positive", page, f"/api/evidence/responses/{rkey}")
    w.read("negative", page, "/api/evidence/responses/R99")


def read_live(w):
    page = w.page()
    w.read("positive", page, "/api/live")
    w.read("negative", page, "/api/live", headers={"Origin": FOREIGN_ORIGIN})


def read_phone_inbox(w):
    page = w.page()
    tkey = "T" + page.send(RED)
    page.device(tkey, "confirm")
    page.device(tkey, "mfa_request")
    w.read("positive", page, f"/api/phone/{page.pair()}?lang=zh-CN&view=customer")
    w.read("negative", page, f"/api/phone/{page.pair()}?view=nobody")


def read_index(w):
    page = w.page()
    w.read("positive", page, "/")
    w.read("negative", page, "/", headers={"Host": FOREIGN_HOST})


def read_phone_page(w):
    page = w.page()
    w.read("positive", page, f"/phone/{page.pair()}")
    w.read("negative", page, "/phone/short")


def read_vendor(w):
    page = w.page()
    w.read("positive", page, "/vendor/corridor-hmac.js")
    w.read("negative", page, "/vendor/corridor-hmac.js", headers={"Host": FOREIGN_HOST})


def read_static(w):
    page = w.page()
    w.read("positive", page, "/static/js/api.js")
    w.read("negative", page, "/static/js/no-such-file.js")


def read_root_static(w):
    page = w.page()
    w.read("positive", page, "/js/api.js")
    w.read("negative", page, "/js/no-such-file.js")


# route: (walk, the success's content type, the refusal's status and code)
READS = {
    "/api/state": (read_state, "application/json", 409, "stale_generation"),
    "/api/i18n": (read_i18n, "application/json", 401, "no_session"),
    "/api/tabs/{name}": (read_tab, "application/json", 404, "not_found"),
    "/api/evidence/responses/{rkey}": (read_evidence, "application/json", 404, "not_found"),
    "/api/live": (read_live, "application/json", 403, "forbidden_origin"),
    "/api/phone/{pair}": (read_phone_inbox, "application/json", 404, "not_found"),
    "/": (read_index, "text/html", 403, "forbidden_origin"),
    "/phone/{pair}": (read_phone_page, "text/html", 404, "not_found"),
    "/vendor/corridor-hmac.js": (read_vendor, "text/javascript", 403, "forbidden_origin"),
    "/static/*": (read_static, "text/javascript", 404, "not_found"),
    "/*": (read_root_static, "text/javascript", 404, "not_found"),
}


def read_routes():
    """Every GET route of the route table but the catch-all 404, with what it needs."""
    found = {}
    for _pattern, handler, options in web_app_module.routes():
        if "GET" in handler.methods and handler is not handlers.Missing:
            route = options["route"]
            found[route] = tuple(options.get("needs", ())) + READ_NEEDS.get(route, ())
    return found


def test_e14_every_open_read_route_has_a_walk_and_the_closed_one_is_named():
    reads = read_routes()
    opened = {route for route, needs in reads.items() if set(needs) <= web_package.FEATURES}
    assert set(READS) == opened
    assert set(reads) - opened == {LAB_GET}


@pytest.mark.parametrize("route", sorted(READS))
def test_e14_each_open_read_route_answers_and_refuses_with_its_counts_kept_apart(
    web_app, monkeypatch, tmp_path, route
):
    """RW-4''' item 2: for every open read route, a reachable success and a reachable
    refusal, and per state the approvals, input signatures, codes sent and submitted,
    green reads, dispatches, refreshes, token issues, live syncs, revision steps and
    changed tokens counted apart. Neither state counts anything: a read only reads."""
    walk_route, content_type, status, code = READS[route]
    walk = ReadWalk(web_app, route, monkeypatch, tmp_path)
    walk_route(walk)
    reader = WebClient(web_app)
    for response in walk.results["positive"]:
        assert response.code == 200, (route, response.code)
        assert response.headers["Content-Type"].startswith(content_type), route
        if content_type == "application/json":
            assert reader.json(response)["ok"] is True, route
    (refusal,) = walk.results["negative"]
    assert refusal.code == status, (route, refusal.code)
    assert reader.json(refusal)["error"]["code"] == code, route
    for state in ("positive", "negative"):
        counts = walk.counts[state]
        for name in READ_COUNTED:
            assert counts[name] == 0, (route, state, name, counts)
