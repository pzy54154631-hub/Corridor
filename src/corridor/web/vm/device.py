"""Device pane view models: each task's step, the cards it shows and its one gesture.

The step comes from ``tasks.task_step`` and a token only from ``ws.gestures``; a token
is shown only while it still matches the task's step, operation, generation and card
digest, so the page never offers a gesture the server would refuse.
"""

from typing import Any

from corridor.web import compat, live, tasks
from corridor.web.catalog import wtr
from corridor.web.compat import H, tr
from corridor.web.vm import chat
from corridor.web.vm.cards import cards_vm, judge
from corridor.web.vm.gate import decision
from corridor.web.vm.results import result_vm
from corridor.web.vm.text import plain

FINISHED = frozenset({"done", "refused", "withdrawn"})
BACK = frozenset({"not_current", "gate_blocked", "gate_unevaluated"})
MFA_STEPS = frozenset({"mfa_request", "mfa_code", "closed_challenge"})
EXPIRED = "The code has expired or is locked. Cancel and start again."
GENERIC_FAILURE = (
    "The operation was not completed. Review current facts and retry from a fresh proposal."
)
STEP_TEXT = {"closed_challenge": EXPIRED, "closed_failed": "live.failure_cancel"}
GESTURE_LABELS = {
    "review_purchase": "web.vm.purchase.confirm",
    "confirm": "Confirm",
    "confirm_intent": "Confirm",
    "confirm_plan": "Confirm",
    "final_confirm": "web.vm.device.final",
    "confirm_subset": "web.vm.device.subset",
    "mfa_request": "web.vm.device.mfa_request",
    "mfa_code": "web.vm.device.mfa_verify",
}
GATE_NOTICES = {"pause": "polish.scam.device_paused", "incomplete": "polish.scam.device_incomplete"}


def hash_vm(full: str) -> dict[str, str]:
    return {"algorithm": "SHA-256(JCS)", "short": full[:16], "full": full}


def phone_url(ws) -> str:
    return f"/phone/{ws.pair}?lang={ws.locale}&view={ws.view}"


def step_text(step: str, tier: str | None = None) -> str:
    if step in STEP_TEXT:
        return plain(tr(STEP_TEXT[step]))
    if step == "confirm_intent" and tier != "red":
        # Only a red draft goes on to an SMS check; a yellow one is signed and carried out
        # by this very confirmation.
        return wtr("web.vm.step.confirm_intent_signs")
    return wtr(f"web.vm.step.{step}")


def card_tier(ws, task) -> str | None:
    cards = compat.authoritative_cards(ws.controller, task.identity)
    tiers = [card.get("tier") for card in cards if isinstance(card, dict)]
    return "red" if "red" in tiers else (tiers[0] if tiers else None)


def gesture_label(info) -> str:
    total = info.detail.get("total") or 0
    if info.step == "confirm_plan" and total > 1:
        # One gesture signs every card of the plan: the button says so (INV-2.8).
        return wtr("web.vm.device.confirm_all", total=total)
    return plain(wtr(GESTURE_LABELS[info.step]))


def gesture_vm(ws, task, info) -> dict[str, Any] | None:
    """The task's live token, only while it is still valid for this exact step."""
    held = ws.gestures.get(task.key)
    op = tasks.OPS.get(info.step)
    if (
        held is None
        or op is None
        or held.op != op
        or held.step != info.step
        or held.generation != ws.generation
        or held.digest != tasks.digest(ws, task, info)
    ):
        return None
    return {
        "op": held.op,
        "token": held.token,
        "card_version": tasks.card_version(ws, held.digest),
        "label": gesture_label(info),
    }


