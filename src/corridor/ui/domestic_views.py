"""Banking chat and simulated IM: presentation only, explicit device gestures."""

from html import escape

import streamlit as st

from corridor.agent.domestic_session import DomesticSession
from corridor.channels.im import IMAdapter
from corridor.i18n import get_locale
from corridor.ui import display_names, live_controls, scam_friction, scam_gate, view_mode
from corridor.ui.business_views import (
    beijing_time,
    remark_notice,
    render_subscription_result,
    render_transaction_rows,
)
from corridor.ui.components import (
    KINDS,
    STATUSES,
    money,
    reference_label,
    render_card,
    render_contact_evidence,
    render_dag,
    render_plans,
    render_result,
    schedule_object,
    subscription_object,
)
from corridor.ui.dag import NODE_STYLES, draft_snapshot
from corridor.ui.messages import source_utterance, tr
from corridor.ui.suggest_views import render_suggestions

EXAMPLES = (
    ("transfer", "Transfer 800 yuan to mom"),
    ("billing", "Review this month's bills"),
    ("wealth", "Help me invest 50,000 CNY"),
    ("cards", "I lost my card"),
    ("subscriptions", "Show my subscriptions"),
    ("birthday", "Plan a birthday"),
)


def session_for(controller):
    session = st.session_state.get("domestic_session")
    if session is None or session.controller is not controller:
        # The chat page shows model suggestions and sends the chosen one as a message.
        session = DomesticSession(
            controller, st.session_state.get("wealth_controller"), suggest=True
        )
        st.session_state["domestic_session"] = session
    return session


def open_route(route):
    st.session_state.pop("conversation_route", None)
    st.session_state.pop("page", None)
    st.session_state["active_page"] = route


def birthday_event_line(controller, event):
    """One display line of trusted calendar facts; never an authorization input."""
    return tr(
        "birthday.event_fact",
        event=event["event_ref"],
        contact=reference_label(event["contact_ref"], controller=controller),
        relation=tr("birthday.relation." + str(event.get("relation") or "none")),
        date=event["event_date"],
        due=beijing_time(event["order_due"]),
        month=event["lock_month"],
    )


def choose_birthday(controller, event_ref, origin):
    """Preview only the chosen trusted event, unsigned, on the task-plan page."""
    st.session_state.pop("birthday_choice_error", None)
    try:
        draft = controller.preview_birthday(
            event_ref=event_ref, request_id=f"{origin}:birthday:{event_ref}"
        )
    except (ValueError, RuntimeError, KeyError):
        draft = None
    if not draft or draft.get("status") != "draft":
        st.session_state["birthday_choice_error"] = True
        return
    st.session_state.pop("birthday_preview", None)
    st.session_state.pop("plan_result", None)
    st.session_state["birthday_draft"] = draft
    st.session_state["birthday_event_ref"] = event_ref
    open_route("birthday")


def birthday_choices(session, response, prefix, *, im=False):
    """List trusted calendar birthdays when chat could not select exactly one."""
    payload = response["payload"]
    candidates = payload.get("candidates") or []
    if response["intent"] != "birthday" or response["status"] != "clarify":
        return False
    if payload.get("birthday_status") == "outside_lock_month" and payload.get("event"):
        st.caption(birthday_event_line(session.controller, payload["event"]))
        return True
    if not candidates:
        return False
    st.caption(tr("birthday.calendar_candidates"))
    for item in candidates:
        line = birthday_event_line(session.controller, item)
        if im or not item.get("lock_window_open"):
            st.write(line)
            continue
        st.button(
            line,
            key=f"{prefix}_birthday:{response['response_id']}:{item['event_ref']}",
            on_click=choose_birthday,
            args=(session.controller, item["event_ref"], response["response_id"]),
        )
    if st.session_state.get("birthday_choice_error"):
        st.error(
            tr("The operation could not complete. Review the current facts and inputs, then retry.")
        )
    return True


def choose_subscription(session, response_id, ref, prefix, label):
    """B1: one listed authorization; its form's own preparer builds the card in chat."""
    st.session_state.pop(prefix + "_subscription_error", None)
    try:
        chosen = session.prepare_subscription_cancel(response_id, ref)
    except (ValueError, KeyError, RuntimeError):
        st.session_state[prefix + "_subscription_error"] = True
        return
    st.session_state[prefix + "_response_id"] = chosen["response_id"]
    st.session_state.setdefault(prefix + "_transcript", []).append({
        "text": tr("chat.subscription.chosen", merchant=label),
        "response_id": chosen["response_id"],
    })


def subscription_choices(session, response, prefix, *, im=False):
    """B1: the customer's own cancellable authorizations, listed when chat named none."""
    payload = response["payload"]
    choices = payload.get("subscription_choices") or []
    if response["intent"] != "subscription_cancel" or response["status"] != "clarify":
        return False
    if not choices:
        return False
    controller = session.controller
    for ref in choices:
        item = controller.state.subscriptions.get(ref)
        if item is None or item.status != "active":
            continue
        # C1 (R-10): the button reads exactly like the card's object row, ordinal included,
        # so twins (same merchant, amount and period) never share a label.
        label = subscription_object(ref, controller=controller) or str(ref)
        if im:
            st.write(label)
            continue
        st.button(
            label,
            key=f"{prefix}_subscription:{response['response_id']}:{ref}",
            on_click=choose_subscription,
            args=(session, response["response_id"], ref, prefix, label),
        )
    if st.session_state.get(prefix + "_subscription_error"):
        st.error(
            tr("The operation could not complete. Review the current facts and inputs, then retry.")
        )
    return True


def choose_schedule_cancel(session, response_id, ref, prefix, label):
    """B2: one listed scheduled authorization; its revocation card follows in chat."""
    st.session_state.pop(prefix + "_schedule_cancel_error", None)
    try:
        chosen = session.prepare_schedule_cancel(response_id, ref)
    except (ValueError, KeyError, RuntimeError):
        st.session_state[prefix + "_schedule_cancel_error"] = True
        return
    st.session_state[prefix + "_response_id"] = chosen["response_id"]
    st.session_state.setdefault(prefix + "_transcript", []).append({
        "text": tr("chat.schedule_cancel.chosen", label=label),
        "response_id": chosen["response_id"],
    })


