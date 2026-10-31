"""Isolated schedule/AA effect builders and evidence projections for experiments.

These are state effects, never fabricated bank receipts. Future scheduled payment
execution is outside this adapter's scope. Production always uses its gateway.
"""

from fractions import Fraction
from typing import Any

from corridor.crypto.jcs import H
from corridor.crypto.sign import RECEIPT, sign
from corridor.kernel.binding import intent_key, resolve_amount
from corridor.kernel.spa import due_at
from corridor.schemas.bank import AnalysisReceipt, AnalysisResult, BusinessEvent, InputTerm
from corridor.schemas.domain import ScheduledTask
from corridor.sim.keys import signing_key, stable_id
from corridor.stores.bank import add_seconds

LOCAL_WRITE_KINDS = frozenset({"schedule_transfer", "aa_request"})


def execute_local_write(
    action: Any, cert: Any, state: Any, token: Any, *, verified: bool
) -> tuple[Any, Any]:
    """Apply only an already-selected effective decision to private state."""
    key = intent_key(action, state)
    if action.kind == "schedule_transfer":
        spa = state.authorizations[cert.authorization_ref]
        runs = {}
        for index in range(spa.max_runs):
            due = due_at(spa, index)
            if due is None:
                break
            run = ScheduledTask(
                id=stable_id(spa.id, index, "scheduled-run"),
                owner=action.principal,
                plan_id=action.plan_id,
                node_id=action.node_id,
                spa_ref=spa.id,
                run_index=index,
                action_template={"kind": spa.target.kind},
                input_refs=sorted({
                    term.input_ref
                    for term in cert.derivations.values()
                    if isinstance(term, InputTerm)
                }),
                due_at=due,
                expires_at=min(add_seconds(due, 86400), spa.end),
                commitment_id=H({"spa_id": spa.id, "run_index": index}),
                status="pending",
            )
            runs[run.id] = run
        if not runs:
            raise ValueError("No schedulable occurrence")
        delta = {
            "spa_hash": H(spa),
            "creation_tier": spa.creation_tier,
            "approval_verified": verified and token is not None,
            "approval_hash": H(token) if token else None,
            "mfa_verified": verified and spa.creation_tier == "red" and bool(cert.mfa_grant_refs),
            "mfa_hash": H(state.security.mfa_grants[cert.mfa_grant_refs[0]])
            if cert.mfa_grant_refs
            else None,
            "source_version": state.accounts[spa.source_ref].version,
            "destination_version": spa.destination_version,
            "authorization_ref": cert.authorization_ref,
        }
        event = BusinessEvent(
            id=stable_id(key, len(state.ledger), "controlled"),
            intent_key=key,
            event="settled",
            kind=action.kind,
            principal=action.principal,
            object_delta=delta,
            ts=state.now,
        )
        return state.model_copy(
            update={"schedules": {**state.schedules, **runs}, "ledger": [*state.ledger, event]}
        ), None
    if action.kind != "aa_request":
        raise ValueError("Unsupported local write")
    amount = resolve_amount(action, state)
    if amount is None:
        raise ValueError("AA total cannot be resolved")
    participants = sorted(action.args.participant_refs)
    quotient, remainder = divmod(int(amount * 100), len(participants))
    results = {
        "total": AnalysisResult(
            value=f"{amount.numerator}/{amount.denominator}", unit="CNY", source_tx_ids=[]
        ),
        "request_ids": AnalysisResult(
            value=[
                stable_id(action.intent_id, i, f"aa-{ref}") for i, ref in enumerate(participants)
            ],
            unit="request_ids",
            source_tx_ids=[],
        ),
        "status": AnalysisResult(value="requested", unit="request_status", source_tx_ids=[]),
    }
    for index, ref in enumerate(participants):
        share = Fraction(quotient + (index < remainder), 100)
        results[f"shares.{ref}"] = AnalysisResult(
            value=f"{share.numerator}/{share.denominator}", unit="CNY", source_tx_ids=[]
        )
    receipt = AnalysisReceipt(
        receipt_id=stable_id(action.intent_id, 0, "aa-receipt"),
        dataset_hash=H({"total": amount, "participants": participants}),
        function_id="aa_split_v1",
        parameters={"principal": action.principal, "action_hash": H(action)},
        results=results,
        trust="builtin",
        computed_at=state.now,
        sig="0" * 128,
    )
    receipt = receipt.model_copy(update={"sig": sign(signing_key("gateway"), RECEIPT, receipt)})
    event = BusinessEvent(
        id=stable_id(action.intent_id, len(state.ledger), "analysis"),
        intent_key=key,
        event="settled",
        kind=action.kind,
        principal=action.principal,
        object_delta={
            "authorization_ref": cert.authorization_ref,
            "analysis_receipt_id": receipt.id,
            "action_hash": H(action),
            "receipt_hash": H(receipt),
        },
        ts=state.now,
    )
    return state.model_copy(
        update={
            "ledger": [*state.ledger, event],
            "receipts": {**state.receipts, receipt.id: receipt},
        }
    ), receipt


def local_effects(state: Any) -> list[dict[str, Any]]:
    """Project real ledger writes for the independent operation/count predicates."""
    events = []
    for event in state.ledger:
        if event.kind not in LOCAL_WRITE_KINDS or event.event != "settled":
            continue
        delta = event.object_delta
        if event.kind == "aa_request":
            receipt = state.receipts.get(delta.get("analysis_receipt_id"))
            if not isinstance(receipt, AnalysisReceipt):
                continue
            payload = receipt.model_dump(mode="json")
            effects = [
                {
                    "effect_type": "state",
                    "object_ref": receipt.id,
                    "field": "aa_requests",
                    "before": None,
                    "after": payload["results"],
                }
            ]
        else:
            effects = [
                {
                    "effect_type": "state",
                    "object_ref": run.id,
                    "field": "schedule",
                    "before": None,
                    "after": run.model_dump(mode="json"),
                }
                for run in state.schedules.values()
                if run.spa_ref == delta.get("authorization_ref")
            ]
        events.append({
            "kind": event.kind,
            "status": "settled",
            "receipt_id": event.id,
            "intent_key": event.intent_key,
            "effect_origin": "ledger_projection",
            "source_event": event.model_dump(mode="json"),
            "exact_effects": effects,
        })
    return events
