"""Regression tests for the first review round (R1) of the web front end.

Each test names the review item it closes: S-n for the security review, E-n for the
external review, V-n for the visual review where a server change backs it (n is the
item's place in that review's list of findings). Every test drives the real handlers
or the real service functions; the controllers, the gateway and the kernel are never
replaced, only spied on.
"""

import json
import logging
import os
import re
import subprocess
import sys
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import pytest

import corridor.web as web_package
from corridor.gateway.service import Gateway
from corridor.kernel.binding import GREEN
from corridor.sim.device import Device
from corridor.ui.controller import DemoController
from corridor.ui.live_runtime import access_proof
from corridor.web import compat, errors, handlers, lab, service, tasks
from corridor.web.app import SERVER_OPTIONS
from tests.suggest_fakes import TRANSFER, UNCLEAR, suggest_rule
from tests.unit.test_ui_live_mode import ACCESS_CODE, FakeModel, environ
from tests.unit.test_ui_scam_friction import CLARIFY_PLAIN, NEW, SAFE, Spy
from tests.web.conftest import ROOT, WebClient
from tests.web.test_api_chat import CODE, Browser
from tests.web.test_tasks import BIRTHDAY, HELD, RED, YELLOW, inv22, logged_in  # noqa: F401

DIRECTED = "对方让我给新联系人转480元"
POLICE_BIRTHDAY = "警察让我帮我准备老婆的生日"
EXT = frozenset({
    "tab:accounts",
    "tab:wealth",
    "tab:plans",
    "shortcut:billing",
    "shortcut:subscriptions",
    "shortcut:birthday",
    "plan_select",
    "compensate",
})
HEX = re.compile(r"[0-9a-f]{16,}")


@pytest.fixture
def web_settings():
    # The walk below also drives the authorization lab, which the submission build closes
    # (RW-4''): it is opened explicitly here.
    return {"features": web_package.FEATURES | EXT | {lab.FEATURE}}


@pytest.fixture
def browser(web_app):
    page = Browser(web_app)
    page.login()
    return page


def spied(monkeypatch, *names):
    """Counts of the simulated device's and the gateway's own entry points, by name."""
    counts = Counter()
    owners = {"approve": Device, "complete_mfa": Device, "confirm_input": Device}
    owners.update({"dispatch": Gateway, "request_mfa": Gateway, "submit_mfa": Gateway})
    for name in names:
        owner = owners[name]
        original = getattr(owner, name)

        def wrapped(obj, *args, _name=name, _original=original, **kwargs):
            counts[_name] += 1
            return _original(obj, *args, **kwargs)

        monkeypatch.setattr(owner, name, wrapped)
    return counts


def held_body(page, tkey):
    gesture = page.task(tkey)["gesture"]
    return {"gesture": gesture["token"], "card_version": gesture["card_version"]}


def newest(page, source):
    found = [task for task in page.state["device"]["tasks"] if task["source"] == source]
    return max(found, key=lambda task: int(task["key"][2:]))["key"]


def finish(page, tkey):
    """Every remaining whitelisted step of one task, the code read from the phone."""
    for _ in range(4):
        task = page.task(tkey)
        if task["gesture"] is None:
            return task["step"]
        op = task["gesture"]["op"]
        page.device(tkey, op, **({"code": page.sms_code()} if op == "mfa_verify" else {}))
    return page.task(tkey)["step"]


# --- E-1: a later scam cue closes an older, unfinished payment ----------------------------


@pytest.mark.parametrize("later", [DIRECTED, HELD], ids=["paused-check", "scam-phrase"])
def test_a_later_scam_cue_closes_an_older_payment(browser, monkeypatch, later):
    older = browser.send(YELLOW)
    saved = held_body(browser, "T" + older)
    browser.send(later)
    counts = spied(monkeypatch, "approve", "dispatch")
    data = browser.post(f"/api/chat/responses/{older}/current", {})
    task = browser.task("T" + older)
    assert task["step"] == "gate_later" and task["gesture"] is None
    assert "风险" in task["step_text"] and task["cancel"] is not None
    assert data["state"]["chat"]["current"] == older
    refused = browser.fail(f"/api/device/tasks/T{older}/confirm", saved, "gesture_unknown")
    assert refused["state"]["device"]["tasks"][0]["step"] == "gate_later"
    assert counts == Counter()


@pytest.mark.parametrize("later", ["查询余额", NEW], ids=["balance", "new-payee-no-cue"])
def test_an_unrelated_later_reply_leaves_the_older_payment_open(browser, later):
    older = browser.send(YELLOW)
    browser.send(later)
    browser.post(f"/api/chat/responses/{older}/current", {})
    task = browser.task("T" + older)
    assert task["step"] == "confirm" and task["gesture"] is not None
    assert browser.device("T" + older, "confirm")["result"] == {"status": "settled"}


def test_a_later_cue_answered_safely_still_closes_the_older_payment(browser, monkeypatch):
    """R2-E1: the later card's own answers clear only that card, never an older payment."""
    older = browser.send(YELLOW)
    saved = held_body(browser, "T" + older)
    gated = browser.send("工作人员说给新联系人转480元")
    browser.answer(gated, SAFE)
    assert browser.response(gated)["gate"]["decision"] == "proceed"
    counts = spied(monkeypatch, "approve", "dispatch")
    browser.post(f"/api/chat/responses/{older}/current", {})
    task = browser.task("T" + older)
    assert task["step"] == "gate_later" and task["gesture"] is None
    assert task["cancel"] is not None
    browser.fail(f"/api/device/tasks/T{older}/confirm", saved, "gesture_unknown")
    assert counts == Counter()


# --- E-7: plans that pay are not signed when their words carry a scam cue -----------------


def test_a_chat_plan_with_a_scam_cue_offers_no_selection_and_no_token(browser, monkeypatch):
    counts = spied(monkeypatch, "approve", "dispatch")
    tkey = "T" + browser.send(POLICE_BIRTHDAY)
    task = browser.task(tkey)
    assert task["step"] == "gate_plan" and task["gesture"] is None
    body = {"card_version": task["nodes_version"], "nodes": ["n0", "n1", "n2", "n3"]}
    browser.fail(f"/api/device/tasks/{tkey}/nodes", body, "not_applicable")
    assert counts == Counter()
    plain = "T" + browser.send(BIRTHDAY)
    assert browser.task(plain)["step"] == "select_nodes"