def schedule_cancel_choices(session, response, prefix, *, im=False):
    """B2: the customer's own scheduled authorizations with runs ahead, one button each."""
    standing = response["payload"].get("schedule_cancel")
    if response["status"] != "clarify" or not isinstance(standing, dict):
        return False
    candidates = standing.get("candidates") or []
    if not candidates:
        return False
    controller = session.controller
    state = controller.state
    for ref in candidates:
        spa = state.authorizations.get(ref)
        if spa is None:
            continue
        runs = sum(
            1
            for task in state.schedules.values()
            if task.spa_ref == ref and task.status in {"pending", "due", "waiting_mfa"}
        )
        # C1 (R-10): the card's own object row (ordinal included), then the runs ahead.
        label = tr(
            "chat.schedule_cancel.choice_object",
            object=schedule_object(ref, controller=controller) or str(ref),
            runs=runs,
        )
        if im:
            st.write(label)
            continue
        st.button(
            label,
            key=f"{prefix}_schedule_cancel:{response['response_id']}:{ref}",
            on_click=choose_schedule_cancel,
            args=(session, response["response_id"], ref, prefix, label),
        )
    if st.session_state.get(prefix + "_schedule_cancel_error"):
        st.error(
            tr("The operation could not complete. Review the current facts and inputs, then retry.")
        )
    return True


_OBJECTIVES = {
    "net_return": "Net performance benchmark (not guaranteed)",
    "total_fee": "Total fees",
    "liquidity": "Liquidity",
}


def purchase_review(session, response):
    """B1: a reviewed purchase before its device gesture; not a canonical card.

    C1 (R-10): every preference the gesture signs is listed, from the signed snapshot the
    session bound to this reply (the confirm refuses terms that changed after review).
    """
    purchase = response["payload"].get("purchase")
    if response["status"] != "awaiting_input" or not isinstance(purchase, dict):
        return False
    controller = session.controller
    terms = response["payload"].get("purchase_terms") or {}
    with st.container(border=True):
        st.caption(tr("chat.purchase.title"))
        st.write(
            tr(
                "chat.purchase.product_line",
                product=reference_label(purchase["product_ref"], controller=controller),
            )
        )
        st.write(tr("chat.purchase.amount_line", amount=money(purchase["amount"])))
        st.write(
            tr(
                "chat.purchase.source_line",
                account=reference_label(
                    terms.get("source_account_ref", purchase["source_ref"]), controller=controller
                ),
            )
        )
        if terms:
            st.write(tr("chat.purchase.horizon_line", days=terms.get("horizon_days")))
            st.write(tr("chat.purchase.access_line", days=terms.get("max_access_days")))
            st.write(
                tr(
                    "chat.purchase.types_line",
                    types=tr("chat.purchase.types.wealth")
                    if terms.get("allowed_types") == ["wealth"]
                    else ", ".join(str(item) for item in terms.get("allowed_types") or []),
                )
            )
        st.caption(
            tr(
                "chat.purchase.objective_line_full" if terms else "chat.purchase.objective_line",
                objective=tr(
                    _OBJECTIVES.get(
                        purchase.get("objective"), "Net performance benchmark (not guaranteed)"
                    )
                ),
            )
        )
    return True


def purchase_candidates(session, response):
    """B1: the catalog products a purchase request could still mean (display only)."""
    parse = response["payload"].get("purchase_parse") or {}
    candidates = parse.get("candidates") or []
    if response["intent"] != "investment" or not candidates:
        return False
    names = "、" if get_locale() == "zh-CN" else ", "
    st.caption(
        tr(
            "chat.purchase.candidates",
            names=names.join(
                reference_label(ref, controller=session.controller) for ref in candidates
            ),
        )
    )
    return True


def submit_request(session, text, prefix, *, route=True, display_text=None, external_files=0):
    # An attached external file never reaches the session, a prompt or the audit: only
    # the count is kept, for the card's display-only reminder (R-e), and passed to the
    # session, which stops offering suggestions once a turn carried one (SG-6).
    mark = live_controls.operation_mark(session.controller)
    response = session.receive(text, external_files=external_files)
    # S7: what this request's live calls produced is bound to its reply (no-op scripted).
    live_controls.remember(prefix, response["response_id"], mark)
    st.session_state[prefix + "_response_id"] = response["response_id"]
    transcript = st.session_state.setdefault(prefix + "_transcript", [])
    transcript.append({
        "text": display_text or session.controller.gateway.vault.tokenize(text),
        "response_id": response["response_id"],
        **({"external_files": external_files} if external_files else {}),
    })
    draft = response["payload"].get("replacement", response["payload"])
    if draft.get("nodes") and draft.get("plan_id") in session.controller._plans:
        session.controller._plans[draft["plan_id"]]["source_text"] = transcript[-1]["text"]
        st.session_state["birthday_draft"] = draft
        if draft.get("event"):
            # The task-plan button re-previews the event chat selected, never another one.
            st.session_state["birthday_event_ref"] = draft["event"]["event_ref"]
    if route:
        st.session_state["conversation_route"] = response["route"]
        st.session_state["next_page"] = response["route"]


def shortcuts(session, prefix, *, fallback=False, route=True):
    if fallback:
        st.caption(tr("I can help with these banking services"))
    with st.container(key=f"{prefix}-quick-{'fallback' if fallback else 'examples'}"):
        columns = st.columns(2 if fallback else 3)
        for index, (kind, prompt) in enumerate(EXAMPLES):
            with columns[index % len(columns)]:
                if st.button(
                    tr(prompt),
                    key=f"{prefix}_{'fallback' if fallback else 'example'}_{kind}",
                    use_container_width=True,
                ):
                    submit_request(
                        session,
                        source_utterance(prompt),
                        prefix,
                        route=route,
                        display_text=tr(prompt),
                    )
                    st.rerun()
        if not fallback:
            # S6 demo preset: one click sends a zh-q-01 style induced-payment message to
            # a new recipient; the scripted planner answers it with the same red card and
            # the same recipient check every time.
            if st.button(
                tr("polish.scam.drill_label"),
                key=f"{prefix}_example_scam_drill",
                use_container_width=True,
            ):
                submit_request(
                    session,
                    source_utterance("polish.scam.drill_utterance"),
                    prefix,
                    route=route,
                    display_text=tr("polish.scam.drill_utterance"),
                )
                st.rerun()
            caption = tr("polish.scam.drill_caption")
            if view_mode.is_judge():
                caption += tr("polish.scam.drill_caption_judge")
            st.caption(caption)


