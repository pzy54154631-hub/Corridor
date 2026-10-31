"""Bilingual wealth pages backed by signed goals and gateway results."""

from fractions import Fraction

import streamlit as st

from corridor.agent.llm import ReplayCorrupt, ReplayMiss
from corridor.crypto.jcs import H, jcs_bytes
from corridor.gateway.output_guard import project_allocation_fact
from corridor.schemas.money import display_amount
from corridor.ui import display_names, view_mode
from corridor.ui.components import (
    STATUSES,
    localized_facts,
    money,
    open_audit,
    reference_label,
    render_card,
    render_result,
    safe_text,
)
from corridor.ui.evaluation import ATTACK_CLASSES, run_attack_class
from corridor.ui.messages import display_value, prose, tr
from corridor.ui.wealth_statement import render_biac

PROFILE_NAMES = {1: "Conservative", 2: "Cautious", 3: "Balanced", 4: "Growth", 5: "Aggressive"}
ANSWER_OPTIONS = {
    "Investment experience": (
        "None",
        "Savings only",
        "Simple funds",
        "Several product types",
        "Complex products",
    ),
    "Loss tolerance": (
        "None",
        "A small loss",
        "A moderate loss",
        "A substantial loss",
        "A large loss",
    ),
    "Income stability": ("No stable income", "Variable", "Mostly stable", "Stable", "Very stable"),
    "Liquidity needs": (
        "Immediate access",
        "Frequent access",
        "Occasional access",
        "Rare access",
        "No near-term need",
    ),
    "Investment horizon": ("Immediate", "Short term", "Medium term", "Long term", "Very long term"),
}


def device_action(controller, method, arguments, result_key):
    try:
        st.session_state[result_key] = getattr(controller, method)(*arguments)
    except (ValueError, RuntimeError, KeyError, OSError):
        st.session_state[result_key] = {
            "status": "rejected",
            "message": tr("The operation was not completed. Review current facts."),
        }


def risk_questionnaire(controller):
    st.subheader(tr("Device risk questionnaire"))
    st.caption(tr("Your answers stay on the simulated device and are never sent to the assistant."))
    profile = controller.risk_profile()
    if profile:
        level = profile["level"]
        st.info(
            f"{tr('Signed profile: C')}"
            f"{level}"
            " · "
            f"{display_value(PROFILE_NAMES[level])}"
            f"{tr(' · valid until ')}"
            f"{display_names.time_text(profile['expires_at'])}"
        )
        suitability_status(controller)
        if profile["expires_at"] <= controller.state.now:
            st.warning(
                f"{tr('Your profile expired on ')}"
                f"{display_names.time_text(profile['expires_at'])}"
                f"{tr('; please retake. BIC-RISK')}"
            )
    else:
        st.info(
            tr("Complete the questionnaire before a suitability comparison can be constructed.")
        )
    st.caption(tr("A synthetic questionnaire, not a professional suitability assessment."))
    st.caption(tr("Only device-signed questionnaire answers set your level; chat cannot raise it."))
    questions = controller.state.policy.risk_questions
    if any(question not in ANSWER_OPTIONS for question in questions):
        st.error(tr("The current questionnaire needs an approved device presentation."))
        return
    with st.form("wealth_risk_form"):
        answers = [
            st.selectbox(
                tr(question),
                [None, *range(5)],
                format_func=lambda value, options=options: (
                    tr("Choose an answer") if value is None else options[value]
                ),
                key=f"wealth_risk_{index}",
                placeholder=tr("Choose an answer"),
            )
            for index, question in enumerate(questions)
            for options in [display_value(ANSWER_OPTIONS[question])]
        ]
        submitted = st.form_submit_button(tr("Sign answers on device"), key="wealth_sign_risk")
    if submitted:
        if None in answers:
            st.warning(tr("Answer all five questions yourself before signing."))
        else:
            result = controller.submit_risk_answers(answers)
            st.session_state["wealth_risk_result"] = result
            if result["status"] == "settled":
                st.rerun()
    render_result(st.session_state.get("wealth_risk_result"))


