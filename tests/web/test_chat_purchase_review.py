"""The native chat purchase review is a device gesture, never a business-tab shortcut."""

from copy import deepcopy
from fractions import Fraction

import pytest

from corridor.web import contracts
from tests.unit.test_ui_scam_friction import Spy
from tests.web.test_api_chat import Browser
from tests.web.test_customer_trim import scan

PURCHASE = "帮我申购2000元稳健30天持有"


def quiet(page):
    c = page.ws.controller
    return deepcopy((c.state.user_inputs, c.state.positions, c.gateway.bank.execute_count))


@pytest.mark.parametrize("view", ["customer", "judge"])
@pytest.mark.parametrize("locale", ["zh-CN", "en"])
def test_natural_language_purchase_reviews_then_mfa_and_final_confirm(
    web_app, monkeypatch, view, locale
):
    page = Browser(web_app, view=view, locale=locale)
    page.login()
    c = page.ws.controller
    spy = Spy(c, monkeypatch)
    before = quiet(page)
    rkey = page.send(PURCHASE)
    task = page.task("T" + rkey)
    raw = page.ws.session.response(page.ws.rkeys[rkey])
    assert raw["status"] == "awaiting_input" and not raw["card"]
    assert task["step"] == "review_purchase" and task["cards"] == []
    assert task["gesture"]["op"] == "confirm" and "hash" not in task
    review = task["purchase_review"]
    facts = {row["key"]: row["value"] for row in review["facts"]}
    assert "P_A" in facts["selected_product_ref"] and "A_CURRENT" in facts["source_account_ref"]
    assert facts["objective"] == ("净收益" if locale == "zh-CN" else "Net return")
    assert {row["key"] for row in review["facts"]} == {
        *raw["payload"]["purchase_terms"],
        "currency",
    }
    assert page.response(rkey)["device_task"] == task["key"]
    state_response = page.get("/api/state")
    contracts.validate_response(state_response, view=view)
    if view == "customer":
        assert scan("purchase review", state_response, view) == []
    assert quiet(page) == before and spy.total() == 0
    page.device(task["key"], "confirm")
    assert page.task(task["key"])["step"] == "mfa_request"
    assert page.task(task["key"])["cards"]
    assert "purchase_review" not in page.task(task["key"])
    assert len(c.state.user_inputs) > len(before[0])
    assert c.gateway.bank.execute_count == 0
    page.device(task["key"], "mfa_request")
    page.device(task["key"], "mfa_verify", code=page.sms_code())
    assert c.gateway.bank.execute_count == 0
    assert page.task(task["key"])["step"] == "final_confirm"
    page.device(task["key"], "confirm")
    assert page.task(task["key"])["step"] == "done"
    assert c.gateway.bank.execute_count == 1
    positions = [p for p in c.state.positions.values() if p.id not in before[1]]
    assert len(positions) == 1
    assert positions[0].product_ref == "P_A"
    assert positions[0].principal_amount == Fraction(2000)


def prepared(web_app):
    page = Browser(web_app)
    page.login()
    rkey = page.send(PURCHASE)
    tkey = "T" + rkey
    g = page.gesture(tkey)
    return page, rkey, tkey, {"gesture": g["token"], "card_version": g["card_version"]}


@pytest.mark.parametrize(
    "field,value",
    [
        ("horizon_days", 730),
        ("max_access_days", 7),
        ("amount", "3000/1"),
        ("selection_mode", "best_interest"),
        ("selected_product_ref", "P_B"),
        ("reason_code", "changed"),
        ("allowed_types", ["deposit"]),
        ("source_account_ref", "A_LOCKED"),
        ("objective", "liquidity"),
        ("version", 2),
    ],
)
def test_each_changed_native_term_closes_the_old_review(web_app, monkeypatch, field, value):
    page, _, tkey, held = prepared(web_app)
    provider = page.ws.session.purchase_review_terms
    monkeypatch.setattr(
        page.ws.session,
        "purchase_review_terms",
        lambda *args: {**provider(*args), field: value},
    )
    before = quiet(page)
    spy = Spy(page.ws.controller, monkeypatch)
    shown = page.get("/api/state")["state"]
    assert next(t for t in shown["device"]["tasks"] if t["key"] == tkey)["gesture"] is None
    page.post(f"/api/device/tasks/{tkey}/confirm", held, expect=409)
    assert quiet(page) == before and spy.total() == 0


