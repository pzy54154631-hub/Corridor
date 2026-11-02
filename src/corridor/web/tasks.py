"""Device tasks, the step whitelist, gesture tokens and the two-phase call.

A device task is a chat reply (or a business page action) that still holds a card the
user can act on. ``task_step`` reads the controller's own state and names the one step
the task is in; only the whitelisted steps can carry a single-use gesture token, and
every common precondition (U1-U8) is checked before a step reaches the whitelist. A
token proves possession of this session's state, nothing more: it is bound to the task,
the operation, a digest of the authoritative cards (with a plan's ordered nodes and
registered selection) and the generation, and it is removed before the controller is
called. Two conservative web rules close payments the per-card recipient check leaves
open: a payment older than a reply with a scam cue is not resumed (a later card's own
answers clear only that card), and a plan that pays someone is graded node by node with
the per-card check's own level, gets no token when any node would be gated, and binds
that evaluation into its token. A reply whose own card copy differs from the
authoritative card in a business fact is never signed. The digest also binds the
records an object row's derived label (name, ordinal) is read from: the device signs
the object reference, the page maps it to a readable name, and a changed mapping voids
the token.
"""

import hmac
import secrets
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from hashlib import sha256

from corridor.web import compat, errors, purchase
from corridor.web.state import DeviceTask, Gesture

fraud_hint = compat.fraud_hint

RECENT_TURNS = 40
OPS = {
    "review_purchase": "confirm",
    "confirm": "confirm",
    "confirm_intent": "confirm",
    "final_confirm": "confirm",
    "confirm_plan": "confirm",
    "confirm_subset": "confirm",
    "mfa_request": "mfa_request",
    "mfa_code": "mfa_verify",
}
# A task in one of these steps accepts no gesture again (only Cancel).
CLOSED = frozenset({"done", "refused", "withdrawn", "closed_failed", "closed_unknown"})
# Finished tasks leave the device pane unless their reply is the current one.
FINISHED = frozenset({"done", "refused", "withdrawn"})
REFUSED_RESULTS = frozenset({"rejected", "refused", "failed", "blocked"})
REFUSED_ENVELOPES = frozenset({"rejected", "refused", "outside_lock_month"})
DONE_ENVELOPES = frozenset({"settled", "controlled"})
LOCAL_DONE = frozenset({"settled", "controlled"})
LOCAL_REFUSED = frozenset({"rejected", "refused", "failed"})
CHAT_PREFIX = "domestic"
# Card kinds that pay someone: the kinds the recipient check covers.
PAYMENTS = frozenset(fraud_hint.KINDS)
# The text cues that make a recipient check strict (scam_friction.STRONG_CUES).
STRONG_CUES = frozenset({"authority", "external"})
VERSION_HEX = 32


@dataclass(frozen=True)
class StepInfo:
    step: str
    op: str | None
    identity: str | None
    detail: dict = field(default_factory=dict)


def prefix_of(task: DeviceTask) -> str:
    return CHAT_PREFIX if task.source == "chat" else task.source


def _info(step: str, task: DeviceTask, **detail) -> StepInfo:
    return StepInfo(step, OPS.get(step), task.identity, detail)


def chat_action(ws, rid: str) -> tuple[str, str | None] | None:
    """(kind, identity) of what a chat reply acts on now; None when it acts on nothing."""
    response = ws.session.response(rid)
    payload = response["payload"]
    local = ws.local.get(rid)
    if local is not None and payload.get("draft_id"):
        return "local", local.get("pending_id") or payload.get("draft_id")
    mode = compat.envelope_mode(ws.session, rid)
    if mode == "plan":
        draft = payload.get("replacement", payload)
        return "envelope", payload.get("plan_id") or draft.get("plan_id")
    if mode in {"transfer", "pending"}:
        return "envelope", payload.get("pending_id") or payload.get("draft_id")
    if mode == "purchase":
        return "envelope", rid
    return None


