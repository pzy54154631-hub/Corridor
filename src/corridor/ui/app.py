"Domestic banking entry point with bilingual copy and explicit device gestures."

import os
from collections.abc import Callable
from typing import Any

import streamlit as st

from corridor.bench.adversary import development_cases
from corridor.crypto.jcs import H
from corridor.i18n import get_locale, set_locale
from corridor.sim.keys import stable_id
from corridor.ui import display_names, phone, view_mode, wealth_views
from corridor.ui.business_views import (
    beijing_time,
    render_aa_collection,
    render_execution_timeline,
    render_income_query,
    render_subscription_result,
    render_transaction_query,
    subscription_projection,
)
from corridor.ui.components import (
    KINDS,
    STYLES,
    fact_rows,
    reference_label,
    render_bic,
    render_card,
    render_dag,
    render_plans,
    render_result,
)
from corridor.ui.controller import DemoController, demo_state
from corridor.ui.dag import draft_snapshot
from corridor.ui.domestic_recovery import render_recovery_examples
from corridor.ui.domestic_views import (
    birthday_event_line,
    conversation_home,
    im_home,
    mfa_choices,
    render_scam_facts,
    response_for_action,
    return_to_response,
    scam_gate_decision,
    scam_gate_origin,
    session_for,
    task_controls,
)
from corridor.ui.evaluation import run_hijacked_case, run_judge_task
from corridor.ui.live_controls import (
    live_label,
    live_revoked,
    render_live_controls,
    render_live_failure,
)
from corridor.ui.manual_mfa import render_manual_mfa
from corridor.ui.messages import tr
from corridor.ui.safety import render_safety
from corridor.ui.structured_transfer import render_structured_transfer
from corridor.ui.suitability import exclusion_text
from corridor.ui.wealth_controller import WealthController

PAGES = (
    "conversation",
    "transfers",
    "billing",
    "wealth",
    "cards",
    "subscriptions",
    "birthday",
    "device",
    "dag",
    "operator",
    "safety",
    "im",
    "audit",
)


def request_id(kind: str, payload: Any = None) -> str:
    """A rerun reuses a request; an explicit new draft advances its sequence."""
    key = f"request:{kind}:{H(payload)}"
    if key not in st.session_state:
        sequence = st.session_state.get("request_sequence", 0)
        st.session_state[key] = stable_id("UI-DEMO", sequence, kind)
        st.session_state["request_sequence"] = sequence + 1
    return st.session_state[key]


def perform(call: Callable[[], dict[str, Any]], key: str = "last_result") -> dict[str, Any] | None:
    try:
        result = call()
    except (ValueError, RuntimeError, KeyError):
        st.error(
            tr("The operation could not complete. Review the current facts and inputs, then retry.")
        )
        return None
    st.session_state[key] = result
    return result


def pending_rows(controller: DemoController) -> list[dict[str, Any]]:
    snapshot = controller.snapshot()
    rows = snapshot.get("pending", snapshot.get("pending_actions", []))
    rows = list(rows.values()) if isinstance(rows, dict) else rows
    return [row for row in rows if not row.get("completed")]


def approve_saved(controller: DemoController, pending_key: str, result_key: str) -> None:
    pending = st.session_state[pending_key]
    result = perform(lambda: controller.approve_pending(pending["pending_id"]), result_key)
    if result and result.get("replacement"):
        st.session_state[pending_key] = result["replacement"]


def approve_device(controller: DemoController, identifier: str) -> None:
    result = perform(lambda: controller.approve_pending(identifier), f"approved:{identifier}")
    st.session_state["device_redisplay"] = bool(result and result.get("status") == "redisplay")


def stop_all(controller: DemoController) -> None:
    bank_result = perform(controller.stop, "stop_result")
    if bank_result is not None and "domestic_session" in st.session_state:
        st.session_state["domestic_session"].withdraw_pending()
    wealth = st.session_state.get("wealth_controller")
    if wealth is not None:
        wealth_result = perform(wealth.stop, "wealth_stop_result")
        if wealth_result and wealth_result.get("execution"):
            st.session_state["stop_result"] = {
                "status": "interrupted",
                "message": wealth_result["message"],
                "bank_result": bank_result,
                "wealth_result": wealth_result,
            }
    retired = {
        "transfer_draft",
        "subscription_pending",
        "subscription_cancel",
        "investment_pending",
        "redemption_pending",
        "birthday_draft",
        "birthday_preview",
        "compensation_pending",
        "transfer_changed",
        "card_pending",
        "card_result",
        "structured_transfer_pending",
        "structured_transfer_result",
        "structured_transfer_request",
        "wealth_start_result",
        "wealth_leg_result",
        "wealth_comparison",
        "aa_pending",
        "aa_result",
        "transaction_query_result",
        "income_query_result",
        "password_pending",
    }
    for key in list(st.session_state):
        if key in retired or key.startswith(("request:", "mfa:", "mfa_result:")):
            del st.session_state[key]


