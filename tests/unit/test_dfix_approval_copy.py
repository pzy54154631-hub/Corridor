"""Fee and approval copy reflects canonical values in either locale."""

from copy import deepcopy

import pytest

from corridor.i18n import reset_locale, set_locale
from corridor.ui import components
from corridor.ui.controller import DemoController


@pytest.mark.parametrize(
    "locale,zero,unknown",
    [
        ("zh-CN", "¥0.00（无手续费）", "以银行回执为准"),
        ("en", "CNY 0.00 (no fee)", "As shown on the bank receipt"),
    ],
)
def test_approval_fees_distinguish_known_zero_from_unknown(locale, zero, unknown):
    token = set_locale(locale)
    try:
        assert zero in components._fact_rows_html({"fees": "0/1"})
        assert unknown in components._fact_rows_html({"fees": None})
        assert "12.50" in components._fact_rows_html({"fees": "25/2"})
        assert zero not in components._fact_rows_html({"fees": "25/2"})
    finally:
        reset_locale(token)


@pytest.mark.parametrize("locale", ["zh-CN", "en"])
def test_budget_reason_names_action_and_amount_without_changing_card(locale):
    controller = DemoController()
    controller.start_session()
    draft = controller.preview_birthday()
    card = next(node["card"] for node in draft["nodes"] if node["card"]["kind"] == "lock_funds")
    assert card["fees"] == "0/1"
    assert card["purpose"] == "birthday"
    original = deepcopy(card)
    token = set_locale(locale)
    try:
        text = components.tier_reason(card, controller=controller)
    finally:
        reset_locale(token)
    assert "1,000.00" in text
    assert ("锁定" if locale == "zh-CN" else "Lock") in text
    assert ("黄色" if locale == "zh-CN" else "yellow") in text
    assert ("生日" if locale == "zh-CN" else "birthday") in text
    assert card == original


@pytest.mark.parametrize("locale", ["zh-CN", "en"])
def test_subscription_and_card_reasons_name_the_exact_effect(locale):
    token = set_locale(locale)
    try:
        lookup = components.tier_reason({"kind": "subscription_query", "tier": "yellow"})
        cancel = components.tier_reason({"kind": "cancel_subscription", "tier": "yellow"})
        lost = components.tier_reason({
            "kind": "report_card_lost",
            "tier": "red",
            "tier_reasons": ["sensitive_operation"],
        })
    finally:
        reset_locale(token)
    assert ("读取订阅" if locale == "zh-CN" else "Read subscription") in lookup
    assert ("未来扣费" if locale == "zh-CN" else "future debits") in cancel
    assert ("挂失" if locale == "zh-CN" else "Report lost card") in lost
    assert ("多因子" if locale == "zh-CN" else "MFA") in lost