def test_a_birthday_draft_from_cued_words_gets_no_token(browser, monkeypatch):
    rkey = browser.send("警察让我帮我准备生日")
    birthday = browser.response(rkey)["birthday"]
    choice = next(item["key"] for item in birthday["choices"] if item["enabled"])
    browser.post(f"/api/chat/responses/{rkey}/birthday", {"choice": choice})
    (task,) = [item for item in browser.state["device"]["tasks"] if item["source"] == "plans"]
    assert task["step"] == "gate_plan" and task["gesture"] is None


def test_compensation_acts_only_on_the_plan_the_panel_showed(browser, monkeypatch):
    before = browser.tab("operator")["compensation"]["compensate_version"]
    plan = "T" + browser.send(BIRTHDAY)
    browser.post(
        f"/api/device/tasks/{plan}/nodes",
        {"card_version": browser.task(plan)["nodes_version"], "nodes": ["n0", "n1", "n2", "n3"]},
    )
    browser.device(plan, "confirm")
    browser.send("停止")
    calls = Counter()
    original = DemoController.compensate

    def counted(self, plan_id):
        calls[plan_id] += 1
        return original(self, plan_id)

    monkeypatch.setattr(DemoController, "compensate", counted)
    # The version was read before the plan existed: refused, nothing prepared.
    browser.fail("/api/chat/compensate", {"plan_version": before}, "card_changed")
    assert calls == Counter() and "compensation" not in browser.ws.business
    panel = browser.state["chat"]["task_panel"]["controls"]
    assert panel["compensate_hint"] and panel["compensate_version"] != before
    browser.post("/api/chat/compensate", {"plan_version": panel["compensate_version"]})
    (plan_id,) = calls
    assert plan_id == tasks.compensation_plan(browser.ws)


def test_business_pages_pay_no_new_recipient(web_app, monkeypatch):
    """Subscriptions, wealth and compensation dispatch only their own kinds, never to a
    contact: the business pages open no path to a new recipient. Each page runs in its
    own session, so the approval-rate lock (APR-LOCK) does not end the walk early."""
    kinds = []
    original = Gateway.dispatch

    def recorded(self, action, *args, **kwargs):
        kinds.append((str(action.kind), action.args.model_dump(mode="json")))
        return original(self, action, *args, **kwargs)

    def page():
        opened = Browser(web_app)
        opened.login()
        return opened

    planned = page()
    plan = "T" + planned.send(BIRTHDAY)
    body = {"card_version": planned.task(plan)["nodes_version"], "nodes": ["n0", "n1", "n2", "n3"]}
    planned.post(f"/api/device/tasks/{plan}/nodes", body)
    planned.device(plan, "confirm")
    planned.send("停止")
    monkeypatch.setattr(Gateway, "dispatch", recorded)
    planned.compensate()
    assert finish(planned, newest(planned, "compensation")) == "done"
    subscribed = page()
    subscribed.post("/api/subscriptions/query", {})
    assert finish(subscribed, newest(subscribed, "subscriptions")) == "done"
    subscribed.pick("/api/subscriptions/cancel", "c0")
    assert finish(subscribed, newest(subscribed, "subscriptions")) == "done"
    invested = page()
    invested.post("/api/wealth/compare", {"objective": "net_return"})
    invested.pick("/api/wealth/purchase", None)
    assert finish(invested, newest(invested, "wealth")) == "done"
    invested.pick("/api/wealth/redeem", "c0")
    assert finish(invested, newest(invested, "wealth")) == "done"
    allowed = {
        "subscription_query",
        "cancel_subscription",
        "purchase_product",
        "redeem_product",
        "unlock_funds",
    }
    # The comparison is a green read the kernel executes without a card (GREEN).
    assert {kind for kind, _ in kinds} - GREEN == allowed, kinds
    assert all(args.get("contact_ref") is None for _, args in kinds)


# --- E-8: only a device gesture approves, asks for a code or dispatches -------------------

GESTURES = frozenset({
    "/api/device/tasks/{tkey}/confirm",
    "/api/device/tasks/{tkey}/mfa/request",
    "/api/device/tasks/{tkey}/mfa/verify",
    "/api/lab/gesture",
})
APPROVALS = ("approve", "complete_mfa", "request_mfa", "submit_mfa", "dispatch")
# Routes on which the kernel runs a green read (GREEN) without a card or a gesture, as
# the Streamlit pages do: a query, never a payment.
GREEN_READ_ROUTES = frozenset({
    "/api/billing/analyze",
    "/api/wealth/compare",
    "/api/wealth/risk",
    # On the preset's own isolated controller (INV-11.5), not the session's.
    "/api/presets/suitability-r4",
})
# POST routes the walk below cannot complete: the live switch needs an access code, which
# tests/web/test_api_live.py configures and drives to completion (no device or gateway
# entry point is among its calls).
UNREACHED = frozenset({
    "/api/live/challenge",
    "/api/live/unlock",
    "/api/live/select",
    "/api/live/toggle",
})
# The previews that sign the simulated device's input record (Device.confirm_input): an
# input signature, not an approval, and no proof that a person checked the input.
INPUT_SIGNED = frozenset({
    "/api/wealth/compare",
    "/api/wealth/purchase",
    "/api/wealth/risk",
    "/api/presets/suitability-r4",
})


def route_of(path):
    path = path.split("?", 1)[0]
    path = re.sub(r"/T[RB]\d+(?=/|$)", "/{tkey}", path)
    return re.sub(r"/R\d+(?=/|$)", "/{rkey}", path)