def conversation(controller: DemoController) -> None:
    st.markdown(tr("## Describe a transfer, then confirm it on your device"))
    st.caption(
        tr("For example: transfer CNY 200 to Chen. Review the facts before authorizing payment.")
    )
    with st.form("transfer_form"):
        text = st.text_input(
            tr("What would you like to do?"),
            placeholder=tr("Transfer CNY 200 to Chen"),
            key="transfer_text",
        )
        submitted = st.form_submit_button(
            tr("Prepare transfer draft"), key="preview_transfer", type="primary"
        )
    if submitted and text.strip():
        perform(
            lambda: controller.preview_transfer(text, request_id=request_id("transfer", text)),
            "transfer_draft",
        )
        st.session_state.pop("transfer_result", None)
        st.session_state.pop("displayed_contact", None)
    draft = st.session_state.get("transfer_draft")
    if draft:
        contacts = draft.get("contacts", [])
        selection = None
        if draft.get("status") == "clarify" and not contacts:
            st.warning(tr("transfer_no_match"))
        elif draft.get("status") == "clarify" or len(contacts) > 1:
            st.warning(
                tr(
                    "More than one contact matches. Select the recipient on your device "
                    "before authorization."
                )
            )
            options = {
                str(item.get("id", item.get("contact_ref"))): str(item.get("label", item.get("id")))
                for item in contacts
                if isinstance(item, dict)
            }
            if not options:
                options = {str(item): str(item) for item in contacts}
            selection = st.selectbox(
                tr("Select the recipient"),
                [None, *options],
                format_func=lambda value: (
                    tr("Choose a contact") if value is None else options[value]
                ),
                key="contact_selection",
                placeholder=tr("Choose a contact"),
            )
            if st.button(
                tr("Show the selected recipient's canonical card"),
                key="show_selected_contact",
                disabled=selection is None,
            ):
                refreshed = perform(
                    lambda: controller.select_contact(draft["draft_id"], selection),
                    "transfer_draft",
                )
                if refreshed and refreshed.get("card"):
                    st.session_state["displayed_contact"] = selection
                    st.rerun()
        card = draft.get("card", {})
        if card:
            render_card(card, canonical=bool(draft.get("canonical", False)))
        elif draft.get("message"):
            st.info(draft["message"])
        confirmed = st.session_state.get("transfer_result")
        if not confirmed and draft.get("draft_id"):
            can_confirm = bool(card) and (
                len(contacts) <= 1 or selection == st.session_state.get("displayed_contact")
            )
            if st.button(
                tr("Confirm on device and execute"),
                key="confirm_transfer",
                type="primary",
                disabled=not can_confirm,
            ):
                result = perform(
                    lambda: controller.confirm_transfer(
                        draft["draft_id"],
                        request_id=request_id("confirm-transfer", draft["draft_id"]),
                    ),
                    "transfer_result",
                )
                if result and result.get("status") == "redisplay" and result.get("card"):
                    st.session_state["transfer_draft"] = {**draft, **result}
                    st.session_state.pop("transfer_result", None)
                    st.session_state["transfer_changed"] = True
                    st.rerun()
                if result and result.get("status") != "redisplay":
                    st.session_state.pop("transfer_changed", None)
        if st.session_state.get("transfer_changed"):
            st.warning(
                tr("The canonical facts changed. Review the new card before confirming again.")
            )
        render_result(st.session_state.get("transfer_result"))
        if st.session_state.get("transfer_result") and st.button(
            tr("Start another transfer"), key="new_transfer"
        ):
            for key in list(st.session_state):
                if key.startswith("request:transfer:") or key in {
                    "transfer_draft",
                    "transfer_result",
                }:
                    del st.session_state[key]
            st.rerun()
    with st.expander(tr("Assistant note (unverified)"), expanded=False):
        st.write(
            tr(
                "The assistant helps prepare your request. Recipient, amount, approval "
                "tier and outcome come from canonical device facts and verified bank "
                "receipts."
            )
        )


def billing(controller: DemoController) -> None:
    from corridor.ui.insights import render_billing

    render_transaction_query(controller)
    render_billing(controller)
    return


def subscriptions(controller: DemoController) -> None:
    st.markdown(tr("## Subscriptions"))
    st.info(
        tr(
            "Subscription queries and renewal reminders require yellow-tier "
            "approval. Confirm before viewing subscriptions. Cancellation does not "
            "refund settled charges."
        )
    )
    if st.button(
        tr("Prepare subscription query approval"), key="preview_subscriptions", type="primary"
    ):
        perform(
            lambda: controller.preview_subscriptions(request_id=request_id("subscriptions")),
            "subscription_pending",
        )
    pending = st.session_state.get("subscription_pending")
    if pending and pending.get("card"):
        render_card(pending["card"])
        st.button(
            tr("Confirm subscription query on device"),
            key="approve_subscriptions",
            on_click=approve_saved,
            args=(controller, "subscription_pending", "subscription_result"),
        )
    result = st.session_state.get("subscription_result")
    if result:
        # A bare conditional expression here was rendered by Streamlit magic as "None".
        render_subscription_result(result, controller)
    if result and result.get("status") in {"settled", "complete"}:
        refs = result.get("cancellable", [])
        if refs:
            names = {
                row["authorization_ref"]: tr(
                    reference_label(row["merchant_ref"], controller=controller)
                )
                for row in subscription_projection(result)["rows"]
                if row["authorization_ref"]
            }
            # The option value stays the authorization reference; only the label is a name.
            chosen = st.selectbox(
                tr("Choose an authorized subscription"),
                refs,
                key="subscription_ref",
                format_func=lambda ref: ref if view_mode.is_judge() else names.get(ref, ref),
            )
            if st.button(tr("Prepare cancellation approval"), key="cancel_subscription"):
                perform(
                    lambda: controller.cancel_subscription(
                        chosen, request_id=request_id("cancel-subscription", chosen)
                    ),
                    "subscription_cancel",
                )
    cancel = st.session_state.get("subscription_cancel")
    if cancel and cancel.get("card"):
        render_card(cancel["card"])
        st.button(
            tr("Confirm cancellation on device"),
            key="approve_cancel_subscription",
            on_click=approve_saved,
            args=(controller, "subscription_cancel", "subscription_cancel_result"),
        )
    render_result(st.session_state.get("subscription_cancel_result"))


