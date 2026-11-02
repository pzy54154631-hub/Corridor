"""Public business entries preserve the existing controller authorization paths."""

import pytest

from corridor.agent.domestic_session import DomesticSession
from corridor.web import compat
from tests.web.test_api_chat import Browser

AA_TEXT = "这顿饭300我们3个人AA"


@pytest.fixture
def page(web_app):
    browser = Browser(web_app, view="customer", locale="zh-CN")
    browser.login()
    return browser


def test_aa_selection_prepares_then_device_confirms(page):
    rkey = page.send(AA_TEXT)
    assert page.state["capabilities"]["aa"] is True
    assert page.response(rkey)["aa"]["needed"] == 2
    assert page.state["device"]["tasks"] == []
    created = page.post(f"/api/chat/responses/{rkey}/aa", {"choices": ["c0", "c1"]})["rkey"]
    assert created != rkey
    assert page.task("T" + created)["step"] == "confirm_intent"
    assert page.device("T" + created, "confirm")["result"]["status"] == "settled"
    assert page.response(created)["result"] is not None


@pytest.mark.parametrize(
    "choices,code,status",
    [
        (["c0"], "invalid_input", 422),
        (["c0", "c0"], "bad_json", 400),
        (["c0", "c999"], "not_found", 404),
    ],
)
def test_aa_invalid_selection_never_prepares(page, monkeypatch, choices, code, status):
    rkey = page.send(AA_TEXT)
    calls = []
    monkeypatch.setattr(DomesticSession, "prepare_aa", lambda *a: calls.append(a))
    page.fail(f"/api/chat/responses/{rkey}/aa", {"choices": choices}, code, status)
    assert calls == [] and page.state["device"]["tasks"] == []


def test_aa_old_reply_and_other_session_cannot_select(page, web_app):
    rkey = page.send(AA_TEXT)
    page.send("查余额")
    page.fail(f"/api/chat/responses/{rkey}/aa", {"choices": ["c0", "c1"]}, "not_current")
    other = Browser(web_app)
    other.login()
    other.fail(f"/api/chat/responses/{rkey}/aa", {"choices": ["c0", "c1"]}, "not_found", 404)


def test_aa_matches_the_existing_session_path(page):
    from tests.web.test_tasks import logged_in

    reference = logged_in()
    with reference.bound():
        raw = reference.session.receive(AA_TEXT)
        chosen = reference.session.prepare_aa(raw["response_id"], reference.session.aa_roster()[:2])
        expected_card = chosen["card"]
        expected = compat.IMAdapter(reference.session).click("confirm", chosen["response_id"])
    rkey = page.send(AA_TEXT)
    created = page.post(f"/api/chat/responses/{rkey}/aa", {"choices": ["c0", "c1"]})["rkey"]
    actual = page.ws.session.response(page.ws.rkeys[created])
    assert actual["card"] == expected_card
    page.device("T" + created, "confirm")
    actual = page.ws.session.response(page.ws.rkeys[created])
    assert actual["status"] == expected["status"]
    assert page.ws.controller.state.model_dump() == reference.controller.state.model_dump()


def card_body(page, kind, **extra):
    form = page.tab("accounts")["card_controls"]
    return {
        "choice": form["choices"][0]["key"],
        "list_version": form["list_version"],
        "kind": kind,
        **extra,
    }


def test_card_limit_requires_device_and_mfa(page):
    before = page.ws.controller.state.cards
    page.post("/api/cards/preview", card_body(page, "raise_card_limit", limit="3000"))
    assert page.ws.controller.state.cards == before
    task = page.state["device"]["tasks"][0]
    assert task["source"] == "cards" and task["step"] == "confirm_intent"
    assert page.red(task["key"])["result"]["status"] == "settled"
    assert page.ws.controller.state.cards != before


@pytest.mark.parametrize(
    "kind,extra",
    [
        ("lower_card_limit", {"limit": "500"}),
        ("restrict_card", {"restrictions": ["online"]}),
        ("freeze_card", {}),
    ],
)
def test_card_controls_confirm_existing_operations(page, kind, extra):
    if kind == "restrict_card":
        current = page.tab("accounts")["card_controls"]["choices"][0]["restrictions"]
        extra = {"restrictions": sorted(set(current) | {"online", "cash"})}
    page.post("/api/cards/preview", card_body(page, kind, **extra))
    key = page.state["device"]["tasks"][0]["key"]
    assert page.device(key, "confirm")["result"]["status"] == "settled"
    if kind == "freeze_card":
        page.post("/api/cards/preview", card_body(page, "unfreeze_card"))
        assert page.red(page.state["device"]["tasks"][0]["key"])["result"]["status"] == "settled"


def test_card_list_binding_and_invalid_fields_sign_nothing(page):
    body = card_body(page, "raise_card_limit", limit="3000")
    before = page.ws.controller.state.model_dump()
    page.fail("/api/cards/preview", {**body, "list_version": "0" * 32}, "card_changed")
    page.fail("/api/cards/preview", {**body, "limit": "3.001"}, "invalid_input", 422)
    page.fail("/api/cards/preview", {**body, "restrictions": []}, "invalid_input", 422)
    assert page.ws.controller.state.model_dump() == before


@pytest.mark.parametrize("text", ["帮我把卡片额度提到3000", "解冻我的卡"])
def test_card_chat_routes_to_existing_accounts_form(page, text):
    rkey = page.send(text)
    assert page.response(rkey)["route_hint"]["target"] == "accounts"


def test_risk_answers_update_the_purchase_controller_only_after_signing(page, web_app):
    other = Browser(web_app)
    other.login()
    before_other = other.ws.controller.state.model_dump()
    form = risk_form(page)
    assert len(form["questions"]) == 5
    before = page.ws.controller.state.model_dump()
    page.fail(
        "/api/wealth/risk",
        {"answers": [0, 0, 0, 0], "version": form["list_version"]},
        "invalid_input",
        422,
    )
    page.fail(
        "/api/wealth/risk",
        {"answers": [True, 0, 0, 0, 0], "version": form["list_version"]},
        "invalid_input",
        422,
    )
    assert page.ws.controller.state.model_dump() == before
    page.post("/api/wealth/risk", {"answers": [0] * 5, "version": form["list_version"]})
    state = page.ws.controller.state
    profile = state.risk_assessments[
        state.customers[page.ws.controller.device.principal].risk_profile_ref
    ]
    assert profile.level == 1
    assert page.tab("wealth")["questionnaire"]["result"]["banner"]
    page.post("/api/wealth/compare", {"objective": "net_return"})
    bic = page.ws.business["wealth_comparison"]["bic"]
    assert bic is None
    assert page.ws.business["wealth_comparison"]["rule_ids"] == ["BIC-NOFEASIBLE"]
    assert all(
        "risk" in reasons
        for reasons in page.ws.business["wealth_comparison"]["exclusions"].values()
    )
    assert other.ws.controller.state.model_dump() == before_other


def risk_form(page):
    form = page.tab("wealth")["questionnaire"]
    if form["retake"]:
        page.post("/api/wealth/risk/retake", {"version": form["list_version"]})
    return page.tab("wealth")["questionnaire"]


def test_risk_stale_form_and_repeated_submission_are_rejected(page):
    form = risk_form(page)
    body = {"answers": [0] * 5, "version": form["list_version"]}
    page.fail("/api/wealth/risk", {**body, "version": "0" * 32}, "card_changed")
    page.post("/api/wealth/risk", body)
    page.fail("/api/wealth/risk", body, "card_changed")
