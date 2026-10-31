"""B1: the device endpoints over HTTP (design §2.5.3, §2.5.4; §6.5 negatives).

Codes are read only from the simulated phone's own API; every other response body of a
red flow is scanned for them.
"""

import json
import re

import pytest

from corridor.web import FEATURES, compat
from tests.unit.test_ui_scam_friction import FRAUD, GUIDED, SAFE, Spy, protected, snapshot
from tests.web.test_api_chat import Browser
from tests.web.test_tasks import (
    BIRTHDAY,
    RED,
    REFUSED,
    YELLOW,
    codes_seen,
    inv22,  # noqa: F401  (INV-2.2 probe)
    no_autocomplete,
)

FORGED = "A" * 22


@pytest.fixture
def web_settings():
    return {"features": FEATURES | {"plan_select"}}


@pytest.fixture
def browser(web_app):
    page = Browser(web_app)
    page.login()
    return page


def gesture_body(page, tkey, **extra):
    gesture = page.gesture(tkey)
    return {"gesture": gesture["token"], "card_version": gesture["card_version"], **extra}


def flip(value):
    return ("1" if value[0] != "1" else "2") + value[1:]


class Unchanged:
    def __init__(self, page, tkey, monkeypatch):
        self.page, self.tkey = page, tkey
        ws = page.ws
        self.spy = Spy(ws.controller, monkeypatch)
        self.snapshot = snapshot(ws.controller, ws.session)
        self.protected = protected(ws.controller, ws.tasks[tkey].identity)

    def check(self):
        ws = self.page.ws
        assert self.spy.total() == 0, dict(self.spy.counts)
        assert snapshot(ws.controller, ws.session) == self.snapshot
        assert protected(ws.controller, ws.tasks[self.tkey].identity) == self.protected


def test_yellow_and_red_transfers_keep_the_code_on_the_phone_only(browser, monkeypatch):
    ws = browser.ws
    spy = Spy(ws.controller, monkeypatch)
    seen = codes_seen(ws.controller, monkeypatch)
    yellow = "T" + browser.send(YELLOW)
    assert browser.device(yellow, "confirm")["result"] == {"status": "settled"}
    assert browser.task(yellow)["step"] == "done"
    red = "T" + browser.send(RED)
    browser.device(red, "confirm")
    assert browser.task(red)["step"] == "mfa_request"
    browser.device(red, "mfa_request")
    assert browser.task(red)["step"] == "mfa_code"
    code = browser.sms_code()
    challenge = compat.pending_entry(ws.controller, ws.tasks[red].identity)["challenge_id"]
    assert code == ws.controller.gateway.mfa.test_code(challenge)
    browser.device(red, "mfa_verify", code=code)
    assert browser.task(red)["step"] == "final_confirm"
    assert browser.device(red, "confirm")["result"] == {"status": "settled"}
    assert browser.sms_code() is None
    for path, text in browser.bodies:
        assert not path.startswith("/api/phone/")
        assert code not in text, path
    assert spy.counts["dispatch"] == 2
    no_autocomplete(spy, seen)


def test_forged_replayed_and_tampered_gestures_change_nothing(browser, monkeypatch):
    red = "T" + browser.send(RED)
    body = gesture_body(browser, red)
    guard = Unchanged(browser, red, monkeypatch)
    path = f"/api/device/tasks/{red}"
    data = browser.fail(path + "/confirm", {**body, "gesture": FORGED}, "gesture_mismatch")
    assert data["state"]["device"]["tasks"][0]["gesture"]["token"] == body["gesture"]
    browser.fail(path + "/mfa/request", body, "gesture_mismatch")
    browser.fail(
        path + "/confirm", {**body, "card_version": flip(body["card_version"])}, "card_changed"
    )
    browser.fail("/api/device/tasks/TR9/confirm", body, "not_found", 404)
    guard.check()
    assert browser.post(path + "/confirm", body)["result"] == {"status": "waiting_mfa"}
    replay = browser.post(path + "/confirm", body, expect=409)
    assert replay["error"]["code"] in {"gesture_mismatch", "gesture_closed"}
    assert guard.spy.counts["confirm_transfer"] == 1


def test_another_browsers_keys_act_on_its_own_session_only(web_app, browser, monkeypatch):
    rkey = browser.send(RED)
    body = gesture_body(browser, "T" + rkey)
    other = Browser(web_app)
    other.login()
    other.send(RED)
    guard = Unchanged(browser, "T" + rkey, monkeypatch)
    data = other.fail(f"/api/device/tasks/T{rkey}/confirm", body, "gesture_mismatch")
    assert data["state"]["generation"] != browser.state["generation"]
    guard.check()
    assert browser.post(f"/api/device/tasks/T{rkey}/confirm", body)["result"]["status"] == (
        "waiting_mfa"
    )