def _chat_rids(ws) -> list[str]:
    rids = []
    for turn in ws.transcript[-RECENT_TURNS:]:
        if turn.role == "assistant" and turn.rid is not None and turn.rid not in rids:
            rids.append(turn.rid)
    return rids


def collect(ws) -> list[DeviceTask]:
    """Register the chat tasks of the recent replies and list every task to show.

    The current reply's task comes first (it stays listed after it finished, with its
    result); then business tasks and other chat replies that still hold a card, newest
    first. Idempotent: a second call on unchanged state changes nothing.
    """
    if not compat.logged_in(ws.controller):
        return []
    current = ws.current.get("domestic")
    first, chat = [], []
    for rid in _chat_rids(ws):
        rkey = ws.rkey_of.get(rid)
        if rkey is None:
            continue
        key = "T" + rkey
        task = ws.tasks.get(key)
        action = chat_action(ws, rid)
        if action is not None and (action[1] or task is not None):
            kind, identity = action
            if task is None:
                task = DeviceTask(key, "chat", rkey, rid, identity, kind)
                if compat.envelope_mode(ws.session, rid) == "purchase":
                    task.review = purchase.snapshot(ws, rid)
                ws.tasks[key] = task
            else:
                # A settled envelope no longer names its action; the task keeps the last one.
                task.kind = kind
                task.identity = identity or task.identity
        if task is None:
            continue
        if rid == current:
            first.append(task)
        elif task_step(ws, task).step not in FINISHED:
            chat.append(task)
    business = sorted(
        (task for task in ws.tasks.values() if task.source != "chat"),
        key=lambda task: int(task.key[2:]),
        reverse=True,
    )
    # The newest business card stays to show its result; older finished ones leave.
    shown = [
        task
        for index, task in enumerate(business)
        if index == 0 or task_step(ws, task).step not in FINISHED
    ]
    return first + shown + chat[::-1]


def find(ws, tkey: str) -> DeviceTask:
    for task in collect(ws):
        if task.key == tkey:
            return task
    raise errors.error("not_found")


def subscription_listing(ws) -> dict | None:
    """The settled subscription query that the cancel choices ``c<i>`` index, for the
    service and the accounts tab alike: the newest page query with a cancellable list,
    else the newest settled query reply over chat."""
    queries = sorted(
        (task for task in ws.tasks.values() if task.source == "subscriptions"),
        key=lambda task: int(task.key[2:]),
        reverse=True,
    )
    for task in queries:
        result = compat.result_entry(ws.controller, task.identity) or {}
        if result.get("status") == "settled" and result.get("cancellable") is not None:
            return result
    for row in reversed(ws.session.history):
        if row.get("intent") == "subscription_query":
            response = ws.session.response(row["response_id"])
            if response["status"] == "settled":
                return response["payload"]
    return None


def cards(ws) -> list[str]:
    return [
        card["id"]
        for card in ws.controller.snapshot()["cards"]
        if card.get("owner") == ws.controller.device.principal
    ]


def risk_version(ws) -> str:
    state = ws.controller.state
    customer = state.customers[ws.controller.device.principal]
    return list_version(
        ws,
        "risk",
        [
            state.policy.model_dump(mode="json"),
            customer.model_dump(mode="json"),
            state.now,
            ws.business.get("risk_result"),
        ],
    )


def positions(ws) -> list[str]:
    """The customer's active positions, in snapshot order: the list ``c<i>`` redeems from."""
    c = ws.controller
    return [
        position["id"]
        for position in c.snapshot()["positions"]
        if position.get("owner") == c.device.principal and position.get("status") == "active"
    ]


def compensation_plan(ws) -> str | None:
    """The plan the task panel's Compensate acts on (``plans[-1]``, as on the page)."""
    plans = ws.controller.snapshot()["plans"]
    return plans[-1]["plan"]["plan_id"] if plans else None


def wealth_listing(ws) -> list:
    """The comparison a purchase choice indexes: its objective, then its candidates."""
    comparison = ws.business.get("wealth_comparison") or {}
    objective = ws.business.get("wealth_objective", "net_return")
    return [objective, *(comparison.get("candidate_ids") or [])]


