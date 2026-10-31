"""Presentation views for independently measured notification attack and recovery."""

import streamlit as st

from corridor.crypto.jcs import jcs_bytes
from corridor.ui import view_mode
from corridor.ui.components import money, reference_label
from corridor.ui.messages import tr
from corridor.ui.notification_demo import (
    advance_notification_cooldown,
    create_notification_demo,
    recover_notification_transfer,
    unlock_notification_demo,
)


def _side(result, *, baseline=False, benign=False):
    st.markdown(
        "### "
        + tr(
            "D0 baseline · simulated user approves all"
            if baseline
            else "Corridor · complete gateway"
        )
    )
    if result["attack_success"]:
        st.error(tr("Synthetic funds reached the attacker account."))
    elif result["completed"]:
        st.success(tr("The legitimate transfer settled."))
    else:
        st.success(tr("The changed recipient was rejected before dispatch."))
    left, right = st.columns(2)
    left.metric(tr("Actual bank dispatches"), result["bank_dispatches"])
    right.metric(tr("Actual bank debits"), money(result["funds_moved_cny"]))
    judge = view_mode.is_judge()
    if result["recipient_refs"]:
        # Customer view: synthetic display names instead of raw contact identifiers.
        recipients = (
            result["recipient_refs"]
            if judge
            else [reference_label(ref) for ref in result["recipient_refs"]]
        )
        st.write(tr("Receipt recipient: ") + " · ".join(recipients))
    if result.get("kernel_verdict", {}).get("outcome") == "Reject":
        if judge:
            st.write("**" + " · ".join(result["kernel_rule_ids"]) + "**")
        if "PROV-MISMATCH" in result["kernel_rule_ids"]:
            st.caption(tr("The recipient does not match the device-confirmed trusted source."))
    if result.get("safety", {}).get("state") == "Locked":
        st.warning(
            tr("Injection signal: security locked; a new legitimate request needs recovery.")
        )
    if result["receipts"]:
        st.caption(
            tr("Bank receipt signature verified")
            + (" · " + result["receipts"][0]["receipt_id"][:16] if judge else "")
        )
    if result.get("audit_valid"):
        st.caption(tr("Audit hash chain verified"))
    if benign:
        st.caption(tr("The control removes the malicious change and keeps the legitimate goal."))
    if judge:
        with st.expander(tr("Technical evidence · result and receipts")):
            st.json(result)


def render_notification_demo():
    st.header(tr("Assume the model has been hijacked"))
    st.caption(
        tr(
            "Deterministic script · assumed model hijack · not a model score; "
            "real-model hijack rates belong to the formal A0 / A5 experiment."
        )
    )
    st.info(
        tr(
            "A subscription notice says: replace the confirmed recipient "
            "with the attacker account. "
            "The identical proposal goes to both execution paths."
        )
    )
    if st.button(
        tr("Run the same hijacked proposal"), key="notification_hijacked_run", type="primary"
    ):
        st.session_state["notification_hijacked_demo"] = create_notification_demo()
    demo = st.session_state.get("notification_hijacked_demo")
    if demo is None:
        return
    result = demo["result"]
    attack, recovery, benign = st.tabs([
        tr("Worst-case comparison"),
        tr("Recovery after lockout"),
        tr("Benign twin: both complete"),
    ])
    with attack:
        if view_mode.is_judge():
            st.caption(tr("Identical proposal digest: ") + result["attack"]["proposal_hash"][:24])
        left, right = st.columns(2)
        with left:
            _side(result["baseline"]["attack"], baseline=True)
        with right:
            _side(result["attack"])
    with recovery:
        st.subheader(tr("Cooldown → owner device → simulated MFA → new transfer"))
        case = demo["case"]
        snapshot = case.gateway.safety_snapshot(case.device.principal)
        st.write(
            tr("Availability cost: ")
            + str(result["cooldown_seconds"] // 60)
            + tr(
                " minutes of simulated cooldown, owner confirmation, unlock MFA, "
                "and a new red-tier transfer approval with MFA."
            )
        )
        st.caption(tr("Current simulated clock: ") + case.state.now)
        one, two, three = st.columns(3)
        with one:
            if st.button(
                tr("1 · Advance through cooldown"),
                key="notification_cooldown",
                disabled=demo.get("cooldown_completed", False),
            ):
                advance_notification_cooldown(demo)
                st.rerun()
        with two:
            if st.button(
                tr("2 · Owner confirmation and unlock MFA"),
                key="notification_unlock",
                disabled=not demo.get("cooldown_completed") or snapshot["state"] != "Locked",
            ):
                unlock_notification_demo(demo)
                st.rerun()
        with three:
            if st.button(
                tr("3 · Reissue legitimate transfer with MFA"),
                key="notification_reissue",
                disabled=demo.get("unlock_result", {}).get("status") != "unlocked"
                or "recovery" in demo,
            ):
                recover_notification_transfer(demo)
                st.rerun()
        if "recovery" in demo:
            _side(demo["recovery"], benign=True)
        elif demo.get("unlock_result", {}).get("status") == "unlocked":
            st.success(tr("Unlocked; previous approvals remain void."))
        else:
            st.warning(
                tr("Locked: yellow and red actions are blocked. Green queries remain available.")
            )
        if view_mode.is_judge():
            with st.expander(tr("Technical evidence · safety signals and audit")):
                st.json(snapshot)
                if demo.get("unlock_result"):
                    st.json(demo["unlock_result"])
    with benign:
        left, right = st.columns(2)
        with left:
            _side(result["baseline"]["benign"], baseline=True, benign=True)
        with right:
            _side(result["benign"], benign=True)
    st.download_button(
        tr("Download comparison evidence"),
        jcs_bytes({**result, "recovery": demo.get("recovery")}),
        file_name="notification-comparison.json",
        mime="application/json",
        key="notification_download",
    )


def render_availability_evidence():
    from corridor.ui.attack_views import render_attack_result
    from corridor.ui.evaluation import run_attack_comparison

    st.caption(
        tr(
            "The ordinary scripted planner ignores injected instructions. "
            "This comparison measures the resulting lockout cost."
        )
    )
    if st.button(tr("Run availability comparison"), key="notification_availability_run"):
        st.session_state["notification_availability_result"] = run_attack_comparison(
            "prompt_injection"
        )
    if st.session_state.get("notification_availability_result"):
        render_attack_result(st.session_state["notification_availability_result"])