def investments(controller: DemoController) -> None:
    st.markdown(tr("## Product comparison and best-interest certificate"))
    st.caption(
        tr(
            "Synthetic product catalogue · performance benchmark (not guarante"
            "ed), for comparison only; not realized income."
        )
    )
    objective = st.selectbox(
        tr("Choose your comparison objective"),
        ["net_return", "total_fee", "liquidity"],
        format_func=lambda item: {
            "net_return": tr("Net performance benchmark (not guaranteed)"),
            "total_fee": tr("Total fees"),
            "liquidity": tr("Liquidity"),
        }[item],
        key="investment_objective",
    )
    if st.button(
        tr("Confirm objective and compare the full catalogue"),
        key="compare_investments",
        type="primary",
    ):
        perform(lambda: controller.compare_investments(objective), "comparison")
    comparison = st.session_state.get("comparison")

    def product_name(item: Any) -> str:
        return display_names.label(item, controller.state) or str(item)

    if comparison:
        render_result(comparison)
        bic = comparison.get("bic") or {}
        if bic and view_mode.is_judge():
            # Customer view: the purchase card's suitability lines replace the BIC table.
            render_bic(bic)
        excluded = comparison.get("excluded", comparison.get("exclusions"))
        if excluded:
            st.write(tr("Excluded products and reasons"))
            for item in excluded:
                if isinstance(item, dict):
                    reasons = item.get("reason_codes", item.get("reasons", []))
                    st.write(
                        product_name(item.get("product_id", ""))
                        + " · "
                        + " · ".join(exclusion_text(reason) for reason in reasons)
                    )
                elif isinstance(excluded, dict):
                    reasons = excluded[item]
                    reasons = reasons if isinstance(reasons, list) else [reasons]
                    st.write(
                        product_name(item)
                        + " · "
                        + " · ".join(exclusion_text(reason) for reason in reasons)
                    )
            if view_mode.is_judge():
                with st.expander(tr("Binding identifiers and complete canonical JSON")):
                    st.json(excluded)
        products = comparison.get("candidate_ids", bic.get("candidate_ids", []))
        selection = st.selectbox(
            tr("Choose another product explicitly (optional)"),
            [None, *products],
            format_func=lambda item: (
                tr("Use the highest-ranked eligible product")
                if item is None
                else product_name(item)
            ),
            key="explicit_product",
            # Streamlit shows its English "Choose an option" for a None value otherwise.
            placeholder=tr("Use the highest-ranked eligible product"),
        )
        if st.button(tr("Confirm preference and prepare purchase card"), key="preview_investment"):
            perform(
                lambda: controller.preview_investment(
                    selection, request_id=request_id("investment", [objective, selection])
                ),
                "investment_pending",
            )
        if view_mode.is_judge():
            # JUDGING-1 list item 17: the isolated metadata-twin check is evidence for
            # judges, not a customer service.
            with st.expander(tr("Marketing metadata twin (isolated)")):
                st.caption(
                    tr(
                        "Change a sponsor flag and check that the complete catalogue and r"
                        "ecommendation remain sponsor-independent within the declared cata"
                        "logue and policy. This test uses an isolated account."
                    )
                )
                sponsored = st.toggle(
                    tr("Toggle synthetic sponsor flag"), key="sponsored_twin", value=False
                )
                if st.button(tr("Verify unchanged recommendation"), key="run_marketing_twin"):
                    perform(lambda: controller.marketing_twin(sponsored), "marketing_twin")
                twin = st.session_state.get("marketing_twin")
                if twin:
                    (st.success if twin.get("unchanged") else st.error)(
                        tr("Marketing twin: recommendation and complete catalogue are unchanged")
                        if twin.get("unchanged")
                        else tr("Marketing twin verification failed")
                    )
                    render_bic(twin.get("bic", {}))
    pending = st.session_state.get("investment_pending")
    refusal = controller.kernel_refusal(pending.get("pending_id")) if pending else None
    if pending and pending.get("card") and refusal is not None:
        # S4 / R6: a purchase the kernel refused shows the refusal card (the S2 R4 card),
        # not a waiting-for-SMS state; no code is requested and nothing is dispatched.
        from corridor.ui.domestic_demos import render_refusal_card

        render_refusal_card(
            pending["card"],
            controller,
            outcome=refusal["outcome"],
            rule_ids=refusal["rule_ids"],
            execute_count=refusal["dispatches"],
        )
    elif pending and pending.get("card"):
        render_card(pending["card"])
        st.info(
            tr(
                "The purchase has not executed. Open Device to complete simulated "
                "MFA and final approval."
            )
        )
    positions = [item["id"] for item in controller.snapshot().get("positions", [])]
    if positions:
        position = st.selectbox(
            tr("Choose one of your synthetic positions"),
            positions,
            key="redemption_position",
            format_func=lambda value: display_names.position_label(
                controller.state, value, positions.index(value) + 1
            ),
        )
        held = controller.state.positions[position]
        st.caption(
            tr("Position eligible for redemption")
            if held.redeemable_at <= controller.state.now
            else tr("Position still within its minimum holding period")
        )
        st.caption(tr("Redeemable from (Beijing time): ") + beijing_time(held.redeemable_at))
        render_income_query(controller, position)
        if st.button(
            tr("Prepare redemption of an existing synthetic position"), key="preview_redemption"
        ):
            perform(
                lambda: controller.redeem(position, request_id=request_id("redemption", position)),
                "redemption_pending",
            )
    redemption = st.session_state.get("redemption_pending")
    if redemption and redemption.get("card"):
        render_card(redemption["card"])
        st.button(
            tr("Confirm redemption on device"),
            key="approve_redemption",
            on_click=approve_saved,
            args=(controller, "redemption_pending", "redemption_result"),
        )
    render_result(st.session_state.get("redemption_result"))