def _chat_status(ws, task: DeviceTask) -> str | None:
    """The status the reply shows: the local selection's, else the envelope's."""
    if task.rid is None:
        return None
    envelope = ws.session.response(task.rid)["status"]
    local = ws.local.get(task.rid)
    if task.kind == "local" and local is not None:
        if envelope != "withdrawn" or local.get("status") in LOCAL_DONE | LOCAL_REFUSED:
            return local.get("status")
    return envelope


def _gate(controller, identity: str) -> tuple[str | None, dict | None]:
    status = controller.scam_gate_status(identity)
    if status is None:
        return None, None
    return ("stale" if status["stale"] else status["decision"]), status


def _selection_step(ws, task: DeviceTask, nodes: list[str]) -> StepInfo:
    selected = ws.selection.get(task.key) or []
    if nodes and selected == nodes:
        return _info("confirm_plan", task, count=len(selected), total=len(nodes))
    if selected and selected == nodes[: len(selected)]:
        return _info("confirm_subset", task, count=len(selected), total=len(nodes))
    return _info("select_nodes", task, count=len(selected), total=len(nodes), error=bool(selected))


def risky_words(text, external_files: int = 0) -> bool:
    """The user's own words name an authority or an outside instruction, say someone told
    them to pay, or came with a file: the fraud warning's gating text cues."""
    if external_files:
        return True
    if not isinstance(text, str) or not text:
        return False
    codes = set(fraud_hint.signals({"kind": "transfer"}, text=text))
    return bool(codes & STRONG_CUES) or fraud_hint.directed(text)


def pays(ws, identity: str | None) -> bool:
    cards = compat.authoritative_cards(ws.controller, identity)
    return any(isinstance(card, dict) and card.get("kind") in PAYMENTS for card in cards)


def _user_turn(ws, rid: str | None):
    for turn in reversed(ws.transcript):
        if rid is not None and turn.rid == rid and turn.role == "user":
            return turn
    return None


def _start(ws, task: DeviceTask) -> int | None:
    """The first transcript index after the task entered the conversation: the turn after
    its reply's last turn, or its own mark (``since`` is the transcript length when a
    business task was registered, i.e. the index of the next turn). One convention, so
    the first later user turn is always scanned (R2-E2)."""
    rid = task.rid or task.origin
    if rid is not None:
        found = [index for index, turn in enumerate(ws.transcript) if turn.rid == rid]
        if found:
            return found[-1] + 1
    return task.since


def _unresolved(ws, rid: str) -> bool:
    """A reply that says "stop and verify": the scam-phrase answer, a paused recipient
    check, or words with a scam cue. The later reply's own answers ("proceed") clear only
    its own card: they say nothing about an older payment (R2-E1), so they are ignored."""
    if (ws.session.response(rid).get("payload") or {}).get("scam_phrase"):
        return True
    action = chat_action(ws, rid)
    identity = action[1] if action else None
    status = ws.controller.scam_gate_status(identity) if identity else None
    if status is not None and not status["stale"] and status["decision"] == "pause":
        return True
    turn = _user_turn(ws, rid)
    return turn is not None and risky_words(turn.hint, turn.external_files)


def later_risk(ws, task: DeviceTask) -> bool:
    """A newer reply carries a scam cue. Whether it concerns this payment cannot be told
    here, so the older payment is not resumed: only Cancel and a fresh request remain."""
    start = _start(ws, task)
    if start is None:
        return False
    own = {task.rid, task.origin}
    seen = set()
    for turn in ws.transcript[start:]:
        rid = turn.rid
        if turn.role != "user" or rid is None or rid in own or rid in seen:
            continue
        seen.add(rid)
        if rid in ws.rkey_of and _unresolved(ws, rid):
            return True
    return False


