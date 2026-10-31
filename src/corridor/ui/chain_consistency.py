"""Read-only birthday evidence and explicitly non-authoritative research comparisons."""

import json
from html import escape
from typing import Any

import streamlit as st

from corridor.crypto.jcs import H
from corridor.kernel.binding import binding, dependency_hashes, resolve_amount
from corridor.schemas.bank import ScheduledPaymentAuthorization
from corridor.stores.bank import available
from corridor.ui import display_names, view_mode
from corridor.ui.components import STATUSES
from corridor.ui.dag import node_label
from corridor.ui.messages import tr

REFERENCE_LABEL = "u1.chain.00"


def approval_preflight(controller: Any, *, required: int = 4) -> dict[str, Any]:
    """Read the public gateway budget; never reserve or grant an approval."""
    if required < 0:
        raise ValueError("Approval count must be nonnegative")
    budget = controller.gateway.approval_budget(controller.device.principal)
    return {
        **budget,
        "window_seconds": controller.state.policy.approval_window_seconds,
        "required": required,
        "can_start": not budget["locked"] and required <= budget["remaining"],
    }


def compare_dependencies(
    approved: dict[str, str] | None, current: dict[str, str]
) -> list[dict[str, Any]]:
    """Missing approval evidence is unknown, never implicitly an equal hash."""
    return [
        {
            "component": key,
            "approved": approved.get(key) if approved is not None else None,
            "current": current.get(key),
            "changed": approved.get(key) != current.get(key) if approved is not None else None,
        }
        for key in sorted(set(approved or {}) | set(current))
    ]


def affected_downstream(nodes: list[dict[str, Any]], changed_ids: set[str]) -> list[str]:
    """Return graph descendants, keeping independent branches out of the projection."""
    affected = set(changed_ids)
    while True:
        found = {
            node["node_id"] for node in nodes if affected.intersection(node.get("depends_on", []))
        }
        if found <= affected:
            break
        affected |= found
    return [node["node_id"] for node in nodes if node["node_id"] in affected - changed_ids]


def reference_preconditions(
    action: Any, cert: Any, approved_state: Any, current_state: Any, *, token: Any = None
) -> dict[str, Any]:
    """Compare a disclosed conservative contract, not the production kernel verdict.

    Financial-version/available-value equality is deliberately excluded. Object,
    input, policy, date, authorization and tier equality remain conservative
    prerequisites. Schedule creation does not imply funds are available at payment.
    """
    result: dict[str, Any] = {
        "label": tr(REFERENCE_LABEL),
        "status": "not_evaluated",
        "changed": [],
    }
    if approved_state is None:
        return {**result, "reason": tr("u1.chain.12")}
    try:
        old = dependency_hashes(action, cert, approved_state)
        new = dependency_hashes(action, cert, current_state)
        changed = [
            name
            for name in sorted(set(old) | set(new))
            if name != "financial" and old.get(name) != new.get(name)
        ]
        old_card = binding(action, cert, approved_state).card_core
        new_card = binding(action, cert, current_state).card_core
        if old_card["tier"] != new_card["tier"]:
            changed.append("risk_tier")
        args = action.args.model_dump(mode="python")
        source = args.get("locked_ref") if action.kind == "unlock_funds" else args.get("source_ref")
        amount = resolve_amount(action, current_state)
        funds_hold = None
        if source and amount is not None and (action.kind != "schedule_transfer"):
            account = current_state.accounts.get(source)
            funds_hold = bool(
                account and available(current_state, source) - amount >= account.floor
            )
            if not funds_hold:
                changed.append("available_after_payment_below_floor")
        authority = current_state.authorizations.get(cert.authorization_ref)
        if authority is None:
            changed.append("authorization_missing")
        elif isinstance(authority, ScheduledPaymentAuthorization):
            if current_state.now >= authority.end:
                changed.append("spa_expired")
            if action.kind != "schedule_transfer" and current_state.now < authority.start:
                changed.append("spa_not_started")
        elif current_state.now >= authority.expires_at:
            changed.append("authorization_expired")
        if token is not None:
            if not token.issued_at <= current_state.now < token.expiry:
                changed.append("token_expired")
            if token.nonce in current_state.security.consumed_nonces:
                changed.append("token_consumed")
            if token.token_id in current_state.security.void_token_ids:
                changed.append("token_voided")
        for grant_id in cert.mfa_grant_refs:
            grant = current_state.security.mfa_grants.get(grant_id)
            if grant is None or not grant.issued_at <= current_state.now < grant.expiry:
                changed.append("mfa_missing_or_expired")
            if grant_id in current_state.security.consumed_grant_ids:
                changed.append("mfa_consumed")
        if action.principal in current_state.security.locked_at:
            changed.append("account_safety_lock")
        return {
            **result,
            "status": "changed" if changed else "holds",
            "changed": list(dict.fromkeys(changed)),
            "funds_hold": funds_hold,
            "scope": tr("u1.chain.13"),
        }
    except (KeyError, ValueError, AttributeError, TypeError):
        return {**result, "reason": tr("u1.chain.28")}


