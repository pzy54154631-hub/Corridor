"""State-level observation shared by both equivalence drivers (design §3.5.1).

The fields are the 21 ``STATE_FIELDS`` of the S6/S7 comparison; page-level fields are not
compared.
``observe_state`` only reads: it never establishes a gate, delivers an SMS or touches a
widget, so taking an observation cannot change what the next gesture meets.
"""

import difflib
import json
from copy import deepcopy

from corridor.crypto.jcs import H
from corridor.kernel.binding import binding

STATE_FIELDS = (
    "response",
    "audit",
    "audit_head",
    "state_hash",
    "bank_hash",
    "execute_count",
    "budget",
    "safety",
    "clock",
    "consumed_nonces",
    "consumed_grants",
    "user_inputs",
    "receipts",
    "pending",
    "drafts",
    "results",
    "kernel",
    "session_history",
    "tool_calls",
    "local_status",
    "local",
)


def action_identity(session, rid, local):
    """The draft or pending identity a reply (or its local selection) acts on."""
    if isinstance(local, dict) and (local.get("pending_id") or local.get("draft_id")):
        return local.get("pending_id") or local.get("draft_id")
    if rid is None:
        return None
    payload = session.response(rid)["payload"]
    return payload.get("pending_id") or payload.get("draft_id")


def kernel_view(controller, identity):
    pending = controller._pending.get(identity)
    if not pending or pending.get("mode") != "prepared":
        return None
    prepared = pending["prepared"]
    verdict = prepared.proposal.verdict
    return {
        "card_core": H(binding(prepared.action, prepared.cert, controller.state).card_core),
        "verified_card_core": H(getattr(verdict, "verified_facts", {}).get("card_core", {})),
        "action": H(prepared.action),
        "cert": H(prepared.cert),
        "proposal_card": H(prepared.proposal.card),
        "display_card": H(pending["display"].card),
        "rule_ids": list(getattr(verdict, "rule_ids", [])),
        "outcome": verdict.outcome,
        "tier": getattr(verdict, "tier", None),
        "challenge": pending.get("challenge_id"),
        "verified": pending.get("verified"),
    }


def observe_state(controller, session, rid, local):
    """One observation of the 21 state fields for the reply ``rid`` (None before any)."""
    gateway = controller.gateway
    principal = controller.device.principal
    state = controller.state
    identity = action_identity(session, rid, local)
    return {
        "response": session.response(rid) if rid is not None else None,
        "audit": gateway.audit.records(),
        "audit_head": gateway.audit.checkpoint()["head"],
        "state_hash": H(state),
        "bank_hash": H(gateway.bank.final_state()),
        "execute_count": gateway.bank.execute_count,
        "budget": gateway.approval_budget(principal),
        "safety": gateway.safety_snapshot(principal)["state"],
        "clock": state.now,
        "consumed_nonces": sorted(state.security.consumed_nonces),
        "consumed_grants": sorted(state.security.consumed_grant_ids),
        "user_inputs": sorted(state.user_inputs),
        "receipts": sorted(state.receipts),
        "pending": {
            key: {
                "mode": value.get("mode"),
                "challenge": value.get("challenge_id"),
                "verified": value.get("verified"),
            }
            for key, value in sorted(controller._pending.items())
        },
        "drafts": sorted(controller._drafts),
        "results": {key: value for key, value in sorted(controller._results.items())},
        "kernel": kernel_view(controller, identity) if identity else None,
        "session_history": session.history,
        "tool_calls": session.tool_calls,
        "local_status": local.get("status") if isinstance(local, dict) else None,
        "local": local,
    }


def normalized(observation):
    """The JSON form both drivers write; comparisons run on this form only."""
    return json.loads(json.dumps(observation, ensure_ascii=False, sort_keys=True, default=str))


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=0)


def short_diff(left, right, limit=12):
    lines = list(
        difflib.unified_diff(
            dumps(left).splitlines(),
            dumps(right).splitlines(),
            "streamlit",
            "web",
            lineterm="",
            n=0,
        )
    )
    body = [line for line in lines if not line.startswith(("---", "+++", "@@"))]
    extra = f" ... (+{len(body) - limit} more)" if len(body) > limit else ""
    return "; ".join(body[:limit]) + extra


LOCALIZED = "<localized text>"


def without_localized_text(stage):
    """The stage with the strings rendered in the gesture's language blanked: the reply's
    ``message`` and the ``text`` / ``display_value`` of receipt narratives, in the reply
    payload and in stored results. Keys, list lengths and exact values stay compared.
    Applied only where the contract change allows it (CONTRACT-CHANGES.md Q-1, option A)."""
    stage = deepcopy(stage)
    response = stage.get("response") or {}
    if "message" in response:
        response["message"] = LOCALIZED
    for holder in [response.get("payload"), *(stage.get("results") or {}).values()]:
        facts = holder.get("facts") if isinstance(holder, dict) else None
        if not isinstance(facts, dict):
            continue
        if "text" in facts:
            facts["text"] = LOCALIZED
        for item in facts.get("facts") or []:
            for key in ("text", "display_value"):
                if isinstance(item, dict) and key in item:
                    item[key] = LOCALIZED
    return stage


def compare_stages(name, left, right, *, localized_from=None, left_name="streamlit"):
    """Field-by-field comparison of one flow (Streamlit or reference ``left``, web ``right``).

    Returns (state differences, call-count differences, report lines). The report follows
    ``compare_dumps.py``: a stage missing on one side, renamed or ended in an error counts
    once, and every differing state field counts once with a short unified diff. Call
    counts (``calls``) are reported apart and never enter the state total. From stage
    index ``localized_from`` on, both sides go through ``without_localized_text`` first.
    """
    lines = [
        f"== flow {name}: {left_name} stages {[s['stage'] for s in left]}; "
        f"web stages {[s['stage'] for s in right]}"
    ]
    differences = calls = 0
    for index in range(max(len(left), len(right))):
        if index >= len(left) or index >= len(right):
            stage = (left if index < len(left) else right)[index]["stage"]
            side = "web" if index >= len(right) else left_name
            lines.append(f"   stage {stage}: MISSING on {side}")
            differences += 1
            continue
        stage, other = left[index], right[index]
        errors = [item.get("error") for item in (stage, other) if item["stage"] == "error"]
        if errors:
            lines.append(f"   stage {index}: RUN ERROR {errors}")
            differences += 1
            continue
        if stage["stage"] != other["stage"]:
            lines.append(f"   stage {stage['stage']}: NAME DIFF web has {other['stage']}")
            differences += 1
        if localized_from is not None and index >= localized_from:
            stage, other = without_localized_text(stage), without_localized_text(other)
            lines.append(f"   stage {stage['stage']}: localized narrative text not compared")
        same = 0
        for field in STATE_FIELDS:
            if stage.get(field) == other.get(field):
                same += 1
            else:
                differences += 1
                lines.append(
                    f"   stage {stage['stage']}: STATE DIFF in {field}: "
                    f"{short_diff(stage.get(field), other.get(field))}"
                )
        if stage.get("calls") != other.get("calls"):
            calls += 1
            lines.append(
                f"   stage {stage['stage']}: CALL COUNT DIFF: "
                f"{short_diff(stage.get('calls'), other.get('calls'))}"
            )
        lines.append(f"   stage {stage['stage']}: state fields identical: {same}")
    return differences, calls, lines