def plan_evaluation(ws, task: DeviceTask) -> dict | None:
    """Each paying node of a plan graded with the per-card recipient check's own level, on
    the plan's own words and the node's contact facts (R2-E9); None when nothing pays.

    The result and the context it was computed from are bound into the plan's token
    digest, so a changed contact fact or record voids the token.
    """
    cards = compat.authoritative_cards(ws.controller, task.identity)
    paying = [card for card in cards if isinstance(card, dict) and card.get("kind") in PAYMENTS]
    if not paying:
        return None
    turn = _user_turn(ws, task.rid or task.origin)
    words = turn.hint if turn is not None else None
    files = turn.external_files if turn is not None else 0
    nodes = []
    for card in paying:
        grade, codes, context = ws.controller._scam_grade([card], words, files)
        nodes.append({
            "card": compat.scam_friction.card_identity(card),
            "grade": int(grade),
            "cues": list(codes),
            "context": context,
        })
    return {
        "words": turn is not None,
        "directed": bool(fraud_hint.directed(words)),
        "risky": turn is not None and risky_words(words, files),
        "nodes": nodes,
    }


def plan_risk(ws, task: DeviceTask) -> bool:
    """A plan that pays someone is not signed when a paying node would be gated by the
    per-card recipient check (a new payee, an authority or outside cue, a file), when its
    words say someone told the user to pay, or when its words cannot be found."""
    evaluation = plan_evaluation(ws, task)
    if evaluation is None:
        return False
    return (
        not evaluation["words"]
        or evaluation["directed"]
        or evaluation["risky"]
        or any(node["grade"] > 0 for node in evaluation["nodes"])
    )


def shown_cards(ws, task: DeviceTask) -> list[dict] | None:
    """The card copy a chat reply shows: its local selection's, its plan nodes' or its own."""
    if task.source != "chat" or task.rid is None:
        return None
    response = ws.session.response(task.rid)
    if task.kind == "local":
        card = (ws.local.get(task.rid) or {}).get("card")
        return [card] if card else None
    nodes = response["payload"].get("replacement", response["payload"]).get("nodes") or []
    if nodes:
        return [node.get("card") or {} for node in nodes]
    return [response["card"]] if response.get("card") else None


def cards_mismatch(ws, task: DeviceTask) -> bool | None:
    """Whether a chat reply's own card copy differs from the authoritative cards in a
    business fact; None when either side is missing."""
    if task.source != "chat" or task.rkey is None:
        return None
    authoritative = compat.authoritative_cards(ws.controller, task.identity)
    shown = shown_cards(ws, task)
    if not authoritative or shown is None:
        return None
    return [compat.business_facts(card) for card in shown] != [
        compat.business_facts(card) for card in authoritative
    ]


# The trusted stores a card's object reference names. The product UI derives an object
# row's label (merchant or product name, ordinal) from these records; the label is not
# in the signed card, so the records it is read from are bound into the digest (R2-E10).
OBJECT_STORES = {
    "authorization_ref": "subscriptions",
    "schedule_ref": "authorizations",
    "card_ref": "cards",
    "position_ref": "positions",
    "contact_ref": "contacts",
}


def _record(item):
    dump = getattr(item, "model_dump", None)
    return dump(mode="json") if callable(dump) else str(item)


def _owner(item):
    return getattr(item, "owner", None) or getattr(item, "principal", None)


def object_facts(ws, cards: list) -> list[dict]:
    """Per card, each referenced object's record, its owner's peer list (the ordinal's
    source) and what else its shown label is read from: a subscription's merchant, a
    holding's product (named by its catalog entry) and a standing authorization's target
    contact (R2-E10, R3-E10)."""
    state = getattr(ws.controller, "state", None)
    merchants = getattr(state, "merchants", None) or {}
    contacts = getattr(state, "contacts", None) or {}
    rows = []
    for card in cards or []:
        objects = card.get("objects") if isinstance(card, dict) else None
        row = {}
        for name, store in OBJECT_STORES.items():
            ref = objects.get(name) if isinstance(objects, dict) else None
            table = getattr(state, store, None)
            if not ref or not isinstance(table, dict) or ref not in table:
                continue
            item = table[ref]
            owner = _owner(item)
            entry = {
                "record": _record(item),
                "peers": [key for key, value in table.items() if _owner(value) == owner],
            }
            merchant = getattr(item, "merchant_ref", None)
            if merchant and merchant in merchants:
                entry["merchant"] = _record(merchants[merchant])
            product = getattr(item, "product_ref", None)
            if product:
                found = compat.display_names.catalog_product(product, state)
                entry["product"] = _record(found) if found is not None else None
            contact = getattr(getattr(item, "target", None), "contact_ref", None)
            if contact and contact in contacts:
                entry["contact"] = _record(contacts[contact])
            row[name] = entry
        rows.append(row)
    return rows