def suitability_status(controller):
    facts = controller.suitability_details
    if not facts or facts.get("effective_level") is None:
        return
    level = facts["effective_level"]
    message = f"{tr('Effective risk level: C')}{level} · {display_value(PROFILE_NAMES[level])}"
    (st.warning if facts.get("clarification") else st.info)(message)
    if facts.get("clarification"):
        st.caption(tr(facts["clarification"]))
    st.caption(tr("Allowed product levels: ") + ", ".join(f"R{n}" for n in range(1, level + 1)))


def dialogue_response(response, controller):
    if not response:
        return
    if response.get("guided"):
        st.warning(tr("Guided mode (assistant unavailable). Signed goals are retained for review."))
    st.info(prose(response["message"]))
    for name, slot in response.get("slots", {}).items():
        if name in {"amount", "liquidity_amount"}:
            value = money(slot["value"])
        elif name == "source_account_ref":
            value = reference_label(slot["value"], controller=controller)
        else:
            value = safe_text(slot["value"])
        st.caption(f"{tr('slot.' + name)}: {value} · {tr(slot['source'])}")
    sources = []
    for fact in localized_facts(response.get("facts", []), controller):
        st.write(fact["text"])
        sources.append({"receipt_ref": fact["receipt_ref"], "field": fact["field"]})
    for fact in response.get("projected_facts", []):
        current = project_allocation_fact(
            fact["source_ref"],
            fact["path"],
            controller.state,
            controller.device.principal,
            controller.gateway.vault,
        )
        st.write(current.text)
        sources.append({"receipt_ref": fact["source_ref"].receipt_ref, "path": fact["path"]})
    if (response.get("slots") or sources) and view_mode.is_judge():
        with st.expander(tr("Technical evidence"), expanded=False):
            st.json({"slots": response.get("slots", {}), "fact_sources": sources})
    if response.get("assistant_note"):
        st.caption(tr("Assistant · unverified"))
        st.text(response["assistant_note"])
    action = response.get("device_action")
    routes = {
        "confirm_goals": tr("Open Device to review and sign your goals."),
        "risk_questionnaire": tr("Open Device to complete or clarify your own risk questionnaire."),
        "select_allocation_items": tr("Open Proposal & Certificate to select the allocation legs."),
        "lookup": tr("Open Device and use Lookup. Do not repeat the purchase."),
        "review_goals": tr("Open Device to review your goals before signing a correction."),
        "edit_allocation": (
            tr("Review the certificate and ask an advisor about a new signed adjustment.")
        ),
        "advisor": tr("Advisor review requested. Remaining work is stopped."),
    }
    if action in routes:
        st.caption(routes[action])


