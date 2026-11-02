"""Every web gesture and business action, framework free.

Each function runs in the executor thread with the request's locale and audience set,
mutates only this session, raises only ``WebError`` and never calls ``refresh`` (the
handler does). The calls and their preconditions mirror the Streamlit pages they
replace; where a Streamlit page relied on a button not being drawn, the check is made
here, before any controller call.
"""

import re
from collections.abc import Generator
from contextlib import contextmanager
from copy import deepcopy
from fractions import Fraction

from corridor.web import compat, errors, live, tasks
from corridor.web.refresh import deliver
from corridor.web.state import SENT_KEPT, DeviceTask, Turn

MAX_TEXT = 4000
LOGIN_OK = frozenset({"accepted", "logged_in", "complete", "ready"})
SHORTCUTS = {
    **{key: prompt for key, prompt in compat.EXAMPLES},
    "scam_drill": "polish.scam.drill_utterance",
}
CODE = re.compile(r"[0-9]{6}")
CHOICE = re.compile(r"c(0|[1-9][0-9]{0,3})")
NODE = re.compile(r"n(0|[1-9][0-9]{0,2})")
SELECTABLE = frozenset({"select_nodes", "confirm_plan", "confirm_subset"})
CHAT = "domestic"
# Business slots Streamlit's top-bar Stop clears (app::stop_all). The single-product
# comparison stays, as the investments page's own ``comparison`` does (O-S12).
RETIRED_SLOTS = (
    "card_pending",
    "subscription_pending",
    "subscription_cancel",
    "investment_pending",
    "redemption_pending",
    "birthday_draft",
    "birthday_preview",
    "compensation",
)
# The page slots that show a business task's card, by source.
SLOTS = {
    "cards": ("card_pending",),
    "subscriptions": ("subscription_pending", "subscription_cancel"),
    "wealth": ("investment_pending", "redemption_pending"),
    "compensation": ("compensation",),
}


def _fail(code: str) -> errors.WebError:
    return errors.error(code)


@contextmanager
def _unknown() -> Generator[None, None, None]:
    """A state-changing product call that no operation or commit wraps: any exception from
    inside it, a ``WebError`` of any status included, has an unknown result, never that of
    a refusal that changed nothing (RW-4'''')."""
    try:
        yield
    except Exception:
        raise errors.WebOutcomeUnknown() from None


def _same(held: str, given: str) -> bool:
    return tasks._same(held, given)


def _logged_in(ws) -> None:
    if not compat.logged_in(ws.controller):
        raise _fail("not_applicable")


def _rid(ws, rkey: str) -> str:
    rid = ws.rkeys.get(rkey)
    if rid is None:
        raise _fail("not_found")
    return rid


def _current(ws, rkey: str) -> str:
    rid = _rid(ws, rkey)
    if ws.current.get(CHAT) != rid:
        raise _fail("not_current")
    return rid


def _register(ws, rid: str) -> str:
    rkey = ws.rkey_of.get(rid)
    if rkey is None:
        rkey = f"R{len(ws.rkeys) + 1}"
        ws.rkeys[rkey] = rid
        ws.rkey_of[rid] = rkey
    return rkey


def _turn(ws, role: str, rid: str | None, **fields) -> Turn:
    letter = {"user": "u", "assistant": "a", "system": "s"}[role]
    turn = Turn(f"{letter}{len(ws.transcript) + 1}", role, rid, fields.pop("hint", None), **fields)
    ws.transcript.append(turn)
    return turn


def _pick(options: list, choice: str):
    index = int(choice[1:]) if isinstance(choice, str) and CHOICE.fullmatch(choice) else -1
    if not 0 <= index < len(options):
        raise _fail("not_found")
    return options[index]


def _pick_listed(ws, source: str, options: list, choice: str, version: str):
    """A business page's choice, only from the list the page showed (``list_version``)."""
    if not _same(tasks.list_version(ws, source, options), version):
        raise _fail("card_changed")
    return _pick(options, choice)


def _replayed(ws, client_msg_id: str, digest: str) -> str | None:
    sent = ws.sent.get(client_msg_id)
    if sent is None:
        return None
    if sent[0] != digest:
        raise _fail("msg_id_conflict")
    return sent[1]


def _submit(ws, text, client_msg_id, digest, *, hint=None, external_files=0, shortcut=None):
    """``submit_request``: the reply, the user's words fixed at sending, the current reply."""
    with live.operation(ws, CHAT) as op:
        # SG-6: the session reads the count (never a file) for its suggestion flag.
        response = ws.session.receive(text, external_files=external_files)
        rid = response["response_id"]
        rkey = _register(ws, rid)
        op.bind(rid)
    with _unknown():
        # The reply exists: the vault and the plan are written while it is recorded.
        words = compat.vault_tokenize(ws.controller, text) if hint is None else hint
        _turn(ws, "user", rid, hint=words, external_files=external_files, shortcut=shortcut)
        _turn(ws, "assistant", rid)
        draft = response["payload"].get("replacement", response["payload"])
        if draft.get("nodes") and compat.plan_entry(ws.controller, draft.get("plan_id")):
            compat.attach_source_text(ws.controller, draft["plan_id"], words)
    ws.current[CHAT] = rid
    ws.sent[client_msg_id] = (digest, rkey)
    while len(ws.sent) > SENT_KEPT:
        ws.sent.popitem(last=False)
    return rkey


