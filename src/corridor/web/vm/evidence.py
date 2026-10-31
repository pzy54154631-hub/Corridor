"""Judge evidence: kernel decision, card consistency, dispatch, audit and logs.

The whole object exists only in the judge view; the customer view never receives it.
A view is a display preference, not access control. Every value is read from existing
objects (the controller snapshot, the stored proposal, the audit checkpoint, the
session history and the planner log); nothing here writes.
"""

from typing import Any

from corridor.web import compat, live
from corridor.web.catalog import bound, wtr
from corridor.web.compat import KINDS, display_names, explanation, scam_friction, tr
from corridor.web.vm import chat
from corridor.web.vm.cards import judge
from corridor.web.vm.device import hash_vm
from corridor.web.vm.gate import decision
from corridor.web.vm.text import plain

OPEN_BY_DEFAULT = ["kernel", "consistency", "dispatch"]
FOOTER = (
    "Financial effects come from verified receipts · synthetic data · simulated "
    "authentication · no real money · append-only audit"
)
INTENT_NOTE = (
    "Intent routing is outside the hash chain. These session observations are not "
    "execution or approval evidence."
)


def rule_lines(rule_ids: list[str]) -> list[str]:
    rules = explanation(rule_ids=rule_ids)["rules"]
    return [
        plain(wtr("web.vm.evidence.rule_line", rule=rule, text=text))
        for rule, text in rules.items()
    ]


def reply_identity(ws, rid: str | None) -> str | None:
    """What a reply acts on: its local selection, its pending or draft, or its plan."""
    if not rid:
        return None
    identity = chat.action_identity(ws, rid)
    if identity:
        return identity
    payload = ws.session.response(rid)["payload"]
    planned = payload.get("plan_id") or payload.get("replacement", payload).get("plan_id")
    if planned:
        return planned
    # A settled reply no longer names its action; its device task keeps the last one.
    rkey = ws.rkey_of.get(rid)
    task = ws.tasks.get("T" + rkey) if rkey else None
    return task.identity if task is not None and task.source == "chat" else None


def kernel_of(ws, identity: str | None) -> tuple[dict[str, Any] | None, bool]:
    """The stored proposal's kernel view, or the copy read while it was still pending
    (the controller drops it once dispatched); the flag says it is that copy."""
    if not identity:
        return None, False
    kernel = compat.kernel_view(ws.controller, identity)
    if kernel is not None:
        return kernel, False
    seen = ws.kernel_seen.get(identity)
    return (dict(seen), True) if seen is not None else (None, False)


# Where a kept kernel view was read (``refresh.remember_kernel``) and when (R2-E11).
SOURCES = {
    "pending_copy": ("web_copy_read_while_pending", "before_dispatch"),
    "confirmation": ("device_confirmation_record", "after_confirmation"),
}
NOTES = {
    "pending_copy": "web.vm.evidence.kernel_snapshot",
    "confirmation": "web.vm.evidence.kernel_confirmation",
}


def kernel_vm(
    kernel: dict[str, Any] | None, raw: dict[str, Any] | None, *, snapshot: bool = False
) -> dict[str, Any]:
    if kernel is not None:
        rules = list(kernel.get("rule_ids") or [])
        note = None
        if snapshot:
            note = wtr(NOTES.get(str(kernel.get("source")), "web.vm.evidence.kernel_snapshot"))
        return {
            "outcome": kernel.get("outcome"),
            "tier": kernel.get("tier"),
            "rule_ids": rules,
            "rule_lines": rule_lines(rules),
            "note": note,
        }
    rules = list((raw or {}).get("rule_ids") or [])
    return {
        "outcome": None,
        "tier": (raw or {}).get("tier"),
        "rule_ids": rules,
        "rule_lines": rule_lines(rules),
        "note": wtr("web.vm.evidence.kernel_none"),
    }


def consistency_vm(cards: list[dict[str, Any]], kernel: dict[str, Any] | None) -> dict[str, Any]:
    """Display card against the kernel-checked card (§2.4.2): full hash and business facts."""
    if kernel is None:
        display = compat.H(cards[0]) if len(cards) == 1 else compat.H(cards)
        return {
            "display_card": hash_vm(display),
            "proposal_card": None,
            "same_hash": None,
            "same_business_facts": None,
            "action": None,
            "cert": None,
            "card_core": None,
            "challenge": "none",
            "not_yet_text": wtr("web.vm.evidence.not_yet"),
            "mismatch_text": None,
        }
    same_facts = bool(kernel.get("same_business_facts"))
    return {
        "display_card": hash_vm(kernel["display_card"]),
        "proposal_card": hash_vm(kernel["proposal_card"]),
        "same_hash": kernel["display_card"] == kernel["proposal_card"],
        "same_business_facts": same_facts,
        "action": hash_vm(kernel["action"]),
        "cert": hash_vm(kernel["cert"]),
        "card_core": hash_vm(kernel["card_core"]) if kernel.get("card_core") else None,
        "challenge": (
            "verified"
            if kernel.get("verified")
            else "issued"
            if kernel.get("challenge")
            else "none"
        ),
        "not_yet_text": None,
        "mismatch_text": None if same_facts else wtr("web.vm.evidence.mismatch"),
    }


