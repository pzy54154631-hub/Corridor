"""C65: device facts expose the controls actually covered by the approval card."""

from copy import deepcopy

import pytest

from corridor.web import compat
from corridor.web.catalog import bound
from corridor.web.vm.cards import card_vm
from tests.web.test_api_chat import Browser
from tests.web.test_public_completeness import card_body


@pytest.fixture(params=["zh-CN", "en"])
def page(web_app, request):
    browser = Browser(web_app, view="customer", locale=request.param)
    browser.login()
    return browser


def facts(page):
    task = page.state["device"]["tasks"][0]
    source = compat.authoritative_cards(page.ws.controller, page.ws.tasks[task["key"]].identity)[0]
    return {row["key"]: row for row in task["cards"][0]["facts"]}, source


@pytest.mark.parametrize("kind,limit", [("lower_card_limit", "500"), ("raise_card_limit", "3000")])
def test_device_shows_both_limits_from_the_approval_card(page, kind, limit):
    page.post("/api/cards/preview", card_body(page, kind, limit=limit))
    shown, source = facts(page)
    with bound(page.ws):
        for phase in ("before", "after"):
            assert shown[f"card_change.{phase}.limit"]["value"] == compat.money(
                source["card_change"][phase]["limit"]
            )
        assert shown["card_change.after.limit"]["value"] == compat.money(limit)
    assert shown["card_change.before.limit"]["value"] != shown["card_change.after.limit"]["value"]
    assert shown["card_change.before.limit"]["label"] != shown["card_change.after.limit"]["label"]
    assert shown["card_object"]["value"]

    # A different form value or current state cannot replace the card's recorded facts.
    original = deepcopy(source)
    ref = source["objects"]["card_ref"]
    current = page.ws.controller.state.cards[ref]
    page.ws.controller.state.cards[ref] = current.model_copy(update={"limit": current.limit * 2})
    with bound(page.ws):
        rebuilt = card_vm(source, controller=page.ws.controller, key="test")
    assert {row["key"]: row for row in rebuilt["facts"]} == shown
    assert source == original


def test_device_lists_restricted_categories(page):
    current = page.tab("accounts")["card_controls"]["choices"][0]["restrictions"]
    categories = sorted(set(current) | {"cash", "online"})
    page.post(
        "/api/cards/preview",
        card_body(page, "restrict_card", restrictions=categories),
    )
    shown, source = facts(page)
    assert source["card_change"]["before"]["restrictions"] == current
    assert source["card_change"]["after"]["restrictions"] == categories
    expected = (
        ("现金交易", "线上交易")
        if page.ws.locale == "zh-CN"
        else ("Cash transactions", "Online transactions")
    )
    for category in expected:
        assert category in shown["card_change.after.restrictions"]["value"]


def test_freeze_and_unfreeze_name_the_card_and_target_status(page):
    for kind, target in (("freeze_card", "frozen"), ("unfreeze_card", "active")):
        page.post("/api/cards/preview", card_body(page, kind))
        shown, source = facts(page)
        assert shown["card_object"]["value"]
        assert source["card_change"]["after"]["status"] == target
        expected = (
            {"frozen": "已冻结", "active": "正常"}
            if page.ws.locale == "zh-CN"
            else {"frozen": "Frozen", "active": "Active"}
        )
        assert shown["card_change.after.status"]["value"] == expected[target]
        assert shown["card_change.before.status"]["value"] != expected[target]
        if kind == "freeze_card":
            key = page.state["device"]["tasks"][0]["key"]
            assert page.device(key, "confirm")["result"]["status"] == "settled"
