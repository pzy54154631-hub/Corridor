"""B1 ext: birthday plans and compensation, subscriptions, bills and wealth over HTTP.

Every business page card becomes a device task ``TB<n>`` with the same whitelist and
tokens as a chat card; the R4 preset stays on its own controller with no task at all.
"""

import pytest

from corridor.web import FEATURES
from tests.unit.test_ui_scam_friction import Spy
from tests.web.test_api_chat import Browser
from tests.web.test_tasks import inv22  # noqa: F401  (INV-2.2 probe)

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
BIRTHDAYS = "帮我准备生日"
SPOUSE = "帮我准备老婆的生日"
REFUND = "退回锁定的预算"


@pytest.fixture
def web_settings():
    return {"features": FEATURES | EXT}


@pytest.fixture
def browser(web_app):
    page = Browser(web_app)
    page.login()
    return page


def business(page):
    return [task for task in page.state["device"]["tasks"] if task["key"].startswith("TB")]


def nodes(page, tkey, keys):
    version = page.task(tkey)["nodes_version"]
    return page.post(f"/api/device/tasks/{tkey}/nodes", {"card_version": version, "nodes": keys})


def finish(page, tkey):
    """Every remaining whitelisted step of one task, the code read from the phone."""
    for _ in range(4):
        task = page.task(tkey)
        if task["gesture"] is None:
            return task["step"]
        op = task["gesture"]["op"]
        extra = {"code": page.sms_code()} if op == "mfa_verify" else {}
        page.device(tkey, op, **extra)
    return page.task(tkey)["step"]


def test_a_chosen_birthday_becomes_a_plans_task_signed_by_its_selection(browser, monkeypatch):
    rkey = browser.send(BIRTHDAYS)
    browser.post(f"/api/chat/responses/{rkey}/birthday", {"choice": "c1"})
    (task,) = business(browser)
    tkey = task["key"]
    assert (task["source"], task["step"]) == ("plans", "select_nodes")
    assert browser.ws.business["birthday_draft"]["plan_id"] == browser.ws.tasks[tkey].identity
    nodes(browser, tkey, ["n0"])
    assert browser.task(tkey)["step"] == "confirm_subset"
    spy = Spy(browser.ws.controller, monkeypatch)
    assert browser.device(tkey, "confirm")["result"] == {"status": "redisplay"}
    assert spy.total() == 0
    replaced = browser.ws.tasks[tkey].identity
    assert browser.ws.business["birthday_draft"]["plan_id"] == replaced
    assert browser.task(tkey)["step"] == "select_nodes"
    nodes(browser, tkey, ["n0"])
    assert browser.device(tkey, "confirm")["result"] == {"status": "settled"}
    assert "birthday_draft" not in browser.ws.business
    assert browser.task(tkey)["step"] == "done"
    browser.post(f"/api/chat/responses/{rkey}/birthday", {"choice": "c1"})
    assert len(business(browser)) == 1


def test_a_birthday_choice_is_for_the_current_listed_reply(browser):
    rkey = browser.send(BIRTHDAYS)
    browser.fail(f"/api/chat/responses/{rkey}/birthday", {"choice": "c9"}, "not_found", 404)
    browser.send(SPOUSE)
    browser.fail(f"/api/chat/responses/{rkey}/birthday", {"choice": "c0"}, "not_current")
    other = browser.send("给妈妈转500元")
    browser.fail(f"/api/chat/responses/{other}/birthday", {"choice": "c0"}, "not_applicable")


def test_a_birthday_plan_then_stop_and_refund_in_chat(browser):
    plan = "T" + browser.send(SPOUSE)
    nodes(browser, plan, ["n0", "n1", "n2", "n3"])
    assert browser.device(plan, "confirm")["result"] == {"status": "settled"}
    browser.send("停止")
    refund = "T" + browser.send(REFUND)
    assert browser.task(refund)["step"] == "confirm_intent"
    assert finish(browser, refund) == "done"


def test_the_task_panel_compensation_is_a_business_task(browser):
    browser.compensate()
    assert browser.ws.business["compensation"] == {"status": "no_active_plan"}
    assert business(browser) == []
    plan = "T" + browser.send(SPOUSE)
    nodes(browser, plan, ["n0", "n1", "n2", "n3"])
    browser.device(plan, "confirm")
    browser.send("停止")
    browser.compensate()
    (task,) = business(browser)
    assert task["source"] == "compensation" and task["step"] == "confirm_intent"
    assert finish(browser, task["key"]) == "done"