def _business(ws, source: str, kind: str, identity: str, origin: str | None = None) -> str:
    """Register a business page's card as a device task ``TB<n>`` (kept until Stop).

    The same card previewed again (the page's request id repeats it, for instance a
    request whose response was lost and is sent again) keeps its one task: one pending is
    never two device tasks with two tokens (R3-E15).
    """
    for key, task in ws.tasks.items():
        if task.source == source and task.identity == identity:
            return key
    sequence = ws.business.get("task_seq", 0) + 1
    ws.business["task_seq"] = sequence
    key = f"TB{sequence}"
    ws.tasks[key] = DeviceTask(
        key, source, None, None, identity, kind, origin=origin, since=len(ws.transcript)
    )
    return key


def _preview(ws, kind: str, payload, call) -> dict:
    """A business preview by request id. A Stop said in the chat retires the controller's
    pending actions but not the page's request ids; when the cached preview names a card
    that no longer exists, a new request id asks again (P-DIFF-25)."""
    result = call(ws.request_id(kind, payload))
    pending = result.get("pending_id")
    c = ws.controller
    if pending and compat.pending_entry(c, pending) is None and not compat.has_result(c, pending):
        ws.forget_request(kind, payload)
        result = call(ws.request_id(kind, payload))
    return result


def _drop_business(ws) -> None:
    for key, task in list(ws.tasks.items()):
        if task.source != "chat":
            del ws.tasks[key]


# --- shell ------------------------------------------------------------------------------


def login(ws) -> dict:
    with live.operation(ws, CHAT, bind=False):
        result = ws.controller.start_session()
        if result.get("status") in LOGIN_OK:
            ws.wealth.start_session()
    return {"status": str(result.get("status"))}


def set_view(ws, view: str) -> None:
    if view not in {"customer", "judge"}:
        raise _fail("invalid_input")
    ws.view = view


def set_locale(ws, locale: str) -> None:
    if locale not in {"zh-CN", "en"}:
        raise _fail("invalid_input")
    ws.locale = locale


# --- chat -------------------------------------------------------------------------------


def send_message(ws, text: str, external_files: int, client_msg_id: str) -> str:
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
        raise _fail("invalid_input")
    if external_files not in (0, 1) or isinstance(external_files, bool):
        raise _fail("invalid_input")
    digest = compat.H({"kind": "message", "text": text, "external_files": external_files})
    replayed = _replayed(ws, client_msg_id, digest)
    if replayed is not None:
        return replayed
    _logged_in(ws)
    return _submit(ws, text, client_msg_id, digest, external_files=external_files)


def send_shortcut(ws, shortcut: str, client_msg_id: str) -> str:
    prompt = SHORTCUTS.get(shortcut)
    if prompt is None:
        raise _fail("not_found")
    digest = compat.H({"kind": "shortcut", "shortcut": shortcut, "external_files": 0})
    replayed = _replayed(ws, client_msg_id, digest)
    if replayed is not None:
        return replayed
    _logged_in(ws)
    # The bubble and the gate read the words in the language they were sent in.
    return _submit(
        ws,
        compat.source_utterance(prompt),
        client_msg_id,
        digest,
        hint=compat.tr(prompt),
        shortcut=shortcut,
    )


def make_current(ws, rkey: str) -> None:
    ws.current[CHAT] = _rid(ws, rkey)


def select_recipient(ws, rkey: str, choice: str) -> None:
    rid = _current(ws, rkey)
    response = ws.session.response(rid)
    payload = response["payload"]
    contacts = list(payload.get("contacts") or [])
    draft_id = payload.get("draft_id")
    if response["status"] != "clarify" or not contacts or not draft_id:
        raise _fail("not_applicable")
    c = ws.controller
    if (
        compat.draft_entry(c, draft_id) is None
        or compat.pending_entry(c, draft_id) is not None
        or compat.has_result(c, draft_id)
        or compat.is_closed(c, draft_id)
    ):
        # A card on the device or a result: the recipient is no longer a choice.
        raise _fail("not_applicable")
    contact = _pick(contacts, choice)
    with live.operation(ws, CHAT, bind=False):
        ws.local[rid] = c.select_contact(draft_id, contact)