def task_cards(ws, task, info) -> list[dict[str, Any]]:
    """The same card view models as the reply's (chat) or the action's own (business)."""
    if info.step in FINISHED:
        return []
    plan = compat.plan_entry(ws.controller, task.identity)
    if task.source == "chat" and task.rid is not None:
        shown, _ = chat.reply_cards(ws, task.rid, task.key)
        payload = ws.session.response(task.rid)["payload"]
        nodes = payload.get("replacement", payload).get("nodes", [])
        order = [str(node.get("id")) for node in nodes]
    else:
        shown = cards_vm(
            compat.authoritative_cards(ws.controller, task.identity),
            prefix=task.key,
            controller=ws.controller,
            canonical=plan is None,
        )
        order = list(plan["node_ids"]) if plan else []
    if plan is None:
        return shown
    # A plan shows only the nodes the server registered as selected (X2).
    selected = set(ws.selection.get(task.key) or [])
    keep = {f"{task.key}-{index}" for index, node in enumerate(order) if node in selected}
    return [card for card in shown if card["key"] in keep]


def selection_vm(ws, task, info) -> dict[str, Any] | None:
    plan = compat.plan_entry(ws.controller, task.identity)
    if plan is None:
        return None
    total = len(plan["node_ids"])
    count = len([node for node in ws.selection.get(task.key) or [] if node in plan["node_ids"]])
    if info.step == "confirm_plan":
        text = wtr("web.vm.plan.selection_all", total=total)
    elif info.step == "confirm_subset":
        text = wtr("web.vm.plan.selection_subset", count=count)
    else:
        text = wtr("web.vm.plan.selection_none", count=count, total=total)
    return {"count": count, "total": total, "text": text}


def failure_vm(ws, task, info) -> dict[str, Any] | None:
    if info.step == "closed_unknown":
        return {
            "kind": "unknown",
            "lines": [wtr("web.vm.failure.unknown")],
            "cancel_hint": plain(tr("live.failure_cancel")),
        }
    if info.step != "closed_failed":
        return None
    recorded = live.failure_vm(ws, tasks.prefix_of(task), task.rid) if task.rid else None
    recorded = recorded or {
        "lines": [tr(GENERIC_FAILURE)],
        "cancel_hint": tr("live.failure_cancel"),
    }
    return {
        "kind": "failed",
        "lines": [plain(line) for line in recorded.get("lines", [])],
        "cancel_hint": plain(recorded.get("cancel_hint")),
    }


def task_result(ws, task, info, snapshot: dict[str, Any]) -> dict[str, Any] | None:
    """A finished task's result: the reply's (chat) or the stored pending's (business)."""
    if info.step not in {"done", "refused"}:
        return None
    if task.source != "chat":
        found = next(
            (
                row.get("result")
                for row in snapshot.get("pending", [])
                if row.get("pending_id") == task.identity
            ),
            None,
        )
        return result_vm(found, controller=ws.controller) if found else None
    if task.rid is None:
        return None
    if task.kind == "local" and ws.local.get(task.rid):
        return result_vm(ws.local[task.rid], controller=ws.controller)
    return chat.envelope_result(ws.session.response(task.rid), ws.controller)


def task_hash(ws, task, info) -> dict[str, Any] | None:
    """H(display card): pending before signing, signed with a result, not yet without a
    proposal (a draft has no kernel-checked card)."""
    cards = compat.authoritative_cards(ws.controller, task.identity)
    if not cards:
        return None
    full = H(cards[0]) if len(cards) == 1 else H(cards)
    if info.step == "done" or compat.has_result(ws.controller, task.identity):
        status = "signed"
    elif compat.kernel_view(ws.controller, task.identity) is not None:
        status = "pending"
    else:
        status = "not_yet"
    label = wtr("web.vm.hash.label", status=wtr(f"web.vm.hash.{status}"))
    return {**hash_vm(full), "status": status, "label": label}


def purchase_review_vm(ws, task):
    review = task.review
    if review is None or compat.envelope_mode(ws.session, task.rid) != "purchase":
        return None
    terms = review["terms"]
    facts = []
    for key, value in terms.items():
        if key in {"selected_product_ref", "source_account_ref"}:
            shown = compat.reference_label(value, controller=ws.controller)
            if str(value) not in shown:
                shown = f"{shown} ({value})"
        elif key == "amount":
            shown = compat.money(value)
        elif key in {"horizon_days", "max_access_days"}:
            shown = wtr("web.vm.purchase.days", days=value)
        elif key == "allowed_types":
            shown = (
                ", ".join(wtr(f"web.vm.purchase.value.{item}") for item in value)
                if isinstance(value, list)
                else compat.text_value(value)
            )
        elif key in {"selection_mode", "reason_code", "objective"}:
            shown = wtr(f"web.vm.purchase.value.{value}")
        else:
            shown = compat.text_value(value)
        facts.append({"key": key, "label": wtr(f"web.vm.purchase.{key}"), "value": plain(shown)})
    facts.append({
        "key": "currency",
        "label": wtr("web.vm.purchase.currency"),
        "value": str(review["account"]["ccy"]),
    })
    return {
        "title": wtr("web.vm.purchase.title"),
        "notice": wtr("web.vm.purchase.notice"),
        "facts": facts,
    }


