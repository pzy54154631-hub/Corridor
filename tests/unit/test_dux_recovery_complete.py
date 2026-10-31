"""The unknown-outcome preset recovers through real unlock and fresh compensation."""

from corridor.agent.domestic_session import DomesticSession
from corridor.ui.domestic_demos import prepare_demo


def test_unknown_recovery_compensates_remaining_lock_with_fresh_approval_and_handoff():
    from corridor.ui.domestic_demos import unlock_recovery_demo

    demo = prepare_demo("birthday_unknown")
    controller = demo["controller"]
    session = DomesticSession(controller)
    count = controller.gateway.bank.execute_count
    assert controller.gateway.safety_snapshot(controller.device.principal)["state"] == "Locked"
    assert unlock_recovery_demo(demo)["status"] == "rejected"
    session.receive("stop")
    for key, event in {row.intent_key: row for row in controller.state.ledger}.items():
        if event.event == "unknown":
            assert controller.gateway.reconcile(key).status == "settled"
    assert controller.gateway.bank.execute_count == count
    locked = controller.gateway.safety_snapshot(controller.device.principal)
    controller.advance_clock(locked["unlock_after"])
    assert unlock_recovery_demo(demo)["status"] == "unlocked"
    before_inputs = len(controller.state.user_inputs)
    pending = controller.compensate(demo["plan_id"])
    assert pending["status"] == "awaiting_approval"
    assert pending["card"]["amount"] == "500/1"
    assert len(controller.state.user_inputs) == before_inputs
    result = controller.approve_pending(pending["pending_id"])
    assert result["status"] == "settled"
    assert controller.snapshot()["balances"]["A_LOCKED"] == "0/1"
    assert controller.gateway.bank.execute_count == count + 1
    assert controller.compensate(demo["plan_id"])["status"] == "no_compensation"
    assert session.receive("handoff")["status"] == "handoff"
    assert controller.gateway.bank.execute_count == count + 1
    assert controller.gateway.verify_log()


def test_recovery_page_stop_lookup_unlock_compensation_and_handoff():
    from tests.e2e.test_demo_ui import app

    page = app()
    page.button(key="enter").click().run()
    page.selectbox(key="demo_preset").select("birthday_unknown").run()
    page.button(key="load_demo_preset").click().run()
    controller = page.session_state["demo_scenario"]["controller"]
    assert controller.snapshot()["balances"]["A_LOCKED"] == "1000/1"
    assert not controller.state.orders
    page.button(key="demo_birthday_orders").click().run()
    count = controller.gateway.bank.execute_count
    page.button(key="demo_recovery_task_stop").click().run()
    page.button(key="demo_reconcile").click().run()
    page.button(key="demo_recovery_cooldown").click().run()
    page.button(key="demo_recovery_unlock").click().run()
    page.button(key="demo_recovery_task_back").click().run()
    assert controller.gateway.bank.execute_count == count
    page.button(key="demo_recovery_compensate_confirm").click().run()
    assert not page.exception
    assert page.session_state["demo_recovery_compensation"]["status"] == "settled"
    assert controller.gateway.bank.execute_count == count + 1
    page.run()
    assert controller.gateway.bank.execute_count == count + 1
    page.button(key="demo_recovery_task_handoff").click().run()
    assert not page.exception
    assert controller.gateway.bank.execute_count == count + 1
    page.button(key="demo_operator_prepare").click().run()
    assert page.session_state["demo_scenario"]["operator_result"]["status"] == "no_compensation"
    assert controller.gateway.bank.execute_count == count + 1