def test_no_request_but_a_device_gesture_approves_or_dispatches(web_app, monkeypatch, tmp_path):
    """Every POST route runs with payments and plans pending; only explicit device
    gestures reach the device's approval, the gateway's code steps or a dispatch.
    The isolated authorization lab uses the same explicit gesture services."""
    seen: dict[str, Counter] = {}
    inputs: dict[str, list] = {}
    dispatched: dict[str, list] = {}
    succeeded: set[str] = set()
    route = {"now": None}
    owners = {name: Device for name in ("approve", "complete_mfa", "confirm_input")}
    owners.update({name: Gateway for name in ("dispatch", "request_mfa", "submit_mfa")})
    for name, owner in owners.items():
        original = getattr(owner, name)

        def wrapped(obj, *args, _name=name, _original=original, **kwargs):
            if _name == "dispatch" and str(args[0].kind) in GREEN:
                # A green read runs without a card or a gesture, as on the page.
                dispatched.setdefault(route["now"], []).append(str(args[0].kind))
                return _original(obj, *args, **kwargs)
            seen.setdefault(route["now"], Counter())[_name] += 1
            if _name == "confirm_input":
                inputs.setdefault(route["now"], []).append(kwargs.get("slot"))
            return _original(obj, *args, **kwargs)

        monkeypatch.setattr(owner, name, wrapped)
    perform = handlers.Write.perform

    def traced(self, ws, action, values, path):
        route["now"] = route_of(self.request.path)
        seen.setdefault(route["now"], Counter())
        try:
            result = perform(self, ws, action, values, path)
            succeeded.add(route["now"])
            return result
        finally:
            route["now"] = None

    monkeypatch.setattr(handlers.Write, "perform", traced)

    def post(page, path, body=None):
        response = page.client.post(path, body or {})
        page.bodies.append((path, response.body.decode("utf-8")))
        data = page.client.json(response)
        if "state" in data:
            page.state = data["state"]
        return data

    page = Browser(web_app)
    page.login()
    for view in ("customer", "judge"):
        post(page, "/api/session/view", {"view": view})
    for locale in ("en", "zh-CN"):
        post(page, "/api/session/locale", {"locale": locale})
    yellow = page.send(YELLOW)
    page.send(RED)
    plan = "T" + page.send(BIRTHDAY)
    post(
        page,
        f"/api/device/tasks/{plan}/nodes",
        {"card_version": page.task(plan)["nodes_version"], "nodes": ["n0", "n1"]},
    )
    clarify = page.send(CLARIFY_PLAIN)
    post(page, f"/api/chat/responses/{clarify}/recipient", {"choice": "c0"})
    # Scripted: no model suggestion is listed, so there is nothing to send.
    choice = {"choice": "c0", "list_version": "0" * 32, "client_msg_id": "suggest-r1-0001"}
    post(page, f"/api/chat/responses/{clarify}/suggestion", choice)
    gated = page.send(NEW)
    # Someone is guiding: the gate pauses, so the retry gesture runs to completion.
    page.answer(gated, {"verified": "yes", "guided": "yes"})
    version = page.gate(gated)["version"]
    post(page, f"/api/chat/responses/{gated}/gate/retry", {"gate_version": version})
    page.answer(gated, SAFE)
    birthdays = page.send("帮我准备生日")
    post(page, f"/api/chat/responses/{birthdays}/birthday", {"choice": "c1"})
    post(page, f"/api/chat/responses/{birthdays}/aa", {"choices": ["c0"]})
    for key in ("transfer", "scam_drill"):
        page.shortcut(key)
    post(page, f"/api/chat/responses/{yellow}/current", {})
    post(page, "/api/subscriptions/query", {})
    query = newest(page, "subscriptions")
    page.device(query, "confirm")
    page.pick("/api/subscriptions/cancel", "c0")
    post(page, "/api/billing/analyze", {})
    post(page, "/api/wealth/compare", {"objective": "net_return"})
    page.pick("/api/wealth/purchase", None)
    page.pick("/api/wealth/redeem", "c0")
    post(page, "/api/presets/suitability-r4", {})
    for path, body in (
        ("/api/live/challenge", {}),
        ("/api/live/unlock", {"nonce": "0" * 32, "proof": "0" * 64}),
        ("/api/live/select", {"deployment": compat.DEPLOYMENTS[0].id}),
        ("/api/live/toggle", {"on": False}),
    ):
        post(page, path, body)
    post(page, f"/api/device/tasks/T{yellow}/cancel", {})
    for path in ("/api/chat/handoff", "/api/chat/stop", "/api/stop"):
        post(page, path, {})
    # Compensation needs a dispatched plan; its own session keeps the approval-rate lock
    # (APR-LOCK) of the walk above out of the way.
    other = Browser(web_app)
    other.login()
    planned = "T" + other.send(BIRTHDAY)
    other.post(
        f"/api/device/tasks/{planned}/nodes",
        {"card_version": other.task(planned)["nodes_version"], "nodes": ["n0", "n1", "n2", "n3"]},
    )
    other.device(planned, "confirm")
    other.send("停止")
    other.compensate()
    selection = Browser(web_app)
    selection.login()
    rkey = selection.send("取消订阅")
    listed = selection.response(rkey)["subscription_select"]
    post(
        selection,
        f"/api/chat/responses/{rkey}/subscription",
        {
            "choice": "c0",
            "list_version": listed["list_version"],
        },
    )
    schedule = Browser(web_app)
    schedule.login()
    created = schedule.send("每月5号给房东转500元")
    schedule.red("T" + created)
    listed_schedule = schedule.send("取消每月5号给房东的转账")
    schedule_vm = schedule.response(listed_schedule)["schedule_cancel_select"]
    post(
        schedule,
        f"/api/chat/responses/{listed_schedule}/schedule-cancel",
        {"choice": "c0", "list_version": schedule_vm["list_version"]},
    )
    # The added public entries must complete, while signing only explicit answers
    # and leaving every card's approval and MFA to the existing device gestures.
    from tests.web.test_public_completeness import card_body

    controls = Browser(web_app)
    controls.login()
    aa = controls.send("这顿饭300我们3个人AA")
    post(controls, f"/api/chat/responses/{aa}/aa", {"choices": ["c0", "c1"]})
    post(controls, "/api/cards/preview", card_body(controls, "raise_card_limit", limit="3000"))
    form = controls.tab("wealth")["questionnaire"]
    post(controls, "/api/wealth/risk/retake", {"version": form["list_version"]})
    form = controls.tab("wealth")["questionnaire"]
    post(controls, "/api/wealth/risk", {"version": form["list_version"], "answers": [0] * 5})
    # Lab setup and withdrawal only prepare cards. The saved old request is tried by
    # a separate click and must fail before any approval/MFA/dispatch call.
    experiment = Browser(web_app)
    experiment.login()
    lab_vm = post(experiment, "/api/lab/start", {"mode": "scripted", "scenario": "red"})["lab"]
    lab_generation = {"lab_generation": lab_vm["session_generation"]}
    lab_vm = post(experiment, "/api/lab/change", lab_generation)["lab"]
    lab_vm = post(experiment, "/api/lab/try-previous", lab_generation)["lab"]
    assert lab_vm["last_attempt"]["status"] == "refused"
    assert lab_vm["proof"]["dispatches"] == 0

    def lab_click(**extra):
        nonlocal lab_vm
        task = next(item for item in lab_vm["state"]["device"]["tasks"] if item["current"])
        gesture = task["gesture"]
        lab_vm = post(
            experiment,
            "/api/lab/gesture",
            {
                **lab_generation,
                "tkey": task["key"],
                "op": gesture["op"],
                "gesture": gesture["token"],
                "card_version": gesture["card_version"],
                **extra,
            },
        )["lab"]

    lab_click()
    lab_click()
    # The experiment's code is read where a person reads it: its own phone page (R2-S2).
    pair = experiment.ws.demos[lab.SLOT]["session"].pair
    phone = experiment.client.json(experiment.client.get(f"/api/phone/{pair}"))
    code = CODE.search(next(item["text"] for item in phone["messages"] if item["latest"])).group()
    lab_click(code=code)
    assert lab_vm["proof"]["dispatches"] == 0
    lab_click()
    assert lab_vm["proof"]["dispatches"] == 1
    # The suggestion route completes only with a live model that answers the suggestion
    # step (scripted, above, it had nothing to send): the test-only fake deployment lists a
    # transfer under a plain clarification, and the click sends its sentence as a message,
    # which prepares a card and approves nothing (R2-E14: the route runs to completion
    # once). The switch is configured last, after the scripted walk above.
    for name, value in environ(tmp_path).items():
        monkeypatch.setenv(name, value)
    FakeModel(monkeypatch, rule=suggest_rule(TRANSFER))
    suggesting = Browser(web_app)
    suggesting.login()
    value = post(suggesting, "/api/live/challenge", {})["nonce"]
    proof = {"nonce": value, "proof": access_proof(ACCESS_CODE, value)}
    assert post(suggesting, "/api/live/unlock", proof)["result"] == "accepted"
    assert post(suggesting, "/api/live/toggle", {"on": True})["live"]["toggle"]["on"] is True
    unclear = suggesting.send(UNCLEAR)
    offered = suggesting.response(unclear)["suggestion_select"]
    assert [item["state"] for item in offered["choices"]] == ["offered"]
    sent = post(
        suggesting,
        f"/api/chat/responses/{unclear}/suggestion",
        {
            "choice": "c0",
            "list_version": offered["list_version"],
            "client_msg_id": "suggest-r1-0002",
        },
    )
    assert sent["ok"] is True and sent["rkey"] != unclear, sent
    assert sent["state"]["chat"]["current"] == sent["rkey"]
    every = set(handlers.POSTS) - GESTURES
    assert set(seen) - {None} >= every, every - set(seen)
    for path, counts in seen.items():
        if path in GESTURES:
            continue
        assert all(counts[name] == 0 for name in APPROVALS), (path, counts)
    assert sum(seen.get(path, Counter())["approve"] for path in GESTURES) > 0
    signed = {path for path, slots in inputs.items() if slots and path not in GESTURES}
    assert signed == INPUT_SIGNED, (signed, inputs)
    assert inputs["/api/wealth/risk"] == ["answers"]
    assert {slot for path in INPUT_SIGNED - {"/api/wealth/risk"} for slot in inputs[path]} <= {
        "preference",
        "amount",
    }
    # R2-E14: the exemptions are explicit. Green reads (GREEN) run without a card on
    # these routes only; and every route ran to completion at least once, so its zero
    # counts above are not only those of a refusal branch.
    green = {path for path, kinds in dispatched.items() if kinds and path not in GESTURES}
    assert green == GREEN_READ_ROUTES, (green, dispatched)
    assert set(handlers.POSTS) - UNREACHED <= succeeded, set(handlers.POSTS) - UNREACHED - succeeded


