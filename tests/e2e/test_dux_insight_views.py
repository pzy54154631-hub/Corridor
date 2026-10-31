"""Billing and attack views remain read-only to the current banking session."""

import pytest

from corridor.crypto.jcs import H
from tests.e2e.test_demo_ui import app, navigate


@pytest.mark.parametrize("locale", ["zh-CN", "en"])
def test_billing_charts_do_not_repeat_queries_on_render(monkeypatch, locale):
    monkeypatch.setenv("CORRIDOR_LOCALE", locale)
    page = app()
    page.button(key="enter").click().run()
    controller = page.session_state["controller"]
    navigate(page, "billing")
    assert not page.exception
    assert not controller.state.receipts
    page.button(key="bill_report").click().run()
    assert not page.exception
    assert page.session_state["bill_result"]["status"] == "settled"
    assert any("viewBox" in item.value for item in page.markdown)
    before = H(controller.state)
    page.run()
    assert H(controller.state) == before
    assert controller.gateway.bank.execute_count == 0
    assert not controller.state.user_inputs


@pytest.mark.parametrize("locale", ["zh-CN", "en"])
def test_privacy_comparison_isolates_live_session_and_renders_both_observations(
    monkeypatch, locale
):
    monkeypatch.setenv("CORRIDOR_LOCALE", locale)
    page = app()
    page.button(key="enter").click().run()
    controller = page.session_state["controller"]
    before = H(controller.state)
    navigate(page, "audit")
    page.button(key="domestic_attack:data_extraction").click().run()
    assert not page.exception
    result = page.session_state["domestic_attack_result"]
    assert result["attack"]["privacy_observation"]["leaks"] == 0
    assert result["baseline"]["attack"]["oracle"]["pii_leaks"] == 1
    assert page.success and page.error
    assert H(controller.state) == before
    assert controller.gateway.bank.execute_count == 0
