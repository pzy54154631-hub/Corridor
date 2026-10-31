"""Opt-in, isolated presentation presets using existing device/gateway workflows."""

from fractions import Fraction

from corridor.crypto.jcs import H
from corridor.ui import display_names, view_mode
from corridor.ui.controller import DemoController, demo_state
from corridor.ui.messages import source_utterance, tr

PRESETS = ("daily_threshold", "notification_injection", "birthday_unknown", "suitability_r4")


def prepare_demo(kind, *, defer_birthday_orders=False):
    """Never receive or replace the user's active controller."""
    if kind not in PRESETS:
        raise ValueError("Unknown demonstration preset")
    state = demo_state()
    if kind == "daily_threshold":
        contacts = dict(state.contacts)
        for ref in ("CONTACT_LIN_A",):
            contact = contacts[ref]
            contacts[ref] = contact.model_copy(
                update={
                    "aliases": [
                        *contact.aliases,
                        *contacts["CONTACT_CHEN"].aliases,
                    ]
                }
            )
        state = state.model_copy(update={"contacts": contacts})
    controller = DemoController(initial_state=state)
    controller.start_session()
    demo = {"kind": kind, "controller": controller}
    if kind == "suitability_r4":
        # Isolated on purpose: on the user's session the same refusal would raise the
        # account reject streak and move the safety lamp to Watch.
        controller.compare_investments("net_return")
        result = controller.preview_investment("P_C", request_id="DEMO-SUITABILITY-R4")
        pending = controller._pending[result["pending_id"]]
        verdict = pending["prepared"].proposal.verdict
        demo["r4"] = {
            "card": result["card"],
            "outcome": verdict.outcome,
            "rule_ids": list(verdict.rule_ids),
            "execute_count": controller.gateway.bank.execute_count,
            "challenge_id": pending.get("challenge_id"),
        }
    elif kind == "daily_threshold":
        draft = controller.preview_transfer(
            source_utterance("demo.prior.transfer"), request_id="DEMO-PRIOR-TRANSFER"
        )
        demo["setup_result"] = controller.confirm_transfer(draft["draft_id"])
    elif kind == "birthday_unknown":
        draft = controller.preview_birthday()
        demo["setup_result"] = controller.confirm_plan(
            draft["plan_id"], [node["id"] for node in draft["nodes"]]
        )
        demo["plan_id"] = draft["plan_id"]
        # Transport loss is an existing MockBank fault; reconciliation uses lookup.
        flower = next(
            run
            for run in controller.state.schedules.values()
            if run.action_template["kind"] == "merchant_order"
        )
        controller.gateway.bank.faults[
            H({"spa_id": flower.spa_ref, "run_index": flower.run_index})
        ] = "response_loss"
        demo["orders_due_at"] = flower.due_at
        if not defer_birthday_orders:
            demo["advance_result"] = controller.advance_clock(flower.due_at)
            controller.start_session()
    return demo


def render_demo_menu():
    import streamlit as st

    names = {
        "daily_threshold": tr("Daily threshold · prior CNY 300 transfer"),
        "notification_injection": tr("Notification injection · attack and control"),
        "birthday_unknown": tr("Birthday plan · one unknown outcome"),
        "suitability_r4": tr("polish.r4.menu"),
    }
    with st.expander(tr("Demo scenarios")):
        st.caption(
            tr(
                "Load an isolated simulation with explicit scripted device setup; "
                "the banking session stays separate."
            )
        )
        selected = st.selectbox(
            tr("Choose preset"), list(names), format_func=names.get, key="demo_preset"
        )
        if st.button(tr("Load demo scenario"), key="load_demo_preset"):
            for key in list(st.session_state):
                if key.startswith("demo_") and key != "demo_preset":
                    del st.session_state[key]
            st.session_state["demo_scenario"] = prepare_demo(selected, defer_birthday_orders=True)
            st.session_state["show_demo"] = True
            st.rerun()
        if st.session_state.get("show_demo") and st.button(
            tr("Return to banking"), key="close_demo"
        ):
            st.session_state["show_demo"] = False
            st.rerun()


def unlock_recovery_demo(demo):
    """Keep cooldown and the existing signed device/MFA protocol intact."""
    from corridor.ui.safety import unlock_current_session

    result = unlock_current_session(demo["controller"])
    if result["status"] == "unlocked":
        demo["controller"].start_session()
    return result