# --- E-2: a business choice holds only for the list the page showed -----------------------


def test_a_changed_subscription_list_refuses_an_old_choice(monkeypatch):
    harness = logged_in()
    shown = ["S1", "S2", "S3"]
    version = tasks.list_version(harness.ws, "subscriptions", shown)
    monkeypatch.setattr(service, "_cancellable", lambda ws: ["S2", "S3", "S4"])
    called = []
    monkeypatch.setattr(
        harness.controller, "cancel_subscription", lambda *args, **kw: called.append(args)
    )
    with pytest.raises(errors.WebError) as caught:
        service.subscriptions_cancel(harness.ws, "c2", version)
    assert caught.value.code == "card_changed" and called == []
    assert not [task for task in harness.ws.tasks.values() if task.source == "subscriptions"]


def test_another_comparison_refuses_an_old_purchase_choice(browser, monkeypatch):
    browser.post("/api/wealth/compare", {"objective": "net_return"})
    old = browser.listed("wealth")
    browser.post("/api/wealth/compare", {"objective": "liquidity"})
    calls = []
    monkeypatch.setattr(
        DemoController, "preview_investment", lambda self, *a, **k: calls.append(a) or {}
    )
    browser.fail("/api/wealth/purchase", {"choice": "c0", "list_version": old}, "card_changed")
    assert calls == [] and browser.ws.business.get("investment_pending") is None