def _authorization(state: Any, snapshot: dict, prepared: Any, token: Any, audit: list) -> dict:
    plan_id = snapshot.get("plan", {}).get("plan_id")
    result = {
        "blocked": snapshot.get("blocked", False),
        "voided": any(
            row["event"] == "plan_voided" and row.get("plan_id") == plan_id for row in audit
        ),
        "token": None,
        "spa": None,
        "mfa": [],
    }
    if token is not None:
        result["token"] = {
            "id": token.token_id,
            "issued_at": token.issued_at,
            "expiry": token.expiry,
            "within_time": token.issued_at <= state.now < token.expiry,
            "consumed": token.nonce in state.security.consumed_nonces,
            "voided": token.token_id in state.security.void_token_ids,
        }
    if prepared is not None:
        authority = state.authorizations.get(prepared.cert.authorization_ref)
        if isinstance(authority, ScheduledPaymentAuthorization):
            result["spa"] = {
                "id": authority.id,
                "start": authority.start,
                "end": authority.end,
                "max_runs": authority.max_runs,
            }
        for ref in prepared.cert.mfa_grant_refs:
            grant = state.security.mfa_grants.get(ref)
            result["mfa"].append({
                "id": ref,
                "expires_at": grant.expiry if grant else None,
                "within_time": bool(grant and grant.issued_at <= state.now < grant.expiry),
                "consumed": ref in state.security.consumed_grant_ids,
            })
    return result


