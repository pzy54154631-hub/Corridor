"""Explicit simulated-device SMS entry; codes never enter chat or audit payloads.

The code reaches the separate simulated phone page as a synthetic SMS copy; the
main banking page shows only that it was sent, a link and the entry field.
"""

import streamlit as st

from corridor.crypto.jcs import H
from corridor.ui import phone
from corridor.ui.messages import tr


def _perform(call):
    try:
        return call()
    except (ValueError, RuntimeError, KeyError):
        st.error(
            tr("The operation could not complete. Review the current facts and inputs, then retry.")
        )
        return None


def _challenge_active(controller, challenge_id):
    """Read the local verifier for display only; never reset attempts or issue a grant."""
    verifier = controller.gateway.mfa
    with verifier._lock:
        challenge = verifier._challenges.get(challenge_id)
        return bool(
            challenge
            and not challenge.closed
            and challenge.issued_at <= controller.state.now < challenge.expiry
        )


def _expired_notice():
    st.warning(tr("The code has expired or is locked. Cancel and start again."))


def submit_manual_mfa(controller, pending_id, field, result_key, before=None):
    """Clear the transient widget value before invoking the real verifier.

    ``before`` runs first (S6: the chat page syncs its recipient check from the widgets
    drawn with this form, so the controller judges the answers of the same batch).
    """
    if before is not None:
        before()
    code = st.session_state.get(field, "")
    st.session_state[field] = ""
    result = (
        _perform(lambda: controller.complete_mfa(pending_id, code=code))
        if code
        else {"status": "waiting_mfa", "rule_ids": []}
    )
    # The outcome belongs to this pending action only; a later card must never read it.
    st.session_state[result_key] = {**result, "pending_id": pending_id} if result else result
    if result and result.get("status") == "mfa_verified":
        phone.retire(controller._pending.get(pending_id, {}).get("challenge_id"), "used")


def render_manual_mfa(controller, pending_id, *, key, before=None):
    pending = controller._pending.get(pending_id)
    if not pending or pending.get("mode") != "prepared":
        return None
    stored = st.session_state.get(f"{key}:result")
    if isinstance(stored, dict) and stored.get("pending_id") not in (None, pending_id):
        # Chat reuses one form key for every card: an earlier action's "verified" outcome
        # would otherwise be read as this action's and skip its typed code.
        st.session_state.pop(f"{key}:result", None)
    if pending.get("verified"):
        # Consumed here: the verified state lives on the pending action itself.
        st.session_state.pop(f"{key}:result", None)
        phone.retire(pending.get("challenge_id"), "used")
        st.success(
            tr("Simulated code verified; canonical approval is still required before dispatch.")
        )
        return {"status": "mfa_verified", "pending_id": pending_id}
    challenge = pending.get("challenge_id")
    if challenge and not _challenge_active(controller, challenge):
        phone.retire(challenge, "expired")
        _expired_notice()
        st.session_state.pop(f"{key}:result", None)
        return None
    if st.button(tr("Send simulated verification code"), key=f"{key}:request"):
        if before is not None:
            before()
        _perform(lambda: controller.request_mfa(pending_id))
        challenge = pending.get("challenge_id")
    if not challenge:
        return None
    if not _challenge_active(controller, challenge):
        phone.retire(challenge, "expired")
        _expired_notice()
        return None
    display = pending.get("display")
    card = getattr(display, "card", None) if display is not None else None
    phone.deliver(controller, challenge, kind=str((card or {}).get("kind", "")), card=card)
    phone.render_sent()
    field = f"{key}:code"
    result_key = f"{key}:result"
    with st.form(f"{key}:form", clear_on_submit=True):
        st.text_input(
            tr("Type the simulated verification code"),
            key=field,
            max_chars=6,
            autocomplete="one-time-code",
        )
        st.caption(tr("polish.phone.autosubmit_hint"))
        st.form_submit_button(
            tr("Submit simulated verification code"),
            key=f"{key}:verify",
            on_click=submit_manual_mfa,
            args=(controller, pending_id, field, result_key, before),
        )
    phone.autosubmit()
    result = st.session_state.pop(result_key, None)
    if result:
        if result["status"] == "mfa_verified":
            st.success(tr("Simulated verification passed. Review and approve the canonical card."))
        elif result["status"] == "waiting_mfa":
            st.warning(tr("Type the code displayed by the simulated device."))
        else:
            st.error(
                tr(
                    "The code is incorrect or expired. The existing verifier "
                    "and safety lock record failed attempts."
                )
            )
        return result
    return None


def submit_wealth_mfa(controller, identifier, field):
    code = st.session_state.get(field, "")
    st.session_state[field] = ""
    result = (
        _perform(lambda: controller.verify_leg(identifier, code=code))
        if code
        else {"status": "waiting_mfa", "rule_ids": []}
    )
    st.session_state["wealth_leg_result"] = result
    if result and result.get("status") == "mfa_verified":
        phone.retire((controller.leg or {}).get("challenge_id"), "used")