def test_a_new_position_refuses_an_old_redemption_choice(browser, monkeypatch):
    old = browser.listed("positions")
    browser.post("/api/wealth/compare", {"objective": "net_return"})
    browser.pick("/api/wealth/purchase", None)
    purchase = [item for item in browser.state["device"]["tasks"] if item["source"] == "wealth"]
    key = purchase[0]["key"]
    for _ in range(4):
        task = browser.task(key)
        if task["gesture"] is None:
            break
        extra = {"code": browser.sms_code()} if task["gesture"]["op"] == "mfa_verify" else {}
        browser.device(key, task["gesture"]["op"], **extra)
    assert len(tasks.positions(browser.ws)) == 2
    called = []
    monkeypatch.setattr(DemoController, "redeem", lambda self, *a, **k: called.append(a) or {})
    browser.fail("/api/wealth/redeem", {"choice": "c0", "list_version": old}, "card_changed")
    assert called == []


# --- E-3: a cancel or redemption card names its signed object ------------------------------


@pytest.mark.parametrize("view", ["customer", "judge"])
def test_cancel_and_redemption_cards_name_the_signed_object(web_app, monkeypatch, view):
    page = Browser(web_app, view=view)
    page.login()
    signed = []
    original = Device.approve

    def approve(self, action, cert, card, **kwargs):
        signed.append(card)
        return original(self, action, cert, card, **kwargs)

    monkeypatch.setattr(Device, "approve", approve)
    page.post("/api/subscriptions/query", {})
    query = [task for task in page.state["device"]["tasks"] if task["source"] == "subscriptions"]
    page.device(query[0]["key"], "confirm")
    page.pick("/api/subscriptions/cancel", "c0")
    page.pick("/api/wealth/redeem", "c0")
    shown = {task["key"]: task for task in page.state["device"]["tasks"] if task["cards"]}
    for task in shown.values():
        (card,) = task["cards"]
        identity = page.ws.tasks[task["key"]].identity
        (canonical,) = compat.authoritative_cards(page.ws.controller, identity)
        field = {"cancel_subscription": "authorization_ref", "redeem_product": "position_ref"}
        name = field.get(canonical["kind"])
        if name is None:
            continue
        row = next(fact for fact in card["facts"] if fact["key"] == f"objects.{name}")
        reference = canonical["objects"][name]
        assert (reference in row["value"]) is (view == "judge"), row
        page.device(task["key"], "confirm")
        assert signed[-1]["objects"][name] == reference


# --- E-4: the chat sync checks the pending explicitly, also under python -O ----------------


def sync_refused(monkeypatch):
    harness = logged_in()
    tkey = "T" + harness.send(RED)
    harness.confirm(tkey)
    harness.mfa_request(tkey)
    assert harness.step(tkey) == "mfa_code"
    task = harness.ws.tasks[tkey]
    clicks = []
    monkeypatch.setattr(
        compat.IMAdapter, "click", lambda self, operation, rid, **kw: clicks.append(operation)
    )
    # The controller claims a verification its pending does not hold.
    monkeypatch.setattr(
        DemoController,
        "complete_mfa",
        lambda self, pending, *, code=None: {"status": "mfa_verified"},
    )
    held = harness.ws.gestures[tkey]
    version = tasks.card_version(harness.ws, held.digest)
    with pytest.raises(errors.WebError) as caught:
        service.device_mfa_verify(harness.ws, tkey, held.token, version, "123456")
    assert caught.value.code == "outcome_unknown"
    assert clicks == [] and task.phase == "closed_unknown"
    assert compat.pending_verified(harness.controller, task.identity) is False


def test_a_false_verification_never_reaches_the_chat_sync(monkeypatch):
    sync_refused(monkeypatch)


def test_the_sync_refuses_a_pending_that_is_not_the_replys_own(monkeypatch):
    harness = logged_in()
    tkey = "T" + harness.send(RED)
    task = harness.ws.tasks[tkey]
    clicks = []
    monkeypatch.setattr(compat.IMAdapter, "click", lambda *args, **kw: clicks.append(args))
    monkeypatch.setattr(compat, "pending_verified", lambda controller, identity: True)
    other = tasks.DeviceTask("TR9", "chat", task.rkey, task.rid, "another-pending", "envelope")
    with pytest.raises(errors.WebError) as caught:
        service._sync_verified(harness.ws, other)
    assert caught.value.code == "outcome_unknown" and clicks == []


@pytest.mark.skipif(sys.flags.optimize > 0, reason="already the optimized run")
def test_the_sync_check_holds_under_python_optimizations():
    """The same refusal with ``python -O``, where an ``assert`` would have vanished."""
    command = [
        sys.executable,
        "-O",
        "-m",
        "pytest",
        "-p",
        "no:cacheprovider",
        "-q",
        f"{__file__}::test_a_false_verification_never_reaches_the_chat_sync",
    ]
    env = {key: value for key, value in os.environ.items() if not key.startswith("PYTEST_")}
    done = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True, timeout=600)
    assert done.returncode == 0, done.stdout[-2000:] + done.stderr[-2000:]
    assert "1 passed" in done.stdout


# --- E-5: the token's own digest is compared, not only the client's version ---------------


def test_a_token_for_one_card_is_refused_with_the_version_of_another():
    harness = logged_in()
    tkey = "T" + harness.send(YELLOW)
    held = harness.ws.gestures[tkey]
    current = held.digest
    # The token as if it had been issued for another card; the version is the current one.
    harness.ws.gestures[tkey] = type(held)(
        held.token, held.op, held.step, "0" * 64, held.generation
    )
    with pytest.raises(errors.WebError) as caught:
        tasks.verify(
            harness.ws, tkey, "confirm", held.token, tasks.card_version(harness.ws, current)
        )
    assert caught.value.code == "card_changed"
    assert len(tasks.card_version(harness.ws, current)) == 32


# --- E-6: an unexpected failure after a call distrusts the session -------------------------


