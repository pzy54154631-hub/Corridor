"""UI-POLISH P8/P10: customer-view cards and tables are plain; canonical facts unchanged."""

import re
from copy import deepcopy

import pytest

from corridor.agent.domestic_session import DomesticSession
from corridor.i18n import reset_locale, set_locale
from corridor.ui import components
from corridor.ui.audience import reset_audience, set_audience
from corridor.ui.controller import DemoController, demo_state
from tests.unit.test_dux_cards_dag import DisplayRecorder

RAW_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
RULE_ID = re.compile(r"\b[A-Z]{2,}(?:-[A-Z0-9]+)+\b")


def _controller():
    controller = DemoController(initial_state=demo_state(showcase=True))
    controller.start_session()
    return controller


def _render(monkeypatch, card, controller, *, audience="customer", explanation=False, **kwargs):
    display = DisplayRecorder(controller)
    monkeypatch.setattr(components, "st", display)
    if not explanation:
        monkeypatch.setattr(components, "render_explanation", lambda **_: None)
    locale = set_locale("zh-CN")
    token = set_audience(audience)
    try:
        components.render_card(card, controller=controller, **kwargs)
    finally:
        reset_audience(token)
        reset_locale(locale)
    return " ".join(display.visible), display.evidence


def _chat_card(controller, text):
    return DomesticSession(controller).receive(text)["card"]


def test_customer_card_is_plain_and_hides_every_binding(monkeypatch):
    controller = _controller()
    card = _chat_card(controller, "给妈妈转800元")
    before = deepcopy(card)
    visible, evidence = _render(monkeypatch, card, controller)
    assert "请核对办理信息" in visible
    assert "妈妈 · 尾号3721（合成）" in visible
    for hidden in ("网关规范审批卡", "联系人 CHEN", "800/1", card["action_hash"]):
        assert hidden not in visible
    if card.get("node_id"):
        assert card["node_id"] not in visible
    assert evidence == []
    assert card == before
    judge_visible, judge_evidence = _render(monkeypatch, card, controller, audience="judge")
    assert "网关规范审批卡" in judge_visible and "妈妈（联系人 CHEN）" in judge_visible
    assert judge_evidence


def test_scheduled_card_times_customer_beijing_judge_keeps_utc(monkeypatch):
    controller = _controller()
    card = _chat_card(controller, "明天给妈妈转50")
    assert card["kind"] == "schedule_transfer"
    visible, evidence = _render(monkeypatch, card, controller)
    assert not RAW_TIME.search(visible) and evidence == []
    _, judge_evidence = _render(monkeypatch, card, controller, audience="judge")
    assert RAW_TIME.search(" ".join(judge_evidence))


def test_customer_rejection_has_plain_reason_without_rule_ids(monkeypatch):
    from corridor.ui import explanations

    display = DisplayRecorder()
    monkeypatch.setattr(components, "st", display)
    monkeypatch.setattr(explanations, "st", display)
    locale = set_locale("zh-CN")
    token = set_audience("customer")
    try:
        components.render_result({"status": "rejected", "rule_ids": ["BAL-AVAILABLE"]})
    finally:
        reset_audience(token)
        reset_locale(locale)
    shown = " ".join(display.visible)
    assert not RULE_ID.search(shown)
    assert "未执行原因" in shown and display.evidence == []


def test_purchase_card_shows_five_suitability_lines_and_stays_red(monkeypatch):
    controller = _controller()
    controller.compare_investments("net_return")
    card = controller.preview_investment(None, request_id="POLISH-PURCHASE")["card"]
    before = deepcopy(card)
    visible, _ = _render(monkeypatch, card, controller)
    assert "稳健30天持有（合成）" in visible
    assert visible.count("<li>") >= 5 and "适当性信息" in visible
    assert "产品风险等级：R2" in visible and "C2" in visible
    assert card["tier"] == "red" and card == before


@pytest.mark.parametrize("audience", ["customer", "judge"])
def test_new_payee_card_shows_the_warning_and_known_payee_does_not(monkeypatch, audience):
    controller = _controller()
    new_card = _chat_card(controller, "给新联系人转480元")
    visible, _ = _render(monkeypatch, new_card, controller, audience=audience)
    # S6 R5: the warning's first line says what the registry says ("registered as a new
    # recipient", a registration label), no longer "your first payment to": both lines come
    # from the same is_new flag, which is not a payment history. The warning's footer,
    # is unchanged.
    assert "防骗提醒" in visible and "登记为新收款人" in visible
    assert "第一次向" not in visible
    assert ("展示层提示" in visible) is (audience == "judge")
    known = _chat_card(_controller(), "给邻居转480元")
    assert known["objects"]["contact_ref"] == "CONTACT_LIN_B"
    quiet, _ = _render(monkeypatch, known, controller, audience=audience)
    assert "防骗提醒" not in quiet


def test_transaction_table_customer_names_and_beijing_time(monkeypatch):
    from corridor.ui import business_views

    controller = _controller()
    result = controller.query_transactions()
    tables = []

    class Table(DisplayRecorder):
        def dataframe(self, rows, **kwargs):
            tables.append(rows)

    monkeypatch.setattr(business_views, "st", Table(controller))
    monkeypatch.setattr(components, "st", Table(controller))
    locale = set_locale("zh-CN")
    try:
        token = set_audience("customer")
        try:
            business_views.render_transaction_rows(result, controller=controller)
        finally:
            reset_audience(token)
        business_views.render_transaction_rows(result, controller=controller)
    finally:
        reset_locale(locale)
    customer, judge = tables
    assert customer and "流水引用" not in customer[0] and "流水引用" in judge[0]
    assert "时间（北京时间）" in customer[0] and "交易对方" in customer[0]
    assert all(not RAW_TIME.search(str(row["时间（北京时间）"])) for row in customer)
    assert any("合成" in str(row["交易对方"]) for row in customer)
    assert any(RAW_TIME.search(str(value)) for row in judge for value in row.values())
