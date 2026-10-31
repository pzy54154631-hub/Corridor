"""Manual device recovery never reads a code on behalf of the user."""

from types import SimpleNamespace

import pytest

from corridor.ui import manual_mfa
from tests.e2e.test_demo_ui import app, navigate
from tests.unit.test_ui_manual_mfa_paths import _start_allocation


def _password_challenge():
    page = app()
    page.button(key="enter").click().run()
    navigate(page, "device")
    page.button(key="preview_password").click().run()
    controller = page.session_state["controller"]
    identifier = page.session_state["password_pending"]["pending_id"]
    page.button(key=f"approve:{identifier}").click().run()
    prefix = f"device_mfa:{identifier}"
    page.button(key=f"{prefix}:request").click().run()
    return page, controller, identifier, prefix


@pytest.mark.parametrize("failure", ["expired", "locked"])
def test_bank_dead_code_is_hidden_and_existing_cancel_remains(failure):
    page, controller, identifier, prefix = _password_challenge()
    challenge = controller._pending[identifier]["challenge_id"]
    code = controller.gateway.mfa.test_code(challenge)
    if failure == "expired":
        controller.advance_clock(120)
        page.run()
    else:
        for _ in range(3):
            page.text_input(key=f"{prefix}:code").set_value(
                "000000" if code != "000000" else "111111"
            )
            page.button(key=f"{prefix}:verify").click().run()
    assert not page.exception
    assert not any(item.key == f"{prefix}:code" for item in page.text_input)
    assert not any(item.value == code for item in page.code)
    assert any("验证码已失效或已锁定" in item.value for item in page.warning)
    assert page.button(key=f"cancel:{identifier}")
    page.button(key=f"cancel:{identifier}").click().run()
    assert not controller._pending
    assert controller.gateway.bank.execute_count == 0
    if failure == "expired":
        page.button(key="preview_password").click().run()
        replacement = page.session_state["password_pending"]["pending_id"]
        assert replacement != identifier
        page.button(key=f"approve:{replacement}").click().run()
        page.button(key=f"device_mfa:{replacement}:request").click().run()
        assert controller._pending[replacement]["challenge_id"] != challenge
        assert not controller._pending[replacement]["verified"]


def test_repeated_bank_request_does_not_reset_attempts_or_expiry():
    page, controller, identifier, prefix = _password_challenge()
    challenge_id = controller._pending[identifier]["challenge_id"]
    challenge = controller.gateway.mfa._challenges[challenge_id]
    expiry = challenge.expiry
    code = controller.gateway.mfa.test_code(challenge_id)
    page.text_input(key=f"{prefix}:code").set_value("000000" if code != "000000" else "111111")
    page.button(key=f"{prefix}:verify").click().run()
    page.button(key=f"{prefix}:request").click().run()
    assert controller._pending[identifier]["challenge_id"] == challenge_id
    assert challenge.attempts == 1
    assert challenge.expiry == expiry
    assert not controller._pending[identifier]["verified"]


def test_wealth_expired_code_is_hidden_and_cancel_retires_all_remaining_legs():
    page = app()
    controller = _start_allocation(page)
    identifier = controller.leg["action"].action_id
    page.button(key=f"wealth_request:{identifier}").click().run()
    challenge = controller.leg["challenge_id"]
    code = controller.gateway.mfa.test_code(challenge)
    controller.advance_clock(120)
    page.run()
    assert not page.exception
    assert not any(item.key == f"wealth_code:{identifier}" for item in page.text_input)
    assert not any(item.value == code for item in page.code)
    assert any("验证码已失效或已锁定" in item.value for item in page.warning)
    page.button(key=f"wealth_cancel_mfa:{identifier}").click().run()
    assert not page.exception
    assert controller.leg is None
    assert controller.execution_snapshot()["status"] == "cancelled"
    assert controller.gateway.bank.execute_count == 0