def cards(controller: DemoController) -> None:
    st.markdown(tr("## Cards"))
    st.caption(
        tr(
            "Synthetic virtual cards display demo tokens. Card operations use "
            "no real card numbers or payment services."
        )
    )
    rows = controller.snapshot().get("cards", [])
    if st.button(
        tr("Apply for a simulated virtual card · show yellow-tier approval"),
        key="preview_apply_card",
    ):
        perform(
            lambda: controller.preview_card_operation(
                "apply_virtual_card",
                request_id=request_id("apply-card", sorted(row["id"] for row in rows)),
            ),
            "card_pending",
        )
        st.session_state.pop("card_result", None)
    if rows:
        by_ref = {row["id"]: row for row in rows}
        ref = st.selectbox(
            tr("Choose one of your simulated cards"),
            list(by_ref),
            format_func=lambda value: (
                display_names.label(value, controller.state)
                or tr("Simulated card {position}", position=list(by_ref).index(value) + 1)
            ),
            key="card_ref",
        )
        card = by_ref[ref]
        fact_rows({
            key: card[key]
            for key in ("card_token", "account_ref", "status", "limit", "restrictions")
        })
        state_kinds = {
            "active": ["freeze_card", "report_card_lost"],
            "frozen": ["unfreeze_card", "report_card_lost"],
            "lost": ["restore_card"],
        }
        kinds = state_kinds[card["status"]]
        if card["status"] != "lost":
            kinds += [
                "lower_card_limit",
                "raise_card_limit",
                "restrict_card",
                "relax_card_restriction",
            ]
        else:
            st.info(
                tr(
                    "A lost card must first be restored to frozen status, then separat"
                    "ely verified to unfreeze. Existing limits and restrictions remain"
                    " in force."
                )
            )
        kind = st.selectbox(
            tr("Choose a card operation"),
            kinds,
            format_func=lambda value: tr(KINDS.get(value, value)),
            key="card_operation",
        )
        change = None
        if kind in {"lower_card_limit", "raise_card_limit"}:
            limit = st.text_input(tr("New limit (CNY, to the nearest cent)"), key="card_limit")
            change = {"limit": limit}
        elif kind in {"restrict_card", "relax_card_restriction"}:
            categories = {
                "online": tr("Online transactions"),
                "overseas": tr("Overseas transactions"),
                "cash": tr("Cash transactions"),
                "entertainment": tr("Entertainment spending"),
            }
            options = sorted(set(categories) | set(card["restrictions"]))
            restricted = st.multiselect(
                tr("All transaction categories to restrict after this change"),
                options,
                default=card["restrictions"],
                format_func=lambda value: categories.get(value, value),
                key=f"card_restrictions:{ref}:{kind}:{card['version']}",
            )
            change = {"restrictions": sorted(restricted)}
            st.caption(
                tr(
                    "Restricting adds categories; relaxing removes categories and requ"
                    "ires independent MFA."
                )
            )
        if st.button(
            tr("Show card operation approval"), key="preview_card_control", type="primary"
        ):
            perform(
                lambda: controller.preview_card_operation(
                    kind,
                    card_ref=ref,
                    change=change,
                    request_id=request_id(
                        "card-control",
                        {
                            "kind": kind,
                            "card_ref": ref,
                            "change": change,
                            "version": card["version"],
                        },
                    ),
                ),
                "card_pending",
            )
            st.session_state.pop("card_result", None)
    else:
        st.info(
            tr(
                "No simulated virtual cards yet. Check the default limit and trans"
                "action restrictions before applying."
            )
        )
    pending = st.session_state.get("card_pending")
    result = st.session_state.get("card_result")
    if pending and pending.get("card"):
        render_card(pending["card"])
        live = next(
            (
                row
                for row in controller.snapshot()["pending"]
                if row["pending_id"] == pending["pending_id"]
            ),
            None,
        )
        if live and live["completed"]:
            result = live["result"]
        if live and not live["completed"]:
            red = pending["card"].get("tier") == "red"
            if red and live["mode"] == "prepared":
                st.info(
                    tr(
                        "Your device confirmed the intent; the operation has not executed."
                        " Open Device for MFA and final approval."
                    )
                )
            else:
                st.button(
                    tr("Confirm intent on device, then verify MFA")
                    if red
                    else tr("Confirm on device and execute card operation"),
                    key="approve_card",
                    type="primary",
                    on_click=approve_saved,
                    args=(controller, "card_pending", "card_result"),
                )
    render_result(result)


def device(controller: DemoController) -> None:
    st.markdown(tr("## Device approvals"))
    st.info(
        tr(
            "Simulated independent device. Complete MFA here; verification cod"
            "es stay out of chat, logs and model input."
        )
    )
    if st.session_state.get("device_redisplay"):
        st.warning(
            tr("The facts changed. Review the current canonical card before confirming again.")
        )
    st.caption(
        tr(
            "Simulated password changes only increment the credential version; "
            "no password is collected. Red-tier MFA and canonical approval are required."
        )
    )
    if st.button(tr("Simulate password change (no password collected)"), key="preview_password"):
        perform(controller.preview_password_change, "password_pending")
    rows = pending_rows(controller)
    if not rows:
        st.caption(
            tr("No operations awaiting approval. Prepare a task to see its canonical card here.")
        )
    for index, pending in enumerate(rows):
        identifier = pending.get("pending_id", pending.get("id"))
        if identifier is None:
            continue
        st.markdown(f"{tr('### Pending operation ')}{index + 1}")
        render_card(pending.get("card", {}), position=index + 1, total=len(rows))
        red = pending.get("needs_mfa") or pending.get("card", {}).get("tier") == "red"
        # S6: the device page has no user words, so it shows only the registered
        # recipient facts (no questions). A check shown on the chat page holds here
        # until it is answered and not paused: the controller's entry points refuse
        # anyway, and the page sends the user back to the chat reply to answer.
        if pending.get("mode") != "draft":
            render_scam_facts(
                controller, pending.get("card", {}), title_key="polish.scam.device_title"
            )
        gate = controller.scam_gate_status(identifier)
        decision = scam_gate_decision(controller, identifier)
        paused = decision not in (None, "proceed")
        if paused:
            notice = tr(
                {
                    "pause": "polish.scam.device_paused",
                    "incomplete": "polish.scam.device_incomplete",
                }.get(decision, "polish.scam.device_stale")
            )
            if decision == "pause" and gate and gate.get("challenge_issued"):
                # A code sent before the pause keeps its own validity window.
                notice += tr("polish.scam.pause_challenge")
            st.warning(notice)
            # The way back goes to the reply (and page) the questions were shown on, as
            # recorded by the gate; an older record without one falls back to the chat.
            origin = scam_gate_origin(gate) or {}
            reply = origin.get("response_id") or response_for_action(
                session_for(controller), "domestic", identifier
            )
            if reply:
                st.button(
                    tr("polish.scam.device_back"),
                    key=f"scam_back:{identifier}",
                    on_click=return_to_response,
                    args=(
                        reply,
                        origin.get("prefix") or "domestic",
                        origin.get("route") or "conversation",
                    ),
                )
        if red and not paused:
            mfa_choices(f"device_method:{identifier}")
        if red and pending.get("mode") == "draft":
            st.info(
                tr(
                    "Review and confirm the intent below, then request independent sim"
                    "ulated MFA. It is not yet signed or executed."
                )
            )
        if red and pending.get("mode") != "draft" and not paused:
            render_manual_mfa(controller, identifier, key=f"device_mfa:{identifier}")
        if not paused:
            st.button(
                tr("Review the facts above and approve"),
                key=f"approve:{identifier}",
                type="primary",
                on_click=approve_device,
                args=(controller, identifier),
            )
        render_result(st.session_state.get(f"approved:{identifier}"))
        st.button(
            tr("Cancel pending tasks"),
            key=f"cancel:{identifier}",
            on_click=stop_all,
            args=(controller,),
        )
    for completed in controller.snapshot().get("pending", []):
        if completed.get("completed"):
            render_result(st.session_state.get(f"approved:{completed['pending_id']}"))