def response_copy(response, *, completion=False, hint_text=None):
    status = response["status"]
    payload = response["payload"]
    if payload.get("stop_receipt") or payload.get("compensation"):
        # The session's receipt names each executed and unexecuted node; show it as is.
        return response["message"]
    if status in {"clarify", "awaiting_device_selection"}:
        if response["intent"] == "birthday" and response["payload"].get("birthday_status"):
            return tr(
                "birthday.outside_lock_month"
                if response["payload"]["birthday_status"] == "outside_lock_month"
                else "birthday.selection_required"
            )
        # B1: the chat asks for the purchase slot it still needs, or which holding or
        # authorization the request names.
        purchase = payload.get("purchase_parse")
        if response["intent"] == "investment" and isinstance(purchase, dict):
            missing = purchase.get("missing") or []
            if missing == ["amount"]:
                return tr("chat.purchase.amount")
            if purchase.get("product") == "deictic":
                return tr("chat.purchase.deictic")
            return tr("chat.purchase.product" if missing == ["product"] else "chat.purchase.both")
        # C1: a question, supposition, past statement or time word on a purchase,
        # redemption or cancellation is answered in chat; nothing was prepared.
        if isinstance(payload.get("mood_hold"), dict):
            return tr("chat.mood_hold")
        if isinstance(payload.get("time_hold"), dict):
            return tr(
                "chat.time_hold.subscription"
                if payload["time_hold"].get("service") == "subscription_cancel"
                else "chat.time_hold.wealth"
            )
        if payload.get("terms_changed"):
            return tr("chat.purchase.changed")
        redemption = payload.get("redemption_parse")
        if response["intent"] == "investment" and isinstance(redemption, dict):
            if redemption.get("status") == "quantity":
                return tr(
                    "chat.redeem.quantity_over"
                    if redemption.get("reason") == "over"
                    else "chat.redeem.quantity"
                )
            return tr(
                "chat.redeem.none"
                if redemption.get("status") == "none"
                else "chat.redeem.no_match"
                if redemption.get("status") == "no_match"
                else "chat.redeem.choose"
            )
        if response["intent"] == "subscription_cancel" and "subscription_choices" in payload:
            parse = payload.get("subscription_parse") or {}
            return tr(
                "chat.subscription.none"
                if parse.get("status") == "none"
                else "chat.subscription.no_match"
                if parse.get("status") == "no_match"
                else "chat.subscription.choose"
            )
        if response["intent"] == "investment":
            return tr(
                "Let's confirm your risk profile and time horizon before reviewing a proposal."
            )
        standing = payload.get("schedule_cancel")
        if isinstance(standing, dict) and not payload.get("scam_phrase"):
            # B2: two statements, then the choice: no new payment or card from this message,
            # and the existing scheduled authorization has not been cancelled.
            return tr(
                "chat.schedule_cancel.choose"
                if standing.get("candidates")
                else "chat.schedule_cancel.none"
            )
        chat_copy = transfer_parse_copy(response["payload"], completion=completion)
        if chat_copy:
            return chat_copy
        if response["payload"].get("contacts"):
            return tr("More than one contact matches. Choose the recipient, then review the card.")
        return tr("Please specify the person, amount or date, or choose them in the service page.")
    if status == "refused":
        return tr(
            "Sorry, that request is outside our banking services. "
            "I can help with transfers, bills, investments and more."
        )
    if status == "withdrawn":
        return tr("This proposal was cancelled. Start a new request when you are ready.")
    if status == "waiting_mfa":
        return tr("Complete simulated verification, then confirm this operation.")
    if status == "mfa_verified":
        return tr("Simulated verification is complete. Review and confirm once more.")
    if status == "settled":
        return tr("A bank receipt is available. Review the result below.")
    if status == "controlled" and (response.get("card") or {}).get("kind") == "cancel_schedule":
        # B2: a revocation is a control too; it must not read as a new authorization.
        return tr("chat.schedule_cancel.done")
    if status == "controlled":
        return tr("chat.schedule.controlled")
    if status == "rejected":
        # STATUSES has no "rejected" entry, so this used to print the raw English status;
        # R6 reports a kernel Reject right after the first confirmation, so say it plainly.
        return tr("chat.rejected")
    if status == scam_friction.STATUS:
        # S6: a gesture reached the controller while the recipient check was paused,
        # still unanswered or answered for another card. Future tense only: a challenge
        # issued earlier is kept.
        if payload.get("reason") == scam_friction.REASON_INCOMPLETE:
            return tr("polish.scam.incomplete_copy")
        if payload.get("reason") == scam_gate.REASON_STALE:
            return tr("polish.scam.stale_copy")
        return tr("polish.scam.paused_copy")
    if status in {"failed", "unknown", "interrupted", "handed_off"}:
        return tr(STATUSES.get(status, status))
    if status == "awaiting_input" and isinstance(payload.get("purchase"), dict):
        # B1: a reviewed purchase has no canonical card until the device gesture.
        return tr("chat.purchase.review")
    # S6 R7: words with an authority or external cue no longer open with "ready for review"
    # but with the anti-fraud opening; the recipient check follows below the card.
    codes = scam_friction.opening_cues(response.get("card"), hint_text)
    if codes:
        return scam_friction.opening(codes)
    return tr("Your request is ready for review.")


def transfer_parse_copy(payload, *, completion=False):
    """Explain why a dated or split-bill request has no card; never a new authority."""
    scam = payload.get("scam_phrase")
    if isinstance(scam, dict) and scam.get("authority") and scam.get("pressure"):
        # S6 R7: the phrase gate held it; the labels are fixed lexicon words.
        separator = tr("polish.scam.phrase_separator")
        return tr(
            "polish.scam.phrase_gate",
            authority=separator.join(scam["authority"]),
            pressure=separator.join(scam["pressure"]),
        )
    if payload.get("negation"):
        # S5: the request carried a negation or cancel word, so no card was prepared.
        return tr("chat.negated")
    if payload.get("echo_mismatch"):
        return tr("chat.card_mismatch")
    aa = payload.get("aa_parse") or {}
    if (
        completion
        and aa.get("status") == "unresolved"
        and aa.get("reason") in {"participants", "ambiguous"}
        and isinstance(payload.get("aa"), dict)
    ):
        # S4: the chat page offers the roster completion card right below; do not send
        # the customer to the service page in the same breath.
        return tr("chat.aa.complete_prompt")
    if aa.get("status") == "unresolved":
        return tr("chat.aa.ambiguous" if aa.get("reason") == "ambiguous" else "chat.aa.unresolved")
    schedule = payload.get("schedule_parse") or {}
    if schedule.get("status") == "unresolved":
        return tr(
            {
                "monthly_day_unsupported": "chat.schedule.monthly_day",
                "residual_time": "chat.schedule.when",
                "number_not_date": "chat.schedule.number_not_date",
            }.get(schedule.get("reason"), "chat.schedule.unresolved")
        )
    return None