def gate_context(ws, task: DeviceTask) -> str | None:
    """What a chat reply's recipient check was evaluated on: the reply and its user turn."""
    turn = _user_turn(ws, task.rid)
    if turn is None:
        return None
    return compat.H({"rid": task.rid, "text": turn.hint, "files": turn.external_files})


def task_step(ws, task: DeviceTask) -> StepInfo:
    """The task's step: the display-only steps first, then the whitelist (pure)."""
    c = ws.controller
    identity = task.identity
    if not compat.logged_in(c) or not identity:
        return _info("unknown", task)
    chat = task.source == "chat"
    reviewing = chat and compat.envelope_mode(ws.session, task.rid) == "purchase"
    status = _chat_status(ws, task) if chat else None
    pending = compat.pending_entry(c, identity)
    draft = compat.draft_entry(c, identity)
    plan = compat.plan_entry(c, identity)
    result = compat.result_status(c, identity)
    if (result is not None and result not in REFUSED_RESULTS) or status in DONE_ENVELOPES:
        return _info("done", task)
    if (
        result is not None
        or compat.is_closed(c, identity)
        or status in REFUSED_ENVELOPES
        or (status in LOCAL_REFUSED and task.kind == "local")
        or (pending is not None and c.kernel_refusal(identity) is not None)
    ):
        return _info("refused", task)
    present = (
        reviewing or pending is not None or (draft or {}).get("has_display") or plan is not None
    )
    if status == "withdrawn" or not present:
        return _info("withdrawn", task)
    if task.phase == "closed_unknown" or getattr(ws, "untrusted", False):
        return _info("closed_unknown", task)
    if task.phase == "closed_failed" or (prefix_of(task), task.rid) in ws.failures:
        return _info("closed_failed", task)
    if task.phase != "idle":
        return _info("unknown", task)
    if (ws.safety or {}).get("state") == "Locked":
        return _info("account_locked", task)
    if chat and ws.current.get("domestic") != task.rid:
        return _info("not_current", task)
    if reviewing:
        if not purchase.latest(ws, task):
            return _info("withdrawn", task)
        if not purchase.valid(ws, task):
            return _info("closed_facts", task)
        # The preference review has no payment card to run the recipient gate on.
        # The native confirm prepares that card; its ordinary gate/MFA checks follow.
        return _info("review_purchase", task)
    if chat and cards_mismatch(ws, task):
        # The reply shows a card whose business facts differ from the card a gesture would
        # sign: nothing is signed, only Cancel and a fresh request remain (R2-E14).
        return _info("closed_facts", task)
    if pays(ws, identity) and later_risk(ws, task):
        return _info("gate_later", task)
    if plan is not None and plan_risk(ws, task):
        return _info("gate_plan", task)
    decision, gate = _gate(c, identity)
    if chat and plan is None:
        record = ws.gate_eval.get(identity)
        card = compat.scam_friction.cards_identity(compat.authoritative_cards(c, identity))
        if (
            record is None
            or card is None
            or record.get("card_identity") != card
            or record.get("context") != gate_context(ws, task)
            or record.get("result") not in {"level0", "gated"}
        ):
            return _info("gate_unevaluated", task)
        if record["result"] == "level0" and decision is not None and decision != "proceed":
            return _info("gate_blocked", task, decision=decision, challenge=False)
    if decision is not None and decision != "proceed":
        challenge = bool(gate and gate.get("challenge_issued"))
        return _info("gate_blocked", task, decision=decision, challenge=challenge)
    if chat and task.kind == "envelope":
        response = ws.session.response(task.rid)
        if not compat.render_message(response, controller=c)["facts_complete"]:
            return _info("closed_facts", task)
    if (
        pending is not None
        and pending["mode"] == "prepared"
        and not pending["verified"]
        and pending["challenge_id"]
        and not compat.challenge_active(c, pending["challenge_id"])
    ):
        return _info("closed_challenge", task)
    if plan is not None:
        if plan["confirmed"]:
            # Signed once without a settled result: never offered again, only Cancel.
            return _info("unknown", task)
        return _selection_step(ws, task, plan["node_ids"])
    if pending is not None:
        if pending["mode"] == "draft":
            return _info("confirm_intent", task)
        if pending["mode"] == "prepared":
            if pending["tier"] != "red":
                return _info("confirm", task)
            if compat.pending_verified(c, identity):
                return _info("final_confirm", task)
            if not pending["challenge_id"]:
                return _info("mfa_request", task)
            if compat.challenge_active(c, pending["challenge_id"]):
                return _info("mfa_code", task)
        return _info("unknown", task)
    if (draft or {}).get("has_display") and task.kind in {"envelope", "local"}:
        return _info("confirm", task)
    return _info("unknown", task)