def plans(controller: DemoController) -> None:
    from corridor.ui.chain_consistency import render_approval_budget, render_chain_consistency
    from corridor.ui.chain_demo import render_chain_demo, render_normal_birthday

    st.markdown(tr("## Task plans"))
    current_draft = st.session_state.get("birthday_draft") or {}
    required = sum(
        bool(
            st.session_state.get(f"plan_select:{current_draft.get('plan_id')}:{node['id']}", False)
        )
        for node in current_draft.get("nodes", [])
    )
    render_approval_budget(controller, required=required, key="birthday")
    if view_mode.is_judge():
        with st.expander(tr("Birthday plan: normal completion"), expanded=False):
            render_normal_birthday()
        with st.expander(tr("Mid-chain changes: isolated gateway"), expanded=False):
            render_chain_demo()
    st.caption(
        tr(
            "Authorize each item separately. Unselected items have no executio"
            "n permission; scheduled tasks are checked again when due."
        )
    )
    if st.button(tr("Prepare birthday task plan"), key="preview_birthday", type="primary"):
        open_birthday_draft(controller, default_birthday_event(controller))
        st.session_state.pop("plan_result", None)
    outcome = st.session_state.get("birthday_preview")
    if outcome and not st.session_state.get("birthday_draft"):
        render_birthday_choices(controller, outcome)
    draft = st.session_state.get("birthday_draft")
    render_chain_consistency(
        controller, draft, source_text=st.session_state.get("birthday_source_text")
    )
    if draft:
        st.info(
            tr(
                "The draft plan is not authorized. Check each amount, date, recipi"
                "ent and irreversible effect."
            )
        )
        if draft.get("event"):
            st.caption(birthday_event_line(controller, draft["event"]))
        render_dag(draft_snapshot(draft), key="birthday-preview")
        task_controls(session_for(controller), "plan_draft")
        selected = []
        for index, node in enumerate(draft.get("nodes", [])):
            identifier = str(node.get("node_id", node.get("id", index)))
            label = tr(str(node.get("label", node.get("kind", "Plan item"))))
            st.markdown(f"### {index + 1}. {label}")
            if node.get("card"):
                render_card(
                    node["card"],
                    canonical=bool(node.get("canonical", draft.get("canonical", False))),
                    position=index + 1,
                    total=len(draft.get("nodes", [])),
                )
            else:
                fact_rows(node)
            if st.checkbox(
                tr("I have reviewed and authorize this item"),
                key=f"plan_select:{draft['plan_id']}:{identifier}",
                value=False,
            ):
                selected.append(identifier)
        st.button(
            tr("Confirm all selected items"),
            key="confirm_plan",
            type="primary",
            disabled=not selected,
            on_click=confirm_birthday,
            args=(controller,),
        )
    render_result(st.session_state.get("plan_result"))
    snapshot = controller.snapshot()
    running = snapshot.get("plans", [])
    if running:
        st.write(tr("Current item status and dependencies"))
        render_plans(running)
        task_controls(session_for(controller), "plan_running")
    schedules = snapshot.get("schedules", [])
    if schedules:
        st.write(tr("Scheduled task status by occurrence"))
        for schedule in schedules:
            fact_rows({
                key: value
                for key, value in schedule.items()
                if key
                in {
                    "id",
                    "status",
                    "due_at",
                    "expires_at",
                    "run_index",
                    "amount",
                    "receipt_ref",
                    "waiting_mfa_at",
                    "mfa_deadline",
                }
            })
    st.warning(
        tr(
            "If daily cumulative spending raises a yellow-tier scheduled task "
            "to red, it waits for MFA for that occurrence. It expires after 24"
            " hours without verification; amounts are not changed to retry."
        )
    )
    render_result(st.session_state.get("clock_result"))
    with st.expander(tr("Independent recovery examples")):
        render_recovery_examples()


def default_birthday_event(controller: DemoController) -> str | None:
    """Use the event the user chose; otherwise only an unambiguous trusted candidate."""
    chosen = st.session_state.get("birthday_event_ref")
    if chosen:
        return str(chosen)
    candidates = controller.birthday_candidates()
    spouse = [
        item for item in candidates if item["relation"] == "spouse" and item["lock_window_open"]
    ]
    if len(spouse) == 1:
        return str(spouse[0]["event_ref"])
    return str(candidates[0]["event_ref"]) if len(candidates) == 1 else None


def open_birthday_draft(controller: DemoController, event_ref: str | None) -> dict[str, Any] | None:
    """Preview one selected trusted event; a non-draft outcome never becomes a draft."""
    result = perform(
        lambda: controller.preview_birthday(
            request_id=request_id("birthday", event_ref), event_ref=event_ref
        ),
        "birthday_preview",
    )
    if not result:
        return None
    if result.get("status") != "draft":
        st.session_state.pop("birthday_draft", None)
        if result.get("status") == "event_not_eligible":
            st.session_state.pop("birthday_event_ref", None)
        return None
    st.session_state.pop("birthday_preview", None)
    st.session_state["birthday_draft"] = result
    st.session_state["birthday_event_ref"] = result["event"]["event_ref"]
    return result