def _gate_for(ws, rkey: str, gate_version: str):
    held = ws.gates.get(rkey)
    if held is None:
        raise _fail("not_applicable")
    version, identity, card_identity, epoch = held
    if not _same(version, gate_version):
        raise _fail("gate_stale")
    status = ws.controller.scam_gate_status(identity)
    if (
        status is None
        or status["stale"]
        or status["card_identity"] != card_identity
        or status["epoch"] != epoch
    ):
        raise _fail("gate_stale")
    return identity, card_identity, epoch, status


def gate_answer(ws, rkey: str, gate_version: str, answers: dict) -> None:
    _current(ws, rkey)
    identity, card_identity, epoch, status = _gate_for(ws, rkey, gate_version)
    clean = compat.scam_friction.clean_answers(answers)
    if all(value is None for value in clean.values()):
        raise _fail("invalid_input")
    if status["decision"] == "pause":
        # Latched until the retry gesture; the controller would only record a refusal.
        raise _fail("not_applicable")
    with _unknown():
        ws.controller.scam_gate_answer(
            identity, card_identity=card_identity, epoch=epoch, answers=clean
        )


def gate_retry(ws, rkey: str, gate_version: str) -> None:
    _current(ws, rkey)
    identity, card_identity, epoch, status = _gate_for(ws, rkey, gate_version)
    if status["decision"] != "pause":
        raise _fail("not_applicable")
    with _unknown():
        ws.controller.scam_gate_retry(identity, card_identity=card_identity, epoch=epoch)


def choose_birthday(ws, rkey: str, choice: str) -> None:
    """``choose_birthday`` (B13): an unsigned draft of the chosen trusted event only."""
    rid = _current(ws, rkey)
    response = ws.session.response(rid)
    payload = response["payload"]
    if response["intent"] != "birthday" or response["status"] != "clarify":
        raise _fail("not_applicable")
    candidates = list(payload.get("candidates") or [])
    if not candidates:
        raise _fail("not_applicable")
    event = _pick(candidates, choice)
    if not event.get("lock_window_open"):
        raise _fail("not_applicable")
    ws.business.pop("birthday_choice_error", None)
    reference = event["event_ref"]
    try:
        draft = ws.controller.preview_birthday(
            event_ref=reference, request_id=f"{rid}:birthday:{reference}"
        )
    except Exception:
        # Every refusal without a side effect was checked above. The preparer records the
        # request and registers the plan's display before it can fail, so any exception
        # from inside it, a ``WebError`` of any status included, has an unknown result and
        # the session is distrusted (R3-E3, RW-4'''), never shown as an ordinary page error
        # or refusal. A result it returns without failing is shown as before.
        raise errors.WebOutcomeUnknown() from None
    if not draft or draft.get("status") != "draft":
        ws.business["birthday_choice_error"] = rkey
        return
    with _unknown():
        # The draft exists: the page's record of it and its task are written after the
        # preparer changed state, so any failure here is an unknown result (RW-5 item 1).
        ws.business["birthday_draft"] = deepcopy(draft)
        ws.business["birthday_event_ref"] = reference
        if not any(
            task.source == "plans" and task.identity == draft["plan_id"]
            for task in ws.tasks.values()
        ):
            _business(ws, "plans", "plan", draft["plan_id"], origin=rid)


# The product session's own final reply states (``DomesticSession.confirm``).
FINAL_REPLIES = frozenset({"settled", "withdrawn", "refused", "rejected", "controlled"})
# The page's own record of the chat choices it had prepared: (kind, listing reply, object)
# -> the reply the preparer produced. Read before the preparer is called again.
CHOSEN = "chat_choices"


def _prepare_choice(ws, prepare) -> str:
    """Run a chat choice's product-layer preparer. Every check that refuses without a
    side effect was made before. Once the operation is entered, a failure may follow a
    prepared card: any exception from then on, from inside ``prepare()`` or from closing
    the operation, a ``WebError`` of any status included, has an unknown result and the
    session is distrusted (R2-E3, RW-4''''), never answered as an ordinary refusal. A
    refusal at the operation's entry, before ``prepare()`` is called, keeps its status."""
    entered = False
    try:
        with live.operation(ws, CHAT) as op:
            entered = True
            completed = prepare()
            created = completed["response_id"]
            op.bind(created)
    except Exception:
        if not entered:
            raise
        raise errors.WebOutcomeUnknown() from None
    return created


def _prepared_before(ws, created: str) -> str | None:
    """The same choice again returns the reply it prepared (the product layer's cache):
    no second transcript turn. A finished card is not offered again (R2-E15)."""
    rkey = ws.rkey_of.get(created)
    if rkey is None:
        return None
    task = ws.tasks.get("T" + rkey)
    finished = ws.session.response(created)["status"] in FINAL_REPLIES or (
        task is not None and tasks.task_step(ws, task).step in tasks.CLOSED
    )
    if finished:
        raise _fail("not_applicable")
    ws.current[CHAT] = created
    return rkey


