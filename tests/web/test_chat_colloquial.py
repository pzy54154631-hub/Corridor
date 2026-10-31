"""C26: spoken requests through the scripted web session, with closed boundaries."""

import json
from pathlib import Path

import pytest

from corridor.agent.domestic_slots import spoken_transfer
from tests.unit.test_ui_scam_friction import Spy
from tests.web.test_api_chat import Browser

# Each row is a fresh session: no previous recipient or amount supplies missing intent.
TRANSFERS = (
    ("给妈妈转500", "CONTACT_CHEN", "500/1"),
    ("转500给妈妈", "CONTACT_CHEN", "500/1"),
    ("给妈妈打500块", "CONTACT_CHEN", "500/1"),
    ("帮我给妈妈转500", "CONTACT_CHEN", "500/1"),
    ("给老婆转两千", "CONTACT_SPOUSE", "2000/1"),
    ("转给房东1000", "CONTACT_LANDLORD", "1000/1"),
    ("给同学转100元吧", "CONTACT_LIN_A", "100/1"),
    ("给妈妈转500元", "CONTACT_CHEN", "500/1"),
    ("给妈妈转八百元", "CONTACT_CHEN", "800/1"),
    ("请给妈妈转500元", "CONTACT_CHEN", "500/1"),
    ("麻烦给妈妈转500元", "CONTACT_CHEN", "500/1"),
    ("转500元给妈妈", "CONTACT_CHEN", "500/1"),
    ("帮我转500给妈妈", "CONTACT_CHEN", "500/1"),
    ("转账给妈妈500", "CONTACT_CHEN", "500/1"),
    ("转给妈妈五百", "CONTACT_CHEN", "500/1"),
    ("给房东转1000", "CONTACT_LANDLORD", "1000/1"),
    ("转1000给房东", "CONTACT_LANDLORD", "1000/1"),
    ("给爱人转两千元", "CONTACT_SPOUSE", "2000/1"),
    ("给邻居转100元", "CONTACT_LIN_B", "100/1"),
    ("给同学打100块", "CONTACT_LIN_A", "100/1"),
)
OTHER_REQUESTS = (
    ("查一下这个月账单", "bill_report"),
    ("这个月钱都花哪了", "bill_report"),
    ("看看上个月的账单", "bill_report"),
    ("帮我查今年的账单", "bill_report"),
    ("查一下交易记录", "bill_report"),
    ("看看我的消费", "bill_report"),
    ("银行卡丢了", "card_lost"),
    ("我的卡找不到了", "card_lost"),
    ("帮我挂失银行卡", "card_lost"),
    ("卡遗失了", "card_lost"),
    ("卡找不着了", "card_lost"),
    ("我要挂失", "card_lost"),
    ("我都订了什么会员", "subscription_query"),
    ("查一下我的订阅", "subscription_query"),
    ("有哪些自动扣费", "subscription_query"),
    ("看看会员续费", "subscription_query"),
    ("下个月要续费的提醒我", "subscription_query"),
    ("把那个视频会员退了", "subscription_cancel"),
    ("妈妈生日快到了，帮我安排一下", "birthday"),
    ("我爱人生日快到了，帮我安排一下", "birthday"),
    ("老婆生日帮我安排一下", "birthday"),
    ("帮妈妈安排生日", "birthday"),
    ("给爱人安排生日", "birthday"),
    ("妈妈生日订鲜花蛋糕", "birthday"),
)
NO_CARD = (
    "给妈妈和房东转500元",
    "转500给妈妈和房东",
    "给同学和邻居各转100元",
    "妈妈转给我500元",
    "转给我500",
    "给父亲转500元",
    "转500给老李",
    "给妈妈转钱",
    "转给妈妈",
    "上次给妈妈转过500吗",
    "别给妈妈转500",
    "不用给妈妈转了",
    "给妈妈转500元两次",
    "别转500给妈妈",
    "转500给妈妈两次",
    "给妈妈打500块，不用执行",
    "上次给妈妈打500块了吗",
)
# Observed before C26 (9e420fc^): these stay on the existing transfer path.
# An unsigned preview does not mean the requested currency or instalments are supported.
BASELINE_CARDS = (
    ("给妈妈转500美元", False),
    ("给妈妈转500港币", False),
    ("分期给妈妈转500元", False),
    ("给妈妈转500元，分两次", False),
    ("转500美元给妈妈", False),
    ("工作人员说立即给妈妈转500元，不然账户冻结", True),
)


