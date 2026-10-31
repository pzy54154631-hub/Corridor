"""The side effects that close every state-changing request (never a GET).

Streamlit performs three writes while it draws a page: it establishes the recipient check
of the reply it shows, it delivers or retires simulated SMS copies, and it syncs a
verification into the reply. The web pages do the same once, here, after each POST, and
add their own checks: the shown cards against the authoritative ones, and the gesture
tokens. A reply's gate is established before its step is computed; a task whose gate
was not evaluated for its current card never receives a token.
"""

import logging
import secrets

from corridor.web import compat, tasks

LOG = logging.getLogger("corridor.web.refresh")
# The reply states in which Streamlit draws the card and its recipient check.
LOCAL_FINAL = frozenset({"settled", "rejected", "failed", "withdrawn"})


def deliver(ws, pending_id: str, challenge_id: str) -> None:
    """Place a live challenge's synthetic SMS in this session's inbox (idempotent)."""
    read = compat.read_challenge(ws.controller, challenge_id)
    if read is None:
        return
    code, expiry = read
    card = compat.pending_card(ws.controller, pending_id) or {}
    compat.OUTBOX.deliver(
        ws.pair,
        compat.build_sms(
            challenge_id,
            code=code,
            expiry=expiry,
            kind=str(card.get("kind", "")),
            card=card,
            state=ws.controller.state,
        ),
    )


def gate_target(ws, task) -> str | None:
    """The identity whose gate the current reply's page establishes, or None."""
    if task.source != "chat" or task.rid is None:
        return None
    response = ws.session.response(task.rid)
    payload = response["payload"]
    local = ws.local.get(task.rid)
    if local is not None and response["status"] == "clarify":
        if not payload.get("contacts") or not payload.get("draft_id"):
            return None
        if local.get("status") in LOCAL_FINAL:
            return None
        return local.get("pending_id") or payload.get("draft_id")
    nodes = payload.get("replacement", payload).get("nodes", [])
    if (
        response["buttons"]
        and response.get("card")
        and not nodes
        and response["status"] not in compat.RETIRED
    ):
        return payload.get("pending_id") or payload.get("draft_id")
    return None


def establish(ws, task) -> None:
    identity = gate_target(ws, task)
    if not identity:
        return
    c = ws.controller
    try:
        turn = ws.turn_of(task.rkey)
        status = c.scam_gate_establish(
            identity,
            text=turn.hint,
            external_files=turn.external_files,
            origin={"prefix": "domestic", "response_id": task.rid, "route": "conversation"},
        )
    except Exception as error:
        LOG.warning("gate not evaluated: %s", type(error).__name__)
        ws.gate_eval.pop(identity, None)
        return
    ws.gate_eval[identity] = {
        "card_identity": compat.scam_friction.cards_identity(
            compat.authoritative_cards(c, identity)
        ),
        # The record holds for this reply and these words only.
        "context": tasks.gate_context(ws, task),
        "result": "level0" if status is None else "gated",
    }


def check_cards(ws, task) -> None:
    """Record whether the reply's own card copy still matches the authoritative card (the
    step itself re-checks it: a mismatch is never signed, ``tasks.task_step``)."""
    if task.source != "chat" or task.rkey is None:
        return
    mismatch = tasks.cards_mismatch(ws, task)
    if mismatch is None:
        ws.card_mismatch.pop(task.rkey, None)
        return
    ws.card_mismatch[task.rkey] = mismatch


def sms(ws) -> None:
    """Deliver live challenges, retire verified (used) and dead or orphaned (expired) ones.
    A locked account shows no live code at all: every unverified copy expires (R2-E14)."""
    c = ws.controller
    locked = (ws.safety or {}).get("state") == "Locked"
    live = set()
    for challenge, entry in compat.challenges(c).items():
        ref = compat.sms_ref(challenge)
        if entry["verified"]:
            compat.OUTBOX.mark(ws.pair, ref, "used")
        elif not locked and compat.challenge_active(c, challenge):
            live.add(ref)
            deliver(ws, entry["pending_id"], challenge)
        else:
            compat.OUTBOX.mark(ws.pair, ref, "expired")
    for message in compat.OUTBOX.inbox(ws.pair):
        if message.status == "active" and message.ref not in live:
            compat.OUTBOX.mark(ws.pair, message.ref, "expired")


def prune(ws, listed) -> None:
    """Selections belong to a task's current plan; a redisplayed plan starts empty."""
    by_key = {task.key: task for task in listed}
    for key in list(ws.selection):
        task = by_key.get(key)
        plan = compat.plan_entry(ws.controller, task.identity) if task else None
        if plan is None or any(node not in plan["node_ids"] for node in ws.selection[key]):
            del ws.selection[key]


def gate_versions(ws, listed) -> None:
    """The version each shown recipient check answers under; a check whose card or epoch
    changed gets a new one. A distrusted session gets none: ``distrust`` revoked its gate
    versions with its tokens, and no refresh after it issues them again (RW-5 item 3 c)."""
    if ws.untrusted:
        ws.gates.clear()
        return
    kept = {}
    for task in listed:
        if task.source != "chat" or not task.identity or task.rkey is None:
            continue
        status = ws.controller.scam_gate_status(task.identity)
        if status is None:
            continue
        key = (task.identity, status["card_identity"], status["epoch"])
        held = ws.gates.get(task.rkey)
        if held is not None and tuple(held[1:]) == key:
            kept[task.rkey] = held
        else:
            kept[task.rkey] = (secrets.token_urlsafe(12), *key)
    ws.gates.clear()
    ws.gates.update(kept)


def remember_kernel(ws, listed) -> None:
    """Keep each action's kernel view for the judge evidence after dispatch: a pending
    action's view as read while pending, or, for a draft confirmed and dispatched in one
    gesture, the view of the device workflow's record of that confirmation."""
    c = ws.controller
    for task in listed:
        identity = task.identity
        if not identity:
            continue
        view = compat.kernel_view(c, identity)
        if view is not None:
            ws.kernel_seen[identity] = {"source": "pending_copy", **view}
            continue
        display = compat.draft_display(c, identity)
        if display:
            ws.kernel_drafts[identity] = display
        view = compat.confirmed_view(c, ws.kernel_drafts.get(identity))
        if view is not None:
            ws.kernel_seen[identity] = {"source": "confirmation", **view}


def refresh(ws) -> None:
    c = ws.controller
    ws.safety = compat.safety(c)
    listed = tasks.collect(ws)
    current = ws.current.get("domestic")
    for task in listed:
        # A distrusted session evaluates nothing again: its gate records stay revoked.
        if task.source == "chat" and task.rid == current and not ws.untrusted:
            establish(ws, task)
    for task in listed:
        check_cards(ws, task)
    remember_kernel(ws, listed)
    sms(ws)
    prune(ws, listed)
    tasks.issue(ws)
    gate_versions(ws, tasks.collect(ws))
