"""Execution labels survive the newly required manual MFA interaction."""

from copy import deepcopy

from tests.e2e.test_demo_ui import app, navigate
from tests.e2e.test_wealth_ui import prepare_wealth


def _watch_execution(monkeypatch):
    from corridor.ui import wealth_statement, wealth_views

    observed = []
    original_summary = wealth_views.execution_summary
    original_statement = wealth_statement.build_statement

    def summary(execution):
        observed.append(deepcopy(execution))
        return original_summary(execution)

    def statement(*args, **kwargs):
        execution = kwargs.get("execution", args[4] if len(args) > 4 else None)
        if execution is not None:
            observed.append(deepcopy(execution))
        return original_statement(*args, **kwargs)

    monkeypatch.setattr(wealth_views, "execution_summary", summary)
    monkeypatch.setattr(wealth_statement, "build_statement", statement)
    return observed


def _start(page):
    controller = prepare_wealth(page)
    for box in page.checkbox:
        if box.key.startswith("wealth_select:"):
            box.check()
    page.run()
    page.button(key="wealth_start").click().run()
    navigate(page, "device")
    return controller


def _approve_manually(page, controller):
    identifier = controller.leg["action"].action_id
    assert not controller.leg["verified"]
    assert page.button(key=f"wealth_approve:{identifier}").disabled
    page.button(key=f"wealth_request:{identifier}").click().run()
    challenge = controller.leg["challenge_id"]
    code = controller.gateway.mfa.test_code(challenge)
    page.text_input(key=f"wealth_code:{identifier}").set_value(code)
    page.button(key=f"wealth_verify:{identifier}").click().run()
    assert controller.leg["verified"]
    page.button(key=f"wealth_approve:{identifier}").click().run()
    assert not page.exception


def test_completed_certificate_survives_readonly_question_after_manual_mfa(monkeypatch):
    observed = _watch_execution(monkeypatch)
    page = app()
    controller = _start(page)
    _approve_manually(page, controller)
    _approve_manually(page, controller)
    assert controller.execution_snapshot()["status"] == "completed"
    assert controller.gateway.bank.execute_count == 2
    assert len(controller.state.security.consumed_grant_ids) == 2

    navigate(page, "wealth")
    page.text_input(key="wealth_message").set_value("What is my allocation?")
    page.button(key="wealth_discuss").click().run()
    assert not page.exception
    observed.clear()
    navigate(page, "wealth")
    assert observed and observed[-1]["status"] == "completed"
    assert all(row["status"] == "settled" and row["receipt_ref"] for row in observed[-1]["items"])
    assert not any(button.key == "wealth_start" for button in page.button)


def test_global_stop_preserves_settled_leg_and_cash_after_manual_mfa(monkeypatch):
    observed = _watch_execution(monkeypatch)
    page = app()
    controller = _start(page)
    _approve_manually(page, controller)
    assert len(controller.state.positions) == 1
    assert controller.gateway.bank.execute_count == 1

    page.button(key="stop_all").click().run()
    assert not page.exception
    snapshot = controller.execution_snapshot()
    assert snapshot["status"] == "partially_executed"
    assert [row["status"] for row in snapshot["items"]] == ["settled", "cancelled"]
    assert snapshot["available_balance"] == "7000/1"
    for route in ("device", "wealth"):
        observed.clear()
        navigate(page, route)
        assert not page.exception
        assert observed and observed[-1]["status"] == "partially_executed"
        assert observed[-1]["available_balance"] == "7000/1"
        assert not any(button.key == "wealth_start" for button in page.button)