def test_an_unexpected_failure_after_a_state_change_revokes_every_token(browser, monkeypatch):
    first = browser.send(YELLOW)
    second = browser.send(RED)
    saved = held_body(browser, "T" + second)
    counts = spied(monkeypatch, "approve", "dispatch", "request_mfa")

    def broken(ws, rkey):
        ws.current["domestic"] = ws.rkeys[rkey]
        raise TypeError("after a change")

    monkeypatch.setattr(service, "make_current", broken)
    data = browser.post(f"/api/chat/responses/{first}/current", {}, expect=500)
    assert data["error"]["code"] == "outcome_unknown" and browser.ws.untrusted
    assert browser.ws.gestures == {} and browser.ws.gate_eval == {}
    assert {task["step"] for task in data["state"]["device"]["tasks"]} == {"closed_unknown"}
    assert all(task["gesture"] is None for task in data["state"]["device"]["tasks"])
    browser.fail(f"/api/device/tasks/T{second}/confirm", saved, "not_applicable")
    message = {"text": YELLOW, "external_files": 0, "client_msg_id": "after-0001"}
    browser.fail("/api/chat/message", message, "not_applicable")
    assert counts == Counter()
    browser.post(f"/api/device/tasks/T{second}/cancel", {})
    browser.post("/api/session/reset", {})
    browser.login()
    assert not browser.ws.untrusted
    assert browser.task("T" + browser.send(YELLOW))["gesture"] is not None


def test_a_gate_record_holds_only_for_its_reply_and_words():
    harness = logged_in()
    rkey = harness.send(YELLOW)
    tkey = "T" + rkey
    assert harness.step(tkey) == "confirm"
    turn = harness.ws.turn_of(rkey)
    turn.hint = turn.hint + " "
    assert harness.step(tkey) == "gate_unevaluated"


def test_an_unknown_status_is_never_passed_on():
    with pytest.raises(errors.WebOutcomeUnknown):
        handlers._status({"status": "/secret/path"})
    assert handlers._status({"status": "settled"}) == {"status": "settled"}
    from corridor.web import contracts

    assert set(contracts.ResultStatus.__args__) == handlers.STATUSES


# --- E-10: a plan's versions bind its ordered node ids --------------------------------------


def test_a_reordered_plan_changes_its_versions(monkeypatch):
    harness = logged_in()
    tkey = "T" + harness.send(BIRTHDAY)
    task = harness.ws.tasks[tkey]
    info = tasks.task_step(harness.ws, task)
    before = (tasks.nodes_version(harness.ws, task), tasks.digest(harness.ws, task, info))
    original = compat.plan_entry

    def reordered(controller, identity):
        entry = original(controller, identity)
        return entry and {**entry, "node_ids": entry["node_ids"][::-1]}

    monkeypatch.setattr(compat, "plan_entry", reordered)
    after = (tasks.nodes_version(harness.ws, task), tasks.digest(harness.ws, task, info))
    assert after[0] != before[0] and after[1] != before[1]


# --- E-11 and E-13: customer metadata, canaries and the SMS code --------------------------

ALLOWED_HEX = (
    "card_version",
    "nodes_version",
    "cancel_version",
    "candidates_version",
    "positions_version",
    "compensate_version",
)
# C60 adds two session-keyed form bindings; no other list field gets an exemption.
FORM_BINDINGS = {
    "body.tab.vm.card_controls.list_version",
    "body.tab.vm.questionnaire.list_version",
}


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


def test_customer_bodies_carry_hex_only_in_whitelisted_version_fields(web_app):
    page = Browser(web_app, view="customer")
    page.login()
    page.send(YELLOW)
    page.device("TR1", "confirm")
    red = "T" + page.send(RED)
    page.red(red)
    page.send(DIRECTED)
    page.send(HELD)
    page.post("/api/subscriptions/query", {})
    page.post("/api/wealth/compare", {"objective": "net_return"})
    page.pick("/api/wealth/purchase", None)
    for name in ("accounts", "wealth", "plans", "operator"):
        page.tab(name)
    page.post("/api/chat/stop", {})
    bad = []
    for path, body in page.bodies:
        if path.startswith("/api/phone/"):
            continue
        for found in hex_paths(json.loads(body)):
            if found not in FORM_BINDINGS and not found.rsplit(".", 1)[-1].startswith(ALLOWED_HEX):
                bad.append((path, found))
    assert not bad, bad[:10]


def test_canaries_in_results_and_exceptions_never_reach_a_body_or_a_log(
    browser, monkeypatch, caplog
):
    canary = "/secret-root/APR-SECRET-sk-0123456789abcdef"
    original = DemoController.approve_pending

    def noisy(self, pending_id):
        result = original(self, pending_id)
        return {**result, "internal_path": canary, "trace": canary}

    monkeypatch.setattr(DemoController, "approve_pending", noisy)
    caplog.set_level(logging.DEBUG)
    browser.post("/api/subscriptions/query", {})
    (task,) = [item for item in browser.state["device"]["tasks"] if item["key"].startswith("TB")]
    browser.device(task["key"], "confirm")

    def broken(*args, **kwargs):
        raise RuntimeError(canary)

    monkeypatch.setattr(service, "billing_analyze", broken)
    browser.post("/api/billing/analyze", {}, expect=500)
    bodies = " ".join(body for _, body in browser.bodies)
    assert canary not in bodies and "RuntimeError" not in bodies
    assert "/api/billing/analyze" in caplog.text
    assert canary not in caplog.text


def test_a_code_appears_only_on_the_simulated_phone(browser, caplog):
    caplog.set_level(logging.DEBUG)
    tkey = "T" + browser.send(RED)
    browser.device(tkey, "confirm")
    browser.device(tkey, "mfa_request")
    code = browser.sms_code()
    pair = browser.pair()
    browser.get(f"/api/evidence/responses/{tkey[1:]}")
    wrong = "000000" if code != "000000" else "111111"
    browser.post(
        f"/api/device/tasks/{tkey}/mfa/verify", {**held_body(browser, tkey), "code": wrong}
    )
    browser.device(tkey, "mfa_verify", code=browser.sms_code())
    browser.device(tkey, "confirm")
    for path, body in browser.bodies:
        assert re.search(rf"(?<![0-9]){code}(?![0-9])", body) is None, path
    # The log canary is not vacuous: the access lines did reach caplog (R2 suggestion).
    assert "/api/device/tasks/{tkey}/mfa/verify" in caplog.text
    assert code not in caplog.text and pair not in caplog.text
    phone = browser.client.json(browser.client.get(f"/api/phone/{pair}"))
    used = [item for item in phone["messages"] if item["status"] == "used"]
    assert used and used[0]["status_text"] and code not in json.dumps(phone)