def parse_note(session, payload):
    """The parser's reading of the request, shown beside the card and never as its facts."""
    schedule = payload.get("schedule_echo") or {}
    aa = payload.get("aa_parse") or {}
    if schedule.get("status") == "complete":
        period = (
            tr("chat.parse_note.monthly", day=schedule["monthly_day"])
            if schedule.get("monthly_day")
            else tr("chat.parse_note.once")
        )
        text = tr(
            "chat.parse_note.schedule",
            phrase=schedule["phrase"],
            date=schedule["date_text"],
            period=period,
        )
    elif aa.get("status") == "complete" and payload.get("card"):
        text = tr(
            "chat.parse_note.aa",
            people=aa["people"],
            total=money(aa["total"]),
            per_person=money(aa["per_person"]),
            collect=money(aa["collect_total"]),
            participants="、".join(
                reference_label(ref, controller=session.controller) for ref in aa["participants"]
            ),
        )
    else:
        return
    if not view_mode.is_judge():
        # The parser's reading is evidence about the card, not one of its facts.
        return
    with st.container(border=True):
        st.caption(tr("chat.parse_note.title"))
        st.caption(text)


def aa_completion_card(session, response, prefix):
    """Choose the remaining split-bill participants; the AA form's preparer follows."""
    payload = response["payload"]
    split = payload.get("aa")
    parse = payload.get("aa_parse") or {}
    if (
        response["status"] != "awaiting_device_selection"
        or not isinstance(split, dict)
        or parse.get("status") != "unresolved"
        or parse.get("reason") not in {"participants", "ambiguous"}
    ):
        return False
    controller = session.controller
    identifier = response["response_id"]
    needed = split["participants"] - 1
    st.caption(
        tr(
            "chat.aa.complete_title",
            total=money(payload["parameters"]["amount"]),
            people=split["participants"],
            per_person=money(split["per_person"]),
            needed=needed,
        )
    )
    chosen = st.multiselect(
        tr("Choose participants"),
        session.aa_roster(),
        format_func=lambda ref: reference_label(ref, controller=controller),
        max_selections=needed,
        key=f"{prefix}_aa_participants:{identifier}",
    )
    if st.button(
        tr("Preview collection requests"),
        key=f"{prefix}_aa_complete:{identifier}",
        disabled=len(chosen) != needed,
    ):
        try:
            completed = session.prepare_aa(identifier, chosen)
        except (ValueError, KeyError, RuntimeError):
            st.error(tr("Choose contacts and a positive whole-cent total."))
            return True
        st.session_state[prefix + "_response_id"] = completed["response_id"]
        st.session_state.setdefault(prefix + "_transcript", []).append({
            "text": tr(
                "chat.aa.chosen",
                names="、".join(reference_label(ref, controller=controller) for ref in chosen),
            ),
            "response_id": completed["response_id"],
        })
        st.rerun()
    return True


def display_response(session, identifier, prefix):
    """Project a locally selected card/result without editing the D2 envelope."""
    response = session.response(identifier)
    selected = st.session_state.get(f"{prefix}_selected:{identifier}")
    if selected and response["status"] != "withdrawn":
        response = {
            **response,
            "status": selected["status"],
            "card": selected.get("card", response["card"]),
            "tier": selected.get("card", {}).get("tier", response["tier"]),
        }
    return response


def cancel_pending(session):
    session.receive("stop")


def mfa_choices(key):
    """One grey line: the simulated SMS code is the only connected strong method."""
    st.caption(tr("polish.mfa.other_methods"))


def _utterance_for(prefix, response_id):
    """The user's own tokenized words for this response, matched by response id."""
    for item in reversed(st.session_state.get(prefix + "_transcript", [])):
        if item.get("response_id") == response_id:
            return item.get("text")
    return None


def _external_for(prefix, response_id):
    """How many external files the user attached in the turn that produced this response."""
    for item in reversed(st.session_state.get(prefix + "_transcript", [])):
        if item.get("response_id") == response_id:
            return int(item.get("external_files", 0))
    return 0


# --- S6 display-layer recipient check ----------------------------------------------------
# Rendered beside the card, never inside it. The controller keeps the gate as an explicit
# per-action state machine (ui/scam_gate.py); the page's widgets are a projection of it.
# Every write from the page carries the action identity, the card identity and the epoch
# the widgets were drawn for, so a stale event never changes the current record. The
# controller's own entry points refuse to continue while the gate does not allow it.


def _scam_record(controller, identity, card_identity, epoch, keys):
    """Radio callback and per-run sync: copy the answered widgets into the controller.

    The controller merges only an event for the current card identity and epoch; an
    unanswered widget never erases a recorded answer, so a page change (Streamlit drops
    the widgets it did not render) cannot lift a pause, and a paused gate is latched.
    """
    answers = {question: st.session_state.get(key) for question, key in keys.items()}
    return controller.scam_gate_answer(
        identity, card_identity=card_identity, epoch=epoch, answers=answers
    )


def _scam_seed(status, keys):
    """Re-create dropped widget state from the controller's record before the widgets
    are built, so the page shows the answers the controller is judging. A key that
    exists with no value (a language switch gives the radios a new element id) is
    seeded too."""
    answers = status["answers"] if status else {}
    for question, key in keys.items():
        if answers.get(question) is not None and st.session_state.get(key) is None:
            st.session_state[key] = answers[question]


def _scam_retry(controller, identity, card_identity, epoch):
    """The retry gesture records only the user's own statement of having verified: it
    opens a new answering epoch with blank questions; the same fixed rule judges the new
    ones. The controller applies it only to the epoch it was drawn for and only while
    the gate is paused; the entry itself is never removed."""
    controller.scam_gate_retry(identity, card_identity=card_identity, epoch=epoch)


def scam_gate_binding_key(key):
    return f"{key}:binding"


def scam_sync_before_gesture(controller, key):
    """Copy the current widgets into the gate before a side-effect gesture runs.

    Streamlit runs widget callbacks in page order (the radios before the buttons), so an
    answer change in the same batch as a confirm click is recorded first; this makes the
    side-effect callbacks independent of that order by syncing the gate themselves.
    """
    binding = st.session_state.get(scam_gate_binding_key(key))
    if not isinstance(binding, dict):
        return None
    return _scam_record(
        controller,
        binding["identity"],
        binding["card_identity"],
        binding["epoch"],
        binding["keys"],
    )


def scam_gate_decision(controller, identity):
    """The controller's decision for an action against its current card, or None without
    a gate: ``incomplete``, ``pause``, ``proceed`` or ``stale``."""
    status = controller.scam_gate_status(identity)
    if status is None:
        return None
    return "stale" if status["stale"] else status["decision"]


def scam_gate_origin(status):
    """Where the questions were shown (prefix, response id, route), for the way back."""
    origin = (status or {}).get("origin") or {}
    return origin if isinstance(origin, dict) and origin.get("response_id") else None


def action_identity(session, prefix, response_id):
    """The draft, pending or plan identity a chat reply (or its local selection) acts on."""
    local = st.session_state.get(f"{prefix}_selected:{response_id}")
    if isinstance(local, dict) and (local.get("pending_id") or local.get("draft_id")):
        return local.get("pending_id") or local.get("draft_id")
    try:
        payload = session.response(response_id)["payload"]
    except (KeyError, ValueError):
        return None
    return payload.get("pending_id") or payload.get("draft_id")