def test_even_matching_mutated_payload_and_current_terms_cannot_replace_original_review(
    web_app, monkeypatch
):
    page, rkey, tkey, held = prepared(web_app)
    rid = page.ws.rkeys[rkey]
    raw = page.ws.session._responses[rid]
    raw["payload"]["purchase"]["amount"] = "3000/1"
    raw["payload"]["purchase_terms"]["amount"] = "3000/1"
    before = quiet(page)
    spy = Spy(page.ws.controller, monkeypatch)
    page.post(f"/api/device/tasks/{tkey}/confirm", held, expect=409)
    assert quiet(page) == before and spy.total() == 0


def test_old_purchase_reply_cannot_be_revived_by_make_current(web_app, monkeypatch):
    page, rkey, tkey, held = prepared(web_app)
    page.send("帮我申购1000元稳健30天持有")
    page.post(f"/api/chat/responses/{rkey}/current", {})
    assert page.task(tkey)["step"] == "withdrawn" and page.task(tkey)["gesture"] is None
    before = quiet(page)
    spy = Spy(page.ws.controller, monkeypatch)
    page.post(f"/api/device/tasks/{tkey}/confirm", held, expect=409)
    assert quiet(page) == before and spy.total() == 0


@pytest.mark.parametrize("stop", ["/api/stop", "/api/chat/stop", "cancel"])
def test_stop_or_cancel_retires_the_unsigned_review(web_app, monkeypatch, stop):
    page, _, tkey, held = prepared(web_app)
    page.post(f"/api/device/tasks/{tkey}/cancel" if stop == "cancel" else stop, {})
    before = quiet(page)
    spy = Spy(page.ws.controller, monkeypatch)
    result = page.client.post(f"/api/device/tasks/{tkey}/confirm", held)
    assert result.code in {404, 409}
    assert quiet(page) == before and spy.total() == 0


def test_first_confirmation_token_cannot_request_mfa_or_sign_twice(web_app, monkeypatch):
    page, _, tkey, held = prepared(web_app)
    page.post(f"/api/device/tasks/{tkey}/mfa/request", held, expect=409)
    page.device(tkey, "confirm")
    before = quiet(page)
    spy = Spy(page.ws.controller, monkeypatch)
    page.post(f"/api/device/tasks/{tkey}/confirm", held, expect=409)
    page.post(f"/api/device/tasks/{tkey}/mfa/request", held, expect=409)
    assert quiet(page) == before and spy.total() == 0


@pytest.mark.parametrize("guard", ["untrusted", "phase", "locked", "owner"])
def test_purchase_review_keeps_common_guards(web_app, monkeypatch, guard):
    page, _, tkey, held = prepared(web_app)
    if guard == "untrusted":
        page.ws.untrusted = True
    elif guard == "phase":
        page.ws.tasks[tkey].phase = "in_call"
    elif guard == "locked":
        monkeypatch.setattr("corridor.web.compat.safety", lambda c: {"state": "Locked"})
    else:
        monkeypatch.setattr(page.ws.controller.device, "principal", "OTHER")
    before = quiet(page)
    spy = Spy(page.ws.controller, monkeypatch)
    page.post(f"/api/device/tasks/{tkey}/confirm", held, expect=409)
    assert quiet(page) == before and spy.total() == 0


def test_foreign_browser_and_old_generation_cannot_sign_review(web_app, monkeypatch):
    page, _, tkey, held = prepared(web_app)
    other, _, other_key, _ = prepared(web_app)
    before = quiet(other)
    spy = Spy(other.ws.controller, monkeypatch)
    other.post(f"/api/device/tasks/{other_key}/confirm", held, expect=409)
    assert quiet(other) == before and spy.total() == 0
    old_generation = page.client.generation
    page.post("/api/session/reset", {})
    page.login()
    page.send(PURCHASE)
    result = page.client.post(
        f"/api/device/tasks/{tkey}/confirm",
        held,
        headers={"X-Corridor-Generation": old_generation},
    )
    assert result.code == 409
    assert page.client.json(result)["error"]["code"] == "stale_generation"


@pytest.mark.parametrize("terms", [None, {}, {"horizon_days": "unknown"}])
def test_incomplete_native_review_never_offers_a_gesture(web_app, monkeypatch, terms):
    page = Browser(web_app)
    page.login()
    original = page.ws.session.purchase_review_terms
    monkeypatch.setattr(
        page.ws.session,
        "purchase_review_terms",
        lambda *args: None if terms is None else {**original(*args), **terms} if terms else {},
    )
    before = quiet(page)
    spy = Spy(page.ws.controller, monkeypatch)
    rkey = page.send(PURCHASE)
    assert page.task("T" + rkey)["step"] == "closed_facts"
    assert page.task("T" + rkey)["gesture"] is None
    assert page.task("T" + rkey)["cards"] == []
    assert quiet(page) == before and spy.total() == 0