def render_recovery_lock(demo):
    import streamlit as st

    from corridor.ui.components import money, render_result

    controller = demo["controller"]
    current = controller.gateway.safety_snapshot(controller.device.principal)
    st.metric(tr("Remaining locked balance"), money(controller.snapshot()["balances"]["A_LOCKED"]))
    if current["state"] == "Locked":
        st.warning(tr("Cooldown and valid device MFA are required."))
        latest = {event.intent_key: event for event in controller.state.ledger}
        stopped = any(
            row["event"] == "plan_interrupted" and row.get("plan_id") == demo["plan_id"]
            for row in controller.gateway.audit.records()
        )
        ready = stopped and not any(event.event == "unknown" for event in latest.values())
        if not ready:
            st.caption(
                tr(
                    "Stop pending work and look up the unknown receipt "
                    "before advancing the demo clock."
                )
            )
        if st.button(
            tr("Advance simulated clock through cooldown"),
            key="demo_recovery_cooldown",
            disabled=not ready,
        ):
            controller.advance_clock(max(controller.state.now, current["unlock_after"]))
            st.rerun()
        if st.button(
            tr("Confirm unlock on simulated owner device"),
            key="demo_recovery_unlock",
            disabled=not ready,
        ):
            demo["unlock_result"] = unlock_recovery_demo(demo)
            if demo["unlock_result"]["status"] == "unlocked":
                st.session_state.pop("demo_recovery_compensation", None)
            st.rerun()
    if demo.get("unlock_result"):
        result = demo["unlock_result"]
        (st.success if result["status"] == "unlocked" else st.warning)(
            tr("Unlocked; previous approvals remain void.")
            if result["status"] == "unlocked"
            else tr("Cooldown and valid device MFA are required.")
        )
    compensation = st.session_state.get("demo_recovery_compensation", {})
    if "BAL-AVAILABLE" in compensation.get("rule_ids", []):
        st.warning(
            tr(
                "The existing compensation proposal exceeds the remaining locked balance. "
                "Review the rejection and hand off for assistance."
            )
        )
    if view_mode.is_judge():
        with st.expander(tr("Technical evidence")):
            st.json(current)
            if demo.get("unlock_result"):
                render_result(demo["unlock_result"])


def render_demo():
    import streamlit as st

    from corridor.agent.domestic_session import DomesticSession
    from corridor.ui.components import money, render_plans, render_result
    from corridor.ui.domestic_views import conversation_home, task_controls
    from corridor.ui.messages import tr

    demo = st.session_state["demo_scenario"]
    controller = demo["controller"]
    st.info(tr("Demo scenario · isolated synthetic account · scripted device setup"))
    if demo["kind"] == "notification_injection":
        from corridor.ui.attack_views import render_attacks

        render_attacks(initial_kind="prompt_injection")
        return
    if demo["kind"] == "suitability_r4":
        render_r4_refusal(demo)
        return
    if "session" not in demo:
        demo["session"] = DomesticSession(controller)
    if demo["kind"] == "daily_threshold":
        st.caption(
            tr(
                "The prior transfer has a receipt. Try the transfer shortcut, "
                "choose the recipient and check the cumulative tier."
            )
        )
        conversation_home(controller, session=demo["session"], prefix="demo")
    else:
        if "advance_result" not in demo:
            st.subheader(tr("Birthday plan: budget locked, orders scheduled"))
            render_plans(controller.snapshot()["plans"])
            render_result(demo["setup_result"])
            st.caption(tr("Advance the simulated clock; one order will lose its bank response."))
            if st.button(
                tr("Run due orders with simulated response loss"), key="demo_birthday_orders"
            ):
                demo["advance_result"] = controller.advance_clock(demo["orders_due_at"])
                controller.start_session()
                st.rerun()
            return
        st.subheader(tr("Birthday plan: reconcile the order"))
        st.caption(
            tr(
                "A lost response does not mean no payment. Stop pending work, "
                "then look up the bank receipt."
            )
        )
        render_plans(controller.snapshot()["plans"])
        task_controls(demo["session"], "demo_recovery")
        if st.button(tr("Look up receipt (no resend)"), key="demo_reconcile"):
            latest = {event.intent_key: event for event in controller.state.ledger}
            demo["reconciled"] = [
                {
                    "status": result.status,
                    "rule_ids": result.rule_ids,
                    "receipt": result.receipt.model_dump(mode="json") if result.receipt else None,
                }
                for key, event in latest.items()
                if event.event == "unknown"
                for result in [controller.gateway.reconcile(key)]
            ]
            st.rerun()
        for result in demo.get("reconciled", []):
            render_result(result)
        render_recovery_lock(demo)
        control_id = st.session_state.get("demo_recovery_response_id")
        if control_id and demo["session"].response(control_id)["status"] == "handoff":
            st.subheader(tr("Operator session and DAG snapshot"))
            render_result(demo["session"].response(control_id)["payload"])
            locked = controller.snapshot()["balances"]["A_LOCKED"]
            if Fraction(locked) == 0:
                # Nothing is locked any more: the operator only checks, nothing to compensate.
                st.info(tr("polish.operator.nothing_locked", amount=money(locked)))
                label = tr("polish.operator.check")
            else:
                st.caption(tr("Compensation is a new action; review and approve it on the device."))
                label = tr("Operator: prepare compensation for device review")
            if st.button(label, key="demo_operator_prepare"):
                demo["operator_result"] = controller.compensate(demo["plan_id"])
                st.rerun()
            if demo.get("operator_result"):
                if demo["operator_result"]["status"] == "no_compensation":
                    st.info(tr("No remaining locked funds require compensation."))
                else:
                    render_result(demo["operator_result"])
            if view_mode.is_judge():
                with st.expander(tr("Technical evidence")):
                    st.json({
                        "session_id": controller.session,
                        "plan": controller.gateway.plan_snapshot(demo["plan_id"]),
                        "audit_checkpoint": controller.gateway.audit.checkpoint(),
                    })
    if view_mode.is_judge():
        with st.expander(tr("Technical evidence")):
            st.json({
                key: value for key, value in demo.items() if key not in {"controller", "session"}
            })