def _chosen_before(ws, key: tuple) -> str | None:
    """The same choice again, known from the page's own record (``CHOSEN``): the reply it
    prepared is shown again, or a finished one is refused (R2-E15), both before the
    preparer is called, so the refusal is one that changed nothing (RW-5 item 2)."""
    created = ws.business.get(CHOSEN, {}).get(key)
    return None if created is None else _prepared_before(ws, created)


def _record_choice(ws, key: tuple, created: str, hint: str) -> str:
    """A prepared chat choice's reply, recorded: the page's record of the choice, the
    reply's key, its two turns, and the current reply. The preparer changed state, so any
    failure here is an unknown result (RW-5 item 1)."""
    with _unknown():
        ws.business.setdefault(CHOSEN, {})[key] = created
        again = _prepared_before(ws, created)
        if again is not None:
            return again
        rkey_new = _register(ws, created)
        _turn(ws, "user", created, hint=hint)
        _turn(ws, "assistant", created)
        ws.current[CHAT] = created
    return rkey_new


def subscription_choices(ws, rid: str) -> tuple[list[str], str]:
    """Pure current-state projection shared by the chat view and its choice handler.

    The version binds the reply, listed order, current object facts and owner ordinals;
    removing an item or changing an amount cannot silently rebind a displayed c<i>.
    """
    response = ws.session.response(rid)
    c = ws.controller
    owned = {
        ref: item
        for ref, item in sorted(c.state.subscriptions.items())
        if item.owner == c.device.principal
    }
    offered = (
        response["payload"].get("subscription_choices", [])
        if response["intent"] == "subscription_cancel" and response["status"] == "clarify"
        else []
    )
    refs = [ref for ref in offered if ref in owned and owned[ref].status == "active"]
    facts = {
        "refs": refs,
        "owned": [item.model_dump(mode="json") for item in owned.values()],
        "merchants": [
            c.state.merchants[ref].model_dump(mode="json")
            for ref in sorted({owned[ref].merchant_ref for ref in refs})
            if ref in c.state.merchants
        ],
    }
    return refs, tasks.list_version(ws, f"chat-subscription:{rid}", [facts])


def choose_subscription(ws, rkey: str, choice: str, list_version: str) -> str:
    """Prepare a new reply for one displayed authorization; never approve it."""
    _logged_in(ws)
    rid = _current(ws, rkey)
    refs, version = subscription_choices(ws, rid)
    if not _same(version, list_version):
        raise _fail("card_changed")
    if not refs:
        raise _fail("not_applicable")
    ref = _pick(refs, choice)
    key = ("subscription", rid, ref)
    again = _chosen_before(ws, key)
    if again is not None:
        return again
    created = _prepare_choice(ws, lambda: ws.session.prepare_subscription_cancel(rid, ref))
    with _unknown():
        label = compat.subscription_object(ref, controller=ws.controller) or str(ref)
        hint = compat.tr("chat.subscription.chosen", merchant=label)
    return _record_choice(ws, key, created, hint)


def schedule_cancel_choices(ws, rid: str) -> tuple[list[str], str]:
    """Project only the displayed reply's live, owned scheduled authorizations."""
    response = ws.session.response(rid)
    if response["intent"] != "clarify" or response["status"] != "clarify":
        offered = []
    else:
        standing = response["payload"].get("schedule_cancel") or {}
        offered = standing.get("candidates", []) if isinstance(standing, dict) else []
    state = ws.controller.state
    principal = ws.controller.device.principal
    owned = {
        ref: item
        for ref, item in sorted(state.authorizations.items())
        if getattr(item, "authorization_type", None) == "spa" and item.principal == principal
    }
    runs = {
        ref: [
            task.model_dump(mode="json")
            for task in sorted(state.schedules.values(), key=lambda row: row.id)
            if task.spa_ref == ref and task.status in {"pending", "due", "waiting_mfa"}
        ]
        for ref in offered
        if ref in owned
    }
    refs = [ref for ref in offered if ref in owned and runs.get(ref)]
    contacts = state.contacts
    targets = [getattr(getattr(owned[ref], "target", None), "contact_ref", None) for ref in refs]
    facts = {
        "refs": refs,
        "authorizations": [owned[ref].model_dump(mode="json") for ref in refs],
        "open_runs": {ref: runs[ref] for ref in refs},
        # What each button's label is also read from (R3-E10): the owner's authorizations
        # in order (the ordinal) and the target contact's record (the name).
        "peers": sorted(owned),
        "contacts": [
            contacts[ref].model_dump(mode="json") if ref in contacts else None for ref in targets
        ],
    }
    return refs, tasks.list_version(ws, f"chat-schedule-cancel:{rid}", [facts])


