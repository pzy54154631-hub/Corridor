"""Visible circuit breaker exercise with the existing device unlock protocol."""

from html import escape

import streamlit as st

from corridor.bench.bank_cases import BankCase
from corridor.bench.planning_runner import prepare_transfer
from corridor.crypto.jcs import H
from corridor.sim.fixtures import make_state
from corridor.stores.bank import add_seconds
from corridor.ui import display_names, view_mode
from corridor.ui.manual_mfa import render_manual_unlock
from corridor.ui.messages import tr


def create_safety_demo(mode):
    if mode not in {"reject", "injection", "mfa"}:
        raise ValueError("Unknown safety demonstration")
    case = BankCase(make_state(), task_id="D2-UI-SAFETY", subcase_id=mode)
    yellow = prepare_transfer(case, "200/1", step=0)
    red = prepare_transfer(case, "1500/1", step=1)
    challenge = case.gateway.request_mfa(red.action, red.cert) if mode == "mfa" else None
    return {
        "case": case,
        "yellow": yellow,
        "red": red,
        "mode": mode,
        "challenge": challenge,
        "events": [],
    }


def trigger_signal(demo):
    case = demo["case"]
    yellow = demo["yellow"]
    if demo["mode"] == "injection":
        case.gateway.propose(yellow.action, yellow.cert, injection_flag=True)
    elif demo["mode"] == "reject":
        invalid = yellow.cert.model_copy(update={"action_hash": H("invalid-proposal")})
        case.gateway.propose(yellow.action, invalid)
    else:
        verifier, challenge = case.gateway.mfa, demo["challenge"]
        code = verifier.test_code(challenge)
        verifier.verify(challenge, "000000" if code != "000000" else "111111", now=case.state.now)
    snapshot = case.gateway.safety_snapshot(case.device.principal)
    demo["events"].append(snapshot)
    return snapshot


def unlock_demo(demo):
    case = demo["case"]
    scope = case.gateway.unlock_scope(case.device.principal)
    challenge = case.gateway.request_unlock_mfa(case.device.principal)
    if challenge is None:
        return {"status": "rejected", "rule_ids": ["CTRL-MFA"]}
    confirmation = case.device.confirm_input(
        intent_id=H(scope), slot="unlock", value=scope, now=case.state.now
    )
    grant = case.device.complete_mfa(case.gateway.mfa, challenge, now=case.state.now)
    result = case.gateway.unlock(case.device.principal, confirmation, grant)
    return {
        "status": result.status,
        "rule_ids": result.rule_ids,
        "audit_checkpoint": case.gateway.audit.checkpoint(),
    }


def unlock_current_session(controller):
    """Use the current controller's device and gateway for the complete protocol."""
    return unlock_demo({"case": controller})


def safety_projection(snapshot):
    """Translate actual signals; an unknown reason stays available in evidence."""
    reasons = []
    labels = {
        # Session-level count: reset by this session's next money proposal the kernel
        # does not reject (breaker money_accepted), so "since the last accepted" is exact.
        "consecutive_money_rejections": tr("polish.safety.signal.session_money"),
        "injection_hits_10m": tr("Injection signals in ten minutes"),
        "consecutive_mfa_failures": tr("Consecutive MFA failures"),
    }
    for name, label in labels.items():
        if snapshot.get(name):
            reasons.append(f"{label} · {snapshot[name]}")
    known = {
        # Two lock paths: the account-level cumulative reject streak and the
        # session-level money-proposal count; the old "consecutive" copy named only one.
        "kernel_rejections": tr("polish.safety.reason.kernel_rejections"),
        "red_injection": tr(
            "External instruction injection was detected during a red-tier operation."
        ),
        "breaker_injection": tr(
            "Repeated injection signals were detected within the observation window."
        ),
        "breaker_mfa": tr("Simulated MFA failed repeatedly."),
        "approval_rate": tr("Approval requests exceeded the safety rate limit."),
    }
    if snapshot.get("reason"):
        reasons.insert(
            0,
            known.get(
                snapshot["reason"],
                tr("The gateway recorded a lock reason; review the technical evidence."),
            ),
        )
    return {
        "state": snapshot["state"],
        "reasons": reasons,
        "rule_ids": snapshot.get("rule_ids", []),
        "unlock_after": snapshot.get("unlock_after"),
    }


