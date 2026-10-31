"""Receipt-backed business views using the unchanged gateway interfaces."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone

import streamlit as st

# The AA preparer and the transaction-history query are shared verbatim with chat through
# this Streamlit-free module.
from corridor.ui import display_names, operations, view_mode
from corridor.ui.components import money, reference_label, render_card, render_result
from corridor.ui.controller import _Builder
from corridor.ui.messages import UI_MESSAGES, source_utterance, tr
from corridor.ui.operations import _state_term, preview_aa_collection


def beijing_time(value):
    """Format a trusted UTC timestamp for display, without consulting the wall clock."""
    if not value:
        return "—"
    return (
        datetime
        .fromisoformat(value.replace("Z", "+00:00"))
        .astimezone(timezone(timedelta(hours=8)))
        .strftime("%Y-%m-%d %H:%M:%S")
    )


def remark_notice(utterance):
    """Clarify unsupported remittance notes without altering the user request."""
    if any(
        source_utterance(key) in utterance
        for key in ("Contact remark keyword", "Remittance note keyword")
    ):
        return tr(
            "Contact remarks identify the recipient; "
            "a remittance message is not sent with the transfer."
        )
    return None


def subscription_projection(result):
    """Read only fields from an approved analysis receipt, including reminder eligibility."""
    receipt = result.get("analysis_receipt") or {}
    if result.get("status") != "settled" or not receipt:
        return {"rows": [], "renewals": []}
    fields = receipt.get("results", {})
    prefixes = sorted({key.rsplit(".", 1)[0] for key in fields if key.endswith(".merchant_ref")})
    rows = []
    for prefix in prefixes:
        row = {
            name: deepcopy(fields.get(f"{prefix}.{name}", {}).get("value"))
            for name in (
                "merchant_ref",
                "account_ref",
                "amount",
                "cancellable",
                "authorization_ref",
            )
        }
        row["next_due"] = fields.get(f"next_due.{row['authorization_ref']}", {}).get("value")
        row["source_tx_ids"] = deepcopy(fields[f"{prefix}.merchant_ref"].get("source_tx_ids", []))
        rows.append(row)
    renewals = [
        {"authorization_ref": ref, "next_due": fields.get(f"next_due.{ref}", {}).get("value")}
        for ref in fields.get("reminder_authorizations", {}).get("value", [])
    ]
    return {"rows": rows, "renewals": renewals}


def render_subscription_result(result, controller):
    view = subscription_projection(result)
    if result.get("status") != "settled":
        render_result(result)
        return
    st.subheader(tr("Subscriptions and recurring charges"))
    receipt = result.get("analysis_receipt") or {}
    judge = view_mode.is_judge()
    computed = str(receipt.get("computed_at", ""))
    st.caption(
        tr("Approved query snapshot: ")
        + (computed if judge else f"{beijing_time(computed)} · " + tr("Beijing time"))
    )
    merchants = {
        row["authorization_ref"]: tr(reference_label(row["merchant_ref"], controller=controller))
        for row in view["rows"]
        if row["authorization_ref"]
    }
    for renewal in view["renewals"]:
        # The customer view names the merchant instead of the raw authorization reference.
        name = (
            renewal["authorization_ref"]
            if judge
            else merchants.get(renewal["authorization_ref"], renewal["authorization_ref"])
        )
        st.info(
            tr("Upcoming renewal · ")
            + f"{name} · {beijing_time(renewal['next_due'])} · "
            + tr("Beijing time")
        )
    if view["rows"] and not judge:
        # The 420 px customer chat column clips a five-column grid; one wrapped line per row.
        for row in view["rows"]:
            facts = {
                "merchant": tr(reference_label(row["merchant_ref"], controller=controller)),
                "amount": money(row["amount"]),
                "cancellable": tr("Yes") if row["cancellable"] else tr("No"),
            }
            # S4: no upcoming charge reads as a sentence, not "—（北京时间）".
            st.markdown(
                tr("polish.subscription.row", **facts, next_due=beijing_time(row["next_due"]))
                if row["next_due"]
                else tr("polish.subscription.row_no_next", **facts)
            )
    elif view["rows"]:
        st.dataframe(
            [
                {
                    tr("Merchant"): tr(reference_label(row["merchant_ref"], controller=controller)),
                    tr("Amount"): money(row["amount"]),
                    tr("Authorization"): row["authorization_ref"]
                    or tr("Detected recurring charge only"),
                    tr("Cancellation available"): tr("Yes") if row["cancellable"] else tr("No"),
                    tr("Next charge (Beijing time)"): beijing_time(row["next_due"]),
                    # Source transaction identifiers are judge-view evidence.
                    **(
                        {tr("Source transactions"): ", ".join(row["source_tx_ids"])}
                        if judge
                        else {}
                    ),
                }
                for row in view["rows"]
            ],
            hide_index=True,
            use_container_width=True,
        )
    else:
        st.info(tr("No recurring charges were identified in this query."))
    st.caption(
        tr(
            "Renewals use this analysis receipt's seven-day reminder field. "
            "Cancellation requires a separate approval."
        )
    )


def query_transactions(controller):
    # Moved verbatim to the Streamlit-free operations module so chat reaches the same query.
    return operations.query_transactions(controller)


def transaction_projection(result):
    receipt = result.get("receipt") or {}
    if result.get("status") != "settled" or receipt.get("kind") != "transactions_query":
        return []
    return [
        deepcopy(row)
        for effect in receipt.get("exact_effects", [])
        if effect.get("field") == "transactions_query" and isinstance(effect.get("after"), list)
        for row in effect["after"]
    ]


def render_transaction_query(controller):
    st.caption(tr("Transaction history · green, no second confirmation"))
    if st.button(tr("Query transaction history"), key="query_transactions"):
        try:
            st.session_state["transaction_query_result"] = query_transactions(controller)
        except (KeyError, ValueError, RuntimeError):
            st.error(tr("The query did not complete. Enter the simulated bank first."))
    result = st.session_state.get("transaction_query_result")
    if result:
        if result["status"] != "settled":
            render_result(result)
            return
        render_transaction_rows(result, controller=controller)


def _transaction_row(row, controller, judge):
    counterparty = row.get("merchant_ref") or row.get("contact_ref")
    shown = {
        tr("Transaction reference"): row["id"],
        # Customer view: Beijing time and synthetic counterparty names (headers overridden).
        tr("Time (UTC)"): display_names.time_text(row["booked_at"]),
        tr("Direction"): tr("Debit") if row["direction"] == "debit" else tr("Credit"),
        tr("Amount"): money(row["amount"]),
        tr("Category"): tr("u1.category." + row["category_code"])
        if "u1.category." + row["category_code"] in UI_MESSAGES
        else row["category_code"],
        tr("Counterparty reference"): (
            counterparty if judge else reference_label(counterparty, controller=controller)
        )
        if counterparty
        else "—",
    }
    if not judge:
        shown.pop(tr("Transaction reference"))
    return shown


def render_transaction_rows(result, *, controller=None):
    """The verified query receipt's rows; the billing page and chat share this table."""
    rows = transaction_projection(result)
    judge = view_mode.is_judge()
    st.dataframe(
        [_transaction_row(row, controller, judge) for row in rows],
        hide_index=True,
        use_container_width=True,
    )
    st.caption(tr("Rows come only from the verified bank query receipt."))