def choose_schedule_cancel(ws, rkey: str, choice: str, list_version: str) -> str:
    """Prepare the native revocation card for one version-bound displayed choice."""
    _logged_in(ws)
    rid = _current(ws, rkey)
    refs, version = schedule_cancel_choices(ws, rid)
    if not _same(version, list_version):
        raise _fail("card_changed")
    if not refs:
        raise _fail("not_applicable")
    ref = _pick(refs, choice)
    key = ("schedule", rid, ref)
    again = _chosen_before(ws, key)
    if again is not None:
        return again
    created = _prepare_choice(ws, lambda: ws.session.prepare_schedule_cancel(rid, ref))
    with _unknown():
        label = compat.schedule_object(ref, controller=ws.controller) or str(ref)
        hint = compat.tr("chat.schedule_cancel.chosen", label=label)
    return _record_choice(ws, key, created, hint)


def aa_selectable(response: dict) -> bool:
    """The shared precondition for offering and preparing an AA participant choice."""
    payload = response["payload"]
    parse = payload.get("aa_parse") or {}
    return (
        response["intent"] == "transfer"
        and response["status"] == "awaiting_device_selection"
        and isinstance(payload.get("aa"), dict)
        and parse.get("status") == "unresolved"
        and parse.get("reason") in {"participants", "ambiguous"}
    )


def complete_aa(ws, rkey: str, choices: list[str]) -> str:
    """``aa_completion_card`` (B12): a new reply for the chosen participants."""
    rid = _current(ws, rkey)
    if len(set(choices)) != len(choices):
        raise _fail("bad_json")
    roster = ws.session.aa_roster()
    refs = [_pick(roster, choice) for choice in choices]
    response = ws.session.response(rid)
    split = response["payload"].get("aa")
    if not aa_selectable(response):
        raise _fail("not_applicable")
    if len(refs) != split.get("participants", 0) - 1:
        raise _fail("invalid_input")
    key = ("aa", rid)
    again = _chosen_before(ws, key)
    if again is not None:
        return again
    # The same eligibility check controls the form and precedes the product call.
    created = _prepare_choice(ws, lambda: ws.session.prepare_aa(rid, refs))
    with _unknown():
        # The chat page's own separator (U+3001), fixed at sending like every other turn.
        names = "\u3001".join(compat.reference_label(ref, controller=ws.controller) for ref in refs)
        hint = compat.tr("chat.aa.chosen", names=names)
    return _record_choice(ws, key, created, hint)


def _withdraw(ws, marker: str) -> str:
    """``cancel_pending``: the chat's own Stop; its receipt is kept in the transcript."""
    _logged_in(ws)
    with live.operation(ws, CHAT, bind=False):
        response = ws.session.receive("stop" if marker == "cancel" else "handoff")
    rid = response["response_id"]
    rkey = _register(ws, rid)
    _turn(ws, "user", rid, marker=marker)
    _turn(ws, "assistant", rid)
    ws.current[CHAT] = rid
    return rkey


def chat_stop(ws) -> str:
    return _withdraw(ws, "cancel")


def handoff(ws) -> str:
    return _withdraw(ws, "handoff")


def compensate_latest(ws, plan_version: str) -> None:
    """The task panel's Compensate (B10): the latest registered plan's locked funds, only
    while it is still the plan the panel showed."""
    _logged_in(ws)
    plan_id = tasks.compensation_plan(ws)
    if not _same(tasks.list_version(ws, "compensation", [plan_id]), plan_version):
        raise _fail("card_changed")
    if plan_id is None:
        ws.business["compensation"] = {"status": "no_active_plan"}
        return
    if any(
        task.source == "compensation" and tasks.task_step(ws, task).step not in tasks.CLOSED
        for task in ws.tasks.values()
    ):
        # One compensation card at a time: the open one is finished or cancelled first, so
        # two executable compensation cards never stand side by side (R3-E15).
        raise _fail("not_applicable")
    try:
        result = ws.controller.compensate(plan_id)
    except Exception:
        # The gateway records the compensation request before the card is prepared: any
        # exception from inside, a ``WebError`` of any status included, has an unknown
        # result, and the session is distrusted (R3-E3, RW-4''').
        raise errors.WebOutcomeUnknown() from None
    ws.business["compensation"] = deepcopy(result)
    if result.get("pending_id") and result.get("card"):
        _business(ws, "compensation", "pending", result["pending_id"])


# --- device -----------------------------------------------------------------------------


def select_nodes(ws, tkey: str, card_version: str, nodes: list[str]) -> None:
    """Register a plan's checked nodes in display order; any change voids the old token."""
    task = tasks.find(ws, tkey)
    plan = compat.plan_entry(ws.controller, task.identity)
    if plan is None:
        raise _fail("not_applicable")
    if task.source == "chat" and ws.current.get(CHAT) != task.rid:
        raise _fail("not_current")
    if tasks.task_step(ws, task).step not in SELECTABLE:
        raise _fail("not_applicable")
    if not _same(tasks.nodes_version(ws, task), card_version):
        raise _fail("card_changed")
    if len(set(nodes)) != len(nodes):
        raise _fail("bad_json")
    shown = plan["node_ids"]
    picked = set()
    for node in nodes:
        index = int(node[1:]) if isinstance(node, str) and NODE.fullmatch(node) else -1
        if not 0 <= index < len(shown):
            raise _fail("not_found")
        picked.add(index)
    ws.selection[tkey] = [shown[index] for index in sorted(picked)]


