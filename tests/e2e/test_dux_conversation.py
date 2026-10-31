"""Product views preserve the existing explicit device boundaries."""

import pytest

from corridor.crypto.jcs import H
from tests.e2e.test_demo_ui import app, navigate, request_device_code, submit_device_code


@pytest.mark.parametrize("locale", ["zh-CN", "en"])
def test_shortcut_only_prepares_unsigned_request_and_cancel_retires_it(monkeypatch, locale):
    monkeypatch.setenv("CORRIDOR_LOCALE", locale)
    page = app()
    page.button(key="enter").click().run()
    page.button(key="domestic_example_transfer").click().run()
    controller = page.session_state["controller"]
    assert not page.exception
    assert controller.snapshot()["dispatch_count"] == 0
    assert not controller.state.user_inputs
    page.text_input(key="domestic_text").set_value("给Chen转200元")
    page.button(key="domestic_send").click().run()
    session = page.session_state["domestic_session"]
    identifier = page.session_state["domestic_response_id"]
    page.button(key="domestic_cancel").click().run()
    assert session.response(identifier)["status"] == "withdrawn"
    assert session.confirm(identifier)["status"] == "withdrawn"
    assert controller.snapshot()["dispatch_count"] == 0


def test_fallback_offers_shortcuts_and_does_not_invoke_tools():
    page = app()
    page.button(key="enter").click().run()
    page.text_input(key="domestic_text").set_value("明天天气怎么样")
    page.button(key="domestic_send").click().run()
    assert not page.exception
    assert page.session_state["domestic_session"].tool_calls == 0
    assert page.button(key="domestic_fallback_billing")


def test_im_display_does_not_change_canonical_response_or_authority():
    from corridor.agent.domestic_session import DomesticSession
    from corridor.channels.im import render_message
    from corridor.ui.controller import DemoController

    controller = DemoController()
    controller.start_session()
    session = DomesticSession(controller)
    response = session.receive("给Chen转200元")
    before = H(response)
    message = render_message(response)
    assert "200/1" not in message["text"]
    assert response["audit_hash"] not in message["text"]
    assert H(response) == before
    assert controller.snapshot()["dispatch_count"] == 0


def test_three_demo_presets_require_explicit_load():
    page = app()
    page.button(key="enter").click().run()
    before = H(page.session_state["controller"].state)
    page.selectbox(key="demo_preset").select("daily_threshold").run()
    assert H(page.session_state["controller"].state) == before

    page.button(key="load_demo_preset").click().run()
    assert not page.exception
    demo = page.session_state["demo_scenario"]
    assert demo["kind"] == "daily_threshold"
    assert demo["setup_result"]["status"] == "settled"
    assert demo["controller"].snapshot()["dispatch_count"] == 1
    assert page.session_state["controller"] is not demo["controller"]
    navigate(page, "conversation")
    assert H(page.session_state["controller"].state) == before


def test_threshold_demo_selection_mfa_and_final_confirmation_are_separate():
    page = app()
    page.button(key="enter").click().run()
    page.selectbox(key="demo_preset").select("daily_threshold").run()
    page.button(key="load_demo_preset").click().run()
    page.button(key="demo_example_transfer").click().run()
    page.selectbox(key="demo_clarify_contact").select("CONTACT_LIN_A").run()
    page.button(key="demo_select_contact").click().run()
    controller = page.session_state["demo_scenario"]["controller"]
    assert controller.snapshot()["dispatch_count"] == 1
    page.button(key="demo_selected_confirm").click().run()
    assert controller.snapshot()["dispatch_count"] == 1
    (local,) = [
        value
        for key, value in page.session_state.filtered_state.items()
        if key.startswith("demo_selected:")
    ]
    assert local["status"] == "waiting_mfa"
    pending = local["pending_id"]
    assert not controller._pending[pending]["verified"]
    request_device_code(page, pending, prefix="demo_selected_mfa")
    assert controller.snapshot()["dispatch_count"] == 1
    submit_device_code(page, controller, pending, prefix="demo_selected_mfa")
    assert controller.snapshot()["dispatch_count"] == 1
    page.button(key="demo_selected_confirm").click().run()
    assert not page.exception
    assert controller.snapshot()["dispatch_count"] == 2
    assert not [button for button in page.button if button.key == "demo_select_contact"]
    page.run()
    assert controller.snapshot()["dispatch_count"] == 2


def test_unknown_demo_lookup_never_replays_payment():
    page = app()
    page.button(key="enter").click().run()
    page.selectbox(key="demo_preset").select("birthday_unknown").run()
    page.button(key="load_demo_preset").click().run()
    controller = page.session_state["demo_scenario"]["controller"]
    assert controller.snapshot()["balances"]["A_LOCKED"] == "1000/1"
    assert not controller.state.orders
    page.button(key="demo_birthday_orders").click().run()
    assert not page.exception
    latest = {event.intent_key: event for event in controller.state.ledger}
    assert any(event.event == "unknown" for event in latest.values())
    before = controller.snapshot()["dispatch_count"]
    page.button(key="demo_recovery_task_stop").click().run()
    page.button(key="demo_reconcile").click().run()
    assert not page.exception
    assert controller.snapshot()["dispatch_count"] == before
    assert page.session_state["demo_scenario"]["reconciled"][0]["status"] == "settled"


def test_partial_birthday_selection_requires_review_of_replacement_nodes():
    page = app()
    page.button(key="enter").click().run()
    page.button(key="domestic_example_birthday").click().run()
    page.checkbox[0].check()
    page.button(key="domestic_confirm").click().run()
    page.run()
    controller = page.session_state["controller"]
    assert controller.snapshot()["dispatch_count"] == 0
    assert len(page.checkbox) == 1
    page.checkbox[0].check()
    page.button(key="domestic_confirm").click().run()
    assert not page.exception
    assert controller.snapshot()["dispatch_count"] == 1
    assert not controller.state.schedules
