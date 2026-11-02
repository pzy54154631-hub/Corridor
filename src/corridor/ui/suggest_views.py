"""Model suggestions under a chat clarification, on the Streamlit pages (presentation only).

Each suggestion is the canonical sentence the session's desk wrote from a fixed template,
shown as a question with the fixed note. Choosing one asks the desk to check the sentence
again and sends it through the page's own submit, exactly as if it were typed; the reply,
its card and every device step are those of the typed sentence. The desk marks it adopted
only once the session received the sentence; a failed send leaves it offered and says so.
Nothing here writes, edits or signs anything, and the simulated IM channel only lists the
questions.
"""

import streamlit as st

from corridor.ui import view_mode
from corridor.ui.messages import tr

FINISHED = ("adopted", "discarded", "expired")
# What a send through the page's submit may raise (the page body's own failure classes): a
# button callback runs outside the page's error handling, so it is caught here.
SEND_FAILURES = (ValueError, RuntimeError, KeyError, TimeoutError, OSError)


def _error_key(prefix):
    return prefix + "_suggestion_error"


def _state(session, response_id, index):
    """The desk's state of one suggestion now (offered, adopted, discarded, expired)."""
    for item in session.suggestions.view(session, response_id):
        if item["index"] == index:
            return item["state"]
    return None


def adopt(session, response_id, index, prefix, submit, route):
    """The button's callback: the desk's sentence, sent as a typed message, or a notice.

    The notice follows the desk's own conclusion: a send that failed before the session
    received the sentence leaves it offered; one that failed after (the page's own
    rendering) says the sentence was received; a second click on a suggestion that was
    already sent shows nothing more.
    """
    st.session_state.pop(_error_key(prefix), None)
    try:
        sent, _ = session.suggestions.adopt(
            session,
            response_id,
            index,
            lambda sentence: submit(session, sentence, prefix, route=route),
        )
    except SEND_FAILURES:
        # Not received: nothing was adopted, the suggestion stays offered, the log says why.
        # Received (the desk adopted it) and then failed: the notice says it was sent.
        received = _state(session, response_id, index) == "adopted"
        notice = "suggest.sent_then_failed" if received else "suggest.send_failed"
        st.session_state[_error_key(prefix)] = (response_id, notice)
        return
    if not sent and _state(session, response_id, index) != "adopted":
        st.session_state[_error_key(prefix)] = (response_id, "suggest.error")


def judge_lines(session, response):
    """Judge view: what the model named, how the sentence was written, and the events."""
    payload = response["payload"].get("suggestions") or {}
    shown = [
        tr("suggest.local.source" if payload.get("source") == "local" else "suggest.judge.source")
    ]
    for item in payload.get("items", []):
        shown.append(
            tr(
                "suggest.judge.item",
                number=item["index"] + 1,
                intent=item["intent"],
                contact=item["contact_ref"] or "—",
                amount=item["amount"] or "—",
            )
        )
    desk = session.suggestions
    for record in desk.log.records(response["response_id"]):
        shown.append(
            tr(
                "suggest.judge.event",
                seq=record["seq"],
                event=record["event"],
                reason=desk.reason(record),
                hash=record["record_hash"][:16],
            )
        )
    return shown


def render_suggestions(session, response, prefix, *, im=False, submit=None, route=True):
    """The reply's suggestions as buttons (or plain lines in IM); False when there are none."""
    desk = getattr(session, "suggestions", None)
    identifier = response["response_id"]
    items = desk.view(session, identifier) if desk is not None else []
    if not items:
        return False
    for item in items:
        label = tr("suggest.question", sentence=item["sentence"])
        if im or submit is None:
            st.write(label)
        else:
            st.button(
                label,
                key=f"{prefix}_suggest:{identifier}:{item['index']}",
                disabled=item["state"] != "offered",
                on_click=adopt,
                args=(session, identifier, item["index"], prefix, submit, route),
            )
        if item["state"] in FINISHED:
            st.caption(tr("suggest.state." + item["state"]))
    st.caption(
        tr(
            "suggest.local.note"
            if response["payload"].get("suggestions", {}).get("source") == "local"
            else "suggest.note"
        )
    )
    notice = st.session_state.get(_error_key(prefix))
    if isinstance(notice, tuple) and len(notice) == 2 and notice[0] == identifier:
        st.error(tr(notice[1]))
    if view_mode.is_judge():
        for line in judge_lines(session, response):
            st.caption(line)
    return True