def _confirm_call(ws, task: DeviceTask, info) -> dict:
    c = ws.controller
    if task.source == "chat" and task.kind == "envelope":
        selected = (
            list(ws.selection.get(task.key) or [])
            if info.step in {"confirm_plan", "confirm_subset"}
            else None
        )
        return compat.IMAdapter(ws.session).click("confirm", task.rid, selected_node_ids=selected)
    if task.source == "chat":
        local = ws.local[task.rid]
        pending = local.get("pending_id")
        if pending:
            result = c.approve_pending(pending)
        else:
            result = c.confirm_transfer(ws.session.response(task.rid)["payload"]["draft_id"])
        ws.local[task.rid] = {**local, **result}
        return result
    if task.kind == "plan":
        result = c.confirm_plan(task.identity, list(ws.selection.get(task.key) or []))
        replacement = result.get("replacement")
        if replacement and replacement.get("plan_id"):
            # app::confirm_birthday: the redisplayed draft replaces the shown one.
            task.identity = replacement["plan_id"]
            ws.business["birthday_draft"] = deepcopy(replacement)
        elif result.get("status") == "settled":
            ws.business.pop("birthday_draft", None)
        return result
    result = c.approve_pending(task.identity)
    replacement = result.get("replacement")
    if replacement and replacement.get("pending_id"):
        # app::approve_saved: the redisplayed card replaces the stored pending.
        for slot in SLOTS.get(task.source, ()):
            stored = ws.business.get(slot)
            if isinstance(stored, dict) and stored.get("pending_id") == task.identity:
                ws.business[slot] = deepcopy(replacement)
        task.identity = replacement["pending_id"]
    return result


def device_confirm(ws, tkey: str, gesture: str, card_version: str) -> dict:
    task, info = tasks.verify(ws, tkey, "confirm", gesture, card_version)
    prefix = tasks.prefix_of(task)
    with tasks.commit(ws, task, prefix):
        with live.operation(ws, prefix, task.rid):
            result = _confirm_call(ws, task, info)
    return {"status": str(result.get("status"))}


def device_mfa_request(ws, tkey: str, gesture: str, card_version: str) -> dict:
    task, _ = tasks.verify(ws, tkey, "mfa_request", gesture, card_version)
    prefix = tasks.prefix_of(task)
    with tasks.commit(ws, task, prefix):
        with live.operation(ws, prefix, task.rid, bind=False):
            result = ws.controller.request_mfa(task.identity)
    challenge = result.get("challenge_id")
    if result.get("status") == "waiting_mfa" and challenge:
        try:
            deliver(ws, task.identity, challenge)
        except (ValueError, KeyError, RuntimeError):
            # The closing refresh delivers it again (idempotent).
            pass
        except Exception:
            # The code was requested: any other failure, a ``WebError`` of any status
            # included, has an unknown result (RW-4'''').
            raise errors.WebOutcomeUnknown() from None
    return {"status": str(result.get("status"))}


def _sync_verified(ws, task: DeviceTask) -> None:
    """Bring a chat reply up to its pending's own verification (no code is filled in).

    The session's ``verify_mfa`` calls the controller without a code and relies on that
    pending being verified already, so both are checked here, explicitly: the reply must
    still act on this task's pending, and the controller must hold it as verified.
    Otherwise the task closes as unknown and the session is never called.
    """
    action = tasks.chat_action(ws, task.rid)
    if (
        action is None
        or action[1] != task.identity
        or compat.pending_verified(ws.controller, task.identity) is not True
    ):
        raise _fail("outcome_unknown")
    compat.IMAdapter(ws.session).click("verify_mfa", task.rid)


def device_mfa_verify(ws, tkey: str, gesture: str, card_version: str, code: str) -> dict:
    if not isinstance(code, str) or not CODE.fullmatch(code):
        raise _fail("invalid_input")
    task, _ = tasks.verify(ws, tkey, "mfa_verify", gesture, card_version)
    challenge = (compat.pending_entry(ws.controller, task.identity) or {}).get("challenge_id")
    prefix = tasks.prefix_of(task)
    with tasks.commit(ws, task, prefix):
        with live.operation(ws, prefix, task.rid, bind=False):
            result = ws.controller.complete_mfa(task.identity, code=code)
            if result.get("status") == "mfa_verified":
                compat.OUTBOX.mark(ws.pair, compat.sms_ref(challenge), "used")
                if task.source == "chat" and task.kind == "envelope":
                    _sync_verified(ws, task)
                elif task.source == "chat":
                    ws.local[task.rid] = {**ws.local[task.rid], **result}
    return {"status": str(result.get("status"))}