def test_cancel_reset_and_lockout_leave_no_active_code(browser):
    tkey = "T" + browser.send(RED)
    browser.device(tkey, "confirm")
    browser.device(tkey, "mfa_request")
    pair = browser.pair()
    browser.post(f"/api/device/tasks/{tkey}/cancel", {})
    phone = browser.client.json(browser.client.get(f"/api/phone/{pair}"))
    assert [item["status"] for item in phone["messages"]] == ["expired"]
    assert phone["messages"][0]["status_text"]
    browser.post("/api/session/reset", {})
    gone = browser.client.json(browser.client.get(f"/api/phone/{pair}"))
    assert gone["messages"] == [] and gone["unpaired"]


# --- E-12: GET switches no planner, issues no token and calls no business controller -----


def test_reads_leave_controllers_tokens_and_the_planner_alone(browser, monkeypatch):
    rkey = browser.send(RED)
    browser.device("T" + rkey, "confirm")
    ws = browser.ws
    spy = Spy(ws.controller, monkeypatch)
    before = (
        compat.H(ws.controller.snapshot()),
        dict(ws.gestures),
        dict(ws.gate_eval),
        ws.controller.client,
        ws.revision,
    )
    for path in (
        "/api/state",
        "/api/i18n",
        "/api/tabs/accounts",
        "/api/tabs/wealth",
        "/api/tabs/plans",
        "/api/tabs/operator",
        f"/api/evidence/responses/{rkey}",
        "/api/live",
    ):
        browser.get(path)
    after = (
        compat.H(ws.controller.snapshot()),
        dict(ws.gestures),
        dict(ws.gate_eval),
        ws.controller.client,
        ws.revision,
    )
    assert after == before and spy.total() == 0


def test_a_live_session_is_built_without_touching_its_ledger(monkeypatch, tmp_path):
    """GET /api/live builds a fresh LiveSession when none exists: building it opens no
    ledger and writes nothing; its snapshot reads the spend ledger (the narrowed claim)."""
    from corridor.ui import live_runtime

    opened = []
    original = live_runtime.SpendLedger._connection

    def counted(self):
        opened.append(self.path)
        return original(self)

    monkeypatch.setattr(live_runtime.SpendLedger, "_connection", counted)
    journal = tmp_path / "journal"
    config = compat.LiveConfig(enabled=True, journal_dir=journal)
    session = compat.LiveSession(config, environ={})
    assert opened == [] and not journal.exists()
    session.snapshot()
    assert opened and all(path.parent == journal for path in opened)


# --- E-15: the judge view's journal line names no host path --------------------------------


def test_journal_lines_are_relative_to_the_configured_directory(tmp_path):
    """The judge counters name a journal relative to LIVE_JOURNAL_DIR, never a host path."""
    from corridor.web import live

    journal = tmp_path / "journal"
    session = compat.LiveSession(compat.LiveConfig(enabled=True, journal_dir=journal), environ={})
    session._journals.append(journal / "deployment-a" / "run-0001")
    session._journals.append(tmp_path / "elsewhere" / "run-0002")
    lines = live._counters(session, session.snapshot())
    text = " ".join(lines)
    assert str(tmp_path) not in text and str(tmp_path.as_posix()) not in text
    assert "deployment-a/run-0001" in text and "run-0002" in text


# --- E-16: the transport negatives the review listed ----------------------------------------


def test_another_local_port_cannot_open_a_session(web_app):
    page = WebClient(web_app)
    store = web_app.settings["store"]
    before = len(store)
    for origin in (
        f"http://127.0.0.1:{page.port + 1}",
        "null",
        f"http://localhost:{page.port + 1}",
    ):
        response = page.post("/api/session", {}, headers={"Origin": origin})
        assert response.code == 403 and not response.headers.get_list("Set-Cookie")
    assert len(store) == before


def test_reads_without_a_tab_return_no_state(browser):
    for path in ("/api/state", "/api/live"):
        response = browser.client.get(path, headers={"X-Corridor-Tab": None})
        data = browser.client.json(response)
        assert response.code == 401 and "state" not in data and "live" not in data


def test_an_old_generation_confirm_calls_nothing(browser, monkeypatch):
    rkey = browser.send(YELLOW)
    body = held_body(browser, "T" + rkey)
    old = browser.client.generation
    browser.post("/api/session/reset", {})
    browser.login()
    browser.send(YELLOW)
    counts = spied(monkeypatch, "approve", "dispatch")
    response = browser.client.post(
        f"/api/device/tasks/T{rkey}/confirm", body, headers={"X-Corridor-Generation": old}
    )
    assert response.code == 409
    assert browser.client.json(response)["error"]["code"] == "stale_generation"
    assert counts == Counter()


