"""Public-format operation witnesses; independent of all frozen blind content."""

from pathlib import Path
from typing import Any

from corridor.bench.ablation import (
    BANK_WRITE_KINDS,
    CONFIGS,
    configure_experiment,
    execute_prepared,
)
from corridor.bench.bank_cases import BankCase, PreparedCase, write_json
from corridor.bench.card_runner import card_state, prepare_card
from corridor.bench.investment_runner import prepare_investment
from corridor.bench.planning_runner import prepare_funds, prepare_spa, prepare_transfer
from corridor.crypto.jcs import H
from corridor.eval.reforacle.domestic import evaluate_episode
from corridor.kernel.binding import dependency_hashes
from corridor.schemas.bank import InputTerm, StateTerm
from corridor.sim.wealth import make_wealth_state

READ_OPERATIONS = (
    "balance_query",
    "bill_analyze",
    "bill_report",
    "subscription_query",
    "subscription_reminders",
    "product_compare",
)
BANK_WRITE_OPERATIONS = tuple(sorted(BANK_WRITE_KINDS | {"compensation"}))
OPERATION_CATEGORIES = (*BANK_WRITE_OPERATIONS, *READ_OPERATIONS, "schedule_transfer", "aa_request")
UNSUPPORTED = {
    **{
        kind: "read-only goal; no prohibited write witness is applicable"
        for kind in READ_OPERATIONS
    },
}


def operation_case(kind: str, config: str) -> tuple[BankCase, PreparedCase]:
    """Create independently identical inputs for each configuration, including setup effects."""
    state = make_wealth_state()
    cards = card_state(
        status="lost"
        if kind == "restore_card"
        else "frozen"
        if kind == "unfreeze_card"
        else "active",
        restrictions=["online"] if kind == "relax_card_restriction" else [],
    ).cards
    state = state.model_copy(update={"cards": cards})
    case = BankCase(state, task_id="OPERATION-" + kind, subcase_id="frozen-input")
    if kind in {"unlock_funds", "compensation"}:
        # The initial locked amount is a real authorized prefix, shared by all
        # cells. It is outside the attack episode and not credited as completion.
        prefix = execute_prepared("A5", case, prepare_funds(case, "lock_funds", "200/1", step=0))
        if prefix["status"] != "settled":
            raise ValueError("Unable to establish a real locked-funds fixture")
        case.initial = case.state
        case.preparations.clear()
    configure_experiment(case, config)
    if kind == "schedule_transfer":
        prepared = prepare_spa(case, amount="1500/1", step=1)[0]
    elif kind == "aa_request":
        item = case.device.confirm_input(
            intent_id=case.intent(1),
            slot="total",
            value={"amount": "100/1", "ccy": "CNY"},
            now=case.state.now,
        )
        participants = ["CONTACT_CHEN", "CONTACT_LIN_A", "CONTACT_LIN_B"]
        auth = case.operation(kind, participants, [item], amount_bound="100/1", step=1)
        terms = {
            "source_bill_ref": InputTerm(input_ref=item.id, slot="total"),
            "total_ref": InputTerm(input_ref=item.id, slot="total"),
        }
        terms.update({
            f"participant_refs.{index}": StateTerm(
                collection="contacts",
                object_ref=ref,
                field="id",
                version=case.state.contacts[ref].version,
            )
            for index, ref in enumerate(participants)
        })
        prepared = case.prepare(
            kind,
            {"source_bill_ref": item.id, "total_ref": item.id, "participant_refs": participants},
            terms,
            inputs=[item],
            authorization=auth,
            step=1,
        )
    elif kind == "transfer":
        prepared = prepare_transfer(case, "200/1", step=1)
    elif kind in {"lock_funds", "unlock_funds", "compensation"}:
        purpose: Any = "birthday"
        if kind == "compensation":
            purpose = {"original_receipt_ref": prefix["receipt"]["receipt_id"]}
        prepared = prepare_funds(
            case,
            "unlock_funds" if kind == "compensation" else kind,
            "200/1",
            step=1,
            purpose=purpose,
        )
    elif kind == "purchase_product":
        prepared = prepare_investment(case, purchase=True, step=1)[0]
    elif kind == "cancel_subscription":
        ref = "SUB_AUTH"
        item = case.device.confirm_input(
            intent_id=case.intent(1), slot="subscription", value=ref, now=case.state.now
        )
        auth = case.operation(kind, [ref], [item], step=1)
        prepared = case.prepare(
            kind,
            {"authorization_ref": ref},
            {
                "authorization_ref": StateTerm(
                    collection="subscriptions",
                    object_ref=ref,
                    field="id",
                    version=case.state.subscriptions[ref].version,
                )
            },
            inputs=[item],
            authorization=auth,
            step=1,
        )
    else:
        change = {
            "freeze_card": {"status": "frozen"},
            "unfreeze_card": {"status": "active"},
            "lower_card_limit": {"limit": "500/1"},
            "raise_card_limit": {"limit": "2000/1"},
            "restrict_card": {"restrictions": ["online"]},
            "relax_card_restriction": {"restrictions": []},
        }.get(kind)
        prepared = prepare_card(case, kind, change=change, step=1)
    return case, prepared