def device_cancel(ws, tkey: str) -> None:
    """Cancel is the global withdrawal on every step, as the pages say."""
    task = tasks.find(ws, tkey)
    if task.source != "chat":
        stop_all(ws, marker="cancel")
        return
    _withdraw(ws, "cancel")
    if task.kind == "local":
        ws.local.pop(task.rid, None)


def stop_all(ws, marker: str = "stop_all") -> None:
    """``app::stop_all`` (B16); it does not pass through the chat, as on the page."""
    _retire_authorization_lab(ws)
    with live.operation(ws, CHAT, bind=False):
        bank = ws.controller.stop()
        ws.session.withdraw_pending()
        wealth = ws.wealth.stop()
    result = bank
    if wealth.get("execution"):
        result = {
            "status": "interrupted",
            "message": wealth["message"],
            "bank_result": bank,
            "wealth_result": wealth,
        }
    for slot in RETIRED_SLOTS:
        ws.business.pop(slot, None)
    ws.requests.clear()
    _drop_business(ws)
    turn = _turn(ws, "system", None, marker=marker)
    ws.business.setdefault("stop_results", {})[turn.id] = deepcopy(result)
    ws.business["stop_result"] = deepcopy(result)


# --- business pages (ext) ---------------------------------------------------------------


CARD_KINDS = (
    "freeze_card",
    "unfreeze_card",
    "report_card_lost",
    "restore_card",
    "lower_card_limit",
    "raise_card_limit",
    "restrict_card",
    "relax_card_restriction",
)
CARD_CATEGORIES = ("online", "overseas", "cash", "entertainment")


def cards_preview(ws, choice: str, list_version: str, kind: str, limit=None, restrictions=None):
    """A listed card and explicit resulting facts enter the existing unsigned preview."""
    _logged_in(ws)
    card = _pick_listed(ws, "cards", tasks.cards(ws), choice, list_version)
    if kind not in CARD_KINDS:
        raise _fail("invalid_input")
    change = None
    if kind in {"lower_card_limit", "raise_card_limit"}:
        if (
            not isinstance(limit, str)
            or not re.fullmatch(r"[0-9]{1,9}(?:\.[0-9]{1,2})?", limit)
            or restrictions is not None
        ):
            raise _fail("invalid_input")
        change = {"limit": limit}
    elif kind in {"restrict_card", "relax_card_restriction"}:
        if (
            limit is not None
            or not isinstance(restrictions, list)
            or any(item not in CARD_CATEGORIES for item in restrictions)
        ):
            raise _fail("invalid_input")
        change = {"restrictions": sorted(restrictions)}
    elif limit is not None or restrictions is not None:
        raise _fail("invalid_input")
    current = ws.controller.state.cards[card]
    allowed_states = {
        "freeze_card": {"active"},
        "unfreeze_card": {"frozen"},
        "report_card_lost": {"active", "frozen"},
        "restore_card": {"lost"},
    }
    if current.status not in allowed_states.get(kind, {"active", "frozen"}):
        raise _fail("not_applicable")
    if kind == "lower_card_limit" and Fraction(limit) >= current.limit:
        raise _fail("not_applicable")
    if kind == "raise_card_limit" and Fraction(limit) <= current.limit:
        raise _fail("not_applicable")
    if kind == "restrict_card" and not set(restrictions) > set(current.restrictions):
        raise _fail("not_applicable")
    if kind == "relax_card_restriction" and not set(restrictions) < set(current.restrictions):
        raise _fail("not_applicable")
    payload = [card, current.version, kind, change]
    if f"request:cards:{compat.H(payload)}" not in ws.requests and any(
        task.source == "cards" and tasks.task_step(ws, task).step not in tasks.CLOSED
        for task in ws.tasks.values()
    ):
        raise _fail("not_applicable")
    with live.operation(ws, "cards", bind=False):
        result = _preview(
            ws,
            "cards",
            payload,
            lambda request: ws.controller.preview_card_operation(
                kind, card_ref=card, change=change, request_id=request
            ),
        )
    ws.business["card_pending"] = deepcopy(result)
    if result.get("pending_id") and result.get("card"):
        _business(ws, "cards", "pending", result["pending_id"])


def subscriptions_query(ws) -> None:
    _logged_in(ws)
    with live.operation(ws, "subscriptions", bind=False):
        result = _preview(
            ws,
            "subscriptions",
            None,
            lambda request: ws.controller.preview_subscriptions(request_id=request),
        )
    ws.business["subscription_pending"] = deepcopy(result)
    if result.get("pending_id") and result.get("card"):
        _business(ws, "subscriptions", "pending", result["pending_id"])


