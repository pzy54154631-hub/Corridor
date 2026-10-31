"""Independent domestic recovery examples exercise real gateway effects."""

import pytest

from corridor.ui.domestic_recovery import prepare_recovery


@pytest.mark.parametrize("mode", ["interrupt", "handoff"])
def test_control_preserves_settled_lock_and_cancels_unexecuted_schedules(mode):
    demo = prepare_recovery(mode)
    controller = demo["controller"]
    assert controller.snapshot()["balances"] == {"A_CURRENT": "9000/1", "A_LOCKED": "1000/1"}
    assert all(run.status == "cancelled" for run in controller.state.schedules.values())
    nodes = controller.gateway.plan_snapshot(demo["plan_id"])["nodes"]
    assert sum(node["status"] == "succeeded" for node in nodes.values()) == 1
    assert (
        sum(
            node["status"] == ("handoff" if mode == "handoff" else "cancelled")
            for node in nodes.values()
        )
        == 3
    )
    dispatches = controller.gateway.bank.execute_count
    controller.advance_clock("2026-10-18T02:00:00Z")
    assert controller.gateway.bank.execute_count == dispatches
    assert controller.gateway.verify_log()


def test_failed_node_compensation_waits_for_new_device_confirmation():
    demo = prepare_recovery("failure")
    controller = demo["controller"]
    assert any(result["status"] == "failed" for result in demo["failure"]["results"])
    assert controller.snapshot()["balances"]["A_LOCKED"] == "1000/1"
    pending = demo["compensation"]
    assert pending["card"]["kind"] == "unlock_funds"
    assert not controller.state.orders
    restored = controller.approve_pending(pending["pending_id"])
    assert restored["status"] == "settled"
    assert controller.snapshot()["balances"] == {"A_CURRENT": "10000/1", "A_LOCKED": "0/1"}
    assert restored["receipt"]["kind"] == "unlock_funds"
    assert controller.gateway.verify_log()


def test_operator_compensation_has_no_bypass_when_approval_is_missing():
    demo = prepare_recovery("handoff")
    controller = demo["controller"]
    pending = controller.compensate(demo["plan_id"])
    assert controller.snapshot()["balances"]["A_LOCKED"] == "1000/1"
    assert pending["card"]["tier"] == "yellow"
    assert controller.approve_pending(pending["pending_id"])["status"] == "settled"
    assert controller.snapshot()["balances"]["A_LOCKED"] == "0/1"
