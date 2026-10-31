"""The conversation and IM views preserve explicit authorization gestures."""

import pytest

from tests.e2e.test_demo_ui import app, navigate


@pytest.mark.parametrize(
    "text,route",
    [
        ("给Chen转200元", "transfers"),
        ("看看上个月的账单", "billing"),
        ("我想买点稳健理财", "wealth"),
        ("我的卡丢了", "cards"),
        ("看看有哪些订阅", "subscriptions"),
        ("帮我安排生日", "birthday"),
    ],
)
def test_home_routes_without_implicit_payment(text, route):
    page = app()
    page.button(key="enter").click().run()
    page.text_input(key="domestic_text").set_value(text)
    page.button(key="domestic_send").click().run()
    assert not page.exception
    session = page.session_state["domestic_session"]
    response = session.response(page.session_state["domestic_response_id"])
    assert response["route"] == route
    assert page.session_state["page"] == route
    assert session.controller.snapshot()["dispatch_count"] == 0
    assert not session.controller.state.user_inputs


def test_home_transfer_and_im_share_session_but_require_separate_device_gestures():
    page = app()
    page.button(key="enter").click().run()
    page.text_input(key="domestic_text").set_value("给Chen转200元")
    page.button(key="domestic_send").click().run()
    page.button(key="domestic_confirm").click().run()
    assert not page.exception
    session = page.session_state["domestic_session"]
    assert session.controller.snapshot()["dispatch_count"] == 1
    navigate(page, "im")
    page.text_input(key="im_text").set_value("再给Chen转100元")
    page.button(key="im_send").click().run()
    assert not page.exception
    assert page.session_state["domestic_session"] is session
    assert session.controller.snapshot()["dispatch_count"] == 1
    page.button(key="im_confirm").click().run()
    assert not page.exception
    assert session.controller.snapshot()["dispatch_count"] == 2
    page.run()
    assert session.controller.snapshot()["dispatch_count"] == 2


def test_unsupported_chat_does_not_invoke_tools():
    page = app()
    page.button(key="enter").click().run()
    page.text_input(key="domestic_text").set_value("写一首生日诗")
    page.button(key="domestic_send").click().run()
    assert not page.exception
    session = page.session_state["domestic_session"]
    response = session.response(page.session_state["domestic_response_id"])
    assert response["status"] == "refused"
    assert session.tool_calls == 0


def test_operator_button_retires_the_same_conversation_drafts():
    page = app()
    page.button(key="enter").click().run()
    page.text_input(key="domestic_text").set_value("给Chen转200元")
    page.button(key="domestic_send").click().run()
    session = page.session_state["domestic_session"]
    identifier = page.session_state["domestic_response_id"]
    navigate(page, "operator")
    page.button(key="handoff").click().run()
    assert not page.exception
    assert session.response(identifier)["status"] == "withdrawn"
    assert not session.controller._drafts
    assert session.controller.snapshot()["dispatch_count"] == 0
