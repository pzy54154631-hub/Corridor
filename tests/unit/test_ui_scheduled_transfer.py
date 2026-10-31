"""Explicit schedule inputs remain unsigned until the existing device gesture."""

import pytest

from corridor.crypto.jcs import H
from corridor.ui.controller import DemoController
from corridor.ui.structured_transfer import preview_scheduled_transfer


@pytest.mark.parametrize("monthly,tier", [(None, "yellow"), (20, "red")])
def test_schedule_preview_and_required_device_authorization(monthly, tier):
    controller = DemoController()
    controller.start_session()
    before = H(controller.state)
    preview = preview_scheduled_transfer(
        controller,
        "A_CURRENT",
        "CONTACT_CHEN",
        "200",
        "ui-schedule",
        date_text="2026-10-20",
        monthly_day=monthly,
    )
    assert H(controller.state) == before
    assert preview["card"]["tier"] == tier
    result = controller.approve_pending(preview["pending_id"])
    if monthly:
        assert result["status"] == "waiting_mfa"
        challenge = controller.request_mfa(preview["pending_id"])["challenge_id"]
        code = controller.gateway.mfa.test_code(challenge)
        controller.complete_mfa(preview["pending_id"], code=code)
        result = controller.approve_pending(preview["pending_id"])
    assert result["status"] == "controlled"
    assert controller.state.schedules
    assert controller.gateway.bank.execute_count == 0


def test_schedule_rejects_past_or_inexact_date_without_signing():
    controller = DemoController()
    controller.start_session()
    before = H(controller.state)
    for date in ("2025-10-20", "invalid"):
        with pytest.raises(ValueError):
            preview_scheduled_transfer(
                controller,
                "A_CURRENT",
                "CONTACT_CHEN",
                "200",
                date,
                date_text=date,
            )
    assert H(controller.state) == before


@pytest.mark.parametrize(
    "date,monthly", [("0001-01-01", None), ("0001-01-01", 20), ("9999-12-31", 20)]
)
def test_schedule_date_overflow_is_a_validation_error_without_signing(date, monthly):
    controller = DemoController()
    controller.start_session()
    before = H(controller.state)
    with pytest.raises(ValueError, match="date"):
        preview_scheduled_transfer(
            controller,
            "A_CURRENT",
            "CONTACT_CHEN",
            "200",
            "ui-date-overflow",
            date_text=date,
            monthly_day=monthly,
        )
    assert H(controller.state) == before
    assert not controller._pending
    assert not controller._request_facts


def test_schedule_page_renders_unsigned_monthly_card():
    from tests.e2e.test_demo_ui import app, navigate

    page = app()
    page.button(key="enter").click().run()
    navigate(page, "transfers")
    page.selectbox(key="structured_source").select("A_CURRENT")
    page.selectbox(key="structured_contact").select("CONTACT_CHEN")
    page.text_input(key="structured_amount").set_value("200")
    page.radio(key="structured_timing").set_value("monthly")
    page.text_input(key="structured_date").set_value("2026-10-20")
    page.button(key="structured_preview").click().run()
    assert not page.exception
    assert page.session_state["structured_transfer_pending"]["card"]["tier"] == "red"
    controller = page.session_state["controller"]
    assert not controller.state.schedules
    assert not controller.state.user_inputs