def _selection(ws, task: DeviceTask, identity: str | None) -> list[str] | None:
    if compat.plan_entry(ws.controller, identity) is None:
        return None
    return ws.selection.get(task.key)


def _nodes(ws, identity: str | None) -> list[str] | None:
    plan = compat.plan_entry(ws.controller, identity)
    return None if plan is None else list(plan["node_ids"])


def digest(ws, task: DeviceTask, info: StepInfo) -> str:
    if info.step == "review_purchase":
        return compat.H({
            "step": info.step,
            "review": task.review,
            "current_review": purchase.snapshot(ws, task.rid),
            "current_terms": purchase.current_terms(ws, task),
        })
    cards = compat.authoritative_cards(ws.controller, info.identity)
    plan = compat.plan_entry(ws.controller, info.identity) is not None
    return compat.H({
        "identity": info.identity,
        "step": info.step,
        "cards": cards,
        "nodes": _nodes(ws, info.identity),
        "selection": _selection(ws, task, info.identity),
        "objects": object_facts(ws, cards),
        "plan_evaluation": plan_evaluation(ws, task) if plan else None,
    })


def _version(ws, value: str) -> str:
    # A concurrency check, not a credential: 128 bits of an HMAC under the session secret.
    return hmac.new(ws.secret, value.encode("ascii"), sha256).hexdigest()[:VERSION_HEX]


def card_version(ws, digest: str) -> str:
    return _version(ws, digest)


def nodes_version(ws, task: DeviceTask) -> str:
    """The version a plan's node selection is registered against: its ordered node ids
    and their cards."""
    cards = compat.authoritative_cards(ws.controller, task.identity)
    return _version(
        ws,
        compat.H({
            "identity": task.identity,
            "step": "select_nodes",
            "cards": cards,
            "nodes": _nodes(ws, task.identity),
            "selection": None,
            "objects": object_facts(ws, cards),
        }),
    )


