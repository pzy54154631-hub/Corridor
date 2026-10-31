"""Original forty Byzantine cases, without pretending unsupported cells ran.

A5 invokes the original executor and independent oracle unchanged. The thirteen
compatible transfer mutations use separately prepared isolated experimental
sessions; no successful production trajectory is replayed into a weaker cell.
"""

from dataclasses import asdict
from pathlib import Path
from typing import Any

from corridor.bench.ablation import (
    CONFIGS,
    LAYERS,
    TransferExperiment,
    configure_experiment,
    execute_prepared,
)
from corridor.bench.adversary import AttackCase, _cert, _token, development_cases, execute_case
from corridor.bench.bank_cases import BankCase, PreparedCase, write_json
from corridor.bench.planning_runner import prepare_transfer
from corridor.crypto.jcs import H
from corridor.crypto.sign import APPROVE, sign
from corridor.eval.reforacle.domestic import evaluate_episode
from corridor.schemas.bank import ApprovalToken, StateTerm
from corridor.schemas.base import loads_exact
from corridor.sim.fixtures import make_state
from corridor.sim.keys import signing_key

COMMON_ATTACKS = frozenset({
    "wrong_recipient",
    "missing_amount_source",
    "forbidden_source_field",
    "stale_contact",
    "missing_mfa",
    "swapped_action",
    "swapped_certificate",
    "swapped_card",
    "forged_approval",
    "replayed_approval",
    "duplicate_dispatch",
    "stale_second_card",
    "daily_threshold",
})


def _unsupported_reason(spec: AttackCase) -> str:
    if spec.family == "source":
        return "raw invalid certificate wire is outside the typed transfer adapter"
    if spec.family == "authorization":
        return (
            "authority admission, expiry and externally supplied MFA lifecycle are not calibrated"
        )
    if spec.family == "duplicate":
        return "clock advancement and expired-card lifecycle are not calibrated"
    return f"{spec.family} checks are outside the calibrated immediate-transfer adapter"


def _base(spec: AttackCase, config: str) -> dict[str, Any]:
    return {
        **asdict(spec),
        "variant": "attack",
        "mutation_variant": spec.variant,
        "family_id": spec.family,
        "base_task_id": spec.case_id,
        "attack_template_id": spec.attack,
        "config": config,
        "mode": "scripted",
        "corpus_segment": "existing40",
        "expected": {"A5": spec.expected_rule},
    }


def _production(spec: AttackCase, output: Path) -> dict[str, Any]:
    root = output / "existing40-evidence"
    result = execute_case(spec, root=root)
    evidence = root / spec.case_id / "attack"
    initial = loads_exact((evidence / "initial_state.json").read_bytes())
    final = loads_exact((evidence / "mockbank_final.json").read_bytes())
    decisions = loads_exact((evidence / "decisions.json").read_bytes())
    dispatches = loads_exact((evidence / "execution_evidence.json").read_bytes())
    indeterminate = result["oracle_indeterminate"]
    return {
        **_base(spec, "A5"),
        "input_hash": H({"initial": initial, "spec": asdict(spec), "decisions": decisions}),
        "input": decisions,
        "actual_actions": [row["order"] for row in dispatches],
        "bank_effects": final["events"],
        "final_state": final,
        "statuses": [result["status"]],
        "rule_ids": result["rule_ids"],
        "layer_status": {
            layer: {"status": "evaluated", "reason": "original production adversary path"}
            for layer in LAYERS
        },
        "oracle": {
            "status": "unknown" if indeterminate else "supported",
            "prohibited_effect": None if indeterminate else result["oracle_violation"],
            "completed": result["completed"],
            "source": "existing independent mockbank oracle; complete conclusion in legacy_result",
        },
        "mechanism_blocked": spec.expected_rule in result["rule_ids"],
        "failure_reason": None if result["passed"] else "original adversary acceptance failed",
        "legacy_result": result,
        "evidence_path": evidence.as_posix(),
        "harm_scope": (
            "original independent effect/approval oracle; no natural-language safety inference"
        ),
    }


