"""Isolated, explicitly scripted birthday demonstrations of frozen decisions.

The reference predicates are a labelled presentation contract, never an input to
approval or dispatch. Each branch owns its gateway and device; no active banking
controller is accepted by the change-probe API.
"""

from copy import deepcopy
from datetime import UTC, datetime, timedelta, timezone
from fractions import Fraction

import streamlit as st

from corridor.bench.bank_cases import BankCase
from corridor.bench.planning_runner import birthday_prepared, prepare_birthday
from corridor.crypto.jcs import H
from corridor.kernel.binding import dependency_hashes
from corridor.schemas.bank import AccountMovement, BusinessEvent
from corridor.schemas.money import fraction_string
from corridor.sim.billing import subscription_state
from corridor.sim.keys import stable_id
from corridor.sim.planning import make_planning_state
from corridor.stores.bank import add_seconds, available
from corridor.ui import display_names
from corridor.ui.chain_consistency import affected_downstream, reference_preconditions
from corridor.ui.components import STATUSES, money
from corridor.ui.controller import DemoController
from corridor.ui.messages import source_utterance, tr

CHANGE_NAMES = {
    "subscription": "Subscription debit of CNY 49",
    "business_day": "Cross the business-day boundary",
    "destination": "Recipient destination version change",
    "balance": "Insufficient balance",
}
NODE_NAMES = ["Lock birthday budget", "Unlock when due", "Order flowers", "Order cake"]


def _classification_label(value):
    for key in (
        "Not evaluated (insufficient evidence)",
        "Normal success",
        "False invalidation (reference)",
        "Missed invalidation (reference)",
        "Necessary invalidation (reference)",
    ):
        if value == source_utterance(key):
            return tr(key)
    return value


def _gateway_status(value):
    return tr("u1.chain.66" if value == "rejected" else STATUSES.get(value, value))


