"""Localized transfer presentation backed by the existing device approval workflow."""

from datetime import timedelta
from types import SimpleNamespace

import streamlit as st

from corridor.crypto.jcs import H
from corridor.schemas.time import parse_time
from corridor.ui.components import reference_label, render_card, render_result

# S6 R7: the structured preview asks the chat's two recipient questions too.
from corridor.ui.domestic_views import render_scam_gate, scam_sync_before_gesture
from corridor.ui.messages import tr

# The preparers live in a Streamlit-free module so chat calls the same function objects.
from corridor.ui.operations import (
    _choices,
    preview_scheduled_transfer,
    preview_structured_transfer,
)

GATE_KEY = "structured_scam"


def render_structured_transfer(controller):
    st.subheader(tr("Transfer with explicit device fields"))
    st.caption(
        tr("Select a verified recipient and enter the exact amount. Previewing signs nothing.")
    )
    try:
        accounts, contacts = _choices(controller)
    except (ValueError, RuntimeError, KeyError):
        st.warning(
            tr("Your session or account changed. Sign in again before preparing a transfer.")
        )
        return
    with st.form("structured_transfer_form"):
        source = st.selectbox(
            tr("Source account"),
            [None, *accounts],
            format_func=lambda value: (
                tr("Choose an account")
                if value is None
                else reference_label(value, controller=controller)
            ),
            key="structured_source",
            placeholder=tr("Choose an account"),
        )
        contact = st.selectbox(
            tr("Verified recipient"),
            [None, *contacts],
            format_func=lambda value: (
                tr("Choose a recipient")
                if value is None
                else reference_label(value, controller=controller)
            ),
            key="structured_contact",
            placeholder=tr("Choose a recipient"),
        )
        amount = st.text_input(tr("Transfer amount (CNY)"), key="structured_amount")
        timing = st.radio(
            tr("Execution time"),
            ("now", "once", "monthly"),
            key="structured_timing",
            format_func=lambda value: tr(
                {"now": "Immediately", "once": "On a date", "monthly": "Monthly"}[value]
            ),
            horizontal=True,
        )
        date_text = st.text_input(
            tr("Execution date / monthly authorization start (UTC+8 YYYY-MM-DD)"),
            value=(parse_time(controller.state.now) + timedelta(days=1, hours=8)).strftime(
                "%Y-%m-%d"
            ),
            key="structured_date",
        )
        monthly_day = st.number_input(
            tr("Day of month (1-28)"), min_value=1, max_value=28, value=20, key="structured_day"
        )
        st.caption(
            tr(
                "Scheduled payments support the current account. Monthly mode permits "
                "up to 3 runs over 90 days, requires red-tier MFA at creation, and is "
                "rechecked by the gateway when due."
            )
        )
        submitted = st.form_submit_button(tr("Preview transfer"), key="structured_preview")
    if submitted:
        fingerprint = H({
            "source": source,
            "contact": contact,
            "amount": amount,
            "timing": timing,
            "date": date_text,
            "day": monthly_day,
        })
        previous = st.session_state.get("structured_transfer_request")
        if previous is None or previous["fingerprint"] != fingerprint:
            previous = {"fingerprint": fingerprint, "id": controller._id("structured-transfer")}
            st.session_state["structured_transfer_request"] = previous
        try:
            pending = (
                preview_structured_transfer(controller, source, contact, amount, previous["id"])
                if timing == "now"
                else preview_scheduled_transfer(
                    controller,
                    source,
                    contact,
                    amount,
                    previous["id"],
                    date_text=date_text,
                    monthly_day=int(monthly_day) if timing == "monthly" else None,
                )
            )
        except (ValueError, RuntimeError, KeyError):
            st.warning(
                tr(
                    "No transfer was prepared. Select an account, recipient and positi"
                    "ve whole-cent amount."
                )
            )
            st.session_state.pop("structured_transfer_pending", None)
            st.session_state.pop("structured_transfer_result", None)
        else:
            st.session_state["structured_transfer_pending"] = pending
            st.session_state.pop("structured_transfer_result", None)
    pending = st.session_state.get("structured_transfer_pending")
    if pending is None:
        return
    identifier = pending["pending_id"]
    if identifier not in controller._pending:
        st.info(tr("This transfer draft was withdrawn. Prepare a new draft to continue."))
        return
    result = controller._results.get(identifier, st.session_state.get("structured_transfer_result"))
    render_card(pending["card"], canonical=True, controller=controller)
    if identifier not in controller._results:
        # S6 R7: a payment to a new payee answers the same
        # two questions as in chat before the device gesture; the controller refuses the
        # gesture while the check does not allow it. No words here, so only the payee's
        # registration grades it; a known recipient draws nothing and is unchanged.
        gate = render_scam_gate(
            SimpleNamespace(controller=controller),
            pending["card"],
            identity=identifier,
            hint_text=None,
            external_files=0,
            key=GATE_KEY,
            origin={"prefix": "structured", "response_id": identifier, "route": "transfers"},
        )
        if gate == "proceed" and st.button(
            tr("Confirm transfer on device"), key="structured_approve", type="primary"
        ):
            scam_sync_before_gesture(controller, GATE_KEY)
            try:
                result = controller.approve_pending(identifier)
            except (ValueError, RuntimeError, KeyError):
                st.warning(
                    tr(
                        "The transfer result could not be confirmed. Check Evidence & Audi"
                        "t before retrying."
                    )
                )
                return
            st.session_state["structured_transfer_result"] = result
            if result.get("replacement"):
                st.session_state["structured_transfer_pending"] = result["replacement"]
                st.rerun()
        if pending["card"].get("tier") == "red":
            st.caption(
                tr(
                    "Confirm this intent, then open Device for independent simulated M"
                    "FA and approval."
                )
            )
        st.caption(tr("This cancels every pending task in this session."))
        if st.button(tr("Cancel pending tasks"), key="structured_cancel"):
            cancel_structured_transfer(controller)
            st.rerun()
    render_result(result)
    if identifier in controller._results and st.button(
        tr("Prepare another transfer"), key="structured_new"
    ):
        for key in (
            "structured_transfer_pending",
            "structured_transfer_result",
            "structured_transfer_request",
        ):
            st.session_state.pop(key, None)
        st.rerun()


def cancel_structured_transfer(controller):
    """Use the existing session stop operation and retire only presentation state here."""
    result = controller.stop()
    for key in (
        "structured_transfer_pending",
        "structured_transfer_result",
        "structured_transfer_request",
    ):
        st.session_state.pop(key, None)
    return result