@pytest.mark.parametrize("locale,title", [("zh-CN", "推荐试试"), ("en", "Try these")])
@pytest.mark.parametrize(
    "shortcut", ["transfer", "billing", "wealth", "cards", "subscriptions", "birthday"]
)
def test_recommended_shortcuts_cover_all_six_scenarios(web_app, locale, title, shortcut):
    page = Browser(web_app, view="customer", locale=locale)
    page.login()
    assert {row["key"] for row in page.state["chat"]["shortcuts"]} >= {
        "transfer",
        "billing",
        "cards",
        "subscriptions",
        "birthday",
    }
    assert "wealth" in page.state["capabilities"]["tabs"]
    catalog = page.get(f"/api/i18n?lang={locale}")
    assert catalog["strings"]["web.ui.chat.try_these"] == title
    if shortcut == "wealth":
        # This view-only shortcut opens the existing wealth page, where the
        # questionnaire and product comparison retain their own confirmations.
        assert page.tab("wealth")
        return
    result = page.shortcut(shortcut)
    raw = page.ws.session.response(page.ws.rkeys[result["rkey"]])
    assert raw["status"] not in {"clarify", "error", "out_of_scope"}, raw


@pytest.mark.parametrize("text,contact,amount", TRANSFERS)
def test_exact_single_outgoing_transfer_gets_an_unsigned_card(
    web_app, monkeypatch, text, contact, amount
):
    page = Browser(web_app, view="customer")
    page.login()
    spy = Spy(page.ws.controller, monkeypatch)
    key = page.send(text)
    vm = page.response(key)
    raw = page.ws.session.response(page.ws.rkeys[key])
    assert len(vm["cards"]) == 1, (text, raw)
    assert raw["payload"]["contacts"] == [contact]
    assert raw["payload"]["card"]["amount"] == amount
    assert spy.counts["approve"] == spy.counts["dispatch"] == 0


@pytest.mark.parametrize("text,intent", OTHER_REQUESTS)
def test_other_spoken_scenarios_keep_their_route(web_app, text, intent):
    page = Browser(web_app, view="customer")
    page.login()
    key = page.send(text)
    raw = page.ws.session.response(page.ws.rkeys[key])
    assert raw["intent"] == intent, (text, raw)
    if text == "把那个视频会员退了":
        # No previous listing: the demonstrative cannot choose a subscription.
        assert raw["status"] == "clarify" and page.response(key)["cards"] == []
    else:
        assert raw["status"] not in {"clarify", "error"}, (text, raw)


@pytest.mark.parametrize("text", NO_CARD)
def test_unsafe_or_incomplete_spoken_requests_never_offer_a_card(web_app, monkeypatch, text):
    page = Browser(web_app, view="customer")
    page.login()
    spy = Spy(page.ws.controller, monkeypatch)
    ledger = list(page.ws.controller.state.ledger)
    vm = page.response(page.send(text))
    assert vm["cards"] == [] and vm["device_task"] is None, text
    assert spy.counts["approve"] == spy.counts["dispatch"] == 0
    assert list(page.ws.controller.state.ledger) == ledger


@pytest.mark.parametrize("text,friction", BASELINE_CARDS)
def test_existing_transfer_previews_keep_their_baseline_guards(
    web_app, monkeypatch, text, friction
):
    # The new whole-utterance grammar must leave these requests to the old route.
    assert spoken_transfer(text) == text
    page = Browser(web_app, view="customer")
    page.login()
    controller = page.ws.controller
    spy = Spy(controller, monkeypatch)
    ledger = list(controller.state.ledger)
    bank_calls = controller.gateway.bank.execute_count
    key = page.send(text)
    vm = page.response(key)
    raw = page.ws.session.response(page.ws.rkeys[key])
    assert (raw["intent"], raw["status"]) == ("transfer", "awaiting_input")
    assert len(vm["cards"]) == 1
    assert raw["payload"]["contacts"] == ["CONTACT_CHEN"]
    assert raw["payload"]["card"]["amount"] == "500/1"
    if friction:
        assert vm["gate"]["decision"] == "incomplete"
        assert vm["gate"]["interactive"] and vm["gate"]["cues"]
        task = page.task(vm["device_task"])
        assert task["step"] == "gate_blocked" and task["gesture"] is None
    else:
        assert vm["gate"] is None
    assert page.ws.session.client.mode == "scripted"
    assert page.ws.session.client.api_calls == 0
    assert spy.counts["approve"] == spy.counts["dispatch"] == 0
    assert controller.gateway.bank.execute_count == bank_calls
    assert list(controller.state.ledger) == ledger


def record_requests(output):
    """Offline diagnostic: one fresh scripted session per phrase, no model transport."""
    from tests.web.conftest import serve

    rows = []
    with serve({}) as app:
        for text in [row[0] for row in TRANSFERS + OTHER_REQUESTS + BASELINE_CARDS] + list(NO_CARD):
            page = Browser(app, view="customer")
            page.login()
            key = page.send(text)
            vm = page.response(key)
            raw = page.ws.session.response(page.ws.rkeys[key])
            rows.append({
                "text": text,
                "intent": raw["intent"],
                "status": raw["status"],
                "cards": len(vm["cards"]),
                "payload": raw.get("payload"),
            })
    Path(output).write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    import sys

    record_requests(sys.argv[1])