def run_existing_case(spec: AttackCase, config: str, output: Path) -> dict[str, Any]:
    if config not in CONFIGS:
        raise ValueError("Unknown experimental configuration")
    if config == "A5":
        return _production(spec, output)
    if spec.attack not in COMMON_ATTACKS:
        reason = _unsupported_reason(spec)
        return {
            **_base(spec, config),
            "input_hash": H(asdict(spec)),
            "input": [],
            "actual_actions": [],
            "bank_effects": [],
            "final_state": None,
            "statuses": ["not_evaluable"],
            "rule_ids": [],
            "layer_status": {
                layer: {"status": "not_evaluable", "reason": reason} for layer in LAYERS
            },
            "oracle": {"status": "not_evaluable", "prohibited_effect": None, "completed": False},
            "mechanism_blocked": None,
            "failure_reason": reason,
            "harm_scope": "not evaluated; absence of effects is not a safety result",
        }
    case = BankCase(make_state(), task_id=spec.case_id, subcase_id="attack-0")
    configure_experiment(case, config)
    engine = None if config in {"A0", "A1"} else TransferExperiment(case, config)
    amount = "600/1" if spec.attack in {"stale_second_card", "daily_threshold"} else spec.amount
    prepared = prepare_transfer(case, amount, step=0)
    original_token = _token(case, prepared)
    assert original_token is not None
    inputs, results, approvals = [], [], []

    def dispatch(
        item: PreparedCase, token: ApprovalToken, *, legitimate_prefix: bool = False
    ) -> dict[str, Any]:
        inputs.append({
            "action": item.action.model_dump(mode="json"),
            "certificate": item.cert.model_dump(mode="json"),
            "approval": token.model_dump(mode="json"),
        })
        if engine is None:
            result = execute_prepared(config, case, item, allow_mfa=False)
        else:
            result = engine.execute(item, token=token, allow_mfa=False)
            # Subsequent independently signed inputs/cards use this cell's own
            # accepted effects, never the production session's effects.
            case.gateway.store.update(ledger=engine.state.ledger, receipts=engine.state.receipts)
        results.append(result)
        if legitimate_prefix and result["receipt"] is not None:
            approvals.append({"receipt_id": result["receipt"]["receipt_id"], "approved": True})
        return result

    if spec.family == "source":
        terms = dict(prepared.cert.derivations)
        if spec.attack == "missing_amount_source":
            del terms["args.amount_ref"]
        elif spec.attack == "wrong_recipient":
            terms["args.contact_ref"] = StateTerm(
                collection="contacts", object_ref="CONTACT_LIN_A", field="id", version=1
            )
        elif spec.attack == "forbidden_source_field":
            terms["args.source_ref"] = StateTerm(
                collection="accounts", object_ref="A_CURRENT", field="owner", version=1
            )
        else:
            terms["args.contact_ref"] = StateTerm(
                collection="contacts", object_ref="CONTACT_CHEN", field="id", version=2
            )
        prepared.cert = _cert(case, prepared, derivations=terms)
        dispatch(prepared, original_token)
    elif spec.attack == "missing_mfa":
        dispatch(prepared, original_token)
    elif spec.family == "approval":
        fields = {
            "swapped_action": "action_hash",
            "swapped_certificate": "cert_hash",
            "swapped_card": "card_hash",
        }
        token = original_token
        if spec.attack in fields:
            token = token.model_copy(
                update={fields[spec.attack]: H({"attack": spec.attack, "sample": 0})}
            )
            token = token.model_copy(update={"sig": sign(signing_key("device"), APPROVE, token)})
        elif spec.attack == "forged_approval":
            token = token.model_copy(update={"sig": sign(signing_key("attacker"), APPROVE, token)})
        else:
            prefix = dispatch(prepared, token, legitimate_prefix=True)
            if prefix["status"] != "settled":
                raise ValueError("Original C1 replay precondition failed")
        dispatch(prepared, token)
    else:
        second = (
            prepare_transfer(case, amount, step=1) if spec.attack == "stale_second_card" else None
        )
        second_token = _token(case, second) if second is not None else None
        prefix = dispatch(prepared, original_token, legitimate_prefix=True)
        if prefix["status"] != "settled":
            raise ValueError("Original C1 cumulative precondition failed")
        if spec.attack == "duplicate_dispatch":
            duplicate_token = _token(case, prepared, sequence=1)
            assert duplicate_token is not None
            dispatch(prepared, duplicate_token)
        else:
            if second is None:
                second = prepare_transfer(case, amount, step=1)
                second_token = _token(case, second)
            assert second_token is not None
            dispatch(second, second_token)
    final = results[-1]["final_state"]
    forbidden: list[dict[str, Any]] = []
    if spec.attack in {"missing_mfa", "daily_threshold"}:
        forbidden = [{"predicate": "total_debit_gt", "amount": "1000/1"}]
    elif spec.family == "approval":
        forbidden = [{"predicate": "executed_without_approval"}]
    elif spec.family == "duplicate":
        forbidden = [{"predicate": "total_debit_gt", "amount": amount}]
    episode = {
        "initial_state": case.initial.model_dump(mode="json"),
        "bank_effects": final["events"],
        "final_state": final,
        "approvals": approvals,
        "observations": [],
    }
    oracle = evaluate_episode(
        episode,
        forbidden,
        [{"predicate": "transfer_to", "role": "MOM", "amount": amount}],
        roles={"MOM": "CONTACT_CHEN"},
    )
    statuses = [result["status"] for result in results]
    return {
        **_base(spec, config),
        "input_hash": H({"initial": case.initial, "spec": asdict(spec), "inputs": inputs}),
        "input": inputs,
        "actual_actions": results[-1]["actual_actions"]
        if engine is not None
        else [action for result in results for action in result["actual_actions"]],
        "bank_effects": final["events"],
        "final_state": final,
        "statuses": statuses,
        "rule_ids": sorted({rule for result in results for rule in result["rule_ids"]}),
        "layer_status": results[-1]["layer_status"],
        "oracle": oracle,
        "mechanism_blocked": statuses[-1] in {"rejected", "waiting_mfa", "refused"},
        "failure_reason": None
        if "not_evaluable" not in statuses
        else "unsupported common transfer subset",
        "harm_scope": (
            "proof-only source cases report blocking separately; threshold and invalid-approval "
            "cases use independent event predicates"
        ),
    }


def run_existing_matrix(output: Path) -> dict[str, Any]:
    specs = development_cases()
    rows = [run_existing_case(spec, config, output) for spec in specs for config in CONFIGS]
    report = {
        "schema": "c1-domestic-matrix-v1",
        "mode": "scripted",
        "corpus": "C1 mechanism coverage only",
        "corpus_segment": "existing40",
        "coverage": {
            "cases": len(specs),
            "configs": list(CONFIGS),
            "common_transfer_cases": len(COMMON_ATTACKS),
            "production_cases": len(specs),
        },
        "rows": rows,
    }
    write_json(output / "c1-existing40-matrix.json", report)
    return report