def task_vm(ws, task, current_rid: str | None, snapshot: dict[str, Any]) -> dict[str, Any]:
    info = tasks.task_step(ws, task)
    step = info.step
    vm: dict[str, Any] = {
        "key": task.key,
        "source": task.source,
        "source_label": wtr(f"web.vm.source.{task.source}"),
    }
    if task.rkey is not None:
        vm["rkey"] = task.rkey
    vm.update({
        "current": task.source != "chat" or task.rid == current_rid,
        "cards": task_cards(ws, task, info),
    })
    review = purchase_review_vm(ws, task)
    if review is not None:
        vm["purchase_review"] = review
    selection = selection_vm(ws, task, info)
    if selection is not None:
        vm["selection"] = selection
    text = step_text(step, card_tier(ws, task))
    if step == "closed_facts" and tasks.cards_mismatch(ws, task):
        # The reply's card copy contradicts the authoritative card: say so (R2-E14).
        text = wtr("web.vm.step.closed_mismatch")
    vm.update({"step": step, "step_text": text})
    if step == "gate_blocked":
        verdict = info.detail.get("decision") or decision(
            ws.controller.scam_gate_status(task.identity)
        )
        text = tr(GATE_NOTICES.get(str(verdict), "polish.scam.device_stale"))
        if verdict == "pause" and info.detail.get("challenge"):
            text += tr("polish.scam.pause_challenge")
        vm["gate_notice"] = {"text": plain(text)}
    if step in BACK and task.rkey is not None:
        label = tr("polish.scam.device_back") if step == "gate_blocked" else None
        vm["back"] = {
            "rkey": task.rkey,
            "label": plain(label) if label else wtr("web.vm.device.back"),
        }
    if step in MFA_STEPS:
        vm["mfa"] = {
            "sent_text": (
                wtr("web.vm.step.mfa_request")
                if step == "mfa_request"
                else plain(tr("polish.phone.sent"))
            ),
            "method_text": plain(tr("polish.mfa.other_methods")),
            "hint": plain(tr("polish.phone.autosubmit_hint")),
            "expired_text": plain(tr(EXPIRED)) if step == "closed_challenge" else None,
        }
    vm["gesture"] = gesture_vm(ws, task, info)
    if compat.plan_entry(ws.controller, task.identity) is not None:
        vm["nodes_version"] = tasks.nodes_version(ws, task)
    failure = failure_vm(ws, task, info)
    if failure is not None:
        vm["failure"] = failure
    result = task_result(ws, task, info, snapshot)
    if result is not None:
        vm["result"] = result
    vm["cancel"] = None if step in FINISHED else chat.cancel_vm()
    if judge():
        found = task_hash(ws, task, info)
        if found is not None:
            vm["hash"] = found
    return vm


def device_vm(ws, *, listed: list[Any], current_rid: str | None, snapshot: dict[str, Any]):
    """The device pane: the current reply's task first (``tasks.collect`` order)."""
    shown = [task_vm(ws, task, current_rid, snapshot) for task in listed]
    return {
        "title": wtr("web.vm.device.title"),
        "idle_text": wtr("web.vm.device.idle"),
        "queue_text": (
            wtr("web.vm.device.queue", count=len(shown) - 1) if len(shown) > 1 else None
        ),
        "tasks": shown,
        "active": shown[0]["key"] if shown else None,
        "sms": {
            "url": phone_url(ws),
            "label": wtr("web.vm.sms.label"),
            "open_label": wtr("web.vm.sms.open"),
            "footer": plain(tr("polish.phone.footer")),
        },
    }
