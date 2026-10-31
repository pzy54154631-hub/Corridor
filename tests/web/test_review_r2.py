"""Regression tests for the second review round (R2) of the web front end.

Each test names the item it closes: S-n for the security review's must items, E-n for
the external review's (n follows WEB-R2-ADOPTION.md). Every test drives the real
handlers or service functions; the controllers, the gateway and the kernel are never
replaced, only spied on, except where a test injects the fault the review described.
"""

import json
import re
import socket
import time
from collections import Counter
from copy import deepcopy

import pytest

import corridor.web as web_package
from corridor.agent.domestic_session import DomesticSession
from corridor.ui.controller import DemoController
from corridor.web import app as web_app_module
from corridor.web import compat, errors, lab, service, tasks
from tests.unit.test_ui_scam_friction import SAFE
from tests.web.conftest import serve
from tests.web.test_api_chat import CODE, Browser
from tests.web.test_customer_trim import scan
from tests.web.test_review_r1 import EXT, held_body, spied
from tests.web.test_tasks import BIRTHDAY, RED, YELLOW, logged_in

DIRECTED = "对方让我给新联系人转480元"
PURCHASE = "帮我申购2000元稳健30天持有"
UUID = re.compile(r"[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}")
HEX = re.compile(r"[0-9a-f]{16,}")
LAB_POSTS = ("/api/lab/start", "/api/lab/change", "/api/lab/try-previous", "/api/lab/gesture")


@pytest.fixture
def web_settings():
    # The submission build closes the lab (RW-4''); these tests open it explicitly.
    return {"features": web_package.FEATURES | EXT | {lab.FEATURE}}


def start_lab(page, scenario="yellow", *, expect=200):
    data = page.post("/api/lab/start", {"scenario": scenario, "mode": "scripted"}, expect=expect)
    return data.get("lab")


def current(vm):
    return next(task for task in vm["state"]["device"]["tasks"] if task["current"])


def lab_post(page, path, vm, *, expect=200, **values):
    return page.post(path, {"lab_generation": vm["session_generation"], **values}, expect=expect)


def lab_gesture(page, vm, *, expect=200, **extra):
    task = current(vm)
    shown = task["gesture"]
    return lab_post(
        page,
        "/api/lab/gesture",
        vm,
        expect=expect,
        tkey=task["key"],
        op=shown["op"],
        gesture=shown["token"],
        card_version=shown["card_version"],
        **extra,
    )


def lab_phone_code(page):
    """The experiment's code where a person reads it: its own pairing's phone page."""
    pair = page.ws.demos[lab.SLOT]["session"].pair
    phone = page.client.json(page.client.get(f"/api/phone/{pair}?lang=zh-CN&view=judge"))
    latest = next(item for item in phone["messages"] if item["latest"])
    return CODE.search(latest["text"]).group(), phone


# --- S-1 / E-8: the lab exists only in the judge view -------------------------------------


def lab_bodies(generation="x" * 12):
    """A well-formed body for every lab POST (the fields check runs before the routes')."""
    gesture = {"tkey": "TR1", "op": "confirm", "gesture": "g" * 22, "card_version": "0" * 32}
    return {
        "/api/lab/start": {"scenario": "yellow", "mode": "scripted"},
        "/api/lab/change": {"lab_generation": generation},
        "/api/lab/try-previous": {"lab_generation": generation},
        "/api/lab/gesture": {"lab_generation": generation, **gesture},
    }


def test_s1_the_customer_view_receives_no_lab_body(web_app):
    page = Browser(web_app, view="customer")
    page.login()
    for path, body in lab_bodies().items():
        data = page.post(path, body, expect=409)
        assert data["error"]["code"] == "not_applicable" and "lab" not in data
    data = page.get("/api/lab", expect=409)
    assert data["error"]["code"] == "not_applicable" and "lab" not in data
    assert page.ws.demos == {}
    assert page.state["capabilities"]["lab"] is True


