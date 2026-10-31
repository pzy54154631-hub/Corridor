"""UI-POLISH P5: suitability lines cite the certificate; the R4 refusal stays isolated."""

from copy import deepcopy

import pytest

from corridor.crypto.jcs import H
from corridor.i18n import reset_locale, set_locale
from corridor.ui import suitability
from corridor.ui.controller import DemoController, demo_state
from tests.e2e.test_demo_ui import app


def _purchase(product=None):
    controller = DemoController(initial_state=demo_state(showcase=True))
    controller.start_session()
    controller.compare_investments("net_return")
    result = controller.preview_investment(product, request_id=f"SUIT-{product}")
    return controller, result["card"]


def test_default_purchase_card_has_exactly_five_lines():
    controller, card = _purchase()
    before = deepcopy(card)
    token = set_locale("zh-CN")
    try:
        lines = suitability.lines(card, controller.state)
    finally:
        reset_locale(token)
    assert len(lines) == 5
    assert "R2" in lines[0]
    assert "C2" in lines[1]
    assert "合格产品集合内" in lines[2] and "以内核裁决为准" in lines[2]
    assert "30 天" in lines[3] and "T+0" in lines[3] and "365 天" in lines[3]
    assert "3%" in lines[4] and "3/100" not in lines[4] and "非存款" in lines[4]
    assert card == before


@pytest.mark.parametrize("product", ["P_A", "P_B", "P_C", "P_D", "P_E"])
def test_match_line_follows_the_certificate_candidates_only(product):
    controller, card = _purchase(product)
    token = set_locale("zh-CN")
    try:
        lines = suitability.lines(card, controller.state)
    finally:
        reset_locale(token)
    certificate = card["best_interest"]
    outside = product not in certificate["candidate_ids"]
    assert ("不在您的合格产品集合内" in lines[2]) is outside
    if product == "P_C":
        assert certificate["exclusions"]["P_C"] == ["risk"]
        assert "风险" in lines[2]


def test_non_purchase_or_incomplete_cards_have_no_lines():
    controller, card = _purchase()
    assert suitability.lines({**card, "risk": None}, controller.state) == []
    assert suitability.lines({**card, "kind": "transfer"}, controller.state) == []
    assert suitability.lines({}, controller.state) == []


def test_r4_preset_is_rejected_in_isolation_and_offers_no_approval(monkeypatch):
    monkeypatch.setenv("CORRIDOR_LOCALE", "zh-CN")
    page = app()
    page.button(key="enter").click().run()
    user = page.session_state["controller"]
    before = H(user.state)
    page.selectbox(key="demo_preset").select("suitability_r4").run()
    page.button(key="load_demo_preset").click().run()
    assert not page.exception
    demo = page.session_state["demo_scenario"]
    r4 = demo["r4"]
    assert r4["outcome"] == "Reject"
    assert "BIC-RISK" in r4["rule_ids"]
    assert r4["execute_count"] == 0
    assert r4["challenge_id"] is None
    assert demo["controller"] is not user
    assert H(user.state) == before
    assert user.gateway.safety_snapshot(user.device.principal)["state"] == "Normal"
    keys = [button.key for button in page.button]
    assert not any(key and (key.endswith(":request") or "approve" in key) for key in keys)
    shown = "\n".join(str(item.value) for item in page.markdown)
    assert "无法申购" in shown and "银行派发 0 次" in shown and "本演示未请求短信验证码" in shown