def gated_response_ids(session, prefix):
    """Replies (newest first) whose live action still has an unpassed recipient check."""
    controller = session.controller
    found = []
    for item in reversed(st.session_state.get(prefix + "_transcript", [])):
        response_id = item.get("response_id")
        identity = action_identity(session, prefix, response_id)
        if not identity or identity in controller._results:
            continue
        if identity not in controller._drafts and identity not in controller._pending:
            continue
        if scam_gate_decision(controller, identity) not in (None, "proceed"):
            found.append(response_id)
    return found


def response_for_action(session, prefix, identity):
    """The chat reply that acts on one draft or pending identity, if the chat page has it."""
    for item in reversed(st.session_state.get(prefix + "_transcript", [])):
        if action_identity(session, prefix, item.get("response_id")) == identity:
            return item.get("response_id")
    return None


def return_to_response(response_id, prefix="domestic", route="conversation"):
    """Make a reply current again (and route to its page) so its gate can be answered."""
    st.session_state[prefix + "_response_id"] = response_id
    if route:
        st.session_state["next_page"] = route


def chat_route(prefix):
    """The page a chat prefix lives on; None for an embedded chat with no page of its own."""
    return {"domestic": "conversation", "im": "im"}.get(prefix)


def render_scam_facts(controller, card, *, title_key="polish.scam.facts_title", force=False):
    """Registered recipient facts and the verification advice; no questions, no buttons.

    Without ``force`` it renders nothing unless the card is a payment to a new payee
    (the device page has no user words); the recipient check itself forces the block so
    the questions never appear without the facts they are asked about.
    """
    if not force and scam_friction.level(card, contact=controller.scam_contact(card)[0]) == 0:
        return False
    lines = scam_friction.facts(card, controller.state)
    st.markdown(
        '<div class="scam-facts">'
        f'<div class="scam-facts-title">{escape(tr(title_key))}</div><ul>'
        + "".join(f"<li>{escape(line)}</li>" for line in lines)
        + "</ul></div>",
        unsafe_allow_html=True,
    )
    return True


def render_scam_gate(session, card, *, identity, hint_text, external_files, key, origin=None):
    """Recipient facts, two questions and the pause state; returns the gate decision.

    The gate is established in the controller before anything is drawn (idempotent).
    The caller renders its next-step gesture (verification code, confirm) only on
    "proceed"; Cancel always stays available. Widget keys carry the action identity,
    the full card identity and the epoch, so an earlier card's or another action's
    answers are never read.
    """
    controller = session.controller
    status = controller.scam_gate_establish(
        identity, text=hint_text, external_files=external_files, origin=origin
    )
    if status is None:
        # Never gated and level 0: the page is exactly as before S6. An action that was
        # gated once always draws its questions again (a re-grade never reaches level
        # 0), so there is no other way past this block without answering.
        return "proceed"
    card_identity, epoch = status["card_identity"], status["epoch"]
    # The locale is part of the key too: a language switch gives the radios new element
    # ids and drops their values, so a new key re-seeds them from the record at once.
    stem = f"{key}:{identity}:{card_identity}:{epoch}:{get_locale()}"
    keys = {question: f"{stem}:{question}" for question in scam_friction.QUESTIONS}
    st.session_state[scam_gate_binding_key(key)] = {
        "identity": identity,
        "card_identity": card_identity,
        "epoch": epoch,
        "keys": keys,
    }
    _scam_seed(status, keys)
    codes = tuple(status.get("cues") or ())
    with st.container(border=True):
        st.markdown(
            f'<div class="scam-title">{escape(scam_friction.title())}</div>'
            f'<div class="scam-sub">{escape(tr("polish.scam.not_signed"))}</div>',
            unsafe_allow_html=True,
        )
        render_scam_facts(controller, card, force=True)
        summary = scam_friction.cue_summary(codes)
        if summary:
            st.markdown(f'<div class="scam-cues">{escape(summary)}</div>', unsafe_allow_html=True)
        st.markdown(
            f'<div class="scam-questions">{escape(tr("polish.scam.questions_title"))}</div>',
            unsafe_allow_html=True,
        )
        latched = status["decision"] == "pause"
        locale = get_locale()
        for question in scam_friction.QUESTIONS:
            st.radio(
                scam_friction.question_label(question),
                scam_friction.OPTIONS[question],
                index=None,
                # The labels are pinned to the locale the widget is drawn in.
                format_func=lambda option, question=question: scam_friction.option_label(
                    question, option, locale=locale
                ),
                key=keys[question],
                disabled=latched,
                on_change=_scam_record,
                args=(controller, identity, card_identity, epoch, keys),
            )
        status = _scam_record(controller, identity, card_identity, epoch, keys)
        verdict = "stale" if status["stale"] else status["decision"]
        if verdict == "stale":
            st.warning(tr("polish.scam.stale"))
        elif verdict == "pause":
            # The title, the action sentence and the answer-again button follow the
            # answers that triggered the pause: guiding alone asks the user to end the
            # guidance and decide alone; a verification trigger asks to verify first.
            title = scam_friction.pause_title(status["level"], status["answers"])
            st.markdown(
                '<div class="scam-pause">'
                f'<div class="scam-pause-title">{escape(title)}</div>'
                + escape(
                    scam_friction.pause_copy(
                        codes,
                        status["answers"],
                        status["level"],
                        challenge=bool(status.get("challenge_issued")),
                        preset=status.get("preset"),
                    )
                )
                + "</div>",
                unsafe_allow_html=True,
            )
            st.button(
                scam_friction.pause_retry_label(status["level"], status["answers"], codes),
                key=f"{stem}:retry",
                on_click=_scam_retry,
                args=(controller, identity, card_identity, epoch),
            )
            st.caption(tr("polish.scam.pause_self_report"))
            st.caption(tr("polish.scam.pause_cancel_hint"))
        elif verdict == "incomplete":
            st.caption(tr("polish.scam.incomplete"))
        st.markdown(
            f'<div class="scam-footer">{escape(scam_friction.footer())}</div>',
            unsafe_allow_html=True,
        )
        if view_mode.is_judge():
            st.markdown(
                f'<div class="scam-judge">{escape(scam_friction.judge_note())}<br/>'
                f"{escape(scam_friction.record_line(status['answers'], verdict))}</div>",
                unsafe_allow_html=True,
            )
            if status.get("preset"):
                st.caption(tr("polish.scam.preset_judge"))
    return verdict