def chain_rows(
    controller: Any, draft: dict | None = None, *, source_text: str | None = None
) -> list[dict[str, Any]]:
    """Project retained UI confirmations and public gateway snapshots without proposing."""
    state = controller.state
    audit = controller.gateway.audit.records()
    # The gateway also retains independent query/card/transfer plans. Only the
    # UI's retained birthday plans belong in this goal-and-approval comparison.
    plans = {
        item["plan"]["plan_id"]: item
        for item in controller.snapshot()["plans"]
        if item["plan"]["plan_id"] in controller._plans
    }
    if draft is not None and draft["plan_id"] not in plans:
        retained = controller._plans.get(draft["plan_id"], {})
        plan = retained.get("bundle", {}).get("plan")
        if plan is not None:
            plans[draft["plan_id"]] = {"plan": plan.model_dump(mode="json"), "nodes": {}}
    rows = []
    budget = approval_preflight(controller, required=0)
    for plan_id, snapshot in plans.items():
        retained = controller._plans.get(plan_id, {})
        plan_source = retained.get("source_text")
        if not isinstance(plan_source, str) or not plan_source:
            plan_source = (
                source_text
                if len(plans) == 1
                and draft is not None
                and plan_id == draft["plan_id"]
                and retained.get("confirmation") is None
                else None
            )
        confirmation = retained.get("confirmation")
        baseline = retained.get("actual", {}).get("approval_states", {})
        nodes = snapshot["plan"]["nodes"]
        inputs = [
            state.user_inputs[ref]
            for ref in snapshot["plan"].get("goal_input_refs", [])
            if ref in state.user_inputs and state.user_inputs[ref].slot == "goal"
        ]
        for node in nodes:
            node_id = node["node_id"]
            prepared = confirmation.prepared.get(node_id) if confirmation else None
            token = confirmation.tokens.get(node_id) if confirmation else None
            actual = confirmation.results.get(node_id) if confirmation else None
            verdicts = [
                record["payload"]
                for record in audit
                if prepared is not None
                and record["event"] == "kernel_verdict"
                and (record.get("node_id") == node_id)
                and (record["payload"].get("action_hash") == H(prepared.action))
            ]
            dependencies = []
            reference = {"label": tr(REFERENCE_LABEL), "status": "not_evaluated", "changed": []}
            # Display only. After a node's own dispatch its token is consumed and later
            # state includes this chain's own effects: that is history, not drift. The
            # reference contract is judged at dispatch time (census convention), and a
            # successful node never propagates its own settlement downstream.
            dispatched = actual is not None
            succeeded = dispatched and actual.status in {"settled", "controlled"}
            if prepared is not None:
                dependencies = compare_dependencies(
                    prepared.cert.dependency_hashes if token is not None else None,
                    dependency_hashes(prepared.action, prepared.cert, state),
                )
                if token is not None:
                    approved_state = baseline.get(node_id)
                    reference = reference_preconditions(
                        prepared.action,
                        prepared.cert,
                        approved_state,
                        approved_state if dispatched else state,
                        token=token,
                    )
                    reference["at_dispatch"] = dispatched
            rows.append({
                "plan_id": plan_id,
                "source_text": plan_source,
                "node_id": node_id,
                "label": node_label(node),
                "signed_goal": [
                    {"id": value.id, "hash": H(value), "value": value.value} for value in inputs
                ],
                "dependencies": dependencies,
                "authorization": {
                    **_authorization(state, snapshot, prepared, token, audit),
                    "budget": budget,
                },
                "depends_on": node.get("depends_on", []),
                "post_dispatch": dispatched,
                "downstream": affected_downstream(nodes, {node_id})
                if not succeeded and any(item["changed"] for item in dependencies)
                else [],
                "actual": {
                    "status": actual.status if actual else "not_dispatched",
                    "rule_ids": list(actual.rule_ids) if actual else [],
                    "kernel": actual.verdict.outcome
                    if actual and actual.verdict
                    else verdicts[-1]["outcome"]
                    if verdicts and actual
                    else None,
                    "runtime": snapshot.get("nodes", {}).get(node_id, {}).get("status"),
                },
                "reference": reference,
                "proposal": {
                    "outcome": prepared.proposal.verdict.outcome,
                    "rule_ids": list(getattr(prepared.proposal.verdict, "rule_ids", [])),
                }
                if prepared is not None
                else None,
            })
    return rows


def _text(value: Any) -> str:
    return escape(str(value))


def _json(value: Any) -> str:
    return escape(json.dumps(value, ensure_ascii=False, indent=2))


def _status(value: str) -> str:
    if value == "not_dispatched":
        return tr("u1.chain.60")
    if value == "rejected":
        return tr("u1.chain.66")
    return tr(STATUSES.get(value, value))


def _change_label(changed: bool | None) -> str:
    return tr("u1.chain.55" if changed else "u1.chain.62" if changed is False else "u1.chain.63")


def _source_anchor(plan_id: str, key: str) -> str:
    return "chain-source-" + H({"panel": key, "plan_id": plan_id})[:16]


def _sources_html(rows: list[dict[str, Any]], key: str) -> str:
    sources = {}
    for row in rows:
        sources.setdefault(row["plan_id"], row.get("source_text"))
    return "".join(
        f'<section id="{_source_anchor(plan_id, key)}">'
        f"<h4>{_text(tr('u1.chain.source_title', index=index))}</h4>"
        f"<p>{_text(tr('u1.chain.22') if source else tr('u1.chain.23'))}</p>"
        f'<pre style="white-space:pre-wrap">{_text(source or tr("u1.chain.24"))}</pre></section>'
        for index, (plan_id, source) in enumerate(sources.items(), 1)
    )