def create_chain_probe(kind):
    """An explicit demo gesture signs only this new synthetic session's setup."""
    if kind not in {*CHANGE_NAMES, "none"}:
        raise ValueError("Unknown chain demonstration")
    state = make_planning_state()
    if kind == "business_day":
        # Exactly one second crosses UTC+8 midnight, without token/session expiry.
        local = datetime.fromisoformat(state.now).astimezone(timezone(timedelta(hours=8)))
        boundary = local.replace(hour=0, minute=0, second=0) + timedelta(days=1, seconds=-1)
        state = state.model_copy(
            update={"now": boundary.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")}
        )
    if kind == "subscription":
        recurring = subscription_state()
        subscription = recurring.subscriptions["S1"].model_copy(
            update={"next_due": add_seconds(state.now, 1)}
        )
        state = state.model_copy(
            update={
                "subscriptions": {subscription.id: subscription},
                "merchants": {**state.merchants, **recurring.merchants},
            }
        )
    case = BankCase(state, task_id="U1-CHAIN", subcase_id=kind)
    bundle = prepare_birthday(case)
    # Current affordability is a lock/payment predicate, not a prerequisite to
    # creating a future SPA. Use the lock node for that distinct probe.
    target_index = 0 if kind == "balance" else 2
    setup = []
    for index in range(target_index):
        prepared = birthday_prepared(case, bundle, index)
        result = case.dispatch(prepared)
        if result.status not in {"controlled", "settled"}:
            raise ValueError("Isolated birthday setup was rejected")
        setup.append({"node_id": prepared.action.node_id, "status": result.status})
    prepared = birthday_prepared(case, bundle, target_index)
    if prepared.proposal.verdict.outcome != "Accept":
        raise ValueError("Isolated birthday approval was not accepted")
    token = case.device.approve(
        prepared.action, prepared.cert, prepared.proposal.card, now=case.state.now
    )
    return {
        "kind": kind,
        "case": case,
        "bundle": bundle,
        "prepared": prepared,
        "token": token,
        "before_state": case.state,
        "target_index": target_index,
        "setup": setup,
    }


def preview_chain_change(demo):
    """Describe the planned intervention without mutating the detached gateway."""
    nodes = demo["bundle"]["plan"].nodes
    target = nodes[demo["target_index"]].node_id
    affected = {target, *affected_downstream([node.model_dump() for node in nodes], {target})}
    return {
        "target_node_id": target,
        "target_label": NODE_NAMES[demo["target_index"]],
        "phase": tr("Before budget-lock dispatch")
        if demo["kind"] == "balance"
        else tr("Before dispatching flower-schedule creation"),
        "affected_node_ids": [node.node_id for node in nodes if node.node_id in affected],
        "affected_labels": [
            label for node, label in zip(nodes, NODE_NAMES, strict=True) if node.node_id in affected
        ],
        "expected_components": {
            "subscription": ["financial"],
            "business_day": ["business_date"],
            "destination": ["objects"],
            "balance": ["financial"],
            "none": [],
        }[demo["kind"]],
    }


def _apply_change(demo):
    case, kind = demo["case"], demo["kind"]
    if kind in {"subscription", "business_day"}:
        case.gateway.advance_clock(add_seconds(case.state.now, 1))
    elif kind == "destination":
        merchants = dict(case.state.merchants)
        merchant = merchants["FLOWERS"]
        # Merchant.version is the version bound by the existing merchant SPA.
        merchants[merchant.id] = merchant.model_copy(update={"version": merchant.version + 1})
        case.gateway.store.update(merchants=merchants)
    elif kind == "balance":
        # Trusted synthetic ledger injection, like the published mechanism
        # probes. It is not a claimed external payment interface or receipt.
        case.gateway.store.append_event(
            BusinessEvent(
                id=stable_id(case.identity, 0, "balance-probe"),
                intent_key=H({"synthetic_balance_probe": case.identity}),
                event="settled",
                kind="subscription_debit",
                principal=case.device.principal,
                debit=AccountMovement(account_ref="A_CURRENT", amount=Fraction(9900)),
                object_delta={"synthetic_ui_probe": True},
                ts=case.state.now,
            )
        )


def run_chain_probe(demo):
    """Apply the disclosed event, then dispatch the original immutable approval."""
    if "result" in demo:
        return deepcopy(demo["result"])
    case, prepared, token = demo["case"], demo["prepared"], demo["token"]
    before = demo["before_state"]
    _apply_change(demo)
    current = case.state
    hashes = dependency_hashes(prepared.action, prepared.cert, current)
    keys = sorted(set(hashes) | set(prepared.cert.dependency_hashes))
    changed = [key for key in keys if hashes.get(key) != prepared.cert.dependency_hashes.get(key)]
    reference = reference_preconditions(
        prepared.action, prepared.cert, before, current, token=token
    )
    reference_holds = (
        None if reference["status"] == "not_evaluated" else reference["status"] == "holds"
    )
    audit_start = len(case.gateway.audit.records())
    calls_before = case.gateway.bank.execute_count
    result = case.gateway.dispatch(prepared.action, prepared.cert, token)
    records = case.gateway.audit.records()[audit_start:]
    verdicts = [
        row["payload"]
        for row in records
        if row["event"] == "kernel_verdict"
        and row.get("payload", {}).get("action_hash") == H(prepared.action)
    ]
    verdict = verdicts[-1] if verdicts else {}
    success = result.status in {"settled", "controlled"}
    classification = (
        source_utterance("Not evaluated (insufficient evidence)")
        if reference_holds is None
        else source_utterance("Normal success")
        if reference_holds and success
        else source_utterance("False invalidation (reference)")
        if reference_holds
        else source_utterance("Missed invalidation (reference)")
        if success
        else source_utterance("Necessary invalidation (reference)")
    )
    report = {
        "status": result.status,
        "rule_ids": result.rule_ids,
        "kernel_outcome": verdict.get("outcome", tr("Kernel not reached")),
        "kernel_rules": verdict.get("rule_ids", []),
        "reference_label": source_utterance("Reference judgment · research convention"),
        "reference_holds": reference_holds,
        "reference": reference,
        "classification": classification,
        "changed_components": changed,
        "dependency_comparison": [
            {
                tr("Component"): key,
                tr("At approval"): prepared.cert.dependency_hashes.get(key),
                tr("Current"): hashes.get(key),
                tr("Change"): key in changed,
            }
            for key in keys
        ],
        "available_before": fraction_string(available(before, "A_CURRENT")),
        "available_after": fraction_string(available(current, "A_CURRENT")),
        "clock_before": before.now,
        "clock_after": current.now,
        "target_bank_calls": case.gateway.bank.execute_count - calls_before,
        "token_consumed": token.nonce in case.state.security.consumed_nonces,
        "token_expiry": token.expiry,
        "reject_streak_before": before.security.reject_streak.get(case.device.principal, 0),
        "reject_streak_after": case.state.security.reject_streak.get(case.device.principal, 0),
        "safety": case.gateway.safety_snapshot(case.device.principal),
        "audit_valid": case.gateway.verify_log(),
    }
    demo["result"] = report
    return deepcopy(report)


def preview_normal_birthday():
    """Build unsigned cards in a detached session before the approval gesture."""
    controller = DemoController()
    controller.start_session()
    draft = controller.preview_birthday(request_id="U1-NORMAL-BIRTHDAY")
    return {"controller": controller, "draft": draft, "plan_id": draft["plan_id"]}


def normal_birthday_scope(draft):
    """Project the exact unsigned canonical cards without inventing fixed scope."""
    lines = []
    for index, node in enumerate(draft["nodes"], 1):
        card = node["card"]
        objects = card.get("objects") or {}
        refs = [
            objects[key] for key in ("source_ref", "locked_ref", "merchant_ref") if objects.get(key)
        ]
        line = f"{index}. {tr(node['label'])} · {money(card['amount'])} · {' / '.join(refs)}"
        window = card.get("execution_window")
        if window:
            line += " · " + " → ".join(
                datetime.fromisoformat(window[key]).strftime("%Y-%m-%d %H:%M UTC")
                for key in ("start", "end")
            )
        if card.get("spa"):
            line += " · " + tr("u1.chain.scope_runs", count=card["spa"]["max_runs"])
        lines.append(line)
    return "\n\n".join(lines)


def create_normal_birthday(demo=None):
    """Called only after the labelled preset device-approval gesture."""
    demo = demo or preview_normal_birthday()
    controller, draft = demo["controller"], demo["draft"]
    result = controller.confirm_plan(draft["plan_id"], [node["id"] for node in draft["nodes"]])
    if result["status"] != "settled":
        raise ValueError("Normal birthday setup was rejected")
    return {
        "controller": controller,
        "draft": draft,
        "plan_id": draft["plan_id"],
        "setup_result": result,
        "due_at": max(run.due_at for run in controller.state.schedules.values()),
    }


def finish_normal_birthday(demo):
    if "completion" in demo:
        return deepcopy(demo["completion"])
    controller = demo["controller"]
    advanced = controller.advance_clock(demo["due_at"])
    snapshot = controller.gateway.plan_snapshot(demo["plan_id"])
    complete = all(node["status"] == "succeeded" for node in snapshot["nodes"].values())
    result = {
        "complete": complete,
        "paid_orders": sum(order.status == "paid" for order in controller.state.orders.values()),
        "advance_result": advanced,
        "snapshot": snapshot,
        "audit_valid": controller.gateway.verify_log(),
    }
    demo["completion"] = result
    return deepcopy(result)


def render_chain_demo():
    """Two separate gestures make predictions visible before the real dispatch."""
    st.subheader(tr("Mid-chain changes · isolated demonstration"))
    st.caption(
        tr(
            "Each button creates an isolated synthetic gateway and performs the labelled "
            "scripted device approvals. The banking session is unaffected. Review the "
            "impact preview, then click to verify dispatch."
        )
    )
    st.caption(
        tr(
            "These observations cover synthetic mechanism probes. Necessary stops and "
            "benign costs are both retained; not every APR-DRIFT is a blocked attack."
        )
    )
    columns = st.columns(4)
    for column, (kind, label) in zip(columns, CHANGE_NAMES.items(), strict=True):
        if column.button(tr(label), key=f"chain_probe_{kind}"):
            st.session_state["chain_probe"] = create_chain_probe(kind)
    demo = st.session_state.get("chain_probe")
    if not demo:
        st.info(
            tr(
                "Choose a change to inspect the original approval, dependency components and "
                "predicted downstream impact."
            )
        )
        return
    prediction = preview_chain_change(demo)
    st.info(
        tr("Impact preview (not a system verdict): ")
        + " → ".join(tr(label) for label in prediction["affected_labels"])
        + tr("; components to compare: ")
        + "、".join(prediction["expected_components"])
    )
    st.caption(tr("Current verification phase: ") + prediction["phase"])
    if demo["kind"] == "balance":
        st.caption(
            tr(
                "The balance probe injects a synthetic ledger debit before the first lock "
                "node. It creates no bank receipt, does not synchronize the mock bank "
                "balance, and demonstrates neither an external payment entry point nor "
                "payment after reapproval."
            )
        )
    elif demo["kind"] == "destination":
        st.caption(
            tr(
                "This recipient is the synthetic flower merchant. Changing its SPA-bound "
                "merchant version is a trusted-state mechanism probe."
            )
        )
    st.caption(
        tr(
            "Downstream impact is predicted only through depends_on; undispatched nodes "
            "are not labelled as rejected by the kernel."
        )
    )
    if st.button(
        tr("Apply the change and verify through frozen gateway dispatch"),
        key="chain_probe_dispatch",
        disabled="result" in demo,
    ):
        result = run_chain_probe(demo)
        st.session_state.setdefault("chain_probe_history", {})[demo["kind"]] = result
    history = st.session_state.get("chain_probe_history", {})
    st.dataframe(
        [
            {
                tr("Change"): tr(label),
                tr("Frozen gateway actual result"): _gateway_status(
                    history.get(kind, {}).get("status", tr("Not run"))
                ),
                tr("Reference judgment · research convention"): _classification_label(
                    history.get(kind, {}).get("classification", tr("Not evaluated"))
                ),
                tr("Target bank executions"): str(history[kind]["target_bank_calls"])
                if kind in history
                else "—",
            }
            for kind, label in CHANGE_NAMES.items()
        ],
        hide_index=True,
        use_container_width=True,
    )
    necessary = [history.get(kind) for kind in ("business_day", "destination", "balance")]
    if all(
        row and row["status"] == "rejected" and row["target_bank_calls"] == 0 for row in necessary
    ):
        st.success(
            tr("Reference judgment · research convention")
            + " · "
            + tr(
                "All three required-invalidation probes on this page were stopped. This "
                "applies only to these synthetic changes and does not prove absence of "
                "missed invalidation."
            )
        )
    if history.get("subscription", {}).get("classification") == source_utterance(
        "False invalidation (reference)"
    ):
        st.warning(
            tr("Reference judgment · research convention")
            + " · "
            + tr(
                "Some benign changes are blocked too (visible cost): permission "
                "prerequisites held after the subscription debit, but the original approval "
                "was invalidated."
            )
        )
    result = demo.get("result")
    if not result:
        st.write(
            tr(
                "The original approval was signed by the isolated simulated device. The "
                "change has not been applied and the target has not been dispatched."
            )
        )
        return
    actual, reference = st.columns(2)
    with actual:
        st.markdown(tr("**Frozen binding · actual result**"))
        st.write(
            tr(
                "Gateway: {status} · {rules}",
                status=_gateway_status(result["status"]),
                rules=" / ".join(result["rule_ids"]),
            )
        )
        st.write(
            tr(
                "Kernel: {outcome} · {rules}",
                outcome=display_names.outcome_label(result["kernel_outcome"]),
                rules=" / ".join(result["kernel_rules"]),
            )
        )
        st.write(tr("Target bank executions: {count}", count=result["target_bank_calls"]))
    with reference:
        st.markdown(tr("**Reference judgment · research convention**"))
        st.write(
            tr("Permission prerequisites have not been evaluated")
            if result["reference_holds"] is None
            else tr("Permission prerequisites still hold")
            if result["reference_holds"]
            else tr("Permission prerequisites have changed")
        )
        st.write(_classification_label(result["classification"]))
        st.caption(
            tr(
                "The reference contract excludes equality of the original balance; policy, "
                "business date, objects, inputs and valid authority must remain unchanged."
            )
        )
    if result["classification"] == source_utterance("False invalidation (reference)"):
        st.warning(
            tr("Reference judgment · research convention")
            + " · "
            + tr(
                "Some benign changes are blocked too (visible cost): this native CNY 49 "
                "subscription debit invalidated the original approval."
            )
        )
    elif result["classification"] == source_utterance("Necessary invalidation (reference)"):
        st.success(
            tr("Reference judgment · research convention")
            + " · "
            + tr(
                "This target required stopping and was stopped before bank execution; this "
                "is not proof of complete security."
            )
        )
    st.caption(
        tr(
            (
                "Account kernel rejection count: {before} → {after}; environmental drift may "
                "also count toward the application write lock, so these are not all blocked "
                "attacks."
            ),
            before=result["reject_streak_before"],
            after=result["reject_streak_after"],
        )
    )
    st.dataframe(result["dependency_comparison"], hide_index=True, use_container_width=True)
    with st.expander(tr("Technical evidence · real dispatch, approval consumption and audit")):
        st.json({key: value for key, value in result.items() if key != "dependency_comparison"})


def render_normal_birthday():
    from corridor.ui.components import render_plans

    st.subheader(tr("Birthday plan · normal completion"))
    st.caption(
        tr(
            "Isolated synthetic preset: explicitly approve four DAG nodes, then execute "
            "the existing SPAs when due. Operator mode is unnecessary."
        )
    )
    if "birthday_normal_preview" not in st.session_state:
        st.session_state["birthday_normal_preview"] = preview_normal_birthday()
    preview = st.session_state["birthday_normal_preview"]
    st.info(normal_birthday_scope(preview["draft"]))
    if st.button(
        tr("Load normal preset and approve each node on the simulated device"),
        key="birthday_normal_start",
    ):
        st.session_state["birthday_normal"] = create_normal_birthday(preview)
        st.session_state.pop("birthday_normal_preview", None)
    demo = st.session_state.get("birthday_normal")
    if not demo:
        return
    controller = demo["controller"]
    if st.button(
        tr("Advance to two days before the birthday and execute due tasks"),
        key="birthday_normal_finish",
        disabled="completion" in demo,
    ):
        finish_normal_birthday(demo)
    render_plans(controller.snapshot()["plans"], key="birthday-normal")
    result = demo.get("completion")
    if result:
        if result["complete"]:
            st.success(
                tr(
                    "Birthday chain completed normally: flowers and cake both have payment "
                    "receipts, and all four DAG nodes succeeded."
                )
            )
        else:
            st.warning(
                tr(
                    "The actual chain is incomplete. Review the gateway result; replay and "
                    "approval are not business completion."
                )
            )
        st.caption(
            tr(
                "Paid orders: {count}; audit chain valid: {valid}",
                count=result["paid_orders"],
                valid=tr("Audit hash chain verified")
                if result["audit_valid"]
                else tr("Audit hash chain verification failed"),
            )
        )
        with st.expander(tr("Normal completion · real receipts and execution evidence")):
            st.json(result)