def clarification_card(session, response, prefix):
    """Keep an existing controller selection local; never rewrite session envelopes."""
    payload = response["payload"]
    contacts = payload.get("contacts", [])
    if response["status"] != "clarify" or not contacts or not payload.get("draft_id"):
        return False
    controller = session.controller
    local_key = f"{prefix}_selected:{response['response_id']}"
    hint = _utterance_for(prefix, response["response_id"])
    files = _external_for(prefix, response["response_id"])
    local = st.session_state.get(local_key)
    if local and local["status"] in {"settled", "rejected", "failed", "withdrawn"}:
        render_card(
            local.get("card", {}), controller=controller, hint_text=hint, external_files=files
        )
        render_result(local)
        return True
    labels = {ref: reference_label(ref, controller=controller) for ref in contacts}
    selected = st.selectbox(
        tr("Recipient"),
        [None, *contacts],
        format_func=lambda ref: labels.get(ref, tr("Choose a contact")),
        key=f"{prefix}_clarify_contact",
        placeholder=tr("Choose a contact"),
    )
    if view_mode.is_judge():
        with st.expander(tr("Technical evidence"), expanded=False):
            render_contact_evidence(contacts, controller=controller)
    if st.button(
        tr("Review recipient"),
        key=f"{prefix}_select_contact",
        disabled=selected is None,
    ):
        # S4: a click without a valid choice (stale or no selection) is a friendly prompt,
        # never an uncaught error; the controller still accepts only listed candidates.
        try:
            if selected not in contacts:
                raise ValueError("No recipient chosen")
            st.session_state[local_key] = controller.select_contact(payload["draft_id"], selected)
        except (ValueError, KeyError):
            st.warning(tr("polish.clarify.choose_first"))
        else:
            st.rerun()
    local = st.session_state.get(local_key)
    if not local:
        return True
    render_card(local.get("card", {}), controller=controller, hint_text=hint, external_files=files)
    pending = local.get("pending_id")
    if local["status"] in {"settled", "rejected", "failed", "withdrawn"}:
        render_result(local)
        return True
    gate_key = f"{prefix}_scam:{response['response_id']}"
    gate = render_scam_gate(
        session,
        local.get("card", {}),
        identity=pending or payload["draft_id"],
        hint_text=hint,
        external_files=files,
        key=gate_key,
        origin={
            "prefix": prefix,
            "response_id": response["response_id"],
            "route": chat_route(prefix),
        },
    )
    failure = live_controls.recorded_failure(prefix, response["response_id"])
    if failure is not None:
        # S7: a gesture on this card failed; no second confirm, Cancel stays below.
        live_controls.render_recorded_failure(failure)
    if local.get("card", {}).get("tier") == "red" and gate == "proceed" and not failure:
        mfa_choices(prefix + "_selection_method")
    if gate != "proceed" or failure is not None:
        # S6: the next-step gesture waits for the recipient check; Cancel stays below.
        pass
    elif local["status"] == "waiting_mfa":
        from corridor.ui.manual_mfa import render_manual_mfa

        result = render_manual_mfa(
            controller,
            pending,
            key=f"{prefix}_selected_mfa",
            before=lambda: scam_sync_before_gesture(controller, gate_key),
        )
        if result and result.get("status") == "mfa_verified":
            st.session_state[local_key] = {**local, **result}
            st.rerun()
    elif st.button(tr("Confirm"), key=f"{prefix}_selected_confirm", type="primary"):
        # S6: the gate is synced from the widgets right before the gesture (see
        # scam_sync_before_gesture); the controller then checks it at its entry.
        scam_sync_before_gesture(controller, gate_key)
        mark = live_controls.operation_mark(controller)
        try:
            result = (
                controller.approve_pending(pending)
                if pending
                else controller.confirm_transfer(payload["draft_id"])
            )
        except (ValueError, RuntimeError, KeyError, TimeoutError, OSError) as error:
            # S7: recorded on this reply (see confirm_response); the rerun shows it.
            live_controls.remember_failure(prefix, response["response_id"], mark, error)
            st.rerun()
        live_controls.remember(prefix, response["response_id"], mark)
        st.session_state[local_key] = {**local, **result}
        st.rerun()
    if st.button(tr("Cancel"), key=f"{prefix}_selected_cancel"):
        cancel_pending(session)
        st.session_state.pop(local_key, None)
        st.rerun()
    return True


def confirm_response(session, identifier, prefix, operation):
    # S6: a side-effect callback first syncs the recipient check from the widgets drawn
    # with it, so an answer change in the same batch is never outrun by the gesture.
    scam_sync_before_gesture(session.controller, f"{prefix}_scam:{identifier}")
    response = session.response(identifier)
    payload = response["payload"]
    nodes = payload.get("replacement", payload).get("nodes", [])
    selected = [
        node["id"]
        for node in nodes
        if st.session_state.get(f"{prefix}:node:{identifier}:{node['id']}", False)
    ]
    # S7: a callback runs outside the page's own error handling, and in live mode the
    # confirm gesture calls the planner again. A failure is recorded on this reply: the
    # page then says what failed, offers Cancel and no second confirm (its device inputs
    # are already consumed), instead of a traceback.
    mark = live_controls.operation_mark(session.controller)
    try:
        IMAdapter(session).click(operation, identifier, selected_node_ids=selected)
    except (ValueError, RuntimeError, KeyError, TimeoutError, OSError) as error:
        live_controls.remember_failure(prefix, identifier, mark, error)
        return False
    live_controls.remember(prefix, identifier, mark)
    return True