def advisor(controller):
    st.header(tr("Wealth Advisor"))
    st.write(tr("Compare the declared catalogue against your signed goals and risk profile."))
    st.caption(tr("polish.wealth.evidence_mode"))
    with st.form("wealth_advisor"):
        text = st.text_input(
            tr("What would you like to achieve?"),
            placeholder=tr("I want something safe"),
            key="wealth_message",
        )
        submitted = st.form_submit_button(tr("Discuss my goals"), key="wealth_discuss")
    if submitted and text.strip():
        try:
            st.session_state["wealth_dialogue"] = controller.receive_goal(
                text, request_id=controller._id("wealth-message")
            )
        except (ReplayMiss, ReplayCorrupt):
            st.error(
                tr(
                    "Strict replay failed. The recording is missing or invalid; no mod"
                    "el fallback ran."
                )
            )
        except (ValueError, RuntimeError):
            st.error(tr("The discussion could not be completed. Review your goals on Device."))
    response = controller.dialogue_result
    dialogue_response(response, controller)
    suitability_status(controller)
    if controller.comparison is not None:
        st.success(tr("Goals signed on device. Your current comparison is ready for review."))
        st.info(
            tr(
                "Open Proposal & Certificate to review your allocation and its sta"
                "tus. Open Device for any remaining approvals."
            )
        )
    else:
        if response:
            st.caption(
                tr("Pending signed goals: ")
                + ", ".join(tr("slot." + name) for name in response["pending_slots"])
            )
        st.info(tr("Open Device to complete your questionnaire and confirm goals."))
    st.subheader(tr("Try to break it"))
    st.caption(tr("polish.wealth.attack_scope"))
    columns = st.columns(2)
    for index, (kind, spec) in enumerate(ATTACK_CLASSES.items()):
        if columns[index % 2].button(
            tr(spec["label"]), key=f"wealth_attack:{kind}", use_container_width=True
        ):
            with st.spinner(tr("Running the actual attack and benign twin...")):
                st.session_state["wealth_attack_result"] = run_attack_class(kind)
    result = st.session_state.get("wealth_attack_result")
    if result:
        if result["attack"].get("attacker_induced_lockout"):
            st.warning(tr("Contained by lockout (legitimate task refused)"))
            st.write(tr("Availability cost: account locked and legitimate task refused."))
        else:
            (st.success if result["passed"] else st.error)(
                tr("Blocked · ") + result["violation_summary"]
                if result["passed"]
                else tr("Security check failed")
            )
        st.write(tr("Attempt: ") + tr(result["attempt"]))
        st.write(tr(result["reason"]))
        if view_mode.is_judge():
            st.caption(tr("Observed rules: ") + " · ".join(result["rule_ids"]))
        st.write(f"{tr('Funds moved: CNY ')}{display_amount(Fraction(result['funds_moved_cny']))}")
        st.caption(
            f"{tr('Benign twin completed: ')}"
            f"{result['benign_completed']}"
            f"{tr(' · evidence: ')}"
            f"{result['evidence_mode']}"
        )
        if view_mode.is_judge():
            with st.expander(tr("Attack audit records")):
                st.json(result["audit_entries"])
                st.json(result["audit_checkpoint"])
        st.download_button(
            tr("Download attack audit"),
            data=result["audit_jsonl"],
            file_name=f"{result['kind']}-audit.jsonl",
            mime="application/x-ndjson",
            key="wealth_attack_audit",
        )