def test_s1_a_judge_lab_is_not_shown_after_switching_to_the_customer_view(web_app):
    page = Browser(web_app, view="judge")
    page.login()
    vm = lab_gesture(page, start_lab(page))["lab"]
    assert vm["proof"]["bank_receipts"] and UUID.search(json.dumps(vm))
    page.post("/api/session/view", {"view": "customer"})
    bodies = [page.get("/api/lab", expect=409)]
    for path, body in lab_bodies(vm["session_generation"]).items():
        bodies.append(page.post(path, body, expect=409))
    for body in bodies:
        assert body["error"]["code"] == "not_applicable"
        text = json.dumps(body, ensure_ascii=False)
        assert "lab" not in body and not UUID.search(text)
        assert lab.SOURCE not in text and "try_previous" not in text
        assert not [path for path in hex_paths(body) if "version" not in path]
        assert scan("/api/lab (customer)", body, "customer") == []
    page.post("/api/session/view", {"view": "judge"})
    assert page.get("/api/lab")["lab"]["proof"]["dispatches"] == 1


def hex_paths(value, path="body"):
    found = []
    if isinstance(value, dict):
        for key, item in value.items():
            found += hex_paths(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found += hex_paths(item, f"{path}[{index}]")
    elif isinstance(value, str) and HEX.search(value):
        found.append(path)
    return found


def test_e8_a_missing_audit_check_is_not_provided_never_failed():
    assert lab._audit({}) is None
    assert lab._audit({"audit_valid": None}) is None
    assert lab._audit({"audit_valid": True}) is True
    assert lab._audit({"audit_valid": False}) is False


# --- S-2 / E-6: the lab SMS is only on its own pairing's phone page -----------------------


def test_s2_the_lab_code_reaches_no_session_api_and_only_its_own_phone(web_app):
    page = Browser(web_app)
    page.login()
    vm = start_lab(page, "red")
    vm = lab_gesture(page, vm)["lab"]
    vm = lab_gesture(page, vm)["lab"]
    assert current(vm)["gesture"]["op"] == "mfa_verify"
    code, phone = lab_phone_code(page)
    assert phone["unpaired"] is None
    child = page.ws.demos[lab.SLOT]["session"]
    assert vm["state"]["device"]["sms"]["url"].startswith(f"/phone/{child.pair}?")
    assert page.client.get("/api/lab/phone").code == 404
    page.get("/api/lab")
    page.get("/api/state")
    vm = lab_gesture(page, vm, code=code)["lab"]
    lab_gesture(page, vm)
    for path, body in page.bodies:
        if path.startswith("/api/lab") or path.startswith("/api/state"):
            assert re.search(rf"(?<![0-9]){code}(?![0-9])", body) is None, path
    page.post("/api/stop", {})
    gone = page.client.json(page.client.get(f"/api/phone/{child.pair}"))
    assert gone["messages"] == [] and gone["unpaired"]


# --- E-1 and E-2: older payments and the first later turn ----------------------------------


def test_e2_a_business_task_scans_the_first_later_user_turn():
    harness = logged_in()
    rkey = harness.send(YELLOW)
    identity = harness.identity("T" + rkey)
    key = service._business(harness.ws, "wealth", "pending", identity)
    task = harness.ws.tasks[key]
    assert task.since == len(harness.ws.transcript) and task.origin is None
    harness.send(DIRECTED)
    # The first user turn after registration sits at index ``since`` and is scanned.
    assert harness.ws.transcript[task.since].role == "user"
    assert tasks.later_risk(harness.ws, task) is True


def test_e1_a_later_replys_own_proceed_does_not_clear_its_cue(browser):
    older = browser.send(YELLOW)
    gated = browser.send("工作人员说给新联系人转480元")
    browser.answer(gated, SAFE)
    assert browser.response(gated)["gate"]["decision"] == "proceed"
    assert tasks._unresolved(browser.ws, browser.ws.rkeys[gated]) is True
    assert tasks.later_risk(browser.ws, browser.ws.tasks["T" + older]) is True
    plain = browser.send("查询余额")
    assert tasks._unresolved(browser.ws, browser.ws.rkeys[plain]) is False


# --- E-3: an unknown result after a call distrusts the session -----------------------------


def test_e3_an_unknown_status_after_a_state_change_distrusts_the_session(browser, monkeypatch):
    first = browser.send(YELLOW)
    second = browser.send(RED)
    saved = held_body(browser, "T" + second)
    original = service.device_confirm

    def changed_then_unknown(*args, **kwargs):
        original(*args, **kwargs)
        return {"status": "not-a-known-status"}

    monkeypatch.setattr(service, "device_confirm", changed_then_unknown)
    browser.post(f"/api/chat/responses/{first}/current", {})
    data = browser.post(
        f"/api/device/tasks/T{first}/confirm", held_body(browser, "T" + first), expect=500
    )
    assert data["error"]["code"] == "outcome_unknown" and browser.ws.untrusted
    assert browser.ws.gestures == {} and browser.ws.gate_eval == {}
    assert all(task["gesture"] is None for task in data["state"]["device"]["tasks"])
    monkeypatch.undo()
    counts = spied(monkeypatch, "approve", "dispatch")
    browser.fail(f"/api/device/tasks/T{second}/confirm", saved, "not_applicable")
    assert counts == Counter()


def test_e3_a_failure_inside_a_choice_preparer_distrusts_the_session(browser, monkeypatch):
    rkey = browser.send("每月5号给房东转500元")
    browser.red("T" + rkey)
    listed = browser.send("取消每月5号给房东的转账")
    shown = browser.response(listed)["schedule_cancel_select"]
    original = DomesticSession.prepare_schedule_cancel

    def prepared_then_failed(self, *args, **kwargs):
        original(self, *args, **kwargs)
        raise RuntimeError("after a prepared card")

    monkeypatch.setattr(DomesticSession, "prepare_schedule_cancel", prepared_then_failed)
    data = browser.post(
        f"/api/chat/responses/{listed}/schedule-cancel",
        {"choice": "c0", "list_version": shown["list_version"]},
        expect=500,
    )
    assert data["error"]["code"] == "outcome_unknown" and browser.ws.untrusted
    assert all(task["gesture"] is None for task in data["state"]["device"]["tasks"])


@pytest.fixture
def browser(web_app):
    page = Browser(web_app)
    page.login()
    return page


# --- E-4: a purchase whose signed terms cannot be read back is withdrawn -------------------


@pytest.mark.parametrize("signed", [None, {"amount": "1/1"}], ids=["unreadable", "differs"])
def test_e4_unverified_signed_terms_leave_no_executable_pending(monkeypatch, signed):
    harness = logged_in()
    c = harness.controller
    rkey = harness.send(PURCHASE)
    tkey = "T" + rkey
    assert harness.step(tkey) == "review_purchase"
    inputs = len(c.state.user_inputs)
    monkeypatch.setattr(DomesticSession, "_signed_terms", lambda self, pending_id: signed)
    counts = spied(monkeypatch, "request_mfa", "dispatch", "approve")
    harness.confirm(tkey)
    raw = harness.session.response(harness.ws.rkeys[rkey])
    assert raw["status"] == "clarify" and raw["payload"]["terms_changed"] is True
    withdrawn = raw["payload"]["withdrawn_input"]
    assert withdrawn["reason"] == ("unreadable" if signed is None else "differs")
    assert harness.session.withdrawn_inputs == [withdrawn]
    # The signed input is kept (and recorded), not deleted as if it had never been signed.
    assert len(c.state.user_inputs) > inputs
    assert withdrawn["preference_ref"] in c.state.user_inputs
    assert withdrawn["pending_id"] not in c._pending
    assert tkey not in harness.ws.gestures
    assert harness.step(tkey) not in tasks.OPS
    assert counts == Counter() and c.gateway.bank.execute_count == 0


# --- E-5: try-previous only verifies; the same task with a changed card refuses -------------


def test_e5_try_previous_never_reaches_the_controller(browser, monkeypatch):
    calls = Counter()
    for name in ("device_confirm", "device_mfa_request", "device_mfa_verify"):
        original = getattr(service, name)

        def counted(*args, _name=name, _original=original, **kwargs):
            calls[_name] += 1
            return _original(*args, **kwargs)

        monkeypatch.setattr(service, name, counted)
    counts = spied(monkeypatch, "approve", "dispatch")
    vm = lab_post(browser, "/api/lab/change", start_lab(browser))["lab"]
    vm = lab_post(browser, "/api/lab/try-previous", vm)["lab"]
    attempt = vm["last_attempt"]
    assert attempt["status"] == "refused" and attempt["code"] == "not_found"
    assert [
        (check["target"], check["code"], check["operation_status"]) for check in attempt["checks"]
    ] == [
        ("withdrawn_request", "not_found", 404),
        ("current_card", "gesture_mismatch", 409),
    ]
    assert calls == Counter() and counts == Counter()
    assert vm["proof"]["dispatches"] == 0


def test_e5_an_old_request_that_verified_fails_the_experiment_without_a_call(browser, monkeypatch):
    vm = lab_post(browser, "/api/lab/change", start_lab(browser))["lab"]
    counts = spied(monkeypatch, "approve", "dispatch")
    confirms = []
    monkeypatch.setattr(service, "device_confirm", lambda *a, **k: confirms.append(a))
    # The regression the review feared: the withdrawn request still verifies.
    monkeypatch.setattr(tasks, "verify", lambda *args, **kwargs: (None, None))
    data = lab_post(browser, "/api/lab/try-previous", vm, expect=500)
    assert data["error"]["code"] == "outcome_unknown"
    assert confirms == [] and counts == Counter()
    closed = browser.get("/api/lab")["lab"]
    assert closed["last_attempt"]["code"] == "previous_verified"
    assert all(task["gesture"] is None for task in closed["state"]["device"]["tasks"])
    assert browser.ws.demos[lab.SLOT]["session"].untrusted
    assert not browser.ws.untrusted


def test_e5_an_old_token_with_the_changed_cards_version_is_refused(monkeypatch):
    harness = logged_in()
    tkey = "T" + harness.send(YELLOW)
    token, _ = harness.token(tkey)
    original = compat.authoritative_cards

    def changed(controller, identity):
        cards = deepcopy(original(controller, identity))
        for card in cards:
            card["amount"] = "1/1"
        return cards

    task = harness.ws.tasks[tkey]
    with monkeypatch.context() as patched:
        patched.setattr(compat, "authoritative_cards", changed)
        with harness.bound():
            info = tasks.task_step(harness.ws, task)
            version = tasks.card_version(harness.ws, tasks.digest(harness.ws, task, info))
            with pytest.raises(errors.WebError) as caught:
                tasks.verify(harness.ws, tkey, "confirm", token, version)
    # A changed card also voids its recipient-check record: refused either way, 409.
    assert caught.value.status == 409
    assert caught.value.code in {"card_changed", "gesture_closed"}
    # Only the derived object mapping changes: the step stays, the digest does not.
    original_facts = tasks.object_facts
    monkeypatch.setattr(
        tasks, "object_facts", lambda ws, cards: [*original_facts(ws, cards), {"remapped": 1}]
    )
    with harness.bound():
        info = tasks.task_step(harness.ws, task)
        assert info.step == "confirm"
        version = tasks.card_version(harness.ws, tasks.digest(harness.ws, task, info))
        with pytest.raises(errors.WebError) as caught:
            tasks.verify(harness.ws, tkey, "confirm", token, version)
    assert caught.value.code == "card_changed"
    assert harness.ws.gestures[tkey].token == token


# --- E-7: the lab's capability, login, budget and failure boundary --------------------------


def test_e7_a_closed_lab_capability_answers_404_and_revokes_a_running_lab(web_app):
    page = Browser(web_app)
    page.login()
    vm = lab_gesture(page, start_lab(page, "red"))["lab"]
    lab_gesture(page, vm)
    child = page.ws.demos[lab.SLOT]["session"]
    assert web_app.settings["store"].pairs.allowed(child.pair)
    web_app.settings["features"] = frozenset(web_app.settings["features"] - {"lab"})
    # A closed lab's paths answer 404 before the session is looked at (RW-4'''), so the
    # session's next request is the one that revokes the running experiment.
    revision = page.ws.revision
    assert page.client.get("/api/lab").code == 404
    for path, body in lab_bodies(vm["session_generation"]).items():
        assert page.client.post(path, body).code == 404
    assert page.ws.revision == revision
    assert page.get("/api/state")["state"]["capabilities"]["lab"] is False
    assert lab.SLOT not in page.ws.demos and child.untrusted and child.gestures == {}
    assert not web_app.settings["store"].pairs.allowed(child.pair)
    assert all(sms.status != "active" for sms in compat.OUTBOX.inbox(child.pair))


def test_e7_the_lab_needs_a_logged_in_parent_and_a_start_budget(web_app, monkeypatch):
    page = Browser(web_app)
    assert start_lab(page, expect=409) is None and page.ws.demos == {}
    page.login()
    monkeypatch.setattr(lab, "STARTS", 2)
    start_lab(page)
    start_lab(page, "red")
    data = page.post("/api/lab/start", {"scenario": "yellow", "mode": "scripted"}, expect=409)
    assert data["error"]["code"] == "not_applicable"


def test_e7_a_failure_inside_the_lab_leaves_the_parent_trusted(browser, monkeypatch):
    yellow = "T" + browser.send(YELLOW)
    original = service.send_message
    parent = browser.ws

    def child_only(ws, *args, **kwargs):
        if ws is not parent:
            raise TypeError("inside the child")
        return original(ws, *args, **kwargs)

    monkeypatch.setattr(service, "send_message", child_only)
    data = browser.post("/api/lab/start", {"scenario": "yellow", "mode": "scripted"}, expect=500)
    assert data["error"]["code"] == "outcome_unknown"
    assert not parent.untrusted and lab.SLOT not in parent.demos
    monkeypatch.undo()
    assert browser.device(yellow, "confirm")["result"] == {"status": "settled"}


@pytest.mark.parametrize("failure", ["web", "other"])
def test_e7_a_failure_during_change_drops_the_child_and_its_cache(browser, monkeypatch, failure):
    vm = start_lab(browser)
    child = browser.ws.demos[lab.SLOT]["session"]
    original = service.send_message

    def broken(ws, *args, **kwargs):
        if ws is child:
            raise errors.error("not_applicable") if failure == "web" else TypeError("change")
        return original(ws, *args, **kwargs)

    monkeypatch.setattr(service, "send_message", broken)
    expect = 409 if failure == "web" else 500
    lab_post(browser, "/api/lab/change", vm, expect=expect)
    assert browser.get("/api/lab")["lab"] == {"active": False, "mode": "scripted"}
    assert child.untrusted and child.gestures == {} and not browser.ws.untrusted


# --- E-9: plans are graded node by node; compensation binds the plan's state ---------------


def test_e9_a_plan_binds_each_paying_nodes_grade_into_its_token(monkeypatch):
    harness = logged_in()
    tkey = "T" + harness.send(BIRTHDAY)
    task = harness.ws.tasks[tkey]
    with harness.bound():
        evaluation = tasks.plan_evaluation(harness.ws, task)
        assert evaluation is not None and evaluation["nodes"]
        assert all(node["grade"] == 0 for node in evaluation["nodes"])
        assert tasks.plan_risk(harness.ws, task) is False
        info = tasks.task_step(harness.ws, task)
        before = tasks.digest(harness.ws, task, info)
    original = DemoController._scam_grade

    def one_gated(self, cards, text, files):
        grade, codes, context = original(self, cards, text, files)
        return 1, codes, {**context, "injected": True}

    monkeypatch.setattr(DemoController, "_scam_grade", one_gated)
    with harness.bound():
        assert tasks.plan_risk(harness.ws, task) is True
        assert tasks.task_step(harness.ws, task).step == "gate_plan"
        assert tasks.digest(harness.ws, task, info) != before


def test_e9_the_compensation_version_binds_the_plans_state(browser, monkeypatch):
    plan = "T" + browser.send(BIRTHDAY)
    browser.post(
        f"/api/device/tasks/{plan}/nodes",
        {"card_version": browser.task(plan)["nodes_version"], "nodes": ["n0", "n1", "n2", "n3"]},
    )
    browser.device(plan, "confirm")
    browser.send("停止")
    version = browser.state["chat"]["task_panel"]["controls"]["compensate_version"]
    original = DemoController.snapshot

    def changed(self):
        snapshot = original(self)
        if snapshot["plans"]:
            snapshot["plans"][-1] = {**snapshot["plans"][-1], "status": "changed-after-view"}
        return snapshot

    monkeypatch.setattr(DemoController, "snapshot", changed)
    calls = []
    monkeypatch.setattr(DemoController, "compensate", lambda self, plan_id: calls.append(plan_id))
    browser.fail("/api/chat/compensate", {"plan_version": version}, "card_changed")
    assert calls == []


# --- E-10: derived object labels and business lists are bound -------------------------------


def test_e10_a_changed_merchant_record_voids_the_cancel_token(browser):
    browser.post("/api/subscriptions/query", {})
    query = [task for task in browser.state["device"]["tasks"] if task["source"] == "subscriptions"]
    browser.device(query[0]["key"], "confirm")
    browser.pick("/api/subscriptions/cancel", "c0")
    cancel = max(
        (task for task in browser.state["device"]["tasks"] if task["source"] == "subscriptions"),
        key=lambda task: int(task["key"][2:]),
    )
    saved = held_body(browser, cancel["key"])
    c = browser.ws.controller
    identity = browser.ws.tasks[cancel["key"]].identity
    (card,) = compat.authoritative_cards(c, identity)
    subscription = c.state.subscriptions[card["objects"]["authorization_ref"]]
    merchant = c.state.merchants[subscription.merchant_ref]
    c.gateway.store.update(
        merchants={
            **c.state.merchants,
            merchant.id: merchant.model_copy(update={"display_alias": "另一个商户"}),
        }
    )
    with pytest.raises(errors.WebError) as caught:
        tasks.verify(browser.ws, cancel["key"], "confirm", saved["gesture"], saved["card_version"])
    assert caught.value.code == "card_changed"


def test_e10_a_position_list_version_binds_the_shown_facts(monkeypatch):
    harness = logged_in()
    ids = tasks.positions(harness.ws)
    before = tasks.list_version(harness.ws, "positions", ids)
    original = DemoController.snapshot

    def changed(self):
        snapshot = original(self)
        snapshot["positions"] = [
            {**row, "principal_amount": "1/1"} for row in snapshot["positions"]
        ]
        return snapshot

    monkeypatch.setattr(DemoController, "snapshot", changed)
    assert tasks.positions(harness.ws) == ids
    assert tasks.list_version(harness.ws, "positions", ids) != before


# --- E-11: the kept kernel view names its own source and reading time ----------------------


def test_e11_kept_kernel_views_keep_their_source_and_reading_time(browser):
    yellow = browser.send(YELLOW)
    browser.device("T" + yellow, "confirm")
    one = browser.get(f"/api/evidence/responses/{yellow}")["technical"]["kernel"]
    red = browser.send(RED)
    browser.red("T" + red)
    two = browser.get(f"/api/evidence/responses/{red}")["technical"]["kernel"]
    # The yellow one-gesture confirmation is the device workflow's record, read after it.
    assert (one["source"], one["read_at"]) == ("device_confirmation_record", "after_confirmation")
    # The red pending is still held by the controller: its live view, not a kept copy.
    assert "source" not in two or (two["source"], two["read_at"]) == (
        "web_copy_read_while_pending",
        "before_dispatch",
    )
    assert "web_copy_read_before_dispatch" not in json.dumps([one, two])
    kept = {browser.ws.kernel_seen[ident]["source"] for ident in browser.ws.kernel_seen}
    assert kept <= {"pending_copy", "confirmation"}


# --- E-12: the entry buffer and the body timeout on the real server -------------------------


def raw_request(port, head, body=b"", *, wait=5.0):
    connection = socket.create_connection(("127.0.0.1", port), timeout=wait)
    try:
        try:
            connection.sendall(head + body)
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            pass
        connection.settimeout(wait)
        chunks = []
        while True:
            try:
                chunk = connection.recv(65536)
            except (TimeoutError, ConnectionResetError, ConnectionAbortedError):
                break
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        connection.close()


def head_for(page, length=None, *, chunked=False):
    lines = [
        b"POST /api/chat/stop HTTP/1.1",
        f"Host: {page.client.host}".encode(),
        f"Origin: {page.client.origin}".encode(),
        b"Content-Type: application/json",
        f"Cookie: corridor_web={page.client.browser}".encode(),
        f"X-Corridor-Tab: {page.client.tab}".encode(),
        f"X-Corridor-Generation: {page.client.generation}".encode(),
        b"Transfer-Encoding: chunked" if chunked else f"Content-Length: {length}".encode(),
        b"Connection: close",
    ]
    return b"\r\n".join(lines) + b"\r\n\r\n"


def test_e12_the_entry_limit_and_the_parse_limit_differ_and_both_hold(browser):
    assert web_app_module.ENTRY_LIMIT == 128 * 1024
    assert web_app_module.SERVER_OPTIONS["max_body_size"] == web_app_module.ENTRY_LIMIT
    port = browser.client.port
    # Above the parse limit, inside the entry: the contract's 413.
    body = b'{"x":"' + b"a" * (80 * 1024) + b'"}'
    answer = raw_request(port, head_for(browser, len(body)), body)
    assert answer.startswith(b"HTTP/1.1 413")
    # Declared above the entry: answered 400 at once, the body never read.
    started = time.monotonic()
    answer = raw_request(port, head_for(browser, 300 * 1024))
    assert answer.startswith(b"HTTP/1.1 400") and time.monotonic() - started < 5
    # Streamed above the entry in chunks: the connection is answered 400 and closed.
    chunk = b"a" * (64 * 1024)
    chunks = b"".join(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n" for _ in range(3))
    answer = raw_request(port, head_for(browser, chunked=True), chunks + b"0\r\n\r\n")
    assert answer == b"" or answer.startswith(b"HTTP/1.1 400")
    assert browser.post("/api/chat/stop", {})["ok"] is True


def test_e12_a_trickling_body_is_cut_and_parallel_ones_do_not_block_others(monkeypatch):
    monkeypatch.setattr(
        web_app_module, "SERVER_OPTIONS", {**web_app_module.SERVER_OPTIONS, "body_timeout": 1.0}
    )
    with serve({}) as app:
        page = Browser(app)
        page.login()
        port = page.client.port
        slow = [socket.create_connection(("127.0.0.1", port), timeout=10) for _ in range(12)]
        try:
            for connection in slow:
                connection.sendall(head_for(page, 1000) + b'{"x":')
            started = time.monotonic()
            assert page.post("/api/chat/stop", {})["ok"] is True
            assert time.monotonic() - started < 5
            time.sleep(2.0)
            for connection in slow:
                connection.settimeout(5)
                try:
                    left = connection.recv(65536)
                except (ConnectionResetError, ConnectionAbortedError):
                    left = b""
                assert left == b"" or not left.startswith(b"HTTP/1.1 200")
        finally:
            for connection in slow:
                connection.close()


# --- E-14: a contradicting card copy, a lockout and a message id -----------------------------


def test_e14_a_reply_card_that_contradicts_the_authoritative_card_is_never_signed(monkeypatch):
    harness = logged_in()
    rkey = harness.send(YELLOW)
    tkey = "T" + rkey
    token, version = harness.token(tkey)
    rid = harness.ws.rkeys[rkey]
    stored = harness.session._responses[rid]
    stored["card"] = {**stored["card"], "amount": "1/1"}
    harness.refresh()
    assert harness.ws.card_mismatch[rkey] is True
    assert harness.step(tkey) == "closed_facts" and tkey not in harness.ws.gestures
    counts = spied(monkeypatch, "approve", "dispatch")
    with pytest.raises(errors.WebError) as caught:
        service.device_confirm(harness.ws, tkey, token, version)
    assert caught.value.code in {"gesture_closed", "gesture_unknown"}
    assert counts == Counter()


def test_e14_the_mismatch_says_so_in_both_views(web_app):
    for view in ("customer", "judge"):
        page = Browser(web_app, view=view)
        page.login()
        rkey = page.send(YELLOW)
        rid = page.ws.rkeys[rkey]
        stored = page.ws.session._responses[rid]
        stored["card"] = {**stored["card"], "amount": "1/1"}
        page.post(f"/api/chat/responses/{rkey}/current", {})
        task = page.task("T" + rkey)
        assert task["step"] == "closed_facts" and task["gesture"] is None
        assert "不一致" in task["step_text"] and task["cancel"] is not None
        assert ("card_mismatch" in page.response(rkey)) is (view == "judge")


def test_e14_a_lockout_leaves_no_active_code_and_no_token(browser):
    first = "T" + browser.send(RED)
    browser.device(first, "confirm")
    browser.device(first, "mfa_request")
    code = browser.sms_code()
    wrong = "000000" if code != "000000" else "111111"
    browser.device(first, "mfa_verify", code=wrong)
    second = "T" + browser.send(RED)
    browser.device(second, "confirm")
    browser.device(second, "mfa_request")
    browser.device(second, "mfa_verify", code=wrong)
    browser.device(second, "mfa_verify", code=wrong)
    assert browser.state["shell"]["safety"]["locked"] is True
    assert all(sms.status != "active" for sms in compat.OUTBOX.inbox(browser.ws.pair))
    phone = browser.client.json(browser.client.get(f"/api/phone/{browser.pair()}"))
    assert phone["messages"] and all(item["status"] != "active" for item in phone["messages"])
    assert all(task["gesture"] is None for task in browser.state["device"]["tasks"])


def test_e14_a_message_id_is_bound_to_its_attachment_count_and_its_session(web_app):
    page = Browser(web_app)
    page.login()
    body = {"text": YELLOW, "external_files": 0, "client_msg_id": "same-id-0002"}
    first = page.post("/api/chat/message", body)["rkey"]
    page.fail("/api/chat/message", {**body, "external_files": 1}, "msg_id_conflict")
    turns = len(page.ws.transcript)
    other = Browser(web_app)
    other.login()
    other.post("/api/chat/message", body)
    assert other.ws is not page.ws and len(other.ws.transcript) == 2
    assert len(page.ws.transcript) == turns and page.ws.rkeys[first]


# --- E-15: a finished revocation is not chosen again; a repeated choice adds nothing --------


def test_e15_a_revoked_schedule_is_not_offered_again(browser, monkeypatch):
    rkey = browser.send("每月5号给房东转500元")
    browser.red("T" + rkey)
    listed = browser.send("取消每月5号给房东的转账")
    shown = browser.response(listed)["schedule_cancel_select"]
    body = {"choice": "c0", "list_version": shown["list_version"]}
    chosen = browser.post(f"/api/chat/responses/{listed}/schedule-cancel", body)["rkey"]
    # Back to the listing and the same choice again: the same card, no second turn.
    browser.post(f"/api/chat/responses/{listed}/current", {})
    turns = len(browser.ws.transcript)
    again = browser.post(f"/api/chat/responses/{listed}/schedule-cancel", body)["rkey"]
    assert again == chosen and len(browser.ws.transcript) == turns
    browser.device("T" + chosen, "confirm")
    rid = browser.ws.rkeys[listed]
    spa = browser.ws.session.response(rid)["payload"]["schedule_cancel"]["candidates"][0]
    with pytest.raises(ValueError):
        browser.ws.session.prepare_schedule_cancel(rid, spa)
    browser.post(f"/api/chat/responses/{listed}/current", {})
    counts = spied(monkeypatch, "approve", "dispatch")
    data = browser.post(f"/api/chat/responses/{listed}/schedule-cancel", body, expect=409)
    assert data["error"]["code"] in {"card_changed", "not_applicable"}
    assert counts == Counter() and not browser.ws.untrusted