def query_income(controller, position_ref):
    controller._require_login()
    builder = _Builder(controller, controller._id("income-query"))
    prepared = builder.prepare(
        "investment_income_query",
        {"position_ref": position_ref},
        {"position_ref": _state_term(controller, "positions", position_ref)},
    )
    return controller._result(controller.gateway.query(prepared.action, prepared.cert))


def render_income_query(controller, position_ref):
    if st.button(tr("Query investment income"), key="query_income"):
        try:
            st.session_state["income_query_result"] = {
                "position": position_ref,
                "result": query_income(controller, position_ref),
            }
        except (KeyError, ValueError, RuntimeError):
            st.error(tr("The income query did not complete. Check the current position."))
    stored = st.session_state.get("income_query_result")
    if not stored or stored["position"] != position_ref:
        return
    result = stored["result"]
    if result["status"] != "settled":
        render_result(result)
        return
    fields = result.get("analysis_receipt", {}).get("results", {})
    st.caption(tr("Green query · income from the signed analysis receipt"))
    st.dataframe(
        [
            {tr("Field"): label, tr("Amount"): money(fields[field]["value"])}
            for field, label in (
                ("realized_income", tr("Realized income")),
                (
                    "projected_income",
                    tr("Projected income (not guaranteed)"),
                ),
                ("principal_amount", tr("Position principal")),
            )
            if field in fields
        ],
        hide_index=True,
        use_container_width=True,
    )