def goal_sheet(controller):
    st.subheader(tr("Confirm goals on device"))
    if controller.candidate is None:
        st.caption(tr("Start a goal discussion in Advisor. No amount or risk level is inferred."))
        return
    st.caption(
        tr(
            "Draft · not signed. Principal and purchase fees are separate. Synthetic "
            "data · simulated device."
        )
    )
    slots = {name: slot.value for name, slot in controller.wealth_chat.slots.items()}
    defaults = {
        "wealth_source": slots.get("source_account_ref", "A_CURRENT"),
        "wealth_amount": display_amount(Fraction(slots["amount"])) if "amount" in slots else "",
        "wealth_horizon": slots.get("horizon_days", 365),
        "wealth_days": slots.get("liquidity_days", 3),
        "wealth_liquid": display_amount(Fraction(slots["liquidity_amount"]))
        if "liquidity_amount" in slots
        else "",
        "wealth_types": slots.get("allowed_types", ["wealth"]),
        "wealth_objective": slots.get("objective", "net_return"),
    }
    fingerprint = H({"intent": controller.wealth_chat.intent_id, "slots": slots})
    changed = st.session_state.get("wealth_goal_defaults") != fingerprint
    for key, value in defaults.items():
        if changed or key not in st.session_state:
            st.session_state[key] = value
    st.session_state["wealth_goal_defaults"] = fingerprint
    with st.form("wealth_goals"):
        source = st.selectbox(
            tr("Source account"),
            ["A_CURRENT"],
            key="wealth_source",
            format_func=lambda value: reference_label(value, controller=controller),
        )
        amount = st.text_input(tr("New principal (CNY)"), key="wealth_amount")
        horizon = st.number_input(tr("Investment horizon (days)"), 1, 365, key="wealth_horizon")
        days = st.number_input(tr("Access needed within (days)"), 0, 365, key="wealth_days")
        liquid = st.text_input(tr("Minimum accessible net principal (CNY)"), key="wealth_liquid")
        types = st.multiselect(
            tr("Allowed product types"), ["wealth", "deposit"], format_func=tr, key="wealth_types"
        )
        objective = st.selectbox(
            tr("Ranking objective"),
            ["net_return", "total_fee", "liquidity"],
            format_func=lambda value: {
                "net_return": tr("Performance benchmark (not guaranteed)"),
                "total_fee": tr("Lowest total fees"),
                "liquidity": tr("Earliest access"),
            }[value],
            key="wealth_objective",
        )
        confirmed = st.form_submit_button(tr("Confirm goals"), key="wealth_confirm_goals")
    if confirmed:
        try:
            result = controller.confirm_goals({
                "source_account_ref": source,
                "amount": amount,
                "horizon_days": horizon,
                "liquidity_days": days,
                "liquidity_amount": liquid,
                "allowed_types": types,
                "objective": objective,
            })
            st.session_state["wealth_comparison"] = result
        except (ValueError, RuntimeError):
            st.session_state["wealth_comparison"] = {
                "status": "rejected",
                "message": tr(
                    "No allocation was constructed. Review your goals, profile and policy."
                ),
            }
        render_result(st.session_state["wealth_comparison"])
    if controller.signed_goals:
        st.success(tr("Goals signed on device. Comparison does not authorize a purchase."))


def execution_summary(execution):
    if execution["status"] == "unknown":
        st.warning(tr("Outcome pending reconciliation - do not retry. Use Lookup on Device."))
    elif execution["status"] == "partially_executed":
        st.warning(tr("Partially executed. Settled holdings remain; remaining work is stopped."))
    elif execution["status"] == "completed":
        st.success(tr("Completed. Every purchase has a verified bank receipt."))
    elif execution["status"] in {"cancelled", "failed"}:
        st.info(tr("Remaining execution stopped. Review the leg status and receipts below."))
    st.caption(
        tr(
            "Settled holdings and remaining legs · gateway execution facts · "
            "synthetic data · simulated bank"
        )
    )
    st.dataframe(
        [
            {
                tr("Product"): reference_label(
                    item["product_ref"], controller=st.session_state.get("wealth_controller")
                ),
                tr("Principal (CNY)"): display_amount(Fraction(item["principal"])),
                tr("Purchase fee (CNY)"): display_amount(Fraction(item["fee_buy"])),
                tr("Status"): tr(STATUSES.get(item["status"], item["status"])),
                tr("Receipt"): tr("Verified receipt")
                if item["receipt_ref"]
                else tr("No settlement receipt"),
            }
            for item in execution["items"]
        ],
        hide_index=True,
    )
    st.write(
        tr("Available cash (CNY): ") + display_amount(Fraction(execution["available_balance"]))
    )
    if view_mode.is_judge():
        with st.expander(tr("Technical evidence"), expanded=False):
            st.json(execution)


