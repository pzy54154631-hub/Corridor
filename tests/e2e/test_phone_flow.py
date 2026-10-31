"""UI-POLISH P4/P7: the code reaches a separate simulated phone page, never the main page."""

import json
from secrets import token_hex
from unittest.mock import patch

from corridor.ui.phone_outbox import OUTBOX
from tests.e2e.test_demo_ui import APP


def unstarted_app():
    def workspace_directory(*args, **kwargs):
        path = APP.parents[3] / ".tmp" / ("streamlit-" + token_hex(8))
        path.mkdir(parents=True)
        return str(path)

    with patch("tempfile.mkdtemp", side_effect=workspace_directory):
        from streamlit.testing.v1 import AppTest

    return AppTest.from_file(str(APP), default_timeout=30)


def visible_values(page):
    return [
        str(item.value)
        for elements in (
            page.code,
            page.markdown,
            page.caption,
            page.text,
            page.info,
            page.warning,
            page.success,
        )
        for item in elements
    ]


def red_transfer_to_code_request(monkeypatch):
    monkeypatch.setenv("CORRIDOR_LOCALE", "zh-CN")
    page = unstarted_app().run()
    page.button(key="enter").click().run()
    page.text_input(key="domestic_text").set_value("给妈妈转1500元")
    page.button(key="domestic_send").click().run()
    page.button(key="domestic_confirm").click().run()
    page.button(key="domestic_verify_mfa:request").click().run()
    assert not page.exception
    controller = page.session_state["controller"]
    session = page.session_state["domestic_session"]
    response = session.response(page.session_state["domestic_response_id"])
    pending_id = response["payload"]["pending_id"]
    challenge = controller._pending[pending_id]["challenge_id"]
    return page, controller, session, pending_id, controller.gateway.mfa.test_code(challenge)


def test_code_only_on_the_phone_page_and_typed_code_still_approves(monkeypatch):
    page, controller, session, pending_id, code = red_transfer_to_code_request(monkeypatch)
    assert not any(code in value for value in visible_values(page))
    pair = page.session_state["phone_pair"]
    inbox = OUTBOX.inbox(pair)
    assert len(inbox) == 1 and inbox[0].code == code and inbox[0].status == "active"
    assert page.text_input(key="domestic_verify_mfa:code")
    assert any("模拟手机" in value for value in visible_values(page))

    phone = unstarted_app()
    phone.query_params["phone"] = pair
    phone.run()
    assert not phone.exception
    assert "controller" not in phone.session_state
    assert "wealth_controller" not in phone.session_state
    shown = "\n".join(str(item.value) for item in phone.markdown)
    assert code in shown and "尾号3721" in shown and "¥1,500.00" in shown

    unknown = unstarted_app()
    unknown.query_params["phone"] = "not-a-pair"
    unknown.run()
    assert not unknown.exception
    assert "未配对" in "\n".join(str(item.value) for item in unknown.markdown)
    assert code not in "\n".join(str(item.value) for item in unknown.markdown)

    page.text_input(key="domestic_verify_mfa:code").set_value(code)
    page.button(key="domestic_verify_mfa:verify").click().run()
    assert not page.exception
    assert controller._pending[pending_id]["verified"]
    assert OUTBOX.inbox(pair)[0].status == "used"
    phone.run()
    assert code not in "\n".join(str(item.value) for item in phone.markdown)
    assert controller.gateway.bank.execute_count == 0
    page.button(key="domestic_confirm").click().run()
    assert not page.exception
    assert controller.gateway.bank.execute_count == 1
    records = json.dumps(controller.gateway.audit.records(), ensure_ascii=False, default=str)
    history = json.dumps(session.history, ensure_ascii=False, default=str)
    assert code not in records and code not in history


def test_second_red_transfer_in_one_chat_still_needs_its_own_typed_code(monkeypatch):
    """Chat reuses one MFA form key; an earlier verified outcome never verifies a later card."""
    page, controller, session, first, code = red_transfer_to_code_request(monkeypatch)
    page.text_input(key="domestic_verify_mfa:code").set_value(code)
    page.button(key="domestic_verify_mfa:verify").click().run()
    page.button(key="domestic_confirm").click().run()
    assert controller.gateway.bank.execute_count == 1
    page.text_input(key="domestic_text").set_value("给妈妈转1600元")
    page.button(key="domestic_send").click().run()
    page.button(key="domestic_confirm").click().run()
    page.button(key="domestic_verify_mfa:request").click().run()
    assert not page.exception
    response = session.response(page.session_state["domestic_response_id"])
    second = response["payload"]["pending_id"]
    assert second != first
    assert response["status"] == "waiting_mfa"
    assert not controller._pending[second]["verified"]
    assert page.text_input(key="domestic_verify_mfa:code")
    challenge = controller._pending[second]["challenge_id"]
    second_code = controller.gateway.mfa.test_code(challenge)
    assert second_code in [
        sms.code for sms in OUTBOX.inbox(page.session_state["phone_pair"]) if sms.status == "active"
    ]
    page.text_input(key="domestic_verify_mfa:code").set_value(second_code)
    page.button(key="domestic_verify_mfa:verify").click().run()
    assert controller._pending[second]["verified"]
    page.button(key="domestic_confirm").click().run()
    assert not page.exception
    assert controller.gateway.bank.execute_count == 2


def test_judge_phone_page_adds_the_simulation_disclosure(monkeypatch):
    page, *_ = red_transfer_to_code_request(monkeypatch)
    pair = page.session_state["phone_pair"]
    phone = unstarted_app()
    phone.query_params["phone"] = pair
    phone.query_params["view"] = "judge"
    phone.run()
    shown = "\n".join(str(item.value) for item in phone.markdown)
    assert "网关不校验短信正文" in shown and "不代表收款账户核验" in shown
    customer = unstarted_app()
    customer.query_params["phone"] = pair
    customer.query_params["view"] = "customer"
    customer.run()
    assert "网关不校验短信正文" not in "\n".join(str(item.value) for item in customer.markdown)
