"""UI-POLISH P4: synthetic SMS text summarises the same canonical card, display only."""

import re
from copy import deepcopy

import pytest

from corridor.agent.domestic_session import DomesticSession
from corridor.crypto.jcs import H
from corridor.ui.components import _MACHINE_VALUE
from corridor.ui.controller import DemoController, demo_state
from corridor.ui.phone import build_sms, sms_text

PHONE = re.compile(r"(?<!\d)1\d{10}(?!\d)")
SHORT_95 = re.compile(r"(?<!\d)95\d{3}(?!\d)")


def _controller():
    controller = DemoController(initial_state=demo_state(showcase=True))
    controller.start_session()
    return controller


def _transfer_card(text):
    controller = _controller()
    session = DomesticSession(controller)
    response = session.receive(text)
    return controller, response["card"]


def _render(sms, locale):
    text = sms_text(sms, locale=locale)
    assert not _MACHINE_VALUE.search(text)
    assert not PHONE.search(text)
    assert not SHORT_95.search(text)
    return text


@pytest.mark.parametrize("locale", ["zh-CN", "en"])
def test_transfer_sms_names_action_amount_and_synthetic_tail(locale):
    controller, card = _transfer_card("给妈妈转800元")
    before = deepcopy(card)
    sms = build_sms(
        "challenge-1",
        code="482913",
        expiry="2026-10-16T02:32:00Z",
        kind=card["kind"],
        card=card,
        state=controller.state,
    )
    text = _render(sms, locale)
    assert "482913" in text and "3721" in text
    assert "2026-10-16 10:32:00" in text
    if locale == "zh-CN":
        assert "转账" in text and "¥800.00" in text and "尾号3721" in text
        assert "第一次" not in text
    else:
        assert "Transfer" in text and "CNY 800.00" in text
    assert card == before


def test_new_payee_adds_the_verification_sentence():
    controller, card = _transfer_card("给新联系人转480元")
    assert card["objects"]["contact_ref"] == "CONTACT_NEW"
    sms = build_sms(
        "challenge-2",
        code="111222",
        expiry="2026-10-16T02:32:00Z",
        kind=card["kind"],
        card=card,
        state=controller.state,
    )
    # S6 R6: the SMS says what the registry says ("registered as a new recipient", the
    # same is_new flag as the fraud warning's first line and the recipient check), not a
    # payment history ("your first payment to"); the verification advice is unchanged.
    zh, en = _render(sms, "zh-CN"), _render(sms, "en")
    assert "该收款人登记为新收款人" in zh and "请用您自己保存的联系方式核实对方身份" in zh
    assert "第一次" not in zh and "首次" not in zh
    assert "registered as a new recipient" in en and "contact details you saved yourself" in en
    assert "first payment" not in en


def test_purchase_and_unlock_templates():
    controller = _controller()
    controller.compare_investments("net_return")
    card = controller.preview_investment(None, request_id="SMS-PURCHASE")["card"]
    state_before = H(controller.state)
    purchase = build_sms(
        "challenge-3",
        code="333444",
        expiry="2026-10-16T02:32:00Z",
        kind=card["kind"],
        card=card,
        state=controller.state,
    )
    text = _render(purchase, "zh-CN")
    assert "申购" in text and "稳健30天持有（合成）" in text and "理财非存款" in text
    assert "P_A" not in text
    unlock = build_sms("challenge-4", code="555666", expiry="2026-10-16T02:32:00Z", kind="unlock")
    assert "解除账户安全锁" in _render(unlock, "zh-CN")
    assert "safety lock" in _render(unlock, "en")
    assert H(controller.state) == state_before


def test_retired_copy_masks_the_code():
    from dataclasses import replace

    sms = build_sms("challenge-5", code="777888", expiry="2026-10-16T02:32:00Z", kind="unlock")
    assert "777888" not in sms_text(replace(sms, status="used", code=""), locale="zh-CN")
