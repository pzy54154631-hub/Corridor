"""English structured transfer gestures retain canonical previews and MFA boundaries."""

from corridor.crypto.jcs import H
from tests.e2e.test_demo_ui import app, navigate, request_device_code, submit_device_code


def transfer_page():
    page = app()
    page.button(key="enter").click().run()
    navigate(page, "transfers")
    return page


def test_structured_form_starts_without_recipient_or_signatures():
    page = transfer_page()
    controller = page.session_state["controller"]
    before = H(controller.state)
    assert page.selectbox(key="structured_source").value is None
    assert page.selectbox(key="structured_contact").value is None
    page.text_input(key="structured_amount").set_value("200")
    page.button(key="structured_preview").click().run()
    assert not page.exception
    assert not page.session_state.filtered_state.get("structured_transfer_pending")
    assert H(controller.state) == before
    assert not controller.state.user_inputs
    assert controller.gateway.bank.execute_count == 0
    assert all(button.key != "structured_approve" for button in page.button)


def test_structured_english_form_preview_and_one_confirmation_use_retained_exact_facts():
    page = transfer_page()
    controller = page.session_state["controller"]
    before = H(controller.state)
    page.selectbox(key="structured_source").select("A_CURRENT")
    page.selectbox(key="structured_contact").select("CONTACT_LIN_B")
    page.text_input(key="structured_amount").set_value("200.25")
    page.button(key="structured_preview").click().run()
    assert not page.exception
    assert H(controller.state) == before
    page.text_input(key="structured_amount").set_value("999")
    page.button(key="structured_approve").click().run()
    assert not page.exception
    assert controller.gateway.bank.final_state()["destinations"] == {"DEST_CONTACT_LIN_B": "801/4"}
    assert controller.gateway.bank.execute_count == 1
    assert page.session_state["structured_transfer_result"]["status"] == "settled"
    page.run()
    assert controller.gateway.bank.execute_count == 1
    assert all(button.key != "structured_approve" for button in page.button)
    assert controller.gateway.verify_log()


def test_structured_red_form_requires_separate_device_mfa_and_final_approval():
    page = transfer_page()
    controller = page.session_state["controller"]
    page.selectbox(key="structured_source").select("A_CURRENT")
    page.selectbox(key="structured_contact").select("CONTACT_CHEN")
    page.text_input(key="structured_amount").set_value("1200")
    page.button(key="structured_preview").click().run()
    assert not controller.state.user_inputs
    page.button(key="structured_approve").click().run()
    pending = page.session_state["structured_transfer_pending"]["pending_id"]
    assert controller.gateway.bank.execute_count == 0
    navigate(page, "device")
    request_device_code(page, pending)
    assert controller.gateway.bank.execute_count == 0
    submit_device_code(page, controller, pending)
    assert controller.gateway.bank.execute_count == 0
    page.button(key=f"approve:{pending}").click().run()
    assert not page.exception
    assert controller.gateway.bank.execute_count == 1
    assert len(controller.state.security.consumed_grant_ids) == 1
    assert len(controller.state.security.consumed_nonces) == 1
    navigate(page, "transfers")
    completed = next(
        row for row in controller.snapshot()["pending"] if row["pending_id"] == pending
    )
    assert completed["completed"]
    assert completed["result"]["status"] == "settled"
    page.run()
    assert controller.gateway.bank.execute_count == 1
