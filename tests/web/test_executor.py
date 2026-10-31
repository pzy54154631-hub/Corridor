"""The two pools, their bounds, the session lock and eviction of busy sessions (design §2.3,
§2.7, §6.5 item 28)."""

import asyncio
import threading
import time
from types import SimpleNamespace

import pytest

from corridor.i18n import get_locale
from corridor.ui.audience import get_audience
from corridor.web.errors import WebError
from corridor.web.executor import Executor
from tests.web.conftest import WebClient, serve, ws_of

MESSAGE = {"text": "hi", "external_files": 0, "client_msg_id": "m-000001"}


@pytest.fixture
def web_settings(doubles):
    return doubles.settings()


def in_thread(function, *args):
    answers = {}

    def run():
        answers["value"] = function(*args)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, answers


def opened_clients(app, count, doubles=None, *, live=False):
    clients = [WebClient(app) for _ in range(count)]
    for client in clients:
        client.open()
        if live:
            doubles.live_sessions.add(id(ws_of(app, client)))
    return clients


def eventually(condition, timeout=10):
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.01)
    return True


def quickly(function, *args):
    started = time.monotonic()
    result = function(*args)
    return result, time.monotonic() - started


def test_four_sessions_blocked_in_live_calls_leave_everyone_else_served(web_app, doubles):
    gate = doubles.block("send_message")
    blocked = opened_clients(web_app, 4, doubles, live=True)
    threads = [in_thread(client.post, "/api/chat/message", MESSAGE) for client in blocked]
    assert doubles.wait_entered("send_message", 2)
    fifth = opened_clients(web_app, 1)[0]
    for call in (
        lambda: fifth.post("/api/chat/stop", {}),
        lambda: fifth.get("/api/state"),
        lambda: fifth.post("/api/stop", {}),
    ):
        response, elapsed = quickly(call)
        assert response.code == 200 and elapsed < 5
    gate.set()
    for thread, answers in threads:
        thread.join(10)
        assert answers["value"].code == 200
    live_threads = {r.thread for r in doubles.records if r.name == "send_message"}
    assert all(name.startswith("web-live") for name in live_threads)
    fifth_ws = ws_of(web_app, fifth)
    assert all(r.thread.startswith("web-main") for r in doubles.records if r.ws is fifth_ws)


@pytest.mark.parametrize("pool", ["live", "main"])
def test_a_full_pool_refuses_with_503_before_the_lock(web_settings, doubles, pool):
    sizes = {f"pool_{pool}": 1, f"queue_{pool}": 1}
    with serve({**web_settings, **sizes}) as app:
        clients = opened_clients(app, 3, doubles, live=pool == "live")
        gate = doubles.block("send_message")
        threads = [in_thread(client.post, "/api/chat/message", MESSAGE) for client in clients[:2]]
        assert doubles.wait_entered("send_message")
        lane = app.settings["executor"].lanes[pool]
        assert eventually(lambda: lane.admitted == 2)  # the second request waits its turn
        refused = clients[2].post("/api/chat/message", MESSAGE)
        data = clients[2].json(refused)
        assert refused.code == 503 and data["error"] == {
            "code": "busy",
            "message": data["error"]["message"],
            "retry": True,
        }
        assert "state" not in data
        third = ws_of(app, clients[2])
        assert third.revision == 0 and third.busy == 0
        assert "send_message" not in doubles.names(third)
        gate.set()
        for thread, answers in threads:
            thread.join(10)
            assert answers["value"].code == 200
        assert eventually(lambda: lane.admitted == 0)


def test_a_stop_on_the_same_session_runs_after_the_call_in_progress(web_app, client, doubles):
    client.open()
    gate = doubles.block("send_message")
    message = in_thread(client.post, "/api/chat/message", MESSAGE)
    assert doubles.wait_entered("send_message")
    stop = in_thread(client.post, "/api/stop", {})
    stop[0].join(0.3)
    assert stop[0].is_alive() and "stop_all" not in doubles.names()
    gate.set()
    for thread, answers in (message, stop):
        thread.join(10)
        assert answers["value"].code == 200
    names = [name for name in doubles.names() if name in ("send_message", "stop_all")]
    assert names == ["send_message", "stop_all"]