def correction_summary(controller):
    """Display only differences authenticated by the existing receipt fact gate."""
    if not controller.wealth_chat or controller.wealth_chat.previous_comparison is None:
        return
    try:
        changes = controller.wealth_chat.change_explanation()
    except ValueError:
        st.warning(tr("The previous comparison could not be verified for a change summary."))
        return
    changed = [fact for fact in changes if Fraction(fact.exact_value)]
    if not changed:
        return
    st.subheader(tr("What changed after your signed correction"))
    st.caption(
        tr(
            "Verified comparison receipts · synthetic data · simulated device "
            "signatures. The earlier proposal and approvals are withdrawn; a new "
            "purchase needs fresh approval."
        )
    )
    st.dataframe(
        [
            {
                tr("Product"): reference_label(fact.product_id, controller=controller)
                if fact.product_id
                else tr("Allocation total"),
                tr("Fact"): tr(fact.label),
                tr("Before (CNY)"): display_amount(Fraction(fact.before)),
                tr("After (CNY)"): display_amount(Fraction(fact.after)),
                tr("Change (CNY)"): fact.display_value,
            }
            for fact in changed
        ],
        hide_index=True,
        use_container_width=True,
    )
    with st.expander(tr("Sources for the signed correction")):
        for reference in changes[0].source_refs:
            st.caption(f"{tr('Verified source: ')}{reference.receipt_ref} · {reference.field}")
        st.caption(tr("Performance benchmark (not guaranteed). Not investment advice."))


def proposal(controller):
    st.header(tr("Proposal & Certificate"))
    suitability_status(controller)
    if (
        controller.parent_id is None
        and controller.comparison is not None
        and not controller.comparison_is_current()
    ):
        controller.withdraw_proposal()
    execution = controller.execution_snapshot()
    if controller.comparison is None and execution is not None:
        execution_summary(execution)
        st.caption(tr("This retained execution record cannot authorize another purchase."))
        return
    if controller.comparison is None:
        st.info(
            tr(
                "No allocation was constructed. Confirm complete goals and a valid"
                " profile on Device."
            )
        )
        return
    if execution is None:
        st.success(tr("Comparison complete – no purchase executed"))
    correction_summary(controller)
    request = controller.signed_goals.value
    render_biac(
        controller.comparison.cert.allocation,
        request,
        controller.state.allocation_policies[request["allocation_policy_ref"]],
        controller.risk_profile(),
        execution=execution,
        audit_href="#wealth-audit",
        audit_jsonl=b"".join(jcs_bytes(row) + b"\n" for row in controller.gateway.audit.records()),
        suitability=controller.comparison.result.analysis_receipt.parameters.get("suitability"),
    )
    st.markdown('<div id="wealth-audit"></div>', unsafe_allow_html=True)
    st.button(tr("View wealth audit"), key="wealth_audit", on_click=open_audit)
    if execution is None:
        overview = controller.prepare_allocation()
        st.subheader(tr("Choose every leg before starting"))
        st.caption(tr(overview["notice"]))
        selected = []
        for row in overview["items"]:
            label = (
                reference_label(row["product_ref"], controller=controller)
                + " · "
                + money(row["principal"])
                + tr(" + fee ")
                + money(row["fee_buy"])
            )
            if st.checkbox(label, key=f"wealth_select:{row['item_ref']}", value=False):
                selected.append(row["item_ref"])
        st.button(
            tr("Start selected allocation"),
            key="wealth_start",
            disabled=len(selected) != len(overview["items"]),
            on_click=device_action,
            args=(controller, "start_allocation", (selected,), "wealth_start_result"),
        )
        result = st.session_state.get("wealth_start_result")
        if result and result.get("status") == "approval_budget":
            st.warning(
                f"{tr('Needs ')}"
                f"{result['needed']}"
                f"{tr(' approvals; ')}"
                f"{result['remaining']}"
                f"{tr(' remain. Window clears at ')}"
                f"{result['window_clears_at']}"
                f"{tr('. No purchases submitted.')}"
            )
        elif result:
            render_result(result)
    else:
        st.info(tr("Open Device to review each leg, verify MFA, and approve."))