def consistency_html(rows: list[dict[str, Any]], *, key: str = "birthday") -> str:
    """Escape every evidence value; five columns keep judgments next to their source."""
    body = []
    numbers = {row["node_id"]: f"{index}. {row['label']}" for index, row in enumerate(rows, 1)}
    for index, row in enumerate(rows, 1):
        source_anchor = _source_anchor(row["plan_id"], key)
        changed = [item["component"] for item in row["dependencies"] if item["changed"]]
        evidence = (
            tr("u1.chain.14")
            if not any(item["approved"] is not None for item in row["dependencies"])
            else tr("u1.chain.post_dispatch") + "、".join(changed)
            if changed and row.get("post_dispatch")
            else tr("u1.chain.45") + "、".join(changed)
            if changed
            else tr("u1.chain.29")
        )
        hashes = "".join(
            f"<tr><td>{_text(item['component'])}</td>"
            f"<td>{_text(item['approved'] or tr('u1.chain.59'))}</td>"
            f"<td>{_text(item['current'] or tr('u1.chain.61'))}</td>"
            f"<td>{_change_label(item['changed'])}</td></tr>"
            for item in row["dependencies"]
        )
        authorization = row["authorization"]
        token = authorization["token"]
        token_text = (
            tr("u1.chain.15")
            if token is None
            else f"Token：{token['issued_at']} → {token['expiry']}；"
            + (tr("u1.chain.56") if token["consumed"] else tr("u1.chain.57"))
            + "；"
            + (tr("u1.chain.46") if token["within_time"] else tr("u1.chain.47"))
        )
        spa = authorization["spa"]
        spa_text = f"SPA：{spa['start']} → {spa['end']}" if spa else tr("u1.chain.16")
        mfa_text = (
            tr("u1.chain.17")
            if not authorization["mfa"]
            else "MFA grant：" + json.dumps(authorization["mfa"], ensure_ascii=False)
        )
        actual = row["actual"]
        reference = row["reference"]
        reference_text = {
            "holds": tr("u1.chain.30"),
            "changed": tr("u1.chain.31"),
            "not_evaluated": tr("u1.chain.32"),
        }[reference["status"]]
        if reference.get("at_dispatch"):
            reference_text = tr("u1.chain.at_dispatch") + reference_text
        goal_text = tr("u1.chain.18") if row["signed_goal"] else tr("u1.chain.19")
        budget = authorization["budget"]
        void_text = tr("u1.chain.52") if authorization["voided"] else tr("u1.chain.53")
        blocked_text = tr("u1.chain.52") if authorization["blocked"] else tr("u1.chain.54")
        kernel_text = (
            display_names.outcome_label(actual["kernel"]) if actual["kernel"] else tr("u1.chain.59")
        )
        proposal = row.get("proposal")
        proposal_text = (
            f"<br>{_text(tr('u1.chain.proposal_check'))}: "
            f"{_text(display_names.outcome_label(proposal['outcome']))}"
            f"<br>{_text(', '.join(proposal['rule_ids']))}"
            if proposal and actual["status"] == "not_dispatched"
            else ""
        )
        dependencies_text = "、".join(
            numbers.get(ref, tr("u1.chain.59")) for ref in row["depends_on"]
        ) or tr("u1.chain.58")
        downstream_text = "、".join(
            numbers.get(ref, tr("u1.chain.59")) for ref in row["downstream"]
        ) or tr("u1.chain.58")
        body.append(
            f"<tr><td><strong>{index}. {_text(row['label'])}</strong><br>{goal_text}"
            f'<br><a href="#{source_anchor}{tr("u1.chain.33")}'
            f"{_json(row['signed_goal'])}</pre></details></td>"
            f"<td>{_text(evidence)}{tr('u1.chain.34')}{hashes}</tbody></table></details></td>"
            f"<td>{_text(token_text)}<br>{_text(spa_text)}<br>{_text(mfa_text)}"
            f"{tr('u1.chain.35')}{void_text}{tr('u1.chain.36')}{blocked_text}"
            f"{tr('u1.chain.37')}{budget['used']}{tr('u1.chain.38')}{budget['remaining']}</td>"
            f"<td>{tr('u1.chain.65')}"
            f"{_text(dependencies_text)}"
            f"{tr('u1.chain.39')}"
            f"{_text(downstream_text)}"
            f"{tr('u1.chain.40')}{_text(_status(actual['status']))}{tr('u1.chain.41')}"
            f"{_text(kernel_text)}"
            f"<br>{_text(', '.join(actual['rule_ids']))}{tr('u1.chain.42')}"
            f"{_text(_status(actual['runtime']) if actual['runtime'] else tr('u1.chain.60'))}"
            f"{proposal_text}"
            f"<hr><strong>{tr(REFERENCE_LABEL)}{tr('u1.chain.43')}{reference_text}"
            f"<br>{_text(', '.join(reference['changed']))}</td></tr>"
        )
    return tr("u1.chain.20") + "".join(body) + "</tbody></table>" + _sources_html(rows, key)


