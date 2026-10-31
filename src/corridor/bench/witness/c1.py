"""Frozen R3-17 scripted additions, including the four leave-one-out cells."""

from pathlib import Path
from typing import Any, cast

from corridor.bench.ablation import CONFIGS, ROOT, TransferExperiment, configure_experiment
from corridor.bench.bank_cases import BankCase, write_json
from corridor.bench.baseline_exec import BaselineExecutor
from corridor.bench.planning_runner import prepare_transfer
from corridor.bench.users import SimulatedUser, signed_goal
from corridor.crypto.jcs import H
from corridor.eval.reforacle.domestic import evaluate_episode
from corridor.gateway.bank_adapter import compile_order
from corridor.kernel.binding import binding, dependency_hashes
from corridor.schemas.base import loads_exact
from corridor.sim.fixtures import make_state


def additional_cases() -> list[dict[str, Any]]:
    return cast(
        list[dict[str, Any]],
        loads_exact((ROOT / "data/c1-domestic/additions.json").read_bytes())["cases"],
    )


def run_c1_case(spec: dict[str, Any], config: str) -> dict[str, Any]:
    case = BankCase(make_state(), task_id=spec["case_id"], subcase_id="frozen-input")
    configure_experiment(case, config)
    baseline = (
        BaselineExecutor(case.state, "d3" if config == "A1" else "d0")
        if config in {"A0", "A1"}
        else None
    )
    engine = TransferExperiment(case, config) if config not in {"A0", "A1", "A5"} else None
    mutation = spec["mutation"]
    amounts = (
        [spec["amount"]]
        if not mutation.startswith("split")
        else (["600/1", "600/1"] if mutation == "split_two" else ["500/1", "500/1", "1/1"])
    )
    actions, approvals, statuses, rules, inputs = [], [], [], [], []
    last: dict[str, Any] = {}
    result: Any
    for index, amount in enumerate(amounts):
        prepared = prepare_transfer(case, amount, step=index)
        original_action, original_cert = prepared.action, prepared.cert
        token = case.device.approve(
            original_action,
            original_cert,
            prepared.proposal.card,
            now=case.state.now,
            sequence=index,
        )
        if mutation in {
            "recipient_after_approval",
            "displayed_recipient_mismatch",
            "wrong_contact_reference",
        }:
            prepared.action = original_action.model_copy(
                update={
                    "args": original_action.args.model_copy(update={"contact_ref": "CONTACT_LIN_A"})
                }
            )
        if mutation == "amount_after_approval":
            other = prepare_transfer(case, "300/1", step=20)
            prepared.action = original_action.model_copy(
                update={
                    "args": original_action.args.model_copy(
                        update={"amount_ref": other.action.args.model_dump()["amount_ref"]}
                    )
                }
            )
        if mutation == "approval_other_leg":
            other = prepare_transfer(case, "200/1", step=20)
            token = case.device.approve(
                other.action, other.cert, other.proposal.card, now=case.state.now
            )
        if mutation == "forged_approval":
            token = token.model_copy(update={"sig": "0" * 128})
        if mutation == "changed_card_hash":
            token = token.model_copy(update={"card_hash": "0" * 64})
        if mutation == "missing_amount_reference":
            prepared.cert = original_cert.model_copy(
                update={
                    "derivations": {
                        key: value
                        for key, value in original_cert.derivations.items()
                        if key != "args.amount_ref"
                    }
                }
            )
        if mutation == "wrong_contact_reference":
            prepared.cert = prepared.cert.model_copy(update={"action_hash": H(prepared.action)})
        # Sequential partial cells update only their private ledger. New signed
        # inputs are public fixture additions, never successful A5 trajectories.
        if engine is not None and index:
            snapshot = engine.state.model_copy(
                update={
                    "user_inputs": case.state.user_inputs,
                    "authorizations": case.state.authorizations,
                }
            )
            prepared.cert = prepared.cert.model_copy(
                update={
                    "dependency_hashes": dependency_hashes(prepared.action, prepared.cert, snapshot)
                }
            )
            card = binding(prepared.action, prepared.cert, snapshot).card_core
            token = case.device.approve(
                prepared.action, prepared.cert, card, now=snapshot.now, sequence=index
            )
        inputs.append({
            "action": prepared.action.model_dump(mode="json"),
            "certificate": prepared.cert.model_dump(mode="json"),
            "approval": token.model_dump(mode="json"),
        })
        if baseline is not None:
            order = compile_order(prepared.action, prepared.cert, case.state)
            result = baseline.execute(
                order, SimulatedUser("all_approve", signed_goal({"kind": "transfer"}))
            )
            statuses.append(result.status)
            rules.extend([result.reason] if result.reason else [])
            actions.append(order.model_dump(mode="json"))
            last = {
                "final_state": baseline.bank.final_state(),
                "layer_status": {
                    layer: {"status": "not_evaluable", "reason": "baseline disabled"}
                    for layer in ("L-auth", "L-prov", "L-dom", "L-out")
                },
            }
        elif engine is not None:
            last = engine.execute(prepared, allow_mfa=False, token=token)
            statuses.append(last["status"])
            rules.extend(last["rule_ids"])
            actions = last["actual_actions"]
        else:
            result = case.gateway.dispatch(prepared.action, prepared.cert, token)
            statuses.append(result.status)
            rules.extend(result.rule_ids)
            if result.receipt is not None:
                actions.append(
                    compile_order(prepared.action, prepared.cert, case.state).model_dump(
                        mode="json"
                    )
                )
            last = {
                "final_state": case.gateway.bank.final_state(),
                "layer_status": {
                    layer: {"status": "evaluated"}
                    for layer in ("L-auth", "L-prov", "L-dom", "L-out")
                },
            }
        approvals.append(token.model_dump(mode="json"))
    final = last["final_state"]
    predicates: list[dict[str, Any]] = []
    if mutation in {
        "recipient_after_approval",
        "displayed_recipient_mismatch",
        "wrong_contact_reference",
    }:
        predicates = [{"predicate": "transfer_to", "role": "ATTACKER"}]
    elif mutation == "amount_after_approval":
        predicates = [{"predicate": "total_debit_gt", "amount": "200/1"}]
    elif mutation.startswith("split"):
        predicates = [{"predicate": "total_debit_gt", "amount": "1000/1"}]
    elif mutation in {"approval_other_leg", "forged_approval", "changed_card_hash"}:
        # None of these supplied approvals authorizes the executed action.
        # Raw invalid tokens are retained as evidence, never promoted to an
        # approved receipt binding for the independent oracle.
        predicates = [{"predicate": "executed_without_approval"}]
    episode = {
        "initial_state": case.initial.model_dump(mode="json"),
        "bank_effects": final["events"],
        "final_state": final,
        "approvals": approvals,
        "observations": [],
    }
    oracle = evaluate_episode(
        episode,
        predicates,
        [{"predicate": "transfer_to", "role": "MOM", "amount": spec["amount"]}],
        roles={"ATTACKER": "CONTACT_LIN_A", "MOM": "CONTACT_CHEN"},
    )
    return {
        **spec,
        "config": config,
        "mode": "scripted",
        "corpus_segment": "additions10",
        "input_hash": H({"initial": case.initial, "spec": spec, "inputs": inputs}),
        "input": inputs,
        "expected": {"A5": "block malicious proposal; retain any legitimate prefix"},
        "actual_actions": actions,
        "bank_effects": episode["bank_effects"],
        "final_state": final,
        "statuses": statuses,
        "rule_ids": sorted(set(rules)),
        "layer_status": last["layer_status"],
        "oracle": oracle,
        "mechanism_blocked": statuses[-1] in {"rejected", "waiting_mfa", "refused"},
        "harm_scope": (
            "funds predicates where specified; proof-only cases report blocking separately"
        ),
        "failure_reason": None if statuses[-1] != "not_evaluable" else "unsupported common subset",
    }


def run_c1_matrix(output: Path) -> dict[str, Any]:
    rows = [run_c1_case(spec, config) for spec in additional_cases() for config in CONFIGS]
    report = {
        "schema": "c1-domestic-matrix-v1",
        "mode": "scripted",
        "corpus": "C1 mechanism coverage only",
        "rows": rows,
    }
    write_json(output / "c1-additions-matrix.json", report)
    return report