def render_response(session, response, *, prefix, im=False):
    response = session.response(response["response_id"])
    identifier = response["response_id"]
    payload = response["payload"]
    draft = payload.get("replacement", payload)
    nodes = draft.get("nodes", [])
    if response["status"] == "redisplay":
        st.warning(tr("The canonical facts changed. Review the new card before confirming again."))
    retired = response["status"] in {"withdrawn", "rejected", "settled", "refused", "controlled"}
    if clarification_card(session, response, prefix):
        pass
    elif birthday_choices(session, response, prefix, im=im):
        pass
    elif subscription_choices(session, response, prefix, im=im):
        pass
    elif schedule_cancel_choices(session, response, prefix, im=im):
        pass
    elif purchase_review(session, response) or purchase_candidates(session, response):
        pass
    elif not im and aa_completion_card(session, response, prefix):
        pass
    elif nodes and not retired:
        for index, node in enumerate(nodes):
            with st.expander(f"{index + 1}. {tr(node['label'])}", expanded=index == 0):
                render_card(node["card"], canonical=False, position=index + 1, total=len(nodes))
                st.checkbox(
                    tr("Select this node for device confirmation"),
                    key=f"{prefix}:node:{identifier}:{node['id']}",
                )
    elif response.get("card") and not retired:
        render_card(
            response["card"],
            canonical=bool(response["payload"].get("canonical", False)),
            controller=session.controller,
            contextual_recipient=response.get("recipient_context", False),
            hint_text=_utterance_for(prefix, identifier),
            external_files=_external_for(prefix, identifier),
        )
        if response["intent"] == "transfer":
            parse_note(session, payload)
    if (response["payload"].get("receipt") or {}).get("kind") == "transactions_query":
        # Chat's history request uses the billing page's own query and table.
        render_result(response["payload"])
        render_transaction_rows(response["payload"], controller=session.controller)
    elif response["payload"].get("analysis_receipt") and response["intent"] == "bill_report":
        from corridor.ui.insights import render_bill_result

        render_bill_result(response["payload"], session.controller)
    elif response["payload"].get("analysis_receipt") and response["intent"] == "subscription_query":
        render_subscription_result(response["payload"], session.controller)
    elif (
        response["payload"].get("receipt")
        or (response["intent"] == "transfer" and response["payload"].get("analysis_receipt"))
        or response["status"]
        in {
            "rejected",
            "failed",
            "interrupted",
            "handed_off",
            "controlled",
        }
    ):
        render_result(response["payload"])
    # Model suggestions under a plain clarification: system-written questions only.
    render_suggestions(
        session, response, prefix, im=im, submit=submit_request, route=not im and prefix != "demo"
    )
    gate = "proceed"
    if response["buttons"] and response.get("card") and not nodes and not retired:
        identity_for_gate = payload.get("pending_id") or payload.get("draft_id")
        if identity_for_gate:
            gate = render_scam_gate(
                session,
                response["card"],
                identity=identity_for_gate,
                hint_text=_utterance_for(prefix, identifier),
                external_files=_external_for(prefix, identifier),
                key=f"{prefix}_scam:{identifier}",
                origin={"prefix": prefix, "response_id": identifier, "route": chat_route(prefix)},
            )
    live_controls.render_response_notes(session.controller, response, prefix)
    if payload.get("scam_phrase") and view_mode.is_judge():
        # What the phrase gate is and is not, within its stated limits.
        st.caption(tr("polish.scam.phrase_gate_judge"))
    failure = None if retired else live_controls.recorded_failure(prefix, identifier)
    if failure is not None:
        # S7: a gesture on this reply failed; no second confirm, Cancel stays below.
        live_controls.render_recorded_failure(failure)
    if response.get("tier") == "red" and response["buttons"] and gate == "proceed" and not failure:
        mfa_choices(prefix + "_mfa_method")
    actions = st.columns(2)
    for button in response["buttons"]:
        operation = button["id"]
        if gate != "proceed" or failure is not None:
            # S6: no verification code and no confirm gesture until the recipient check is
            # answered and not paused; the Cancel button below stays.
            continue
        with actions[0]:
            if operation == "verify_mfa":
                from corridor.ui.manual_mfa import render_manual_mfa

                pending = payload.get("pending_id") or payload.get("draft_id")
                if pending:
                    gate_key = f"{prefix}_scam:{identifier}"
                    result = render_manual_mfa(
                        session.controller,
                        pending,
                        key=f"{prefix}_{operation}",
                        before=lambda: scam_sync_before_gesture(session.controller, gate_key),
                    )
                    if result and result.get("status") == "mfa_verified":
                        previous_status = session.response(identifier)["status"]
                        completed = confirm_response(session, identifier, prefix, operation)
                        if (
                            not completed
                            or session.response(identifier)["status"] != previous_status
                        ):
                            st.rerun()
                continue
            st.button(
                tr("Confirm") if operation == "confirm" else tr("Verify simulated MFA"),
                key=f"{prefix}_{operation}",
                type="primary",
                use_container_width=True,
                on_click=confirm_response,
                args=(session, identifier, prefix, operation),
            )
    if response["buttons"]:
        with actions[1]:
            if st.button(tr("Cancel"), key=f"{prefix}_cancel", use_container_width=True):
                cancel_pending(session)
                st.rerun()
        st.caption(
            tr("Cancel retires all pending proposals in this session; settled effects remain.")
        )
    if response["intent"] == "out_of_scope":
        shortcuts(session, prefix, fallback=True, route=prefix != "demo")
    elif response["route"] != "conversation" and prefix != "demo":
        st.button(
            tr("Open this service"),
            key=f"{prefix}_open",
            on_click=open_route,
            args=(response["route"],),
        )
    if not view_mode.is_judge():
        return
    with st.expander(tr("Technical evidence")):
        if response["intent"] == "subscription_query" and payload.get("analysis_receipt"):
            receipt = payload["analysis_receipt"]
            st.caption(tr("Signed analysis receipt: ") + receipt["receipt_id"])
            st.caption(tr("Dataset hash: ") + receipt["dataset_hash"])
        else:
            st.json(response)


def task_controls(session, prefix):
    actions = st.columns(3)
    with actions[0]:
        if st.button(tr("Stop"), key=prefix + "_task_stop", use_container_width=True):
            cancel_pending(session)
            st.rerun()
    with actions[1]:
        if st.button(tr("Compensate"), key=prefix + "_task_back", use_container_width=True):
            plans = session.controller.snapshot()["plans"]
            st.session_state.pop(prefix + "_control_error", None)
            st.session_state.pop(prefix + "_compensation", None)
            if plans:
                try:
                    st.session_state[prefix + "_compensation"] = session.controller.compensate(
                        plans[-1]["plan"]["plan_id"]
                    )
                except (ValueError, RuntimeError, KeyError):
                    st.session_state[prefix + "_control_error"] = True
            else:
                st.session_state[prefix + "_compensation"] = {"status": "no_active_plan"}
            st.rerun()
    with actions[2]:
        if st.button(tr("Handoff"), key=prefix + "_task_handoff", use_container_width=True):
            response = session.receive("handoff")
            st.session_state[prefix + "_response_id"] = response["response_id"]
            st.rerun()
    pending = st.session_state.get(prefix + "_compensation")
    if pending:
        if pending.get("card"):
            render_card(pending["card"])
            if st.button(
                tr("Confirm new compensation"),
                key=prefix + "_compensate_confirm",
            ):
                try:
                    st.session_state[prefix + "_compensation"] = session.controller.approve_pending(
                        pending["pending_id"]
                    )
                    st.session_state.pop(prefix + "_control_error", None)
                except (ValueError, RuntimeError, KeyError):
                    st.session_state[prefix + "_control_error"] = True
                st.rerun()
        else:
            render_result(pending)
    if st.session_state.get(prefix + "_control_error"):
        st.error(
            tr("The operation could not complete. Review the current facts and inputs, then retry.")
        )
        safety = session.controller.gateway.safety_snapshot(session.controller.device.principal)
        if safety["state"] == "Locked":
            st.warning(
                tr("Locked: yellow and red actions are blocked. Green queries remain available.")
            )
            st.info(
                tr(
                    "Unlock requires the cooldown, owner device confirmation and simulated MFA. "
                    "Old approvals stay void."
                )
            )
        if view_mode.is_judge():
            with st.expander(tr("Technical evidence")):
                st.json(safety)