def allocation_device(controller):
    window = controller.approval_window()
    st.subheader(tr("Allocation approvals"))
    percent = min(100, window["used"] * 100 // window["limit"])
    st.progress(
        percent,
        text=(
            f"{tr('Approval window: ')}"
            f"{window['used']}"
            " / "
            f"{window['limit']}"
            f"{tr(' requests · ')}"
            f"{window['remaining']}"
            f"{tr(' further requests before lock')}"
        ),
    )
    if window["next_release_at"]:
        st.caption(
            f"{tr('Next approval released at ')}"
            f"{window['next_release_at']}"
            f"{tr('; window clears at ')}"
            f"{window['window_clears_at']}"
            "."
        )
    else:
        st.caption(tr("No earlier approvals are expiring in the current window."))
    st.caption(tr("The threshold request triggers the safety lock."))
    if window["locked"]:
        st.warning(tr("Safety lock active. New approvals are unavailable."))
    execution = controller.execution_snapshot()
    if execution is None:
        st.caption(tr("Select all legs in Proposal & Certificate to start."))
        return
    execution_summary(execution)
    st.caption(tr("APR-BUDGET preflight · ") + tr(execution["approval_budget"]["message"]))
    if execution["status"] == "unknown":
        st.warning(tr("Outcome pending reconciliation – do not retry"))
        st.button(
            tr("Lookup pending outcome"),
            key="wealth_lookup",
            on_click=device_action,
            args=(controller, "lookup_pending", (), "wealth_lookup_result"),
        )
    elif execution["status"] == "partially_executed":
        st.warning(
            tr(
                "Partially executed. Settled purchases are retained; nothing rever"
                "sed automatically."
            )
        )
    elif execution["status"] == "completed":
        st.success(tr("Allocation completed. Every purchase has a verified bank receipt."))
    leg = controller.leg
    if leg:
        action = leg["action"]
        item = execution["next_leg"]
        render_card(
            leg["card"], position=item["leg_index"], total=item["leg_count"], controller=controller
        )
        st.caption(tr("Verify MFA → Approve · separate device grant for this leg"))
        identifier = action.action_id
        from corridor.ui.manual_mfa import render_wealth_mfa

        render_wealth_mfa(controller, identifier)
        st.button(
            tr("Approve"),
            key=f"wealth_approve:{identifier}",
            disabled=not leg["verified"],
            type="primary",
            on_click=device_action,
            args=(controller, "approve_leg", (identifier,), "wealth_leg_result"),
        )
    if st.session_state.get("wealth_leg_result"):
        st.caption(tr("Latest device action result · the receipt identifies the approved leg"))
        render_result(st.session_state["wealth_leg_result"])
    if controller.parent_id and execution["status"] not in {"completed", "unknown"}:
        left, right = st.columns(2)
        if left.button(tr("Keep settled purchases and stop"), key="wealth_keep"):
            controller.keep_or_handoff()
            st.rerun()
        if right.button(tr("Ask an advisor"), key="wealth_handoff"):
            controller.keep_or_handoff(handoff=True)
            st.rerun()
    st.caption(
        tr(
            "Stopped or failed plans cannot resume automatically. New work nee"
            "ds newly signed goals."
        )
    )
    if view_mode.is_judge():
        with st.expander(tr("Leg receipts and audit checkpoint")):
            st.json(execution)


def evidence(controller):
    st.markdown('<div id="wealth-audit"></div>', unsafe_allow_html=True)
    st.subheader(tr("Wealth evidence & audit"))
    snapshot = controller.snapshot()
    st.caption(tr("polish.wealth.audit_mode"))
    (st.success if snapshot["audit_valid"] else st.error)(
        tr("Wealth audit chain verified")
        if snapshot["audit_valid"]
        else tr("Wealth audit verification failed")
    )
    st.write(f"{tr('Simulated bank dispatches: ')}{snapshot['dispatch_count']}")
    if view_mode.is_judge():
        with st.expander(tr("Technical evidence"), expanded=False):
            for parent in [
                *controller.history,
                *([controller.parent_id] if controller.parent_id else []),
            ]:
                st.json(controller.execution_snapshot(parent))
            st.json({key: snapshot[key] for key in ("audit", "receipts")})
    st.caption(tr("polish.wealth.w_suite"))