def render_birthday_choices(controller: DemoController, outcome: dict[str, Any]) -> None:
    """Show why no draft exists; candidates come from the trusted calendar only."""
    if outcome.get("status") == "outside_lock_month":
        st.warning(tr("birthday.outside_lock_month"))
        st.caption(birthday_event_line(controller, outcome["event"]))
        return
    st.info(
        tr(
            "birthday.not_eligible"
            if outcome.get("status") == "event_not_eligible"
            else "birthday.selection_required"
        )
    )
    for item in outcome.get("candidates", []):
        st.caption(birthday_event_line(controller, item))
        if item.get("lock_window_open"):
            st.button(
                tr("birthday.preview_this"),
                key="birthday_pick:" + str(item["event_ref"]),
                on_click=open_birthday_draft,
                args=(controller, item["event_ref"]),
            )


def confirm_birthday(controller: DemoController) -> None:
    draft = st.session_state["birthday_draft"]
    plan_id = draft["plan_id"]
    selected = [
        node["id"]
        for node in draft["nodes"]
        if st.session_state.get(f"plan_select:{plan_id}:{node['id']}", False)
    ]
    result = perform(lambda: controller.confirm_plan(plan_id, selected), "plan_result")
    if result and result.get("replacement"):
        st.session_state["birthday_draft"] = result["replacement"]
    elif result and result.get("status") == "settled":
        st.session_state.pop("birthday_draft", None)


def handoff(controller: DemoController) -> None:
    st.markdown(tr("## Human advisor handoff"))
    st.caption(
        tr(
            "Handoff pauses automatic execution. The advisor sees redacted evi"
            "dence; later financial actions still require authorization and ga"
            "teway checks."
        )
    )
    if st.button(tr("Hand off to a simulated advisor"), key="handoff", type="primary"):
        perform(lambda: session_for(controller).receive("handoff")["payload"], "handoff_result")
    result = st.session_state.get("handoff_result")
    if result:
        render_result(result)
    session = st.session_state.get("domestic_session")
    if session is not None and session.controller is controller:
        latest = next(
            (row for row in reversed(session.history) if row.get("intent") == "handoff"),
            None,
        )
        if latest is not None:
            response = session.response(latest["response_id"])
            payload = response.get("payload", {})

            def select_fields(value, keys):
                return {key: value[key] for key in keys if key in value}

            redacted = select_fields(response, ("response_id", "intent", "status"))
            redacted["session_snapshot"] = [
                select_fields(row, ("utterance_hash", "response_id", "intent", "status"))
                for row in payload.get("session_snapshot", [])
            ]
            redacted["dag_snapshot"] = [
                {
                    **select_fields(item["plan"], ("plan_id", "intent_id")),
                    "plan_hash": H(item["plan"]),
                    **select_fields(item, ("ownership_epoch", "blocked")),
                    "nodes": {
                        node_id: select_fields(node, ("status", "receipt_ref"))
                        for node_id, node in item.get("nodes", {}).items()
                    },
                    "audit_checkpoint": select_fields(
                        item.get("audit_checkpoint", {}), ("head", "seq")
                    ),
                }
                for item in payload.get("dag_snapshot", [])
            ]
            execution = payload.get("wealth", {}).get("execution")
            if execution:
                redacted_execution = select_fields(
                    execution,
                    (
                        "allocation_ref",
                        "plan_hash",
                        "biac_hash",
                        "status",
                        "control_status",
                        "rule_ids",
                    ),
                )
                redacted_execution["items"] = [
                    select_fields(
                        item,
                        (
                            "item_ref",
                            "product_ref",
                            "status",
                            "receipt_ref",
                            "decision_key",
                            "child_plan_id",
                        ),
                    )
                    for item in execution.get("items", [])
                ]
                redacted_execution["audit_checkpoint"] = select_fields(
                    execution.get("audit_checkpoint", {}), ("head", "seq")
                )
                redacted["wealth"] = {"execution": redacted_execution}
            st.write(tr("Latest conversation handoff snapshot"))
            if view_mode.is_judge():
                with st.expander(tr("Technical evidence")):
                    st.json(redacted)
    snapshot = controller.snapshot()
    st.write(tr("Session and plan status"))
    render_plans(snapshot.get("plans", []))
    plan_ids = [item["plan"]["plan_id"] for item in snapshot.get("plans", [])]
    if plan_ids:
        selected = st.selectbox(
            tr("Choose a plan for follow-up"),
            plan_ids,
            key="recovery_plan",
            format_func=lambda value: tr("Plan ") + str(plan_ids.index(value) + 1),
        )
        if st.button(
            tr("Prepare reversible compensation and a new approval card"),
            key="preview_compensation",
        ):
            perform(lambda: controller.compensate(selected), "compensation_pending")
        pending = st.session_state.get("compensation_pending")
        if pending:
            if pending.get("card"):
                render_card(pending["card"])
                st.button(
                    tr("Confirm the new compensation action on device"),
                    key="approve_compensation",
                    on_click=approve_saved,
                    args=(controller, "compensation_pending", "compensation_result"),
                )
            else:
                render_result(pending)
        render_result(st.session_state.get("compensation_result"))
        with st.expander(tr("Reconfirm automatic execution scope")):
            scope = controller.resume_scope(selected)
            fact_rows(scope)
            if st.button(tr("Confirm this scope on device and resume"), key="resume_plan"):
                perform(lambda: controller.resume(selected), "resume_result")
            render_result(st.session_state.get("resume_result"))
    st.info(
        tr(
            "Settled transfers cannot be directly reversed. Reversible compens"
            "ation needs a new approval; a refund request is not a received re"
            "fund."
        )
    )