def list_facts(ws, source: str, items: list, plans: list | None = None):
    """What a business list shows for its items, besides their ids (R2-E10): a version
    bound to these facts changes when a shown name, ordinal, amount, status or plan
    changes. Each row's label is read from the same records a card's object row is
    (``object_facts``), so the same reference shown under another name or ordinal voids
    the version (R3-E10). The chat's own lists pass their facts in ``items``
    (``service.subscription_choices``, ``service.schedule_cancel_choices``). A caller that
    holds this read's ``plans`` passes them, so the compensation list reads no snapshot."""
    c = ws.controller
    if source == "cards":
        state = c.state
        return {
            "rows": [_record(state.cards[ref]) for ref in items],
            "labels": [compat.display_names.label(ref, state) for ref in items],
        }
    if source == "subscriptions":
        return object_facts(ws, [{"objects": {"authorization_ref": ref}} for ref in items])
    if source == "positions":
        found = {row["id"]: row for row in c.snapshot()["positions"]}
        return {
            "rows": [found.get(ref) for ref in items],
            "objects": object_facts(ws, [{"objects": {"position_ref": ref}} for ref in items]),
        }
    if source == "wealth":
        # ``items`` is the objective, then the candidates; each candidate is shown by its
        # catalog entry's name.
        state = c.state
        products = [compat.display_names.catalog_product(ref, state) for ref in items[1:]]
        return {
            "comparison": ws.business.get("wealth_comparison"),
            "products": [_record(item) if item is not None else None for item in products],
        }
    if source == "compensation":
        plans = c.snapshot()["plans"] if plans is None else plans
        # The panel names the plan by its place among the plans (``compensate_hint``).
        return {"count": len(plans), "plan": plans[-1] if plans else None}
    return None


def list_version(ws, source: str, items: list, plans: list | None = None) -> str:
    """The version of a business page's ordered choice list, which ``c<i>`` indexes: the
    ids in order and the facts their rows show."""
    return _version(
        ws,
        compat.H({
            "source": source,
            "generation": ws.generation,
            "items": list(items),
            "facts": list_facts(ws, source, items, plans),
        }),
    )


def _same(held: str, given: str) -> bool:
    return hmac.compare_digest(held.encode("utf-8"), str(given).encode("utf-8"))


def verify(ws, tkey: str, op: str, gesture: str, card_version: str) -> tuple[DeviceTask, StepInfo]:
    """Pre-check a gesture against the state right now; a failure consumes nothing."""
    task = find(ws, tkey)
    ws.safety = compat.safety(ws.controller)
    info = task_step(ws, task)
    if info.step in CLOSED:
        raise errors.error("gesture_closed")
    held = ws.gestures.get(tkey)
    if held is None:
        raise errors.error("gesture_unknown")
    if held.op != op or held.generation != ws.generation or not _same(held.token, gesture):
        raise errors.error("gesture_mismatch")
    if info.op != op or info.step != held.step:
        raise errors.error("gesture_closed")
    current = digest(ws, task, info)
    # The token was issued for one card digest; a valid version of another card is refused.
    if not _same(held.digest, current) or not _same(_version(ws, current), card_version):
        raise errors.error("card_changed")
    return task, info


@contextmanager
def commit(ws, task: DeviceTask, prefix: str) -> Generator[None, None, None]:
    """Two phases: the token is gone and the task is in a call before the controller runs.

    An expected failure (recorded by ``live.operation``) closes the task as failed; any
    other exception closes it as unknown, never retried.
    """
    ws.gestures.pop(task.key, None)
    task.phase = "in_call"
    try:
        yield
    except errors.WebError as error:
        if error.code == "operation_failed":
            task.phase = "closed_failed"
            raise
        task.phase = "closed_unknown"
        raise errors.error("outcome_unknown") from None
    except Exception:
        task.phase = "closed_unknown"
        raise errors.error("outcome_unknown") from None
    task.phase = "idle"


def issue(ws) -> None:
    """Keep, issue or remove each task's token (called by refresh only)."""
    listed = {task.key: task for task in collect(ws)}
    for key in list(ws.gestures):
        if key not in listed:
            del ws.gestures[key]
    for key, task in listed.items():
        info = task_step(ws, task)
        if info.op is None:
            ws.gestures.pop(key, None)
            continue
        current = digest(ws, task, info)
        held = ws.gestures.get(key)
        if (
            held is not None
            and held.op == info.op
            and held.step == info.step
            and held.digest == current
            and held.generation == ws.generation
        ):
            continue
        ws.gestures[key] = Gesture(
            secrets.token_urlsafe(16), info.op, info.step, current, ws.generation
        )
