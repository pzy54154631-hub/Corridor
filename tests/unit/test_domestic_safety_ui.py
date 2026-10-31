"""Safety-page gestures cannot bypass the gateway's signed unlock protocol."""

import pytest

from corridor.stores.bank import add_seconds
from corridor.ui.safety import create_safety_demo, trigger_signal, unlock_demo


@pytest.mark.parametrize("mode,count", [("reject", 3), ("injection", 2), ("mfa", 3)])
def test_safety_demo_requires_all_signals_and_explicit_signed_unlock(mode, count):
    demo = create_safety_demo(mode)
    case = demo["case"]
    principal = case.device.principal
    assert case.gateway.safety_snapshot(principal)["state"] == "Normal"
    for index in range(count):
        snapshot = trigger_signal(demo)
        assert snapshot["state"] == ("Locked" if index == count - 1 else "Watch")
    assert case.gateway.bank.execute_count == 0
    early = unlock_demo(demo)
    assert early["status"] == "rejected" and "CTRL-MFA" in early["rule_ids"]
    assert case.gateway.safety_snapshot(principal)["state"] == "Locked"
    case.gateway.advance_clock(
        add_seconds(case.state.now, case.state.policy.unlock_cooldown_seconds)
    )
    assert case.gateway.safety_snapshot(principal)["state"] == "Locked"
    assert unlock_demo(demo)["status"] == "unlocked"
    assert case.gateway.safety_snapshot(principal)["state"] == "Normal"
    assert case.gateway.dispatch(demo["yellow"].action, demo["yellow"].cert).rule_ids == [
        "APR-VOID"
    ]
    events = [row["event"] for row in case.gateway.audit.records()]
    assert "mfa_attempt" in events and events.count("unlocked") == 1
    assert case.gateway.audit.verify()
    assert unlock_demo(demo)["status"] == "rejected"


@pytest.mark.parametrize("missing", ["signature", "mfa"])
def test_safety_demo_does_not_unlock_without_valid_device_proofs(monkeypatch, missing):
    demo = create_safety_demo("injection")
    case = demo["case"]
    for _ in range(2):
        trigger_signal(demo)
    case.gateway.advance_clock(
        add_seconds(case.state.now, case.state.policy.unlock_cooldown_seconds)
    )
    if missing == "signature":
        confirm = case.device.confirm_input
        monkeypatch.setattr(
            case.device,
            "confirm_input",
            lambda **kwargs: confirm(**kwargs).model_copy(update={"sig": "0" * 128}),
        )
    else:
        monkeypatch.setattr(case.device, "complete_mfa", lambda *args, **kwargs: None)
    assert unlock_demo(demo)["status"] == "rejected"
    assert case.gateway.safety_snapshot(case.device.principal)["state"] == "Locked"
    assert not any(row["event"] == "unlocked" for row in case.gateway.audit.records())


def test_safety_demo_unknown_mode_is_not_an_implicit_mfa_command():
    with pytest.raises(ValueError, match="Unknown safety demonstration"):
        create_safety_demo("unknown")


def test_current_session_unlock_uses_current_gateway_and_all_device_requirements():
    from corridor.ui.controller import DemoController
    from corridor.ui.safety import unlock_current_session

    controller = DemoController()
    controller.start_session()
    controller.gateway.lock(controller.device.principal, "test_current_session")
    isolated = create_safety_demo("injection")
    trigger_signal(isolated)
    trigger_signal(isolated)
    assert unlock_current_session(controller)["status"] == "rejected"
    controller.advance_clock(controller.state.policy.unlock_cooldown_seconds)
    assert controller.gateway.safety_snapshot(controller.device.principal)["state"] == "Locked"
    assert unlock_current_session(controller)["status"] == "unlocked"
    assert controller.gateway.safety_snapshot(controller.device.principal)["state"] == "Normal"
    assert (
        isolated["case"].gateway.safety_snapshot(controller.device.principal)["state"] == "Locked"
    )
    assert any(row["event"] == "unlocked" for row in controller.gateway.audit.records())


def test_current_session_safety_page_requires_separate_cooldown_and_device_clicks():
    from tests.e2e.test_demo_ui import app, navigate

    page = app()
    page.button(key="enter").click().run()
    controller = page.session_state["controller"]
    controller.gateway.lock(controller.device.principal, "test_current_ui")
    navigate(page, "safety")
    assert controller.gateway.safety_snapshot(controller.device.principal)["state"] == "Locked"
    key = "safety_current_unlock"

    def state():
        return controller.gateway.safety_snapshot(controller.device.principal)["state"]

    page.button(key=f"{key}:request").click().run()
    code = controller.gateway.mfa.test_code(page.session_state[f"{key}:challenge"])
    page.text_input(key=f"{key}:code").set_value(code)
    page.button(key=f"{key}:verify").click().run()
    assert state() == "Locked"  # cooldown still enforced by the gateway
    page.button(key="safety_current_cooldown").click().run()
    assert state() == "Locked"
    page.button(key=f"{key}:request").click().run()
    page.button(key=f"{key}:verify").click().run()  # empty form never reads the SMS
    assert state() == "Locked"
    code = controller.gateway.mfa.test_code(page.session_state[f"{key}:challenge"])
    page.text_input(key=f"{key}:code").set_value(code)
    page.button(key=f"{key}:verify").click().run()
    assert not page.exception
    assert state() == "Normal"
    assert page.session_state.filtered_state.get(f"{key}:code", "") == ""