def test_a_dropped_connection_neither_cancels_the_call_nor_frees_the_lock(web_app, client, doubles):
    client.open()
    ws = ws_of(web_app, client)
    gate = doubles.block("send_message")
    client.abandon("/api/chat/message", MESSAGE)
    assert doubles.wait_entered("send_message")
    read = in_thread(client.get, "/api/state")
    assert eventually(lambda: ws.busy == 2)
    read[0].join(0.3)
    assert read[0].is_alive()
    gate.set()
    read[0].join(10)
    data = client.json(read[1]["value"])
    # The abandoned call completed and was recorded before the read got the lock.
    assert data["revision"] == 1 and ws.rkeys == {"R1": "rid-R1"}
    names = doubles.names(ws)
    assert names[names.index("send_message") :][:4] == [
        "send_message",
        "live.sync",
        "refresh",
        "app_state",
    ]
    assert ws.busy == 0


def test_busy_sessions_are_never_evicted(web_settings, doubles):
    with serve({**web_settings, "max_sessions": 2}) as app:
        first, second = opened_clients(app, 2)
        gate = doubles.block("send_message")
        busy = in_thread(first.post, "/api/chat/message", MESSAGE)
        assert doubles.wait_entered("send_message")
        third = WebClient(app)
        third.open()  # evicts the idle second session, not the older busy first one
        assert ws_of(app, second) is None and ws_of(app, first) is not None
        held = in_thread(third.post, "/api/chat/message", MESSAGE)
        assert doubles.wait_entered("send_message")
        refused = WebClient(app).post("/api/session", {})
        assert refused.code == 503 and WebClient(app).json(refused)["error"]["code"] == "busy"
        assert not refused.headers.get_list("Set-Cookie")
        gate.set()
        for thread, answers in (busy, held):
            thread.join(10)
            assert answers["value"].code == 200


# --- the executor alone ---------------------------------------------------------------------


def session(view="customer", locale="en"):
    return SimpleNamespace(view=view, locale=locale, busy=0, lock=asyncio.Lock())


def test_jobs_run_in_their_sessions_context_and_leave_none_behind():
    executor = Executor(pool_main=1, queue_main=4)

    async def scenario():
        seen = [
            await executor.run(
                session(view, locale), lambda: (get_locale(), get_audience()), pool="main"
            )
            for view, locale in (("customer", "en"), ("judge", "zh-CN"))
        ]
        seen.append(await executor.call(lambda: (get_locale(), get_audience())))
        return seen

    try:
        assert asyncio.run(scenario()) == [
            ("en", "customer"),
            ("zh-CN", "judge"),
            ("zh-CN", "judge"),  # the defaults again: nothing leaked into the thread
        ]
    finally:
        executor.shutdown()


def test_a_cancelled_request_keeps_the_lock_until_its_job_returns():
    executor = Executor(pool_main=2, queue_main=2)
    ws = session()
    entered, release, done = threading.Event(), threading.Event(), []

    def job():
        entered.set()
        release.wait(10)
        done.append(True)
        return "recorded"

    async def scenario():
        request = asyncio.ensure_future(executor.run(ws, job, pool="main"))
        await asyncio.get_running_loop().run_in_executor(None, entered.wait, 10)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        assert ws.lock.locked() and ws.busy == 1
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(ws.lock.acquire(), 0.2)
        release.set()
        await asyncio.wait_for(ws.lock.acquire(), 5)
        ws.lock.release()
        return executor.lanes["main"].admitted

    try:
        assert asyncio.run(scenario()) == 0
        assert done == [True] and ws.busy == 0
    finally:
        executor.shutdown()


def test_admission_is_counted_per_pool():
    executor = Executor(pool_main=1, queue_main=0, pool_live=1, queue_live=0)
    release = threading.Event()

    async def scenario():
        first = asyncio.ensure_future(
            executor.run(session(), lambda: release.wait(10), pool="live")
        )
        await asyncio.sleep(0.05)
        with pytest.raises(WebError) as refused:
            await executor.run(session(), lambda: None, pool="live")
        assert refused.value.code == "busy"
        assert await executor.run(session(), lambda: "main", pool="main") == "main"
        release.set()
        await first

    try:
        asyncio.run(scenario())
    finally:
        executor.shutdown()