def timeline_projection(records, history):
    """Keep audit order and session observations in separate, detached projections."""
    execution = []
    for record in records:
        payload = record.get("payload", {})
        execution.append({
            "seq": record["seq"],
            "ts": record["ts"],
            "event": record["event"],
            "node_id": record.get("node_id") or "—",
            "status": payload.get("status")
            or payload.get("outcome")
            or payload.get("decision")
            or payload.get("verdict")
            or "—",
            "rule_ids": deepcopy(payload.get("rule_ids", [])),
            "record_hash": record["record_hash"],
        })
    return {"execution": execution, "intent": deepcopy(history)}


def render_execution_timeline(controller, session=None):
    history = session.history if session is not None and session.controller is controller else []
    view = timeline_projection(controller.gateway.audit.records(), history)
    left, right = st.columns(2)
    with left:
        st.subheader(tr("Execution timeline"))
        st.caption(
            tr("Read-only projection in hash-chain sequence; timestamps use the simulated clock.")
        )
        st.dataframe(
            [
                {
                    tr("u1.timeline.seq"): row["seq"],
                    tr("u1.timeline.ts"): row["ts"],
                    # Plain words; the judge view keeps the raw event name and outcome.
                    tr("u1.timeline.event"): display_names.event_label(row["event"]),
                    tr("u1.timeline.status"): display_names.outcome_label(row["status"]),
                }
                for row in view["execution"]
            ],
            hide_index=True,
            use_container_width=True,
        )
    with right:
        st.subheader(tr("Session intent log"))
        st.info(
            tr(
                "Intent routing is outside the hash chain. "
                "These session observations are not execution or approval evidence."
            )
        )
        if view["intent"]:
            # Localized headers; identifiers are shortened so the half-width table is not
            # clipped (the full values stay in the session snapshot evidence).
            st.dataframe(
                [
                    {
                        tr("polish.intentlog.intent"): row.get("intent"),
                        tr("polish.intentlog.status"): row.get("status"),
                        tr("polish.intentlog.response"): str(row.get("response_id") or "")[:8],
                        tr("polish.intentlog.utterance"): str(row.get("utterance_hash") or "")[:12],
                    }
                    for row in view["intent"]
                ],
                hide_index=True,
                use_container_width=True,
            )
        else:
            st.caption(tr("No intent observations in this session yet."))


def render_aa_collection(controller):
    with st.expander(tr("Split-bill collection (simulated)")):
        st.caption(
            tr(
                "Create collection requests. No funds are debited "
                "and no external messages are sent."
            )
        )
        amount = st.text_input(tr("Total amount (CNY)"), value="100", key="aa_total")
        participants = st.multiselect(
            tr("Choose participants"),
            [
                ref
                for ref, item in controller.state.contacts.items()
                if item.owner == controller.device.principal
            ],
            format_func=lambda ref: reference_label(ref, controller=controller),
            placeholder=tr("u1.aa.placeholder"),
            key="aa_participants",
        )
        if st.button(tr("Preview collection requests"), key="aa_preview"):
            try:
                st.session_state["aa_pending"] = preview_aa_collection(
                    controller, amount, participants
                )
                st.session_state.pop("aa_result", None)
            except (ValueError, KeyError, RuntimeError):
                st.error(tr("Choose contacts and a positive whole-cent total."))
        pending = st.session_state.get("aa_pending")
        if pending and not st.session_state.get("aa_result"):
            render_card(pending.get("card", {}), controller=controller)
            if st.button(
                tr("Confirm simulated collection requests"),
                key="aa_confirm",
            ):
                st.session_state["aa_result"] = controller.approve_pending(pending["pending_id"])
        result = st.session_state.get("aa_result")
        if result and result.get("status") == "settled":
            fields = result["analysis_receipt"]["results"]
            st.success(tr("Simulated collection requests created; no debit occurred."))
            st.dataframe(
                [
                    {
                        tr("Participant"): reference_label(
                            field.split(".", 1)[1], controller=controller
                        ),
                        tr("Share"): money(value["value"]),
                    }
                    for field, value in fields.items()
                    if field.startswith("shares.")
                ],
                hide_index=True,
                use_container_width=True,
            )
            st.caption(tr("Request references: ") + ", ".join(fields["request_ids"]["value"]))
        elif result:
            render_result(result)