def audit_judge(controller: DemoController) -> None:
    st.markdown('<div id="audit-entry"></div>', unsafe_allow_html=True)
    st.markdown(tr("## Evidence & Audit"))
    snapshot = controller.snapshot()
    render_execution_timeline(controller, st.session_state.get("domestic_session"))
    st.write(tr("Current session audit and receipts"))
    (st.success if snapshot.get("audit_valid") else st.error)(
        tr("Audit hash chain verified")
        if snapshot.get("audit_valid")
        else tr("Audit hash chain verification failed")
    )
    if view_mode.is_judge():
        with st.expander(tr("Technical evidence")):
            st.json({key: snapshot.get(key) for key in ("audit", "receipts", "dispatch_count")})
    st.divider()
    # Results come from the frozen tag and the submission; this page runs scripted only.
    st.info(tr("polish.audit.scope"))
    hijacked = st.toggle(tr("Hijacked AI mode"), key="hijacked", value=False)
    if hijacked:
        specs = development_cases()
        labels = {item.case_id: f"{item.case_id} · {item.family} · {item.attack}" for item in specs}
        selected = st.selectbox(
            tr("Choose an isolated adversarial case"),
            list(labels),
            format_func=labels.get,
            key="attack_case",
        )
        if st.button(
            tr("Run attack and benign twin in isolation"), key="run_attack", type="primary"
        ):
            with st.spinner(tr("Checking gateway rejection and benign completion…")):
                perform(lambda: run_hijacked_case(selected), "evaluation_result")
    else:
        selected = st.selectbox(
            tr("Choose a banking judge task (P0 + P1)"),
            [f"J{number:02d}" for number in range(1, 25)],
            key="judge_task",
        )
        if st.button(tr("Run selected judge task"), key="run_judge", type="primary"):
            with st.spinner(tr("Running all assertions in an isolated synthetic session…")):
                perform(lambda: run_judge_task(selected), "evaluation_result")
    result = st.session_state.get("evaluation_result")
    if result:
        passed = result.get("passed", result.get("attack", {}).get("passed", False))
        (st.success if passed else st.error)(
            tr("Evaluation PASS") if passed else tr("Evaluation FAIL / incomplete")
        )
        if view_mode.is_judge():
            with st.expander(tr("Technical evidence")):
                st.json(result)
    st.caption(tr("polish.audit.tasks"))


def operator_panel(controller: DemoController) -> None:
    """Show simulation controls only when an operator explicitly enables them."""
    if os.environ.get("CORRIDOR_OPERATOR_MODE") != "1":
        return
    with st.sidebar.expander(tr("Operator panel"), expanded=False):
        st.caption(tr("Supporting-bank clock · separate from the wealth simulation"))
        if st.button(tr("Advance simulated clock by 10 minutes 1 second"), key="advance_window"):
            perform(lambda: controller.advance_clock(601), "global_clock_result")
            st.rerun()
        # One jump past a run's 24-hour window would expire it, so the operator
        # advances to the next future due run rather than to a fixed date.
        target = controller.next_due_target()
        if st.button(
            tr("Advance simulated clock to the next due time"),
            key="advance_clock",
            disabled=target is None,
        ):
            perform(lambda: controller.advance_clock(target), "clock_result")
            st.rerun()
        if target is not None:
            st.caption(tr("Next due time: ") + beijing_time(target) + " · " + tr("Beijing time"))


def remember_page() -> None:
    """Retain route selection independently of translated widget identities."""
    st.session_state["active_page"] = st.session_state["page"]
    st.session_state["show_demo"] = False


def persistent_notice(slot: Any, live_deployment: str | None = None) -> None:
    """The fixed corner notice; with a live banking planner it also names the deployment."""
    text = tr("simulation_notice")
    if live_deployment:
        text += " · " + tr("live.corner", deployment=live_deployment)
    slot.markdown(
        '<div class="simulation-persistent" style="position:fixed;right:24px;bottom:12px;'
        "z-index:999;background:#f4f0fa;color:#5b456f;border:1px solid #ded4eb;"
        'border-radius:20px;padding:7px 16px;font-size:13px;pointer-events:none">'
        + text
        + "</div>",
        unsafe_allow_html=True,
    )


