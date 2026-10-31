"""Explicit isolated simulated-device recovery demonstrations."""

from typing import Any

import streamlit as st

from corridor.crypto.jcs import H
from corridor.ui import view_mode
from corridor.ui.controller import DemoController
from corridor.ui.messages import tr


def prepare_recovery(mode: str) -> dict[str, Any]:
    """Prepare a clearly labelled scripted example; compensation stays unsigned."""
    if mode not in {"interrupt", "failure", "handoff"}:
        raise ValueError("Unknown recovery example")
    controller = DemoController()
    controller.start_session()
    draft = controller.preview_birthday()
    selected = [node["id"] for node in draft["nodes"]]
    result = controller.confirm_plan(draft["plan_id"], selected)
    if result["status"] != "settled":
        raise ValueError("The simulated device could not confirm the initial plan")
    demo = {"mode": mode, "controller": controller, "plan_id": draft["plan_id"]}
    if mode == "failure":
        unlock = next(
            run
            for run in controller.state.schedules.values()
            if run.action_template["kind"] == "unlock_funds"
        )
        controller.gateway.bank.faults[
            H({"spa_id": unlock.spa_ref, "run_index": unlock.run_index})
        ] = "failed"
        demo["failure"] = controller.advance_clock(unlock.due_at)
        # A failed predecessor stops the remaining plan. No later task revives.
        controller.stop(draft["plan_id"])
        controller.start_session()
        demo["compensation"] = controller.compensate(draft["plan_id"])
    elif mode == "handoff":
        demo["control"] = controller.handoff(draft["plan_id"])
    else:
        demo["control"] = controller.stop(draft["plan_id"])
    return demo


def render_recovery_examples() -> None:
    from corridor.ui.components import render_card, render_plans, render_result

    st.subheader(tr("Independent recovery examples"))
    st.caption(tr("These isolated scripted examples include explicit simulated device approvals."))
    modes = {
        "interrupt": "Demonstrate interruption",
        "failure": "Demonstrate failed node and compensation",
        "handoff": "Demonstrate operator takeover",
    }
    for mode, label in modes.items():
        if st.button(tr(label), key=f"recovery_{mode}"):
            st.session_state["recovery_demo"] = prepare_recovery(mode)
    demo = st.session_state.get("recovery_demo")
    if not demo:
        return
    controller = demo["controller"]
    st.info(tr("Isolated synthetic recovery session; no real funds or operator."))
    render_plans(controller.snapshot()["plans"])
    if demo.get("failure"):
        render_result(demo["failure"])
    if demo.get("control"):
        render_result(demo["control"])
    if demo["mode"] == "handoff":
        st.write(tr("Operator session and DAG snapshot"))
        if view_mode.is_judge():
            st.json({
                "session_id": controller.session,
                "plan": controller.gateway.plan_snapshot(demo["plan_id"]),
                "audit_checkpoint": controller.gateway.audit.checkpoint(),
            })
        if st.button(
            tr("Operator: prepare compensation for device review"), key="operator_prepare"
        ):
            demo["compensation"] = controller.compensate(demo["plan_id"])
    pending = demo.get("compensation")
    if pending and not demo.get("compensation_result"):
        st.warning(tr("Compensation is a new action; review and approve it on the device."))
        render_card(pending["card"])
        if st.button(tr("Approve new compensation on device"), key="recovery_approve"):
            demo["compensation_result"] = controller.approve_pending(pending["pending_id"])
            st.rerun()
    if demo.get("compensation_result"):
        render_result(demo["compensation_result"])
    if view_mode.is_judge():
        st.json({
            "balances": controller.snapshot()["balances"],
            "audit_checkpoint": controller.gateway.audit.checkpoint(),
        })