def receipts_vm(snapshot: dict[str, Any]) -> list[dict[str, str]]:
    shown = []
    for receipt in snapshot.get("receipts", []):
        if receipt.get("receipt_type") != "bank":
            continue
        executed = receipt.get("executed_at")
        row = {
            "id": str(receipt.get("receipt_id", "")),
            "kind": plain(tr(KINDS.get(str(receipt.get("kind")), "Business task"))),
            "status": str(receipt.get("status", "")),
            "time_text": (
                wtr("web.vm.evidence.beijing", time=display_names.beijing(executed))
                if executed
                else display_names.time_text(executed)
            ),
        }
        if display_names.is_raw_time(executed):
            row["time_iso"] = str(executed)
        shown.append(row)
    return shown


def planner_log(ws) -> list[dict[str, Any]]:
    """One row per recorded operation: the planner that actually answered it (X11)."""
    rows = []
    for entry in ws.op_log:
        rid = entry.get("rid") or ws.rkeys.get(entry.get("rkey") or "")
        rkey = entry.get("rkey") or (ws.rkey_of.get(rid) if rid else None)
        if not rkey or not rid:
            continue
        raw = ws.session.response(rid)
        kernel, _ = kernel_of(ws, reply_identity(ws, rid))
        rows.append({
            "rkey": rkey,
            "planner": str(entry.get("planner") or "scripted"),
            "intent": str(raw.get("intent")),
            "outcome": (kernel or {}).get("outcome"),
            "tier": (kernel or {}).get("tier") or raw.get("tier"),
            "rule_ids": list((kernel or {}).get("rule_ids") or raw.get("rule_ids") or []),
        })
    return rows


def evidence_vm(ws, *, current_rid: str | None, snapshot: dict[str, Any]) -> dict[str, Any] | None:
    """EvidenceVM, or None outside the judge view."""
    if not judge():
        return None
    c = ws.controller
    checkpoint = c.gateway.audit.checkpoint()
    vm: dict[str, Any] = {
        "open_by_default": list(OPEN_BY_DEFAULT),
        "planner": {
            "badge": plain(live.badge(ws).get("text")),
            "source": str(c.planner_source),
            "counters": [wtr("web.vm.evidence.counter", count=len(ws.op_log))],
        },
    }
    raw = ws.session.response(current_rid) if current_rid in ws.rkey_of else None
    identity = reply_identity(ws, current_rid) if raw else None
    kernel, copied = kernel_of(ws, identity)
    cards = compat.authoritative_cards(c, identity) if identity else []
    status = c.scam_gate_status(identity) if identity else None
    if raw is not None:
        rules = list(raw.get("rule_ids") or [])
        vm["response"] = {
            "rkey": ws.rkey_of[current_rid],
            "intent": str(raw["intent"]),
            "status": str(raw["status"]),
            "rule_ids": rules,
            "rule_lines": rule_lines(rules),
            "audit_hash": hash_vm(str(raw.get("audit_hash") or checkpoint["head"])),
        }
        vm["kernel"] = kernel_vm(kernel, raw, snapshot=copied)
    # After dispatch the kernel copy alone carries the hashes the section compares.
    if cards or kernel is not None:
        vm["consistency"] = consistency_vm(cards, kernel)
    vm.update({
        "dispatch": {
            "count": int(snapshot.get("dispatch_count", 0)),
            "receipts": receipts_vm(snapshot),
        },
        "audit": {
            "count": len(snapshot.get("audit", [])),
            "head": hash_vm(str(checkpoint["head"])),
            "verified": bool(snapshot.get("audit_valid")),
            "note": wtr("web.vm.evidence.audit_note"),
        },
        "safety": {
            "state": str((ws.safety or {}).get("state", "Normal")),
            "reason": (ws.safety or {}).get("reason"),
        },
    })
    if status is not None:
        vm["gate_record"] = plain(scam_friction.record_line(status["answers"], decision(status)))
    vm.update({
        "intent_log": [
            {
                "intent": str(row.get("intent")),
                "status": str(row.get("status")),
                "response": ws.rkey_of.get(row.get("response_id"))
                or str(row.get("response_id") or "")[:8],
                "utterance": str(row.get("utterance_hash") or "")[:12],
            }
            for row in ws.session.history
        ],
        "intent_log_note": plain(tr(INTENT_NOTE)),
        "planner_log": planner_log(ws),
        "footer": plain(tr(FOOTER)),
    })
    return vm


def technical(ws, rkey: str) -> dict[str, Any] | None:
    """EvidenceTechnical for one reply (judge view only); KeyError for an unknown key."""
    rid = ws.rkeys[rkey]
    with bound(ws):
        if not judge():
            return None
        identity = reply_identity(ws, rid)
        c = ws.controller
        kernel, snapshot = kernel_of(ws, identity)
        if snapshot:
            # Two kept sources, each with its own reading time: the web's copy read while
            # the action was pending, or the device workflow's record of a confirmation
            # that dispatched in the same gesture (read after that confirmation).
            source, read_at = SOURCES.get(str(kernel.get("source")), ("unknown", "unknown"))
            kernel = {**kernel, "source": source, "read_at": read_at}
        return {
            "envelope": ws.session.response(rid),
            "cards": compat.authoritative_cards(c, identity),
            "kernel": kernel,
            "gate_record": c.scam_gate_status(identity) if identity else None,
        }