@pytest.mark.parametrize(
    "code", ["missing", None, "", "12345", "1234567", "12a456", "１２３４５６", 123456]
)
def test_a_code_that_is_not_six_digits_never_reaches_a_verifier(code, browser, monkeypatch):
    red = "T" + browser.send(RED)
    browser.device(red, "confirm")
    browser.device(red, "mfa_request")
    body = gesture_body(browser, red)
    if code != "missing":
        body["code"] = code
    seen = codes_seen(browser.ws.controller, monkeypatch)
    guard = Unchanged(browser, red, monkeypatch)
    browser.fail(f"/api/device/tasks/{red}/mfa/verify", body, "invalid_input", 422)
    assert seen == [] and guard.spy.counts["complete_mfa"] == 0
    guard.check()
    assert browser.gesture(red)["token"] == body["gesture"]


@pytest.mark.parametrize("field", ["card", "amount", "contact_ref", "action", "nodes"])
def test_a_confirmation_carries_no_card_fields(field, browser):
    yellow = "T" + browser.send(YELLOW)
    body = {**gesture_body(browser, yellow), field: "x"}
    data = browser.post(f"/api/device/tasks/{yellow}/confirm", body, expect=400)
    assert data["error"]["code"] == "unknown_field"
    assert browser.ws.controller.gateway.bank.execute_count == 0


@pytest.mark.parametrize(
    "failure,code,status",
    [(RuntimeError, "operation_failed", 422), (TypeError, "outcome_unknown", 500)],
)
def test_a_call_that_began_and_failed_closes_its_task(failure, code, status, browser, monkeypatch):
    yellow = "T" + browser.send(YELLOW)
    body = gesture_body(browser, yellow)

    def broken(*args, **kwargs):
        raise failure("/secret/path")

    monkeypatch.setattr(browser.ws.controller.workflow, "confirm_transfer", broken)
    data = browser.fail(f"/api/device/tasks/{yellow}/confirm", body, code, status)
    text = json.dumps(data, ensure_ascii=False)
    assert "secret" not in text and "Traceback" not in text
    task = browser.task(yellow)
    assert task["step"] == ("closed_failed" if failure is RuntimeError else "closed_unknown")
    assert task["gesture"] is None and task["cancel"] is not None
    monkeypatch.undo()
    # An unknown result distrusts the whole session (R2-E3): every later gesture is
    # refused before the task is even looked up; a known failure closes only its task.
    refused = "gesture_closed" if failure is RuntimeError else "not_applicable"
    assert browser.ws.untrusted is (failure is not RuntimeError)
    guard = Unchanged(browser, yellow, monkeypatch)
    browser.fail(f"/api/device/tasks/{yellow}/confirm", body, refused)
    browser.fail(f"/api/device/tasks/{yellow}/confirm", {**body, "gesture": FORGED}, refused)
    guard.check()
    browser.post(f"/api/device/tasks/{yellow}/cancel", {})
    assert browser.ws.controller.at_transaction_boundary()


def test_three_wrong_codes_lock_and_a_reset_recovers(web_app, browser, monkeypatch):
    red = "T" + browser.send(RED)
    browser.device(red, "confirm")
    browser.device(red, "mfa_request")
    code = browser.sms_code()
    for value in [item for item in ("000000", "111111", "222222", "333333") if item != code][:3]:
        browser.device(red, "mfa_verify", code=value)
    safety = browser.state["shell"]["safety"]
    assert safety["locked"] is True and safety["text"] and safety["hint"]
    assert browser.task(red)["step"] == "account_locked"
    yellow = "T" + browser.send(YELLOW)
    assert browser.task(yellow)["step"] == "account_locked"
    assert browser.task(yellow)["gesture"] is None
    guard = Unchanged(browser, yellow, monkeypatch)
    browser.fail(
        f"/api/device/tasks/{yellow}/confirm",
        {"gesture": FORGED, "card_version": "0" * 32},
        "gesture_unknown",
    )
    guard.check()
    old_generation = browser.state["generation"]
    browser.post("/api/session/reset", {})
    assert browser.state["generation"] != old_generation
    assert browser.state["shell"]["safety"]["locked"] is False
    browser.login()
    again = "T" + browser.send(YELLOW)
    assert browser.device(again, "confirm")["result"] == {"status": "settled"}