def test_a_chunked_body_is_limited_like_any_other(browser):
    import http.client

    connection = http.client.HTTPConnection("127.0.0.1", browser.client.port, timeout=30)
    try:
        connection.putrequest("POST", "/api/chat/stop", skip_host=True)
        for name, value in (
            ("Host", browser.client.host),
            ("Origin", browser.client.origin),
            ("Content-Type", "application/json"),
            ("Transfer-Encoding", "chunked"),
            ("Cookie", f"corridor_web={browser.client.browser}"),
            ("X-Corridor-Tab", browser.client.tab),
            ("X-Corridor-Generation", browser.client.generation),
        ):
            connection.putheader(name, value)
        connection.endheaders()
        chunk = b'{"x":"' + b"a" * (70 * 1024) + b'"}'
        connection.send(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n0\r\n\r\n")
        answer = connection.getresponse()
        answer.read()
    finally:
        connection.close()
    assert answer.status == 413


def test_the_server_bounds_bodies_and_their_time():
    # R2-E12: the entry buffers at most 128 KiB; the application parses at most 64 KiB.
    assert SERVER_OPTIONS["max_body_size"] == SERVER_OPTIONS["max_buffer_size"] == 128 * 1024
    assert 0 < SERVER_OPTIONS["body_timeout"] <= 60
    assert 0 < SERVER_OPTIONS["idle_connection_timeout"] <= 300


# --- replay, brute force and duplicate messages (E suggestions) ---------------------------


def test_one_token_sent_twice_at_once_enters_the_controller_once(browser, monkeypatch):
    rkey = browser.send(YELLOW)
    body = held_body(browser, "T" + rkey)
    counts = spied(monkeypatch, "approve")
    barrier = threading.Barrier(2)

    def confirm(_):
        client = WebClient(browser.app)
        client.browser, client.tab = browser.client.browser, browser.client.tab
        client.generation = browser.client.generation
        barrier.wait(10)
        return client.post(f"/api/device/tasks/T{rkey}/confirm", body).code

    with ThreadPoolExecutor(2) as pool:
        codes = sorted(pool.map(confirm, range(2)))
    assert codes[0] == 200 and codes[1] == 409 and counts["approve"] == 1


def test_three_wrong_codes_across_two_challenges_lock_every_task(browser):
    first = "T" + browser.send(RED)
    browser.device(first, "confirm")
    browser.device(first, "mfa_request")
    code = browser.sms_code()
    wrong = "000000" if code != "000000" else "111111"
    old = held_body(browser, first)
    browser.device(first, "mfa_verify", code=wrong)
    # A wrong code re-issues the step's token: the one used before it is refused.
    browser.fail(f"/api/device/tasks/{first}/mfa/verify", {**old, "code": code}, "gesture_mismatch")
    second = "T" + browser.send(RED)
    browser.device(second, "confirm")
    browser.device(second, "mfa_request")
    browser.device(second, "mfa_verify", code=wrong)
    browser.device(second, "mfa_verify", code=wrong)
    yellow = "T" + browser.send(YELLOW)
    assert browser.state["shell"]["safety"]["locked"] is True
    assert browser.task(yellow)["gesture"] is None
    assert all(task["gesture"] is None for task in browser.state["device"]["tasks"])


def test_a_message_id_is_bound_to_its_words(browser):
    body = {"text": YELLOW, "external_files": 0, "client_msg_id": "same-id-0001"}
    first = browser.post("/api/chat/message", body)["rkey"]
    again = browser.post("/api/chat/message", body)["rkey"]
    assert again == first
    browser.fail("/api/chat/message", {**body, "text": RED}, "msg_id_conflict")


# --- security review items with a server change ----------------------------------------------


@pytest.mark.parametrize("view", ["customer", "judge"])
def test_a_yellow_intent_step_says_the_click_signs(web_app, view):
    """S-1: only a red draft is followed by an SMS step."""
    page = Browser(web_app, view=view)
    page.login()
    task = page.task("T" + page.send("冻结我的卡"))
    assert task["step"] == "confirm_intent"
    assert "短信" not in task["step_text"] and "尚未签名" not in task["step_text"]
    assert "签名" in task["step_text"]


def test_a_red_intent_step_keeps_the_sms_sentence(monkeypatch):
    from corridor.web.vm import device

    assert "短信验证码" in device.step_text("confirm_intent", "red")
    assert "短信验证码" not in device.step_text("confirm_intent", "yellow")


def test_a_purchase_after_a_chat_stop_gets_a_live_card(browser):
    """S-3 (P-DIFF-25): a Stop said in the chat retires the controller's pending actions;
    the next business preview asks again instead of showing the dead card."""
    browser.post("/api/wealth/compare", {"objective": "net_return"})
    browser.pick("/api/wealth/purchase", "c0")
    browser.send(YELLOW)
    browser.post("/api/chat/stop", {})
    browser.pick("/api/wealth/purchase", "c0")
    wealth = [task for task in browser.state["device"]["tasks"] if task["source"] == "wealth"]
    newest = max(wealth, key=lambda task: int(task["key"][2:]))
    assert newest["gesture"] is not None, newest["step"]


def test_one_click_over_several_cards_says_so_on_the_button(browser):
    """S-7 (INV-2.8)."""
    tkey = "T" + browser.send(BIRTHDAY)
    body = {"card_version": browser.task(tkey)["nodes_version"], "nodes": ["n0", "n1", "n2", "n3"]}
    browser.post(f"/api/device/tasks/{tkey}/nodes", body)
    assert browser.task(tkey)["gesture"]["label"] == "确认全部 4 项（逐张签名）"


def test_the_judge_red_card_does_not_call_the_check_independent(web_app):
    """S-5: the SMS frame sits beside the code field on the same page."""
    page = Browser(web_app, view="judge")
    page.login()
    card = page.response(page.send(RED))["cards"][0]
    text = json.dumps({"tier": card["tier"], "notes": card["notes"]}, ensure_ascii=False)
    assert "独立" not in text and "independent" not in text


def test_a_settled_reply_keeps_its_kernel_evidence_with_its_true_source(browser):
    """V-2: after dispatch the judge evidence shows the kept kernel view, and (R2-E11)
    names where and when it was read: a one-gesture yellow confirmation is the device
    workflow's record of that confirmation, never "read by the web before dispatch"."""
    rkey = browser.send(YELLOW)
    browser.device("T" + rkey, "confirm")
    evidence = browser.state["evidence"]
    assert evidence["kernel"]["outcome"] == "Accept" and evidence["kernel"]["note"]
    assert evidence["consistency"]["same_business_facts"] is True
    technical = browser.get(f"/api/evidence/responses/{rkey}")["technical"]
    kept = browser.ws.kernel_seen[browser.ws.tasks["T" + rkey].identity]["source"]
    expected = {
        "pending_copy": ("web_copy_read_while_pending", "before_dispatch"),
        "confirmation": ("device_confirmation_record", "after_confirmation"),
    }[kept]
    assert (technical["kernel"]["source"], technical["kernel"]["read_at"]) == expected