def refusal_title(rule_ids):
    """The risk-mismatch title only when the kernel named the risk rule; else a general one."""
    return tr("polish.r4.title" if "BIC-RISK" in (rule_ids or []) else "polish.refusal.title")


def render_refusal_card(card, controller, *, outcome, rule_ids, execute_count, isolated=False):
    """Red refusal card from a kernel verdict; no MFA or approval control (S2 R4, S4 R6)."""
    from html import escape

    import streamlit as st

    from corridor.ui import suitability
    from corridor.ui.components import money

    lines = suitability.lines(card, controller.state)
    rows = "".join(f"<li>{escape(line)}</li>" for line in lines)
    suit = (
        f'<div class="suit-block"><div class="suit-title">{escape(tr("polish.suit.title"))}'
        f"</div><ul>{rows}</ul>"
        f'<div class="suit-footer">{escape(suitability.footer())}</div></div>'
        if lines
        else ""
    )
    amount = money(card["amount"]) if card.get("amount") is not None else ""
    reference = (card.get("objects") or {}).get("product_ref")
    product = (display_names.label(reference, controller.state) if reference else None) or ""
    result = tr("polish.r4.result" if isolated else "polish.refusal.result", count=execute_count)
    st.markdown(
        '<section class="approval-card"><div class="approval-top">'
        f'<span class="tier tier-red">{escape(refusal_title(rule_ids))}</span></div>'
        f'<div class="approval-purpose">{escape(product)}</div>'
        f'<div class="fact-amount">{escape(amount)}</div>'
        + suit
        + f'<p class="approval-reason">{escape(result)}</p></section>',
        unsafe_allow_html=True,
    )
    st.caption(tr("polish.r4.isolation" if isolated else "polish.refusal.next"))
    if view_mode.is_judge():
        st.caption(
            tr(
                "polish.r4.judge",
                rules=" · ".join(rule_ids),
                outcome=display_names.outcome_label(outcome),
            )
        )


def render_r4_refusal(demo):
    """Red refusal card from the isolated kernel verdict; no MFA or approval control."""
    import streamlit as st

    from corridor.ui import suitability

    r4 = demo["r4"]
    render_refusal_card(
        r4["card"],
        demo["controller"],
        outcome=r4["outcome"],
        rule_ids=r4["rule_ids"],
        execute_count=r4["execute_count"],
        isolated=True,
    )
    if view_mode.is_judge():
        with st.expander(tr("Technical evidence")):
            st.json(suitability.refusal_facts(demo))