def _render_snapshot(snapshot):
    view = safety_projection(snapshot)
    lamps = []
    for state, color, label in (
        ("Normal", "#178060", tr("Normal")),
        ("Watch", "#ad7411", tr("Watch")),
        ("Locked", "#be3850", tr("Locked")),
    ):
        active = state == view["state"]
        lamps.append(
            '<div style="flex:1;min-width:0;box-sizing:border-box;'
            "border-radius:14px;padding:18px 12px;"
            f"background:{color if active else '#f0edf5'};"
            f'color:{"white" if active else "#81788e"};text-align:center">'
            f'<div style="font-size:22px">{"●" if active else "○"}</div>'
            f"<strong>{escape(label)}</strong></div>"
        )
    st.markdown(
        '<div style="display:flex;gap:12px;margin:10px 0 18px">' + "".join(lamps) + "</div>",
        unsafe_allow_html=True,
    )
    if view["reasons"]:
        for reason in view["reasons"]:
            st.write("• " + reason)
    else:
        st.success(tr("No safety watch or lock has been triggered."))
    if view["rule_ids"] and view_mode.is_judge():
        st.caption(tr("Observed rules: ") + " · ".join(view["rule_ids"]))
    if view["unlock_after"]:
        st.caption(tr("polish.safety.unlock_after") + display_names.beijing(view["unlock_after"]))
    if view_mode.is_judge():
        with st.expander(tr("Technical evidence · safety signals and audit")):
            st.json(snapshot)


def render_safety(controller):
    st.header(tr("Safety lock"))
    st.caption(tr("Normal → Watch → Locked; green queries remain available while locked."))
    st.write(tr("Current banking session"))
    current = controller.gateway.safety_snapshot(controller.device.principal)
    _render_snapshot(current)
    st.info(
        tr(
            "Unlock requires the cooldown, owner device confirmation and simulated MFA. "
            "Old approvals stay void."
        )
    )
    if current["state"] == "Locked":
        if st.button(
            tr("Advance current banking clock through cooldown"), key="safety_current_cooldown"
        ):
            controller.advance_clock(max(controller.state.now, current["unlock_after"]))
            st.rerun()
        st.caption(tr("Confirm current session unlock on simulated owner device"))
        render_manual_unlock(controller, key="safety_current_unlock")
    typed = st.session_state.pop("safety_current_unlock:result", None)
    if typed:
        st.session_state["safety_current_result"] = {"controller": controller, "result": typed}
    saved = st.session_state.get("safety_current_result")
    if saved and saved["controller"] is controller:
        result = saved["result"]
        (st.success if result["status"] == "unlocked" else st.error)(
            tr("Unlocked; previous approvals remain void.")
            if result["status"] == "unlocked"
            else tr("Cooldown and valid device MFA are required.")
        )
        if view_mode.is_judge():
            with st.expander(tr("Technical evidence · unlock result")):
                st.json(result)
    if not view_mode.is_judge():
        return
    with st.expander(tr("Unlock method and technical evidence")):
        st.write(
            tr(
                "Wait for cooldown, then explicitly confirm the device unlock "
                "and complete simulated MFA on this page."
            )
        )
        st.code("uv run --frozen corridor unlock --state <state.jsonl> --audit <audit.jsonl>")
    st.subheader(tr("Isolated safety demonstration"))
    mode = st.selectbox(
        tr("Trigger"),
        ("reject", "injection", "mfa"),
        format_func=lambda value: tr(
            {
                "reject": "polish.safety.trigger.reject",
                "injection": "Two injection signals in ten minutes",
                "mfa": "Three failed MFA attempts",
            }[value]
        ),
        key="safety_mode",
    )
    if st.button(tr("Start isolated safety session"), key="safety_start"):
        st.session_state["safety_demo"] = create_safety_demo(mode)
    demo = st.session_state.get("safety_demo")
    if not demo:
        return
    case = demo["case"]
    snapshot = case.gateway.safety_snapshot(case.device.principal)
    _render_snapshot(snapshot)
    if st.button(
        tr("Submit one simulated suspicious signal"),
        key="safety_signal",
        disabled=snapshot["state"] == "Locked",
    ):
        trigger_signal(demo)
        st.rerun()
    if snapshot["state"] == "Locked":
        st.error(tr("Locked: yellow and red actions are blocked. Green queries remain available."))
        if st.button(tr("Advance simulated clock through cooldown"), key="safety_cooldown"):
            case.gateway.advance_clock(
                add_seconds(case.state.now, case.state.policy.unlock_cooldown_seconds)
            )
            st.rerun()
        if st.button(tr("Confirm unlock on simulated owner device"), key="safety_unlock"):
            demo["unlock_result"] = unlock_demo(demo)
            st.rerun()
    if demo.get("unlock_result"):
        result = demo["unlock_result"]
        (st.success if result["status"] == "unlocked" else st.error)(
            tr("Unlocked; previous approvals remain void.")
            if result["status"] == "unlocked"
            else tr("Cooldown and valid device MFA are required.")
        )
        with st.expander(tr("Technical evidence · demonstration unlock result")):
            st.json(result)
