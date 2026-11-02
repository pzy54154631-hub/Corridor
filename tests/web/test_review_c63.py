"""C63: invalid forms are read-only and completed choices cannot execute twice."""

import pytest

from tests.web.test_api_chat import Browser
from tests.web.test_public_completeness import AA_TEXT, card_body, risk_form


@pytest.fixture
def page(web_app):
    browser = Browser(web_app, view="customer", locale="zh-CN")
    browser.login()
    return browser


@pytest.mark.parametrize(
    "kinds,statuses",
    [
        (["freeze_card", "unfreeze_card", "freeze_card"], ["frozen", "active", "frozen"]),
        (["report_card_lost", "restore_card", "report_card_lost"], ["lost", "frozen", "lost"]),
    ],
)
def test_card_protection_can_be_repeated_after_reversal(page, kinds, statuses):
    keys = []
    for kind, status in zip(kinds, statuses, strict=True):
        page.post("/api/cards/preview", card_body(page, kind))
        task = page.state["device"]["tasks"][0]
        assert task["key"] not in keys
        keys.append(task["key"])
        result = (
            page.device(task["key"], "confirm") if kind == "freeze_card" else page.red(task["key"])
        )
        assert result["result"]["status"] == "settled"
        assert page.ws.controller.state.cards["CARD_UI_DEMO"].status == status


@pytest.mark.parametrize("ending", ["settled", "cancelled"])
def test_one_bill_has_only_one_aa_collection(page, ending):
    source = page.send(AA_TEXT)
    path = f"/api/chat/responses/{source}/aa"
    created = page.post(path, {"choices": ["c0", "c1"]})["rkey"]
    turns = len(page.state["chat"]["turns"])
    for choices in (["c0", "c1"], ["c1", "c0"], ["c1", "c2"]):
        page.post(f"/api/chat/responses/{source}/current")
        assert page.response(source)["aa"]["locked"]
        assert page.post(path, {"choices": choices})["rkey"] == created
        assert len(page.state["chat"]["turns"]) == turns
        assert len(page.state["device"]["tasks"]) == 1
    if ending == "settled":
        assert page.device("T" + created, "confirm")["result"]["status"] == "settled"
    else:
        page.post(f"/api/device/tasks/T{created}/cancel")
    before = page.ws.controller.state.model_dump()
    for choices in (["c0", "c1"], ["c1", "c0"], ["c1", "c2"]):
        page.post(f"/api/chat/responses/{source}/current")
        page.fail(path, {"choices": choices}, "not_applicable")
    assert page.ws.controller.state.model_dump() == before
    assert not page.ws.untrusted


@pytest.mark.parametrize("prefix", ["明天", "昨天"])
def test_unsupported_aa_time_is_a_read_only_refusal(page, prefix):
    source = page.send(prefix + AA_TEXT)
    before = page.ws.controller.state.model_dump()
    count = page.ws.effects.count
    page.fail(f"/api/chat/responses/{source}/aa", {"choices": ["c0", "c1"]}, "not_applicable")
    assert not page.ws.untrusted
    assert page.ws.effects.count == count
    assert page.ws.controller.state.model_dump() == before


def test_risk_limit_blocks_both_gestures_without_rejections(page):
    form = risk_form(page)
    page.post("/api/wealth/risk", {"answers": [4] * 5, "version": form["list_version"]})
    form = page.tab("wealth")["questionnaire"]
    assert form["limit_reached"] and not form["retake"]
    before = page.ws.controller.state.model_dump()
    count = page.ws.effects.count
    for _ in range(3):
        page.fail("/api/wealth/risk/retake", {"version": form["list_version"]}, "not_applicable")
        page.fail(
            "/api/wealth/risk",
            {"answers": [4] * 5, "version": form["list_version"]},
            "not_applicable",
        )
    assert page.ws.controller.state.model_dump() == before
    assert page.ws.effects.count == count
    assert not page.ws.untrusted
    page.post("/api/cards/preview", card_body(page, "freeze_card"))
    assert (
        page.device(page.state["device"]["tasks"][0]["key"], "confirm")["result"]["status"]
        == "settled"
    )


@pytest.mark.parametrize(
    "kind,extra",
    [
        ("raise_card_limit", {"limit": "1000"}),
        ("raise_card_limit", {"limit": "2000"}),
        ("lower_card_limit", {"limit": "3000"}),
        ("lower_card_limit", {"limit": "2000"}),
        ("unfreeze_card", {}),
        ("restore_card", {}),
    ],
)
def test_invalid_card_gestures_do_not_reach_product(page, kind, extra):
    before = page.ws.controller.state.model_dump()
    count = page.ws.effects.count
    for _ in range(3):
        page.fail("/api/cards/preview", card_body(page, kind, **extra), "not_applicable")
    assert page.ws.controller.state.model_dump() == before
    assert page.ws.effects.count == count
    assert not page.ws.untrusted and not page.state["device"]["tasks"]


def test_one_open_card_preview_preserves_replay_and_allows_next_after_cancel(page):
    body = card_body(page, "lower_card_limit", limit="500")
    page.post("/api/cards/preview", body)
    task = page.state["device"]["tasks"][0]
    page.post("/api/cards/preview", body)
    assert [item["key"] for item in page.state["device"]["tasks"]] == [task["key"]]
    before = page.ws.controller.state.model_dump()
    count = page.ws.effects.count
    for limit in ("600", "700", "800"):
        page.fail("/api/cards/preview", {**body, "limit": limit}, "not_applicable")
    assert page.ws.controller.state.model_dump() == before
    assert page.ws.effects.count == count
    assert len(page.ws.tasks) == 1
    page.post(f"/api/device/tasks/{task['key']}/cancel")
    page.post("/api/cards/preview", card_body(page, "lower_card_limit", limit="600"))
    assert page.state["device"]["tasks"][0]["step"] == "confirm_intent"
