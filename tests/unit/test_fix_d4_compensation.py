"""Receipt-bound compensation counts scheduled releases at propose and dispatch."""

from fractions import Fraction

import pytest

from corridor.crypto.jcs import H
from corridor.kernel import check
from corridor.kernel.planning import compensation_errors
from corridor.schemas.bank import BusinessEvent
from corridor.schemas.models import Reject
from corridor.stores.bank import add_seconds
from tests.bank_cases import recertify
from tests.unit.test_kernel_planning import compensation_case


@pytest.mark.rule("DOMAIN-COMPENSATION")
@pytest.mark.parametrize("status", ["reserved", "unknown", "settled", "failed"])
@pytest.mark.parametrize("phase", ["propose", "dispatch"])
def test_scheduled_unlock_reduces_receipt_remainder_at_every_check(status, phase):
    action, cert, state = compensation_case()
    scheduled = action.model_copy(
        update={"args": action.args.model_copy(update={"purpose_ref": "BIRTHDAY"})}
    )
    release = BusinessEvent(
        id="SCHEDULED_RELEASE",
        intent_key=H("SCHEDULED_RELEASE"),
        event=status,
        kind="unlock_funds",
        principal=action.principal,
        debit={"account_ref": "A_LOCKED", "amount": "150/1"},
        credit={"account_ref": "A_CURRENT", "amount": "150/1"},
        object_delta={"action": scheduled.model_dump(mode="json")},
        ts=state.now,
    )
    state = state.model_copy(update={"ledger": [*state.ledger, release]})
    if status == "failed":
        assert compensation_errors(action, state, Fraction(200)) == []
        assert compensation_errors(action, state, Fraction(201)) == ["DOMAIN-COMPENSATION"]
        return
    assert compensation_errors(action, state, Fraction(50)) == []
    assert compensation_errors(action, state, Fraction(51)) == ["DOMAIN-COMPENSATION"]
    verdict = check(*recertify(action, cert, state), phase=phase)
    assert isinstance(verdict, Reject)
    assert "DOMAIN-COMPENSATION" in verdict.rule_ids


@pytest.mark.rule("DOMAIN-COMPENSATION")
def test_dispatch_rechecks_after_the_original_scheduled_unlock_settles():
    from corridor.bench.planning_runner import (
        _case,
        approve_birthday,
        approve_case,
        prepare_birthday,
        prepare_funds,
    )
    from corridor.sim.planning import BIRTHDAY_DUE

    case = _case("FIX-D4", "dispatch-remainder", None)
    bundle = prepare_birthday(case)
    approve_birthday(case, bundle)
    extra = prepare_funds(case, "lock_funds", "1000/1", step=20, purpose="savings")
    assert approve_case(case, extra)[0].status == "settled"
    case.gateway.advance_clock(add_seconds(case.state.now, 601))
    original = next(
        event
        for event in case.state.ledger
        if event.kind == "lock_funds" and event.event == "settled"
    )
    compensation = prepare_funds(
        case,
        "unlock_funds",
        "1000/1",
        step=21,
        purpose={"original_receipt_ref": original.bank_receipt_id},
    )
    assert compensation.proposal.verdict.outcome != "Reject"
    case.gateway.advance_clock(BIRTHDAY_DUE)
    verdict = check(compensation.action, compensation.cert, case.state, phase="dispatch")
    assert isinstance(verdict, Reject)
    assert "DOMAIN-COMPENSATION" in verdict.rule_ids


def test_partial_then_complete_compensation_projects_settled_audit_and_preserves_stop():
    from corridor.bench.planning_runner import prepare_funds
    from corridor.ui.controller import DemoController

    controller = DemoController()
    controller.start_session()
    draft = controller.preview_birthday(selected_indices=[0])
    plan_id, node_id = draft["plan_id"], draft["nodes"][0]["id"]
    assert controller.confirm_plan(plan_id, [node_id])["status"] == "settled"
    controller.stop(plan_id)
    original = next(
        event
        for event in controller.state.ledger
        if event.kind == "lock_funds" and event.event == "settled"
    )
    partial = controller._preview_operation(
        lambda builder: prepare_funds(
            builder,
            "unlock_funds",
            "400/1",
            step=0,
            purpose={"original_receipt_ref": original.bank_receipt_id},
        ),
        request_id="partial-compensation",
    )
    assert controller.approve_pending(partial["pending_id"])["status"] == "settled"
    assert controller.gateway.plan_snapshot(plan_id)["nodes"][node_id]["status"] == "compensating"
    controller.stop(plan_id)
    assert controller.gateway.plan_snapshot(plan_id)["nodes"][node_id]["status"] == "compensating"
    remaining = controller.compensate(plan_id)
    assert remaining["card"]["amount"] == "600/1"
    assert controller.approve_pending(remaining["pending_id"])["status"] == "settled"
    assert controller.gateway.plan_snapshot(plan_id)["nodes"][node_id]["status"] == "compensated"
    events = [
        row for row in controller.gateway.audit.records() if row["event"] == "compensation_settled"
    ]
    assert len(events) == 2
    assert all(row["plan_id"] == plan_id and row["node_id"] == node_id for row in events)
    assert len({row["payload"]["compensation_receipt_ref"] for row in events}) == 2
    assert controller.gateway.verify_log()