def main() -> None:
    if "locale" not in st.session_state:
        st.session_state["locale"] = get_locale()
    set_locale(st.session_state["locale"])
    view_mode.start_run()
    st.set_page_config(
        page_title=tr("Demo Bank · Corridor"),
        page_icon="▧",
        layout="wide",
        initial_sidebar_state="collapsed",
    )
    st.markdown(STYLES, unsafe_allow_html=True)
    st.markdown(view_mode.page_css(), unsafe_allow_html=True)
    corner = st.empty()
    persistent_notice(corner)
    # The simulated phone is its own browser session: it never creates a controller.
    if phone.phone_route():
        return
    brand, language = st.columns([4, 1])
    with brand:
        # The customer view keeps only the fixed notice; the judge view repeats it here.
        subtitle = (
            f'<div class="bank-subtitle">{tr("simulation_notice")}</div>'
            if view_mode.is_judge()
            else ""
        )
        st.markdown(
            '<div class="bank-header"><div class="bank-mark">⇄</div><div>'
            '<div class="bank-brand">CORRIDOR</div>'
            f'<div class="bank-name">{tr("bank_name")} · '
            f"{tr('Corridor assistant')}</div>"
            f"{subtitle}</div></div>",
            unsafe_allow_html=True,
        )
    with language:
        st.selectbox(
            tr("language_label"),
            ("zh-CN", "en"),
            format_func=lambda value: tr("language_zh") if value == "zh-CN" else "English",
            key="locale",
            on_change=lambda: set_locale(st.session_state["locale"]),
        )
        view_mode.render_toggle()
    if "controller" not in st.session_state:
        st.session_state["controller"] = DemoController(initial_state=demo_state(showcase=True))
    if "wealth_controller" not in st.session_state:
        st.session_state["wealth_controller"] = WealthController()
    controller = st.session_state["controller"]
    wealth = st.session_state["wealth_controller"]
    if "next_page" in st.session_state:
        route = st.session_state.pop("next_page")
        st.session_state["page"] = route
        st.session_state["active_page"] = route
    if "active_page" not in st.session_state:
        st.session_state["active_page"] = st.session_state.get("page", PAGES[0])
    pages = view_mode.visible_pages(PAGES)
    if st.session_state["active_page"] not in pages:
        st.session_state["active_page"] = "conversation"
    st.session_state["page"] = st.session_state["active_page"]
    page_labels = {page: tr("nav." + page) for page in PAGES}
    left, right = st.columns([3, 1])
    with left:
        st.selectbox(
            tr("Primary navigation"),
            pages,
            format_func=page_labels.__getitem__,
            key="page",
            label_visibility="collapsed",
            on_change=remember_page,
        )
    with right:
        st.button(
            tr("Stop"),
            key="stop_all",
            use_container_width=True,
            on_click=stop_all,
            args=(controller,),
        )
    # S7: the judge strip keeps its place above the badge but is filled after the switch
    # was applied, so it names the planner that actually answers in this run.
    strip = st.empty()
    # S7: the planner source badge and, only with CORRIDOR_LIVE=1, the live-model switch.
    render_live_controls(controller)
    with strip.container():
        view_mode.judge_strip(live_label(controller), revoked=live_revoked(controller))
    if live_label(controller) and not live_revoked(controller):
        # The always-visible corner notice names the live planner too.
        persistent_notice(corner, live_label(controller))
    if st.session_state.get("stop_result"):
        render_result(st.session_state["stop_result"])
    snapshot = controller.snapshot()
    logged_in = bool(snapshot.get("logged_in", getattr(controller, "logged_in", False)))
    if not logged_in:
        st.markdown(
            '<div class="hero"><div class="eyebrow">CORRIDOR</div>'
            f"<h2>{tr('home_title')}</h2><p>{tr('home_intro')}</p></div>",
            unsafe_allow_html=True,
        )
        if st.button(tr("Enter synthetic demo"), key="enter", type="primary"):
            result = perform(controller.start_session)
            if result and result.get("status") in {"accepted", "logged_in", "complete", "ready"}:
                perform(wealth.start_session, "wealth_login")
                st.rerun()
        st.caption(
            tr(
                "Fictional customers, institutions and products. Starting does not"
                " authorize payment."
            )
        )
        return
    from corridor.ui.domestic_demos import render_demo, render_demo_menu

    render_demo_menu()
    if st.session_state.get("show_demo"):
        render_demo()
        return
    operator_panel(controller)
    render_result(st.session_state.get("global_clock_result"))
    if snapshot.get("status") in {"handoff", "handed_off", "paused"}:
        st.warning(tr("Automatic execution is paused"))
    handlers = {
        "conversation": conversation_home,
        "billing": billing,
        "subscriptions": subscriptions,
        "cards": cards,
        "birthday": plans,
        "dag": plans,
        "operator": handoff,
        "safety": render_safety,
        "im": im_home,
    }
    selected_page = st.session_state["page"]
    if selected_page == "conversation":
        # Only a trusted partner relation is announced as a partner birthday.
        partner = [
            item for item in controller.birthday_candidates() if item["relation"] == "spouse"
        ]
        ready = [item for item in partner if item["lock_window_open"]]
        if ready:
            suggestion = ready[0]
            st.info(
                tr(
                    "A partner birthday was found in the synthetic calendar. "
                    "Preview a budget lock and flower and cake orders two days before the birthday."
                )
            )
            st.caption(
                tr("Read-only birthday suggestion; no operation authorized. Due: ")
                + beijing_time(suggestion["order_due"])
                + " · "
                + tr("Beijing time")
            )
            if st.button(tr("Preview partner birthday plan"), key="birthday_suggestion"):
                st.session_state.pop("birthday_source_text", None)
                if open_birthday_draft(controller, suggestion["event_ref"]):
                    st.session_state["next_page"] = "birthday"
                    st.rerun()
        elif partner:
            st.caption(birthday_event_line(controller, partner[0]))
            st.caption(tr("birthday.reminder_only"))
    try:
        if selected_page == st.session_state.get("conversation_route") and selected_page not in {
            "conversation",
            "operator",
        }:
            conversation_home(controller)
        elif selected_page == "wealth":
            wealth_views.advisor(wealth)
            wealth_views.proposal(wealth)
            with st.expander(tr("Product comparison")):
                investments(controller)
        elif selected_page == "device":
            wealth_views.allocation_device(wealth)
            with st.expander(
                tr("Risk questionnaire and signed goals"), expanded=wealth.parent_id is None
            ):
                wealth_views.risk_questionnaire(wealth)
                wealth_views.goal_sheet(wealth)
            with st.expander(
                tr("Supporting-bank device approvals"), expanded=bool(pending_rows(controller))
            ):
                device(controller)
        elif selected_page == "audit":
            from corridor.ui.attack_views import render_attacks
            from corridor.ui.notification_views import render_availability_evidence
            from corridor.ui.recorded_panel import render_recorded_panel

            mechanisms, availability, recorded = st.tabs([
                tr("Mechanism evidence"),
                tr("Availability cost: injection signal triggers lockout"),
                tr("Recorded model evidence"),
            ])
            with mechanisms:
                render_attacks()
            with availability:
                render_availability_evidence()
            with recorded:
                render_recorded_panel()
            with st.expander(tr("Receipts and audit")):
                wealth_views.evidence(wealth)
                audit_judge(controller)
        elif selected_page == "transfers":
            st.caption(tr("Separate synthetic supporting-bank account"))
            render_structured_transfer(controller)
            render_aa_collection(controller)
            if view_mode.is_judge():
                # S6 R7: the legacy free-text form does not ask the recipient questions;
                # it stays as judge-view evidence only.
                with st.expander(tr("Legacy conversation demonstration")):
                    conversation(controller)
        else:
            handlers[selected_page](controller)
    except (ValueError, RuntimeError, TimeoutError, KeyError, OSError) as error:
        st.error(
            tr(
                "The operation was not completed. Review current facts and retry f"
                "rom a fresh proposal."
            )
        )
        # S7: a live model's failure is named as such; nothing scripted ran instead.
        render_live_failure(controller, error)
    if view_mode.is_judge():
        st.divider()
        st.caption(
            tr(
                "Financial effects come from verified receipts · synthetic data · "
                "simulated authentication · no real money · append-only audit"
            )
        )


if __name__ == "__main__":
    main()