def test_a_refused_proposal_takes_no_gesture(browser, monkeypatch):
    tkey = "T" + browser.send(REFUSED)
    assert browser.device(tkey, "confirm")["result"] == {"status": "rejected"}
    assert browser.task(tkey)["step"] == "refused" and browser.task(tkey)["gesture"] is None
    guard = Unchanged(browser, tkey, monkeypatch)
    forged = {"gesture": FORGED, "card_version": "0" * 32}
    browser.fail(f"/api/device/tasks/{tkey}/mfa/request", forged, "gesture_closed")
    guard.check()


@pytest.mark.parametrize("answers", [None, GUIDED, "stale_version"])
def test_no_gesture_passes_an_unpassed_gate_over_http(answers, browser, monkeypatch):
    rkey = browser.send(FRAUD)
    tkey = "T" + rkey
    if answers == "stale_version":
        browser.answer(rkey, SAFE)
        old = gesture_body(browser, tkey)
        browser.answer(rkey, GUIDED)
    elif answers is not None:
        browser.answer(rkey, answers)
    assert browser.task(tkey)["step"] == "gate_blocked"
    assert browser.task(tkey)["gesture"] is None
    guard = Unchanged(browser, tkey, monkeypatch)
    body = old if answers == "stale_version" else {"gesture": FORGED, "card_version": "0" * 32}
    for path, extra in (("confirm", {}), ("mfa/request", {}), ("mfa/verify", {"code": "123456"})):
        data = browser.post(f"/api/device/tasks/{tkey}/{path}", {**body, **extra}, expect=409)
        assert data["error"]["code"] == "gesture_unknown"
    guard.check()


def test_plan_selection_is_registered_on_the_server(browser, monkeypatch):
    rkey = browser.send(BIRTHDAY)
    tkey = "T" + rkey
    assert browser.task(tkey)["step"] == "select_nodes"
    version = browser.task(tkey)["nodes_version"]
    path = f"/api/device/tasks/{tkey}/nodes"
    browser.post(path, {"card_version": version, "nodes": ["n1"]})
    assert browser.task(tkey)["step"] == "select_nodes" and browser.task(tkey)["gesture"] is None
    assert browser.ws.selection[tkey] and browser.task(tkey)["nodes_version"] == version
    browser.post(path, {"card_version": version, "nodes": ["n0"]})
    subset = gesture_body(browser, tkey)
    assert browser.task(tkey)["step"] == "confirm_subset"
    browser.post(path, {"card_version": version, "nodes": ["n3", "n2", "n1", "n0"]})
    assert browser.task(tkey)["step"] == "confirm_plan"
    full = gesture_body(browser, tkey)
    browser.fail(path, {"card_version": flip(version), "nodes": []}, "card_changed")
    data = browser.post(path, {"card_version": version, "nodes": ["n0", "n0"]}, expect=400)
    assert data["error"]["code"] == "bad_json"
    browser.fail(path, {"card_version": version, "nodes": ["n9"]}, "not_found", 404)
    spy = Spy(browser.ws.controller, monkeypatch)
    browser.fail(
        f"/api/device/tasks/{tkey}/confirm",
        {**full, "card_version": subset["card_version"]},
        "card_changed",
    )
    assert spy.total() == 0
    assert browser.post(f"/api/device/tasks/{tkey}/confirm", full)["result"] == {
        "status": "settled"
    }


def test_cancel_and_stop_retire_the_code_and_write_the_chat(browser):
    red = "T" + browser.send(RED)
    browser.device(red, "confirm")
    browser.device(red, "mfa_request")
    assert browser.sms_code()
    browser.post(f"/api/device/tasks/{red}/cancel", {})
    assert browser.sms_code() is None
    roles = [turn["role"] for turn in browser.state["chat"]["turns"][-2:]]
    assert roles == ["user", "assistant"]
    assert browser.state["device"]["tasks"] == []
    browser.send(RED)
    browser.post("/api/stop", {})
    assert browser.state["chat"]["turns"][-1]["role"] == "system"
    # The current reply keeps its task on the pane, withdrawn and without a gesture.
    assert [(task["step"], task["gesture"]) for task in browser.state["device"]["tasks"]] == [
        ("withdrawn", None)
    ]
    assert browser.ws.requests == {}
    assert re.fullmatch(r"[A-Za-z0-9_-]{12}", browser.state["generation"])
