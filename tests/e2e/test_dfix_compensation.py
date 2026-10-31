"""Compensation uses remaining trusted effects, retaining approval and unknown gates."""

from fractions import Fraction

import pytest

from corridor.ui.controller import DemoController
from corridor.ui.domestic_demos import prepare_demo


def test_partially_released_birthday_lock_compensates_only_remaining_balance():
    controller = DemoController()
    controller.start_session()
    draft = controller.preview_birthday()
    assert (
        controller.confirm_plan(draft["plan_id"], [node["id"] for node in draft["nodes"]])["status"]
        == "settled"
    )
    controller.advance_clock(min(run.due_at for run in controller.state.schedules.values()))
    controller.start_session()
    controller.stop(draft["plan_id"])
    assert controller.snapshot()["balances"]["A_LOCKED"] == "500/1"
    before = controller.gateway.bank.execute_count
    pending = controller.compensate(draft["plan_id"])
    assert pending["card"]["amount"] == "500/1"
    assert controller.gateway.bank.execute_count == before
    assert controller.approve_pending(pending["pending_id"])["status"] == "settled"
    assert controller.gateway.bank.execute_count == before + 1
    assert controller.snapshot()["balances"]["A_LOCKED"] == "0/1"
    assert controller.compensate(draft["plan_id"])["status"] == "no_compensation"
    assert len(controller.state.orders) == 2
    assert controller.gateway.verify_log()


def test_unknown_payment_prevents_compensation_execution_until_reconciled():
    demo = prepare_demo("birthday_unknown")
    controller = demo["controller"]
    controller.stop(demo["plan_id"])
    before = controller.gateway.bank.execute_count
    pending = controller.compensate(demo["plan_id"])
    with pytest.raises(ValueError, match="APR-LOCK"):
        controller.approve_pending(pending["pending_id"])
    assert controller.gateway.bank.execute_count == before
    assert controller.snapshot()["balances"]["A_LOCKED"] == "500/1"


def test_compensation_card_drift_cannot_unlock_a_new_balance():
    controller = DemoController()
    controller.start_session()
    draft = controller.preview_birthday(selected_indices=[0])
    controller.confirm_plan(draft["plan_id"], [draft["nodes"][0]["id"]])
    controller.stop(draft["plan_id"])
    pending = controller.compensate(draft["plan_id"])
    assert pending["card"]["amount"] == "1000/1"
    before = controller.gateway.bank.execute_count
    account = controller.state.accounts["A_LOCKED"]
    controller.gateway.store.update(
        accounts={
            **controller.state.accounts,
            "A_LOCKED": account.model_copy(update={"opening_balance": Fraction(500)}),
        }
    )
    result = controller.approve_pending(pending["pending_id"])
    assert result["status"] in {"rejected", "redisplay"}
    assert controller.gateway.bank.execute_count == before


def test_unlinked_unlock_and_new_lock_cannot_revive_old_compensation():
    from corridor.bench.planning_runner import prepare_funds

    controller = DemoController()
    controller.start_session()
    original = controller.preview_birthday(selected_indices=[0])
    assert (
        controller.confirm_plan(original["plan_id"], [original["nodes"][0]["id"]])["status"]
        == "settled"
    )
    controller.stop(original["plan_id"])
    release = controller._preview_operation(
        lambda builder: prepare_funds(
            builder, "unlock_funds", "1000/1", step=0, purpose="other-plan"
        ),
        request_id="dfix-unlinked-release",
    )
    assert controller.approve_pending(release["pending_id"])["status"] == "settled"
    assert controller.snapshot()["balances"]["A_LOCKED"] == "0/1"
    unrelated = controller.preview_birthday(selected_indices=[0])
    assert (
        controller.confirm_plan(unrelated["plan_id"], [unrelated["nodes"][0]["id"]])["status"]
        == "settled"
    )
    controller.stop(unrelated["plan_id"])
    dispatches = controller.gateway.bank.execute_count
    assert controller.compensate(original["plan_id"])["status"] == "no_compensation"
    assert controller.snapshot()["balances"]["A_LOCKED"] == "1000/1"
    assert controller.gateway.bank.execute_count == dispatches
    assert controller.compensate(unrelated["plan_id"])["card"]["amount"] == "1000/1"


def test_release_retry_after_lock_is_counted_after_its_earlier_failed_attempt():
    from types import SimpleNamespace

    from corridor.bench.planning_runner import prepare_funds
    from corridor.gateway.orchestration import compensation_suggestions
    from corridor.stores.bank import BankStore, available

    controller = DemoController()
    controller.start_session()
    original = controller.preview_birthday(selected_indices=[0])
    controller.confirm_plan(original["plan_id"], [original["nodes"][0]["id"]])
    controller.stop(original["plan_id"])
    release = controller._preview_operation(
        lambda builder: prepare_funds(
            builder, "unlock_funds", "1000/1", step=0, purpose="retried-release"
        ),
        request_id="dfix-retried-release",
    )
    assert controller.approve_pending(release["pending_id"])["status"] == "settled"
    unrelated = controller.preview_birthday(selected_indices=[0])
    controller.confirm_plan(unrelated["plan_id"], [unrelated["nodes"][0]["id"]])
    controller.stop(unrelated["plan_id"])
    reserved = next(
        event
        for event in controller.state.ledger
        if event.kind == "unlock_funds" and event.event == "reserved"
    )
    # A synthetic failed attempt precedes the original lock. Its later retry is
    # the real settled release above; same-clock append order defines the attempt.
    synthetic_state = controller.state.model_copy(
        update={
            "ledger": [
                reserved.model_copy(update={"id": "EARLIER_RELEASE_RESERVATION"}),
                reserved.model_copy(update={"id": "EARLIER_RELEASE_FAILED", "event": "failed"}),
                *controller.state.ledger,
            ]
        }
    )
    observer = SimpleNamespace(_plans=controller.gateway._plans, store=BankStore(synthetic_state))
    dispatches = controller.gateway.bank.execute_count
    suggestions = compensation_suggestions(observer, original["plan_id"], None)
    assert not any(item["tool"] == "unlock_funds" for item in suggestions)
    assert available(synthetic_state, "A_LOCKED") == 1000
    assert controller.gateway.bank.execute_count == dispatches