def render_wealth_mfa(controller, identifier):
    if controller.leg["verified"]:
        phone.retire(controller.leg.get("challenge_id"), "used")
        st.success(tr("The simulated code for this leg is verified."))
        return
    challenge = controller.leg.get("challenge_id")
    if not challenge:
        if st.button(tr("Send this leg a simulated code"), key=f"wealth_request:{identifier}"):
            _perform(lambda: controller.request_leg_mfa(identifier))
            challenge = controller.leg.get("challenge_id")
    if not challenge:
        return
    if not _challenge_active(controller, challenge):
        phone.retire(challenge, "expired")
        _expired_notice()
        if st.button(tr("Cancel remaining allocation"), key=f"wealth_cancel_mfa:{identifier}"):
            _perform(controller.withdraw_proposal)
            st.session_state.pop("wealth_leg_result", None)
            st.rerun()
        return
    st.caption(tr("Trusted simulated device: separate SMS verifier for each leg"))
    card = controller.leg.get("card")
    phone.deliver(controller, challenge, kind=str((card or {}).get("kind", "")), card=card)
    phone.render_sent()
    field = f"wealth_code:{identifier}"
    with st.form(f"wealth_mfa:{identifier}", clear_on_submit=True):
        st.text_input(
            tr("Type the simulated code for this leg"),
            key=field,
            max_chars=6,
            autocomplete="one-time-code",
        )
        st.caption(tr("polish.phone.autosubmit_hint"))
        st.form_submit_button(
            tr("Submit the simulated code for this leg"),
            key=f"wealth_verify:{identifier}",
            on_click=submit_wealth_mfa,
            args=(controller, identifier, field),
        )
    phone.autosubmit()


def submit_unlock_mfa(controller, field, challenge_key, result_key):
    """Typed code -> existing verifier -> existing signed unlock (mirrors `corridor unlock`)."""
    code = st.session_state.get(field, "")
    st.session_state[field] = ""
    challenge = st.session_state.get(challenge_key)
    if not code or not challenge:
        st.session_state[result_key] = {"status": "waiting_mfa", "rule_ids": []}
        return
    st.session_state[result_key] = _perform(
        lambda: _verify_unlock(controller, code, challenge, challenge_key)
    )


def _verify_unlock(controller, code, challenge, challenge_key):
    gateway, principal = controller.gateway, controller.device.principal
    grant = gateway.mfa.verify(challenge, code, now=controller.state.now)
    if grant is None:
        return {"status": "rejected", "rule_ids": ["CTRL-MFA"]}
    phone.retire(challenge, "used")
    st.session_state.pop(challenge_key, None)
    scope = gateway.unlock_scope(principal)
    confirmation = controller.device.confirm_input(
        intent_id=H(scope), slot="unlock", value=scope, now=controller.state.now
    )
    result = gateway.unlock(principal, confirmation, grant)
    return {
        "status": result.status,
        "rule_ids": result.rule_ids,
        "audit_checkpoint": gateway.audit.checkpoint(),
    }


def request_unlock_code(controller, challenge_key, result_key, error_key):
    """Button callback: runs before the page, so the request button is gone in that run."""
    try:
        challenge = controller.gateway.request_unlock_mfa(controller.device.principal)
    except (ValueError, RuntimeError, KeyError):
        challenge = None
        st.session_state[error_key] = True
    if challenge is None:
        st.session_state[result_key] = {"status": "rejected", "rule_ids": ["CTRL-MFA"]}
    st.session_state[challenge_key] = challenge


def render_manual_unlock(controller, *, key):
    """Two explicit gestures: request a simulated code, then type it."""
    challenge_key, result_key = f"{key}:challenge", f"{key}:result"
    error_key = f"{key}:error"
    if st.session_state.pop(error_key, False):
        st.error(
            tr("The operation could not complete. Review the current facts and inputs, then retry.")
        )
    challenge = st.session_state.get(challenge_key)
    if not challenge:
        # S4: an on_click callback (not `if st.button`) so the run triggered by the click
        # already renders the code field without the request button (screenshot 45).
        st.button(
            tr("Send simulated unlock code"),
            key=f"{key}:request",
            on_click=request_unlock_code,
            args=(controller, challenge_key, result_key, error_key),
        )
        return
    if st.button(tr("Cancel unlock verification"), key=f"{key}:cancel"):
        phone.retire(challenge, "expired")
        st.session_state.pop(challenge_key, None)
        st.session_state.pop(result_key, None)
        st.session_state.pop(f"{key}:code", None)
        st.rerun()
    if not _challenge_active(controller, challenge):
        phone.retire(challenge, "expired")
        _expired_notice()
        st.caption(
            tr("A cancelled challenge keeps its original 120-second window and attempt limit.")
        )
        return
    phone.deliver(controller, challenge, kind="unlock")
    phone.render_sent()
    field = f"{key}:code"
    with st.form(f"{key}:form", clear_on_submit=True):
        st.text_input(
            tr("Type the simulated verification code"),
            key=field,
            max_chars=6,
            autocomplete="one-time-code",
        )
        st.caption(tr("polish.phone.autosubmit_hint"))
        st.form_submit_button(
            tr("Submit simulated verification code"),
            key=f"{key}:verify",
            on_click=submit_unlock_mfa,
            args=(controller, field, challenge_key, result_key),
        )
    phone.autosubmit()