def _unsigned_draft(rows: list[dict[str, Any]]) -> bool:
    """Every row is still an unauthorized preview: no token and nothing dispatched."""
    return all(
        row["authorization"].get("token") is None
        and row["actual"].get("status") == "not_dispatched"
        for row in rows
    )


def render_chain_consistency(
    controller: Any, draft: dict | None = None, *, source_text: str | None = None
) -> None:
    if not view_mode.is_judge():
        return
    st.subheader(tr("u1.chain.01"))
    st.caption(tr("u1.chain.02"))
    rows = chain_rows(controller, draft, source_text=source_text)
    if not rows:
        st.info(tr("u1.chain.21"))
        return
    if _unsigned_draft(rows):
        # An unsigned draft has no tokens, schedules or receipts yet; say so first.
        st.info(tr("polish.chain.unsigned_notice"))
        with st.expander(tr("polish.chain.unsigned_table")):
            st.markdown(consistency_html(rows), unsafe_allow_html=True)
    else:
        st.markdown(consistency_html(rows), unsafe_allow_html=True)
    st.caption(tr("u1.chain.03"))


def render_approval_budget(controller: Any, *, required: int = 4, key: str = "birthday") -> None:
    if not view_mode.is_judge():
        return
    planned = len((st.session_state.get(f"{key}_draft") or {}).get("nodes", []))
    # Unselected items still need their own approval: count the plan, never "0 needed".
    needed = max(required, planned) if planned else required
    budget = approval_preflight(controller, required=needed)
    st.subheader(tr("u1.chain.04"))
    columns = st.columns(4)
    columns[0].metric(
        tr("u1.chain.05"),
        f"{budget['window_seconds']}{tr('u1.chain.25')}{budget['limit']}{tr('u1.chain.26')}",
    )
    columns[1].metric(tr("u1.chain.06"), budget["used"])
    columns[2].metric(tr("u1.chain.07"), budget["remaining"])
    if planned:
        columns[3].metric(
            tr("polish.chain.required_label"),
            tr("polish.chain.required_value", total=planned, selected=required),
        )
    elif required == 0:
        columns[3].metric(tr("polish.chain.required_label"), "—")
    else:
        columns[3].metric(tr("u1.chain.08"), budget["required"])
    st.caption(tr("u1.chain.09"))
    if budget["locked"]:
        st.warning(tr("u1.chain.27"))
    elif not budget["can_start"]:
        st.warning(
            f"{tr('u1.chain.48')}{needed}{tr('u1.chain.49')}{budget['remaining']}{tr('u1.chain.50')}{budget['next_release_at']}{tr('u1.chain.51')}"
        )
    elif required == 0:
        # Nothing is selected yet: never claim the chain can start.
        if not planned:
            st.caption(tr("polish.chain.no_draft"))
    else:
        st.success(tr("u1.chain.44"))
    if st.button(tr("u1.chain.10"), key=key + "_advance_600"):
        controller.advance_clock(600)
        st.rerun()
    st.caption(tr("u1.chain.11"))