def task_node_status(response):
    """Project the response outcome without treating verification as completion."""
    status = response.get("status", "pending")
    aliases = {
        "clarify": "waiting_input",
        "awaiting_device_selection": "waiting_input",
        "login_required": "waiting_input",
        "selection_required": "waiting_input",
        "intent_confirmation_required": "waiting_approval",
        "redisplay": "waiting_approval",
        "mfa_verified": "waiting_approval",
        "rejected": "blocked",
        "refused": "blocked",
        "outside_lock_month": "blocked",
        "withdrawn": "cancelled",
        "handed_off": "handoff",
    }
    status = aliases.get(status, status)
    if status in NODE_STYLES:
        return status
    return "waiting_approval" if response.get("buttons") else "pending"


def task_plans(response, snapshots):
    """Show only a plan explicitly identified by the current response."""
    payload = response.get("payload", {})
    draft = payload.get("replacement", payload)
    plan_id = (
        draft.get("plan_id")
        or response.get("plan_id")
        or response.get("card", {}).get("plan_id")
        or draft.get("card", {}).get("plan_id")
    )
    if not plan_id:
        return []
    return [snapshot for snapshot in snapshots if snapshot["plan"]["plan_id"] == plan_id]


def task_panel(session, response, prefix):
    with st.container(border=True):
        st.subheader(tr("Current task"))
        if not response:
            st.caption(tr("Describe your request to see the next step here."))
            st.markdown(tr("**Request → Review → Receipt**"))
            return
        st.write(tr("nav." + response["route"]))
        tier = response.get("tier") or response.get("requested_level")
        if tier in {"green", "yellow", "red"}:
            # The same labels as the approval card, so the panel and card never disagree.
            label = {
                "green": tr("Green · automatic query"),
                "yellow": tr("Yellow · device approval required"),
                "red": tr("Red · independent simulated MFA required"),
            }[tier]
            st.markdown(
                f'<span class="tier tier-{tier}">{escape(label)}</span>', unsafe_allow_html=True
            )
        st.write(
            response_copy(
                response,
                completion=prefix != "im",
                hint_text=_utterance_for(prefix, response["response_id"]),
            )
        )
        if response.get("requested_level") and not response.get("card"):
            st.caption(tr("This service requires red-tier review; no executable card is ready."))
        draft = response["payload"].get("replacement", response["payload"])
        nodes = draft.get("nodes", [])
        plans = task_plans(response, session.controller.snapshot()["plans"])
        if plans:
            render_plans(plans)
        elif nodes:
            render_dag(draft_snapshot(draft), key=prefix + "-draft")
        else:
            # The card's own kind names the node when there is one (a split-bill reply is
            # a collection request, not a transfer; JUDGING-1 list item 17).
            kind = (response.get("card") or {}).get("kind")
            tool = kind if kind in KINDS else response["intent"]
            snapshot = {
                "plan": {
                    "nodes": [
                        {
                            "node_id": "current",
                            "tool": tool,
                            "label": KINDS.get(tool, "Business task"),
                            "depends_on": [],
                        }
                    ]
                },
                "nodes": {"current": {"status": task_node_status(response)}},
            }
            render_dag(snapshot, key=prefix + "-current")
        task_controls(session, prefix)


def conversation_home(controller, *, im=False, session=None, prefix=None):
    prefix = prefix or ("im" if im else "domestic")
    session = session or session_for(controller)
    st.header(tr("Simulated IM banking" if im else "Tell us what you need"))
    if im:
        st.markdown(
            '<div class="im-banner">' + escape(tr("Simulated IM channel · Banking")) + "</div>",
            unsafe_allow_html=True,
        )
    else:
        st.caption(tr("Start with a message. You stay in control of every approval."))
    left, right = st.columns([3, 2], gap="large")
    # The keyed container lets the customer view narrow chat to phone width; the task
    # panel (stop, compensate, handoff) stays in both views.
    with left, st.container(key=f"{prefix}_chatcol"):
        shortcuts(session, prefix, route=not im and prefix != "demo")
        transcript = st.session_state.get(prefix + "_transcript", [])
        if not transcript:
            with st.chat_message("assistant", avatar="🏦"):
                st.write(tr("Hello. How can I help you today?"))
        current = st.session_state.get(prefix + "_response_id")
        for item in transcript[-8:]:
            with st.chat_message("user"):
                # R8: the customer view shows display values, never vault tokens; the
                # stored (tokenized) text is what the session, hint and audit used.
                st.write(display_names.display_text(item["text"], session.controller.state))
                if item.get("external_files"):
                    st.caption(tr("polish.attach.sent"))
                notice = remark_notice(item["text"])
                if notice:
                    st.info(notice)
            with st.chat_message("assistant", avatar="🏦"):
                st.write(
                    response_copy(
                        display_response(session, item["response_id"], prefix),
                        # Only the current response has the completion card below it.
                        completion=not im and item["response_id"] == current,
                        hint_text=item["text"],
                    )
                )
        identifier = st.session_state.get(prefix + "_response_id")
        response = display_response(session, identifier, prefix) if identifier else None
        if response:
            render_response(session, response, prefix=prefix, im=im)
        gated = [item for item in gated_response_ids(session, prefix) if item != identifier]
        if gated:
            # S6: the chat page renders only the current reply; an earlier reply whose
            # recipient check is paused or unanswered must stay reachable to be answered.
            st.info(tr("polish.scam.other_gated"))
            st.button(
                tr("polish.scam.other_gated_button"),
                key=f"{prefix}_scam_return",
                on_click=return_to_response,
                args=(gated[0], prefix, None),
            )
        with st.form(prefix + "_form", clear_on_submit=True):
            text = st.text_input(
                tr("Your banking request"),
                key=prefix + "_text",
                placeholder=tr("Describe a request or add a missing detail…"),
            )
            with st.expander(tr("polish.attach.label")):
                attachment = st.text_area(
                    tr("polish.attach.field"),
                    key=prefix + "_attachment",
                    height=90,
                    placeholder=tr("polish.attach.placeholder"),
                )
                st.caption(tr("polish.attach.help"))
            sent = st.form_submit_button(
                tr("Send request"), key=prefix + "_send", type="primary", use_container_width=True
            )
        if sent and text.strip():
            submit_request(
                session,
                text,
                prefix,
                route=not im and prefix != "demo",
                external_files=1 if attachment and attachment.strip() else 0,
            )
            st.rerun()
    with right:
        task_panel(session, response, prefix)


def im_home(controller):
    with st.container(key="im_chat"):
        conversation_home(controller, im=True)