def _cancellable(ws) -> list[str]:
    """The authorizations the newest settled query listed (the list the accounts tab shows)."""
    return list((tasks.subscription_listing(ws) or {}).get("cancellable") or [])


def subscriptions_cancel(ws, choice: str, list_version: str) -> None:
    _logged_in(ws)
    reference = _pick_listed(ws, "subscriptions", _cancellable(ws), choice, list_version)
    with live.operation(ws, "subscriptions", bind=False):
        result = _preview(
            ws,
            "cancel-subscription",
            reference,
            lambda request: ws.controller.cancel_subscription(reference, request_id=request),
        )
    ws.business["subscription_cancel"] = deepcopy(result)
    if result.get("pending_id") and result.get("card"):
        _business(ws, "subscriptions", "pending", result["pending_id"])


def billing_analyze(ws, period: str | None) -> None:
    _logged_in(ws)
    with live.operation(ws, "billing", bind=False):
        if period is None:
            result = compat.load_billing(ws.controller)
        else:
            result = compat.load_billing(ws.controller, period)
    ws.business["billing"] = deepcopy(result)


def wealth_compare(ws, objective: str) -> None:
    if objective not in {"net_return", "total_fee", "liquidity"}:
        raise _fail("invalid_input")
    _logged_in(ws)
    with live.operation(ws, "wealth", bind=False):
        result = ws.controller.compare_investments(objective)
    ws.business["wealth_comparison"] = deepcopy(result)
    ws.business["wealth_objective"] = objective


def wealth_risk(ws, answers: list[int], version: str) -> None:
    """The submit gesture signs all five answers on the purchasing controller's device."""
    _logged_in(ws)
    if not _same(tasks.risk_version(ws), version):
        raise _fail("card_changed")
    if len(answers) != 5 or any(type(item) is not int or not 0 <= item <= 4 for item in answers):
        raise _fail("invalid_input")
    if ws.controller.risk_retake_blocked():
        raise _fail("not_applicable")
    with live.operation(ws, "wealth", bind=False):
        result = ws.controller.submit_risk_answers(answers)
    ws.business["risk_result"] = deepcopy(result)
    if result.get("status") == "settled":
        ws.business.pop("wealth_comparison", None)


def wealth_risk_retake(ws, version: str) -> None:
    """An explicit clock gesture, never an automatic delay or an answer submission."""
    _logged_in(ws)
    if not _same(tasks.risk_version(ws), version):
        raise _fail("card_changed")
    if ws.controller.risk_retake_blocked():
        raise _fail("not_applicable")
    state = ws.controller.state
    customer = state.customers[ws.controller.device.principal]
    profile = state.risk_assessments.get(customer.risk_profile_ref or "")
    if profile is None or profile.issued_at != state.now:
        raise _fail("not_applicable")
    with live.operation(ws, "wealth", bind=False):
        ws.controller.advance_clock(1)


def wealth_purchase(ws, choice: str | None, list_version: str) -> None:
    _logged_in(ws)
    comparison = ws.business.get("wealth_comparison")
    if not comparison:
        raise _fail("not_applicable")
    objective, *listed = tasks.wealth_listing(ws)
    # The version covers the objective too: another comparison is another list.
    if not _same(tasks.list_version(ws, "wealth", tasks.wealth_listing(ws)), list_version):
        raise _fail("card_changed")
    product = None if choice is None else _pick(listed, choice)
    with live.operation(ws, "wealth", bind=False):
        result = _preview(
            ws,
            "investment",
            [objective, product],
            lambda request: ws.controller.preview_investment(product, request_id=request),
        )
    ws.business["investment_pending"] = deepcopy(result)
    pending = result.get("pending_id")
    if pending and ws.controller.kernel_refusal(pending) is None and result.get("card"):
        _business(ws, "wealth", "pending", pending)


def wealth_redeem(ws, choice: str, list_version: str) -> None:
    _logged_in(ws)
    position = _pick_listed(ws, "positions", tasks.positions(ws), choice, list_version)
    with live.operation(ws, "wealth", bind=False):
        result = _preview(
            ws,
            "redemption",
            position,
            lambda request: ws.controller.redeem(position, request_id=request),
        )
    ws.business["redemption_pending"] = deepcopy(result)
    if result.get("pending_id") and result.get("card"):
        _business(ws, "wealth", "pending", result["pending_id"])


def preset_r4(ws) -> None:
    """The suitability refusal demo runs on its own isolated controller (INV-11.5)."""
    with live.operation(ws, "wealth", bind=False):
        ws.demos["suitability_r4"] = compat.prepare_demo("suitability_r4")


def _retire_authorization_lab(ws) -> None:
    # Imported at the lifecycle boundary to avoid a service/lab import cycle.
    from corridor.web import lab

    lab.retire(ws)
