"""Round-5 red team, dimension: orchestration (domestic DAG compensation / stop / handoff).

Every test below is a reproducer: it FAILS on tag domestic-freeze-2 and should pass once
the corresponding defect is fixed. Only public controller / gateway / kernel APIs and the
existing scripted device are used; nothing under src/ is modified.
"""

from fractions import Fraction

from corridor.bench.planning_runner import (
    _case,
    approve_birthday,
    approve_case,
    prepare_birthday,
    prepare_funds,
)
from corridor.schemas.bank import BankReceipt
from corridor.sim.planning import BIRTHDAY_DUE
from corridor.ui.controller import DemoController


def _settled_lock_receipt(state, plan_id):
    """The settled lock_funds receipt produced by this plan's own lock node."""
    for event in state.ledger:
        action = event.object_delta.get("action")
        if (
            event.kind == "lock_funds"
            and event.event == "settled"
            and isinstance(action, dict)
            and action.get("plan_id") == plan_id
        ):
            receipt = state.receipts[event.bank_receipt_id]
            assert isinstance(receipt, BankReceipt)
            return receipt
    raise AssertionError("plan has no settled lock receipt")


def _released_from_locked(state, after_index):
    """Sum of non-failed debits from A_LOCKED whose first ledger entry is after index."""
    first: dict[str, int] = {}
    latest = {}
    for index, event in enumerate(state.ledger):
        first.setdefault(event.intent_key, index)
        latest[event.intent_key] = event
    return sum(
        (
            event.debit.amount
            for key, event in latest.items()
            if event.event in {"reserved", "unknown", "settled"}
            and event.debit is not None
            and event.debit.account_ref == "A_LOCKED"
            and first[key] > after_index
        ),
        Fraction(),
    )


# ---------------------------------------------------------------------------
# orchestration-01: one lock is compensated twice; the second card drains another
# plan's locked budget. The kernel's DOMAIN-COMPENSATION "remaining amount" only
# subtracts earlier unlocks that cite the same receipt, so the plan's own scheduled
# 500 release is invisible to it and the gateway-computed remainder is never
# re-checked at dispatch.
# ---------------------------------------------------------------------------


def test_orchestration_01_duplicate_compensation_cards_drain_another_plans_lock():
    controller = DemoController()
    controller.start_session()
    # Plan B: an independent, still-locked 1000 budget in the shared A_LOCKED account.
    plan_b = controller.preview_birthday(selected_indices=[0])
    assert (
        controller.confirm_plan(plan_b["plan_id"], [plan_b["nodes"][0]["id"]])["status"]
        == "settled"
    )
    # Plan A: the full four-node birthday DAG (lock 1000, scheduled unlock 500, two orders).
    plan_a = controller.preview_birthday()
    assert (
        controller.confirm_plan(plan_a["plan_id"], [node["id"] for node in plan_a["nodes"]])[
            "status"
        ]
        == "settled"
    )
    controller.advance_clock(min(run.due_at for run in controller.state.schedules.values()))
    controller.start_session()
    controller.stop(plan_a["plan_id"])
    assert controller.snapshot()["balances"]["A_LOCKED"] == "1500/1"  # A 500 left + B 1000

    # The user presses Compensate twice before approving (both cards come from the
    # gateway's own trusted suggestion; the Device page lists every pending card).
    first = controller.compensate(plan_a["plan_id"])
    second = controller.compensate(plan_a["plan_id"])
    assert first["card"]["amount"] == second["card"]["amount"] == "500/1"
    assert controller.approve_pending(first["pending_id"])["status"] == "settled"
    result = controller.approve_pending(second["pending_id"])
    if result["status"] == "redisplay":
        result = controller.approve_pending(result["replacement"]["pending_id"])

    lock_a = _settled_lock_receipt(controller.state, plan_a["plan_id"])
    lock_a_index = next(
        index
        for index, event in enumerate(controller.state.ledger)
        if event.intent_key == lock_a.intent_key
    )
    released_after_a = _released_from_locked(controller.state, lock_a_index)
    # Plan A locked 1000 once; together with its own scheduled release, compensation
    # must never take plan B's still-locked budget.
    assert result["status"] != "settled", (result["status"], result.get("rule_ids"))
    assert controller.snapshot()["balances"]["A_LOCKED"] == "1000/1"
    assert released_after_a <= 1000


def test_orchestration_01_kernel_compensation_bound_ignores_the_locks_own_release():
    case = _case("RT5", "orch-01-kernel", None)
    bundle = prepare_birthday(case)
    approve_birthday(case, bundle)
    savings = prepare_funds(case, "lock_funds", "1000/1", step=20, purpose="savings")
    assert approve_case(case, savings)[0].status == "settled"
    case.gateway.advance_clock(BIRTHDAY_DUE)
    case.gateway.tick()
    balances = case.gateway.bank.final_state()["balances"]
    # The birthday lock already released 500 to its own scheduled unlock node.
    assert balances["A_LOCKED"] == "1500/1"
    lock = _settled_lock_receipt(case.state, bundle["plan"].plan_id)
    compensation = prepare_funds(
        case,
        "unlock_funds",
        "1000/1",
        step=21,
        purpose={"original_receipt_ref": lock.id},
    )
    verdict = compensation.proposal.verdict
    # DOMAIN-COMPENSATION: "Bound a declared lock reversal to the original effect and
    # remaining amount" -- only 500 of this lock remains, yet 1000 is accepted.
    assert verdict.outcome == "Reject", (verdict.outcome, verdict.tier)
    assert "DOMAIN-COMPENSATION" in verdict.rule_ids


# ---------------------------------------------------------------------------
# orchestration-04: the DAG state "compensated" (已补偿) is unreachable. After the
# required Stop the projection freezes succeeded nodes, and no code path records
# a compensated status or a compensation_settled event.
# ---------------------------------------------------------------------------


def test_orchestration_04_compensated_lock_node_is_never_marked_compensated():
    controller = DemoController()
    controller.start_session()
    draft = controller.preview_birthday(selected_indices=[0])
    assert (
        controller.confirm_plan(draft["plan_id"], [draft["nodes"][0]["id"]])["status"] == "settled"
    )
    controller.stop(draft["plan_id"])
    pending = controller.compensate(draft["plan_id"])
    assert controller.approve_pending(pending["pending_id"])["status"] == "settled"
    assert controller.snapshot()["balances"]["A_LOCKED"] == "0/1"
    lock_node = draft["nodes"][0]["id"]
    status = controller.gateway.plan_snapshot(draft["plan_id"])["nodes"][lock_node]["status"]
    assert status == "compensated", status