def test_subscriptions_from_the_accounts_tab(browser):
    browser.fail(
        "/api/subscriptions/cancel", {"choice": "c0", "list_version": "0" * 32}, "card_changed"
    )
    data = browser.post("/api/subscriptions/query", {})
    assert data["tab"]["name"] == "accounts"
    (query,) = business(browser)
    assert (query["source"], query["step"]) == ("subscriptions", "confirm_intent")
    assert finish(browser, query["key"]) == "done"
    browser.pick("/api/subscriptions/cancel", "c0")
    cancel = business(browser)[0]
    assert cancel["key"] != query["key"] and cancel["step"] == "confirm_intent"
    assert finish(browser, cancel["key"]) == "done"
    assert [task["key"] for task in business(browser)] == [cancel["key"]]


def test_the_accounts_tab_keeps_listing_what_its_cancel_choices_index(browser):
    # CONTRACT-CHANGES.md B1-2 item 2: a registered cancel card must not hide the query
    # the choices index, and a newer chat query does not replace the page's own list.
    browser.post("/api/subscriptions/query", {})
    (query,) = business(browser)
    finish(browser, query["key"])
    listed = browser.get("/api/tabs/accounts")["tab"]["vm"]["subscriptions"]
    assert listed["cancel_choices"]
    browser.pick("/api/subscriptions/cancel", "c0")
    after = browser.get("/api/tabs/accounts")["tab"]["vm"]["subscriptions"]
    assert after == listed
    finish(browser, "T" + browser.send("查看我的订阅"))
    assert browser.get("/api/tabs/accounts")["tab"]["vm"]["subscriptions"] == listed


def test_subscriptions_shown_in_chat_can_be_cancelled_from_the_tab(browser):
    rkey = browser.send("查看我的订阅")
    assert finish(browser, "T" + rkey) == "done"
    browser.pick("/api/subscriptions/cancel", "c0")
    (cancel,) = business(browser)
    assert cancel["step"] == "confirm_intent"


def test_bills_in_chat_and_on_the_accounts_tab(browser, monkeypatch):
    spy = Spy(browser.ws.controller, monkeypatch)
    for text in ("看看9月账单", "有没有异常扣款"):
        rkey = browser.send(text)
        assert browser.response(rkey)["status"] == "settled"
        assert browser.response(rkey)["device_task"] is None
    data = browser.post("/api/billing/analyze", {"period": "2026-08"})
    assert data["tab"]["name"] == "accounts"
    assert browser.ws.business["billing"]["period"] == "2026-08"
    browser.post("/api/billing/analyze", {})
    assert browser.ws.business["billing"]["period"] == "2026-09"
    # Green queries: no device signature, no code, no device task.
    assert spy.counts["approve"] == 0 and spy.counts["request_mfa"] == 0
    assert browser.state["device"]["tasks"] == []


def test_wealth_purchase_redemption_and_the_isolated_refusal(browser, monkeypatch):
    browser.fail(
        "/api/wealth/purchase", {"choice": None, "list_version": "0" * 32}, "not_applicable"
    )
    data = browser.post("/api/wealth/compare", {"objective": "net_return"})
    assert data["tab"]["name"] == "wealth"
    browser.pick("/api/wealth/purchase", None)
    (purchase,) = business(browser)
    assert purchase["source"] == "wealth" and purchase["step"] == "mfa_request"
    assert finish(browser, purchase["key"]) == "done"
    browser.pick("/api/wealth/redeem", "c0")
    redemption = business(browser)[0]
    assert redemption["step"] == "confirm_intent"
    assert finish(browser, redemption["key"]) == "done"
    controller = browser.ws.controller
    dispatched = controller.gateway.bank.execute_count
    spy = Spy(controller, monkeypatch)
    browser.post("/api/presets/suitability-r4", {})
    demo = browser.ws.demos["suitability_r4"]
    assert demo["r4"]["outcome"] == "Reject" and demo["controller"] is not controller
    assert spy.total() == 0 and controller.gateway.bank.execute_count == dispatched
    assert [task["key"] for task in business(browser)] == [redemption["key"]]
    browser.fail(
        "/api/wealth/redeem",
        {"choice": "c9", "list_version": browser.listed("positions")},
        "not_found",
        404,
    )


def test_a_stop_withdraws_business_cards_and_a_new_card_still_confirms(browser):
    browser.post("/api/wealth/compare", {"objective": "net_return"})
    browser.pick("/api/wealth/purchase", None)
    (old,) = business(browser)
    old_identity = browser.ws.tasks[old["key"]].identity
    browser.send("停止")
    withdrawn = browser.task(old["key"])
    assert withdrawn["step"] == "withdrawn" and withdrawn["gesture"] is None
    browser.post("/api/stop", {})
    assert business(browser) == [] and browser.ws.requests == {}
    # The comparison stays: a new purchase card follows without comparing again.
    assert browser.ws.business["wealth_comparison"]
    browser.pick("/api/wealth/purchase", None)
    (fresh,) = business(browser)
    assert old["key"] not in browser.ws.tasks
    assert browser.ws.tasks[fresh["key"]].identity != old_identity
    assert finish(browser, fresh["key"]) == "done"