def test_unlock_cancel_keeps_attempts_until_original_window_expires():
    page = app()
    page.button(key="enter").click().run()
    controller = page.session_state["controller"]
    controller.gateway.lock(controller.device.principal, "test_unlock_cancel")
    navigate(page, "safety")
    page.button(key="safety_current_cooldown").click().run()
    prefix = "safety_current_unlock"
    page.button(key=f"{prefix}:request").click().run()
    challenge_id = page.session_state[f"{prefix}:challenge"]
    challenge = controller.gateway.mfa._challenges[challenge_id]
    code = controller.gateway.mfa.test_code(challenge_id)
    page.text_input(key=f"{prefix}:code").set_value("000000" if code != "000000" else "111111")
    page.button(key=f"{prefix}:verify").click().run()
    page.button(key=f"{prefix}:cancel").click().run()
    page.button(key=f"{prefix}:request").click().run()
    assert page.session_state[f"{prefix}:challenge"] == challenge_id
    assert challenge.attempts == 1
    controller.advance_clock(120)
    page.run()
    assert not any(item.key == f"{prefix}:code" for item in page.text_input)
    assert not any(item.value == code for item in page.code)
    assert any("验证码已失效或已锁定" in item.value for item in page.warning)
    page.button(key=f"{prefix}:cancel").click().run()
    page.button(key=f"{prefix}:request").click().run()
    assert page.session_state[f"{prefix}:challenge"] != challenge_id
    assert challenge.attempts == 1
    assert controller.gateway.safety_snapshot(controller.device.principal)["state"] == "Locked"


def test_current_session_unlock_requires_cooldown_and_typed_code(monkeypatch):
    page = app()
    page.button(key="enter").click().run()
    controller = page.session_state["controller"]
    controller.gateway.lock(controller.device.principal, "test_current_ui")
    navigate(page, "safety")
    prefix = "safety_current_unlock"
    page.button(key=f"{prefix}:request").click().run()
    code = controller.gateway.mfa.test_code(page.session_state[f"{prefix}:challenge"])
    page.text_input(key=f"{prefix}:code").set_value(code)
    page.button(key=f"{prefix}:verify").click().run()
    assert controller.gateway.safety_snapshot(controller.device.principal)["state"] == "Locked"
    page.button(key="safety_current_cooldown").click().run()
    assert controller.gateway.safety_snapshot(controller.device.principal)["state"] == "Locked"
    page.button(key=f"{prefix}:request").click().run()
    page.button(key=f"{prefix}:verify").click().run()
    assert controller.gateway.safety_snapshot(controller.device.principal)["state"] == "Locked"
    code = controller.gateway.mfa.test_code(page.session_state[f"{prefix}:challenge"])
    monkeypatch.setattr(
        controller.device,
        "complete_mfa",
        lambda *args, **kwargs: pytest.fail("No automatic code completion"),
    )
    page.text_input(key=f"{prefix}:code").set_value(code)
    page.button(key=f"{prefix}:verify").click().run()
    assert not page.exception
    assert controller.gateway.safety_snapshot(controller.device.principal)["state"] == "Normal"
    assert page.session_state.filtered_state.get(f"{prefix}:code", "") == ""
    assert controller.gateway.audit.verify()
    assert controller.gateway.bank.execute_count == 0


@pytest.mark.parametrize("error", [ValueError, RuntimeError, KeyError])
@pytest.mark.parametrize("kind", ["bank", "wealth", "unlock"])
def test_mfa_callback_errors_clear_code_and_do_not_escape(monkeypatch, error, kind):
    def failed(*args, **kwargs):
        raise error("Do not expose internal details")

    state = {"code": "123456", "challenge": "challenge-id"}
    errors = []
    monkeypatch.setattr(manual_mfa.st, "session_state", state)
    monkeypatch.setattr(manual_mfa.st, "error", errors.append)
    controller = SimpleNamespace(
        complete_mfa=failed,
        verify_leg=failed,
        gateway=SimpleNamespace(mfa=SimpleNamespace(verify=failed)),
        device=SimpleNamespace(principal="C_DEMO"),
        state=SimpleNamespace(now="2026-10-16T00:00:00Z"),
    )
    if kind == "bank":
        manual_mfa.submit_manual_mfa(controller, "pending", "code", "result")
    elif kind == "wealth":
        manual_mfa.submit_wealth_mfa(controller, "leg", "code")
    else:
        manual_mfa.submit_unlock_mfa(controller, "code", "challenge", "result")
    assert state["code"] == ""
    assert errors
    assert "Do not expose" not in str(errors)
