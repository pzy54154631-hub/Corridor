"""The updated manual gestures retain wealth, unknown-outcome and transfer semantics."""

import pytest

from tests.e2e.test_demo_ui import app, navigate
from tests.e2e.test_structured_transfer_ui import transfer_page
from tests.e2e.test_wealth_ui import prepare_wealth


def _start_allocation(page):
    controller = prepare_wealth(page)
    for box in page.checkbox:
        if box.key.startswith("wealth_select:"):
            box.check()
    page.run()
    page.button(key="wealth_start").click().run()
    navigate(page, "device")
    assert not page.exception
    return controller


def _verify_wealth_leg_manually(page, controller):
    identifier = controller.leg["action"].action_id
    assert page.button(key=f"wealth_approve:{identifier}").disabled
    page.button(key=f"wealth_request:{identifier}").click().run()
    assert not page.exception
    assert page.button(key=f"wealth_approve:{identifier}").disabled
    # Submitting the empty manual form cannot silently read the simulated SMS.
    page.button(key=f"wealth_verify:{identifier}").click().run()
    assert not controller.leg["verified"]
    assert page.button(key=f"wealth_approve:{identifier}").disabled
    challenge = controller.leg["challenge_id"]
    code = controller.gateway.mfa.test_code(challenge)
    field = f"wealth_code:{identifier}"
    page.text_input(key=field).set_value(code)
    page.button(key=f"wealth_verify:{identifier}").click().run()
    assert not page.exception
    assert controller.leg["verified"]
    assert page.session_state.filtered_state.get(field, "") == ""
    assert not page.button(key=f"wealth_approve:{identifier}").disabled
    return identifier


@pytest.mark.parametrize("locale", ["zh-CN", "en"])
def test_manual_wealth_two_leg_completion_keeps_separate_grants_and_receipts(monkeypatch, locale):
    monkeypatch.setenv("CORRIDOR_LOCALE", locale)
    page = app()
    controller = _start_allocation(page)
    for index in range(2):
        identifier = _verify_wealth_leg_manually(page, controller)
        assert controller.gateway.bank.execute_count == index
        page.button(key=f"wealth_approve:{identifier}").click().run()
        assert not page.exception
        assert len(controller.state.positions) == index + 1
    page.run()
    assert controller.gateway.bank.execute_count == 2
    assert controller.execution_snapshot()["available_balance"] == "2000/1"
    assert controller.execution_snapshot()["status"] == "completed"
    assert len(controller.state.security.consumed_grant_ids) == 2
    assert not controller.state.security.locked_at
    assert controller.gateway.verify_log()


def test_manual_wealth_unknown_outcome_requires_lookup_without_repayment():
    from corridor.kernel.binding import intent_key

    page = app()
    controller = _start_allocation(page)
    controller.gateway.bank.faults[intent_key(controller.leg["action"], controller.state)] = (
        "response_loss"
    )
    identifier = _verify_wealth_leg_manually(page, controller)
    page.button(key=f"wealth_approve:{identifier}").click().run()
    assert not page.exception
    assert controller.execution_snapshot()["status"] == "unknown"
    assert page.button(key="wealth_lookup")
    assert not [
        button for button in page.button if (button.key or "").startswith("wealth_approve:")
    ]
    page.button(key="wealth_lookup").click().run()
    assert not page.exception
    assert controller.gateway.bank.execute_count == 1
    assert controller.leg is None
    assert controller.gateway.verify_log()


def test_structured_red_transfer_needs_typed_code_and_separate_final_approval():
    page = transfer_page()
    controller = page.session_state["controller"]
    page.selectbox(key="structured_source").select("A_CURRENT")
    page.selectbox(key="structured_contact").select("CONTACT_CHEN")
    page.text_input(key="structured_amount").set_value("1200")
    page.button(key="structured_preview").click().run()
    assert not controller.state.user_inputs
    page.button(key="structured_approve").click().run()
    identifier = page.session_state["structured_transfer_pending"]["pending_id"]
    assert controller.gateway.bank.execute_count == 0
    navigate(page, "device")
    key = f"device_mfa:{identifier}"
    page.button(key=f"{key}:request").click().run()
    page.button(key=f"{key}:verify").click().run()
    assert not controller._pending[identifier]["verified"]
    assert controller.gateway.bank.execute_count == 0
    code = controller.gateway.mfa.test_code(controller._pending[identifier]["challenge_id"])
    page.text_input(key=f"{key}:code").set_value(code)
    page.button(key=f"{key}:verify").click().run()
    assert not page.exception
    assert controller._pending[identifier]["verified"]
    assert page.session_state.filtered_state.get(f"{key}:code", "") == ""
    assert controller.gateway.bank.execute_count == 0
    page.button(key=f"approve:{identifier}").click().run()
    assert not page.exception
    assert controller.gateway.bank.execute_count == 1
    assert len(controller.state.security.consumed_grant_ids) == 1
    assert len(controller.state.security.consumed_nonces) == 1
    navigate(page, "transfers")
    assert controller._results[identifier]["status"] == "settled"
    page.run()
    assert controller.gateway.bank.execute_count == 1
    assert controller.gateway.verify_log()