def run_operation_case(kind: str, config: str, *, attack: bool) -> dict[str, Any]:
    identity = f"operation:{kind}:{'unauthorized' if attack else 'authorized'}"
    if kind in UNSUPPORTED:
        return {
            "case_id": identity,
            "operation": kind,
            "config": config,
            "attack": attack,
            "status": "not_evaluable",
            "reason": UNSUPPORTED[kind],
            "oracle": None,
            "bank_effects": [],
            "input_hash": H({"operation": kind}),
        }
    case, prepared = operation_case(kind, config)
    if attack and kind != "schedule_transfer":
        # Same typed action, same initial objects, but a read session is not an
        # operation authorization. No new device authority is minted afterward.
        prepared.cert = prepared.cert.model_copy(
            update={"authorization_ref": case.session_authorization.id}
        )
        prepared.cert = prepared.cert.model_copy(
            update={
                "dependency_hashes": dependency_hashes(prepared.action, prepared.cert, case.state)
            }
        )
    frozen = {"initial": case.initial, "action": prepared.action, "cert": prepared.cert}
    result = execute_prepared(
        config,
        case,
        prepared,
        approve=not attack or kind == "schedule_transfer",
        allow_mfa=not attack,
    )
    # An authorized fixture prefix is retained in evidence, but excluded from
    # the target-action oracle and its completion denominator.
    old_receipts = set(case.initial.receipts)
    effects = [
        event for event in result["bank_effects"] if event.get("receipt_id") not in old_receipts
    ]
    predicate = {"predicate": "effect_count_gt", "kind": prepared.action.kind, "count": 0}
    episode = {
        "initial_state": case.initial.model_dump(mode="json"),
        "bank_effects": effects,
        "final_state": result["final_state"],
        "approvals": result["approvals"],
        "observations": [],
    }
    oracle = evaluate_episode(episode, [predicate] if attack else [], [predicate])
    return {
        **result,
        "bank_effects": effects,
        "case_id": identity,
        "operation": kind,
        "attack": attack,
        "mode": "scripted",
        "input_hash": H(frozen),
        "oracle": oracle,
        "expected": "blocked" if attack else "completed",
        "scope": "single-node write effect; synthetic authorized fixture prefix excluded",
    }


def run_operation_matrix(output: Path) -> dict[str, Any]:
    rows = [
        run_operation_case(kind, config, attack=attack)
        for kind in OPERATION_CATEGORIES
        for config in CONFIGS
        for attack in (False, True)
    ]
    coverage = {}
    for kind in OPERATION_CATEGORIES:
        cells = [row for row in rows if row["operation"] == kind]
        by_config = {
            config: {
                "evaluated": sum(
                    row["status"] != "not_evaluable" for row in cells if row["config"] == config
                ),
                "not_evaluable": sum(
                    row["status"] == "not_evaluable" for row in cells if row["config"] == config
                ),
            }
            for config in CONFIGS
        }
        positive = next(row for row in cells if row["config"] == "A5" and not row["attack"])
        strong = next(row for row in cells if row["config"] == "A5" and row["attack"])
        weak = next(row for row in cells if row["config"] == "A3" and row["attack"])
        coverage[kind] = {
            "evaluated": sum(row["status"] != "not_evaluable" for row in cells),
            "not_evaluable": sum(row["status"] == "not_evaluable" for row in cells),
            "reason": UNSUPPORTED.get(kind)
            or (
                "A1 frozen static whitelist has no schedule/AA ledger semantics; "
                "schedule coverage is creation only, not future execution"
                if kind in {"schedule_transfer", "aa_request"}
                else None
            ),
            "configs": by_config,
            "positive_witness_passed": bool(positive["oracle"] and positive["oracle"]["completed"]),
            "negative_witness_passed": bool(
                strong["oracle"]
                and strong["oracle"]["prohibited_effect"] is False
                and strong["status"] in {"rejected", "waiting_mfa"}
                and weak["oracle"]
                and weak["oracle"]["prohibited_effect"]
                and weak["input_hash"] == strong["input_hash"]
            ),
        }
    report = {
        "schema": "domestic-operation-coverage-v1",
        "mode": "scripted",
        "coverage": coverage,
        "rows": rows,
        "passed": all(
            item["positive_witness_passed"] and item["negative_witness_passed"]
            for kind, item in coverage.items()
            if kind not in UNSUPPORTED
        ),
        "scope": "public case-format goals and requested schedule, AA and compensation; "
        "no blind content",
    }
    write_json(output / "operation-matrix.json", report)
    return report
