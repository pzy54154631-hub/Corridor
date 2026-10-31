"""Domestic presentation remains separate from canonical financial facts."""

from copy import deepcopy
from unittest.mock import MagicMock

from corridor.i18n import set_locale
from corridor.ui import components
from corridor.ui.app import PAGES
from corridor.ui.messages import tr


def test_domestic_navigation_and_bilingual_bank_identity():
    assert PAGES == (
        "conversation",
        "transfers",
        "billing",
        "wealth",
        "cards",
        "subscriptions",
        "birthday",
        "device",
        "dag",
        "operator",
        "safety",
        "im",
        "audit",
    )
    try:
        set_locale("zh-CN")
        assert tr("bank_name") == "演示银行（模拟环境）"
        assert tr("simulation_notice") == "合成数据 · 模拟认证 · 无真实资金"
        assert tr("nav.conversation") == "对话办业务"
        set_locale("en")
        assert tr("bank_name") == "Demo Bank (simulated environment)"
    finally:
        set_locale(None)


def test_card_localization_never_mutates_canonical_facts(monkeypatch):
    card = {
        "kind": "transfer",
        "amount": "200/1",
        "tier": "yellow",
        "total_before": "0/1",
        "total_after": "200/1",
        "tier_reasons": ["small_transfer"],
    }
    expected = deepcopy(card)
    display = MagicMock()
    monkeypatch.setattr(components, "st", display)
    try:
        set_locale("zh-CN")
        components.render_card(card)
        shown = " ".join(str(call) for call in display.mock_calls)
        assert "黄色" in shown and "1000" in shown and "确认" in shown
        assert "200/1" in shown
        assert card == expected
        display.reset_mock()
        set_locale("en")
        components.render_card(card)
        assert card == expected
        assert "Yellow" in " ".join(str(call) for call in display.mock_calls)
    finally:
        set_locale(None)


def test_localized_statements_keep_identical_numeric_evidence():
    from corridor.ui.wealth_statement import build_statement
    from tests.unit.test_ui_wealth_statement import statement_inputs

    try:
        inputs = statement_inputs()
        set_locale("zh-CN")
        chinese = build_statement(*inputs)
        set_locale("en")
        english = build_statement(*inputs)
        assert chinese.numeric_sources == english.numeric_sources
        assert 'lang="zh-CN"' in chinese.html
        assert "适当性声明" in chinese.text
        assert 'lang="en"' in english.html
        assert "Suitability Statement" in english.text
    finally:
        set_locale(None)


def test_stored_receipt_facts_refresh_locale_without_audit_or_state_changes():
    from corridor.crypto.jcs import H
    from corridor.ui.components import localized_facts
    from corridor.ui.controller import DemoController

    controller = DemoController()
    controller.start_session()
    try:
        set_locale("zh-CN")
        original = controller.query_balance()["facts"]["facts"]
        state_hash = H(controller.state)
        checkpoint = controller.gateway.audit.checkpoint()
        set_locale("en")
        english = localized_facts(original, controller)
        assert [fact["exact_value"] for fact in original] == [
            fact["exact_value"] for fact in english
        ]
        assert [fact["text"] for fact in original] != [fact["text"] for fact in english]
        assert H(controller.state) == state_hash
        assert controller.gateway.audit.checkpoint() == checkpoint
    finally:
        set_locale(None)


def test_every_business_action_kind_has_bilingual_display_copy():
    from typing import get_args

    from corridor.gateway.plans import _STATUSES
    from corridor.schemas.bank import ActionKind
    from corridor.ui.components import KINDS, LABELS, STATUSES
    from corridor.ui.messages import UI_MESSAGES

    assert set(get_args(ActionKind)) <= set(KINDS)
    assert _STATUSES <= set(STATUSES)
    for label in {*KINDS.values(), *LABELS.values(), *STATUSES.values()}:
        assert label in UI_MESSAGES, label
        assert UI_MESSAGES[label]["en"] == label
        assert UI_MESSAGES[label]["zh-CN"] != label


def test_language_switch_keeps_navigation_and_receipt_state(monkeypatch):
    from corridor.crypto.jcs import H
    from tests.e2e.test_demo_ui import app, navigate

    monkeypatch.setenv("CORRIDOR_LOCALE", "zh-CN")
    page = app()
    page.button(key="enter").click().run()
    navigate(page, "billing")
    page.button(key="bill_report").click().run()
    controller = page.session_state["controller"]
    result_hash = H(page.session_state["bill_result"])
    state_hash = H(controller.state)
    checkpoint = controller.gateway.audit.checkpoint()
    for locale in ("en", "zh-CN"):
        page.selectbox(key="locale").select(locale).run()
        page.run()
        assert not page.exception
        assert page.selectbox(key="locale").value == locale
        assert page.selectbox(key="page").value == "billing"
        assert page.button(key="bill_report")
        assert H(page.session_state["bill_result"]) == result_hash
        assert H(controller.state) == state_hash
        assert controller.gateway.audit.checkpoint() == checkpoint


def test_successful_birthday_plan_retires_only_the_unsigned_ui_draft():
    from corridor.crypto.jcs import H
    from tests.e2e.test_demo_ui import app, navigate

    page = app()
    page.button(key="enter").click().run()
    navigate(page, "birthday")
    page.button(key="preview_birthday").click().run()
    for checkbox in page.checkbox:
        checkbox.check()
    page.button(key="confirm_plan").click().run()
    controller = page.session_state["controller"]
    assert not page.exception
    assert page.session_state["plan_result"]["status"] == "settled"
    assert "birthday_draft" not in page.session_state
    assert "confirm_plan" not in {button.key for button in page.button}
    assert len(page.checkbox) == 0
    assert len(controller.state.schedules) == 3
    state_hash = H(controller.state)
    result_hash = H(page.session_state["plan_result"])
    page.run()
    assert H(controller.state) == state_hash
    assert H(page.session_state["plan_result"]) == result_hash
