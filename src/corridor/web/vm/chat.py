"""Chat view models: ``conversation_home``, ``render_response`` and ``task_panel`` as data.

Every reply is projected from the session's envelope and the controller's own state;
cards of a reply that still acts on a draft, a pending action or a plan come from the
controller's authoritative cards, the same list the device pane shows and the gesture
digest covers. Recipient choice, the two questions, birthday and split-bill choices are
interactive on the current reply only.
"""

from typing import Any

from corridor.web import compat, live, service, suggest, tasks
from corridor.web.catalog import WEB_MESSAGES, wtr
from corridor.web.compat import (
    EXAMPLES,
    NODE_STYLES,
    birthday_event_line,
    display_names,
    draft_snapshot,
    node_label,
    reference_label,
    remark_notice,
    response_copy,
    task_node_status,
    task_plans,
    tr,
)
from corridor.web.vm import cards as card_vms
from corridor.web.vm.cards import judge, tier_vm
from corridor.web.vm.gate import decision, gate_vm
from corridor.web.vm.results import bill_vm, result_vm, subscriptions_vm, transactions_vm
from corridor.web.vm.text import lines, plain

RECENT_TURNS = 40
PREFIX = "domestic"
# ``clarification_card``: a local selection in one of these states shows its result only.
LOCAL_FINAL = frozenset({"settled", "rejected", "failed", "withdrawn"})
# ``display_response`` (P-DIFF-1): a finished local selection outlives a later withdrawal.
LOCAL_SHOWN = frozenset({"settled", "rejected", "failed", "controlled"})
ENVELOPE_RESULTS = frozenset({"rejected", "failed", "interrupted", "handed_off", "controlled"})
ROUTE_TABS = {
    "billing": "accounts",
    "subscriptions": "accounts",
    "wealth": "wealth",
    "birthday": "plans",
}
IN_CHAT = frozenset({"conversation", "transfers", "cards"})
TONES = {
    "pending": "neutral",
    "ready": "neutral",
    "scheduled": "neutral",
    "waiting_input": "wait",
    "waiting_approval": "wait",
    "awaiting_approval": "wait",
    "waiting_mfa": "wait",
    "paused": "paused",
    "running": "run",
    "waiting_settlement": "run",
    "compensating": "run",
    "succeeded": "ok",
    "settled": "ok",
    "completed": "ok",
    "controlled": "ok",
    "compensated": "ok",
    "failed": "bad",
    "blocked": "bad",
    "non_reversible": "bad",
    "unknown": "warn",
    "handoff": "warn",
    "handed_off": "warn",
}
CANCEL_HINT = "Cancel retires all pending proposals in this session; settled effects remain."
TO_OPERATOR = "Operator handoff · automatic execution paused"


def cancel_vm() -> dict[str, str]:
    return {"label": plain(tr("Cancel")), "hint": plain(tr(CANCEL_HINT))}


def user_turn(ws, rid: str | None):
    """The user turn that produced a reply: its words are the reply's hint (O-M2)."""
    for turn in reversed(ws.transcript):
        if turn.role == "user" and turn.rid == rid:
            return turn
    return None


def hint_of(ws, rid: str | None) -> tuple[str | None, int]:
    turn = user_turn(ws, rid)
    if turn is None:
        return None, 0
    return turn.hint, int(turn.external_files or 0)


def display_response(ws, rid: str) -> dict[str, Any]:
    """The envelope with the reply's local selection projected over it (P-DIFF-1)."""
    response = ws.session.response(rid)
    selected = ws.local.get(rid)
    if selected and (response["status"] != "withdrawn" or selected.get("status") in LOCAL_SHOWN):
        response = {
            **response,
            "status": selected["status"],
            "card": selected.get("card", response["card"]),
            "tier": selected.get("card", {}).get("tier", response["tier"]),
        }
    return response


def reply_copy(response: dict[str, Any], *, completion: bool, hint: str | None) -> str:
    """``response_copy``; a handoff written into the chat (P-DIFF-4) names the handoff."""
    if response.get("intent") == "handoff":
        return tr(TO_OPERATOR)
    return response_copy(response, completion=completion, hint_text=hint)


def action_identity(ws, rid: str) -> str | None:
    """The draft or pending identity a reply (or its local selection) acts on."""
    local = ws.local.get(rid)
    if isinstance(local, dict) and (local.get("pending_id") or local.get("draft_id")):
        return local.get("pending_id") or local.get("draft_id")
    payload = ws.session.response(rid)["payload"]
    return payload.get("pending_id") or payload.get("draft_id")


def status_text(status: str, card_kind: str | None = None) -> str:
    if status == "controlled" and card_kind == "cancel_schedule":
        return wtr("web.vm.status.schedule_cancelled")
    key = f"web.vm.status.{status}"
    return wtr(key if key in WEB_MESSAGES else "web.vm.status.other")


def notices(ws, items: list[dict[str, Any]], scope: str) -> list[dict[str, str]]:
    """Notice dicts with a stable id each (generation, revision, sequence)."""
    shown = []
    for index, item in enumerate(items or []):
        level = (
            item.get("level") if item.get("level") in {"info", "success", "warning"} else "error"
        )
        shown.append({
            "id": str(item.get("id") or f"{ws.generation}:{ws.revision}:{scope}{index}"),
            "level": level,
            "text": plain(item.get("text")),
        })
    return shown


def dag_vm(snapshot: dict[str, Any]) -> dict[str, Any]:
    """``dag_svg``'s layering and states as data; node keys follow plan node order."""
    nodes = snapshot.get("plan", {}).get("nodes", [])
    ids = {node["node_id"] for node in nodes}
    ranks: dict[str, int] = {}
    for _ in nodes:
        for node in nodes:
            deps = [ref for ref in node.get("depends_on", []) if ref in ids]
            if node["node_id"] not in ranks and all(ref in ranks for ref in deps):
                ranks[node["node_id"]] = max((ranks[ref] + 1 for ref in deps), default=0)
    keys = {node["node_id"]: f"n{index}" for index, node in enumerate(nodes)}
    seen: dict[int, int] = {}
    shown = []
    for node in nodes:
        identifier = node["node_id"]
        status = str(snapshot.get("nodes", {}).get(identifier, {}).get("status", "pending"))
        layer = ranks.get(identifier, 0)
        order = seen.get(layer, 0)
        seen[layer] = order + 1
        shown.append({
            "key": keys[identifier],
            "label": plain(node_label(node)),
            "status": status,
            "status_text": plain(tr(NODE_STYLES.get(status, ("", status))[1])),
            "tone": TONES.get(status, "neutral"),
            "layer": layer,
            "order": order,
        })
    vm: dict[str, Any] = {
        "nodes": shown,
        "edges": [
            [keys[ref], keys[node["node_id"]]]
            for node in nodes
            for ref in node.get("depends_on", [])
            if ref in keys
        ],
        "blocked_text": (
            plain(tr("Automatic execution has stopped for this plan."))
            if snapshot.get("blocked")
            else None
        ),
    }
    if judge():
        vm["details"] = [
            {"node": node, "state": snapshot.get("nodes", {}).get(node["node_id"], {})}
            for node in nodes
        ]
    return vm


def response_dag(response: dict[str, Any], plans: list[dict[str, Any]]) -> dict[str, Any]:
    """``task_panel``'s graph: the reply's registered plan, its draft, or one node."""
    payload = response["payload"]
    draft = payload.get("replacement", payload)
    matched = task_plans(response, plans)
    if matched:
        return dag_vm(matched[0])
    if draft.get("nodes"):
        return dag_vm(draft_snapshot(draft))
    kind = (response.get("card") or {}).get("kind")
    tool = kind if kind in compat.KINDS else response["intent"]
    return dag_vm({
        "plan": {
            "nodes": [
                {
                    "node_id": "current",
                    "tool": tool,
                    "label": compat.KINDS.get(tool, "Business task"),
                    "depends_on": [],
                }
            ]
        },
        "nodes": {"current": {"status": task_node_status(response)}},
    })


def compensate_vm(ws, plans: list[dict[str, Any]]) -> dict[str, str]:
    """Which plan Compensate acts on, and the version that binds the click to it."""
    plan_id = plans[-1]["plan"]["plan_id"] if plans else None
    vm = {"compensate_version": tasks.list_version(ws, "compensation", [plan_id], plans)}
    if plan_id is not None:
        vm["compensate_hint"] = wtr("web.vm.plan.compensate_target", index=len(plans))
    return vm


def task_panel_vm(ws, rid: str, capabilities: dict[str, Any], plans: list[dict[str, Any]]):
    response = display_response(ws, rid)
    hint, _ = hint_of(ws, rid)
    tier = response.get("tier") or response.get("requested_level")
    controls = {
        "stop": plain(tr("Stop")),
        "compensate": plain(tr("Compensate")) if capabilities.get("compensate") else None,
        "handoff": plain(tr("Handoff")),
    }
    if capabilities.get("compensate"):
        controls.update(compensate_vm(ws, plans))
    return {
        "route_label": plain(tr("nav." + response["route"])),
        "tier": tier_vm(tier) if tier in {"green", "yellow", "red"} else None,
        "copy": plain(reply_copy(response, completion=True, hint=hint)),
        "requested_hint": (
            plain(tr("This service requires red-tier review; no executable card is ready."))
            if response.get("requested_level") and not response.get("card")
            else None
        ),
        "dag": response_dag(response, plans),
        "controls": controls,
    }


def node_cards(ws, identity: str | None, nodes: list[dict[str, Any]], prefix: str):
    """Plan node cards from the controller's displayed plan, in node order."""
    authoritative = {
        card.get("node_id"): card
        for card in compat.authoritative_cards(ws.controller, identity)
        if isinstance(card, dict)
    }
    total = len(nodes)
    shown, source = [], "authoritative"
    for index, node in enumerate(nodes):
        card = authoritative.get(node.get("id"))
        if card is None:
            card, source = node.get("card") or {}, "envelope"
        shown.append(
            card_vms.card_vm(
                card,
                controller=ws.controller,
                key=f"{prefix}-{index}",
                canonical=False,
                position=index + 1,
                total=total,
            )
        )
    return shown, source


def action_cards(ws, identity: str | None, fallback: list[dict], prefix: str, **options):
    """An action's cards: authoritative while the controller holds it, else the copy."""
    cards = compat.authoritative_cards(ws.controller, identity)
    source = "authoritative"
    if not cards:
        cards, source = [card for card in fallback if card], "envelope"
    return card_vms.cards_vm(cards, prefix=prefix, controller=ws.controller, **options), source


def selection_mode(selected: list[str], nodes: list[str]) -> str:
    if not selected:
        return "none"
    if selected == nodes:
        return "all"
    if selected == nodes[: len(selected)]:
        return "subset"
    return "invalid"


def plan_select_vm(ws, tkey: str, nodes: list[dict[str, Any]], plan_id: str | None):
    """Server-registered node selection (X2); keys ``n<i>`` follow the plan's node order."""
    plan = compat.plan_entry(ws.controller, plan_id)
    order = list(plan["node_ids"]) if plan else [str(node.get("id")) for node in nodes]
    labels = {str(node.get("id")): node.get("label", "Node") for node in nodes}
    selected = [node for node in ws.selection.get(tkey) or [] if node in order]
    mode = selection_mode(selected, order)
    return {
        "tkey": tkey,
        "rule_text": wtr("web.vm.plan.rule", total=len(order), count=len(selected)),
        "nodes": [
            {
                "key": f"n{index}",
                "label": plain(f"{index + 1}. {tr(labels.get(node, 'Node'))}"),
                "checked": node in selected,
            }
            for index, node in enumerate(order)
        ],
        "mode": mode,
        "count": len(selected),
        "total": len(order),
        "error_text": wtr("web.vm.plan.error") if mode == "invalid" else None,
    }


def clarify_vm(ws, rid: str, payload: dict[str, Any]) -> dict[str, Any]:
    """``clarification_card``'s recipient list; locked once the draft is in use."""
    c = ws.controller
    contacts = list(payload.get("contacts") or [])
    local = ws.local.get(rid) or {}
    chosen_ref = ((local.get("card") or {}).get("objects") or {}).get("contact_ref")
    draft_id = payload.get("draft_id")
    locked = (
        compat.pending_entry(c, draft_id) is not None
        or compat.has_result(c, draft_id)
        or compat.is_closed(c, draft_id)
        or local.get("status") in LOCAL_FINAL
    )
    vm: dict[str, Any] = {
        "label": plain(tr("Recipient")),
        "choices": [
            {"key": f"c{index}", "label": plain(reference_label(ref, controller=c))}
            for index, ref in enumerate(contacts)
        ],
        "chosen": f"c{contacts.index(chosen_ref)}" if chosen_ref in contacts else None,
        "review_label": plain(tr("Review recipient")),
        "locked": bool(locked),
        "locked_text": wtr("web.vm.clarify.locked") if locked else None,
        "error_text": None,
    }
    if judge():
        registry = getattr(c.state, "contacts", {}) or {}
        vm["evidence_lines"] = [
            plain(tr("Recipient aliases from the trusted contact registry"))
        ] + [
            f"{ref}: {' · '.join(alias for alias in registry[ref].aliases if alias)}"
            for ref in contacts
            if ref in registry
        ]
    return vm


def demo_contacts(ws, state) -> list[str]:
    """Display names of the contacts a transfer can name: the slot parser's trusted set."""
    principal = ws.controller.device.principal
    names = []
    for ref, contact in sorted(state.contacts.items()):
        if contact.owner != principal or contact.verified_at is None:
            continue
        key = f"web.vm.contact.{ref}"
        name = wtr(key) if key in WEB_MESSAGES else display_names.contact_parts(ref, state)[0]
        names.append(plain(name))
    return names


def recipient_unmatched(raw: dict[str, Any]) -> bool:
    """A transfer that stopped because no contact matched, read from the parse alone.

    The session records an empty recipient and no candidates; a split bill has its own
    roster. The check never reads the words, so nothing the user typed reaches the hint.
    """
    payload = raw["payload"]
    parameters = payload.get("parameters")
    return (
        raw["intent"] == "transfer"
        and raw["status"] in {"clarify", "awaiting_device_selection"}
        and isinstance(parameters, dict)
        and parameters.get("recipient") is None
        and not payload.get("recipient_candidates")
        and not payload.get("contacts")
        and not raw.get("card")
        and not payload.get("aa_parse")
    )


def contact_hint_vm(raw: dict[str, Any], contacts: list[str]) -> dict[str, Any] | None:
    """What the demo address book holds, under a transfer that matched none of it.

    An explanation only: it selects nothing and prepares no card.
    """
    if not recipient_unmatched(raw):
        return None
    return {
        "lead": wtr("web.vm.contact_hint.lead"),
        "contacts": contacts,
        "retry": wtr("web.vm.contact_hint.retry"),
    }


def birthday_vm(ws, response: dict[str, Any]) -> dict[str, Any] | None:
    """``birthday_choices``: trusted calendar events when chat could not pick one."""
    payload = response["payload"]
    if response["intent"] != "birthday" or response["status"] != "clarify":
        return None
    c = ws.controller
    if payload.get("birthday_status") == "outside_lock_month" and payload.get("event"):
        return {
            "lines": [plain(birthday_event_line(c, payload["event"]))],
            "choices": [],
            "error_text": None,
        }
    candidates = payload.get("candidates") or []
    if not candidates:
        return None
    return {
        "lines": [plain(tr("birthday.calendar_candidates"))],
        "choices": [
            {
                "key": f"c{index}",
                "label": plain(birthday_event_line(c, item)),
                "enabled": bool(item.get("lock_window_open")),
            }
            for index, item in enumerate(candidates)
        ],
        "error_text": None,
    }


def aa_vm(ws, response: dict[str, Any]) -> dict[str, Any] | None:
    """``aa_completion_card``: choose the remaining split-bill participants."""
    payload = response["payload"]
    split = payload.get("aa")
    parse = payload.get("aa_parse") or {}
    if (
        response["status"] != "awaiting_device_selection"
        or not isinstance(split, dict)
        or parse.get("status") != "unresolved"
        or parse.get("reason") not in {"participants", "ambiguous"}
    ):
        return None
    c = ws.controller
    needed = split["participants"] - 1
    return {
        "prompt": plain(
            tr(
                "chat.aa.complete_title",
                total=compat.money(payload["parameters"]["amount"]),
                people=split["participants"],
                per_person=compat.money(split["per_person"]),
                needed=needed,
            )
        ),
        "needed": needed,
        "choices": [
            {"key": f"c{index}", "label": plain(reference_label(ref, controller=c))}
            for index, ref in enumerate(ws.session.aa_roster())
        ],
    }


def route_hint(response: dict[str, Any], capabilities: dict[str, Any]) -> dict[str, Any] | None:
    """Where the reply's route leads on this page; never a dead link (O-S14)."""
    route = response["route"]
    if response["intent"] == "out_of_scope" or route in IN_CHAT:
        return None
    if route == "operator":
        return {"target": "operator", "label": wtr("web.vm.route.operator"), "note": None}
    tab = ROUTE_TABS.get(route)
    if tab in capabilities.get("tabs", []):
        return {
            "target": tab,
            "label": plain(tr("Open this service")),
            "note": wtr("web.vm.route.wealth_note") if tab == "wealth" else None,
        }
    return {
        "target": "fallback",
        "label": plain(tr("nav." + route)),
        "note": wtr("web.vm.route.fallback"),
    }


def parse_note_lines(ws, payload: dict[str, Any]) -> list[str] | None:
    """``parse_note``: the parser's reading of a dated or split request (judge evidence)."""
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
            total=compat.money(aa["total"]),
            per_person=compat.money(aa["per_person"]),
            collect=compat.money(aa["collect_total"]),
            participants=tr("polish.scam.phrase_separator").join(
                reference_label(ref, controller=ws.controller) for ref in aa["participants"]
            ),
        )
    else:
        return None
    return [plain(tr("chat.parse_note.title")), plain(text)]


def envelope_result(raw: dict[str, Any], controller: Any) -> dict[str, Any] | None:
    """The result blocks ``render_response`` draws under a reply."""
    payload = raw["payload"]
    if raw["intent"] == "handoff":
        # The chat shows the handoff (P-DIFF-4) with its own banner (W7).
        return result_vm({**payload, "status": "handed_off"}, controller=controller)
    if (payload.get("receipt") or {}).get("kind") == "transactions_query":
        vm = result_vm(payload, controller=controller)
        rows = transactions_vm(payload, controller=controller)
        if vm is not None and rows is not None:
            vm["transactions"] = rows
        return vm
    if payload.get("analysis_receipt") and raw["intent"] == "bill_report":
        vm = result_vm(payload, controller=controller)
        bill = bill_vm(payload, controller=controller)
        if vm is not None and bill is not None:
            vm["bill"] = bill
        return vm
    if payload.get("analysis_receipt") and raw["intent"] == "subscription_query":
        vm = result_vm(payload, controller=controller)
        subscriptions = subscriptions_vm(payload, controller=controller)
        if vm is not None and subscriptions is not None:
            vm["subscriptions"] = subscriptions
        return vm
    if (
        payload.get("receipt")
        or (raw["intent"] == "transfer" and payload.get("analysis_receipt"))
        or raw["status"] in ENVELOPE_RESULTS
    ):
        vm = result_vm(payload, controller=controller)
        if (
            vm is not None
            and payload.get("status") == "controlled"
            and (raw.get("card") or {}).get("kind") == "cancel_schedule"
        ):
            vm["banner"] = {
                "tone": "success",
                "text": plain(tr("chat.schedule_cancel.done")),
            }
        return vm
    return None


def failure_of(ws, task, rid: str) -> dict[str, Any] | None:
    if task is not None and task.phase == "closed_unknown":
        return {
            "kind": "unknown",
            "lines": [wtr("web.vm.failure.unknown")],
            "cancel_hint": plain(tr("live.failure_cancel")),
        }
    failure = live.failure_vm(ws, PREFIX, rid)
    if failure is None:
        return None
    return {
        "kind": "unknown" if failure.get("kind") == "unknown" else "failed",
        "lines": [plain(line) for line in failure.get("lines", [])],
        "cancel_hint": plain(failure.get("cancel_hint")),
    }


def is_clarify(raw: dict[str, Any]) -> bool:
    payload = raw["payload"]
    return (
        raw["status"] == "clarify"
        and bool(payload.get("contacts"))
        and bool(payload.get("draft_id"))
    )


def reply_cards(ws, rid: str, prefix: str) -> tuple[list[dict[str, Any]], str | None]:
    """The cards a reply shows, keyed ``<prefix>-<i>``: one function for both panes, so
    the phone and the device show field-for-field the same authoritative cards."""
    raw = ws.session.response(rid)
    payload = raw["payload"]
    draft = payload.get("replacement", payload)
    nodes = draft.get("nodes", [])
    retired = raw["status"] in compat.RETIRED
    hint, files = hint_of(ws, rid)
    options = {"hint_text": hint, "external_files": files}
    if is_clarify(raw):
        local = ws.local.get(rid)
        if not local:
            return [], None
        identity = local.get("pending_id") or payload["draft_id"]
        return action_cards(ws, identity, [local.get("card") or {}], prefix, **options)
    if birthday_vm(ws, raw) is not None or aa_vm(ws, raw) is not None:
        return [], None
    if nodes and not retired:
        return node_cards(ws, payload.get("plan_id") or draft.get("plan_id"), nodes, prefix)
    if raw.get("card") and not retired:
        return action_cards(
            ws,
            payload.get("pending_id") or payload.get("draft_id"),
            [raw["card"]],
            prefix,
            canonical=bool(payload.get("canonical", False)),
            contextual_recipient=bool(raw.get("recipient_context", False)),
            **options,
        )
    return [], None


def response_vm(
    ws,
    rkey: str,
    rid: str,
    *,
    current_rid: str | None,
    capabilities,
    contacts: list[str],
    listed=frozenset(),
):
    """ResponseVM of one reply (``render_response`` and ``clarification_card``)."""
    c = ws.controller
    current = rid == current_rid
    raw = ws.session.response(rid)
    shown = display_response(ws, rid)
    payload = raw["payload"]
    draft = payload.get("replacement", payload)
    nodes = draft.get("nodes", [])
    retired = raw["status"] in compat.RETIRED
    hint, _ = hint_of(ws, rid)
    tkey = "T" + rkey
    task = ws.tasks.get(tkey)
    cards, source = reply_cards(ws, rid, rkey)
    clarify = plan_select = birthday = aa = gate = result = failure = cancel = None
    gate_identity = None
    active = False
    local = ws.local.get(rid)
    if is_clarify(raw):
        clarify = clarify_vm(ws, rid, payload)
        if local and local.get("status") in LOCAL_FINAL:
            result = result_vm(local, controller=c)
        elif local:
            gate_identity, active = local.get("pending_id") or payload["draft_id"], True
    else:
        birthday = birthday_vm(ws, raw) if capabilities.get("plan_select") else None
        aa = aa_vm(ws, raw) if capabilities.get("aa") else None
        if nodes and not retired and capabilities.get("plan_select"):
            plan_select = plan_select_vm(
                ws, tkey, nodes, payload.get("plan_id") or draft.get("plan_id")
            )
        result = envelope_result(raw, c)
        if raw["buttons"] and raw.get("card") and not nodes and not retired:
            gate_identity = payload.get("pending_id") or payload.get("draft_id")
        active = bool(raw["buttons"]) and not retired
    if gate_identity:
        held = ws.gates.get(rkey)
        version = held[0] if held and held[1] == gate_identity else None
        first = compat.authoritative_cards(c, gate_identity)
        gate = gate_vm(
            c.scam_gate_status(gate_identity),
            card=first[0] if first else shown.get("card"),
            controller=c,
            version=version,
            interactive=current,
        )
    if active:
        failure = failure_of(ws, task, rid)
        cancel = cancel_vm()
    vm: dict[str, Any] = {
        "rkey": rkey,
        "current": current,
        "status": str(shown["status"]),
        "status_text": status_text(str(shown["status"]), (shown.get("card") or {}).get("kind")),
        "copy_lines": lines(reply_copy(shown, completion=current, hint=hint)),
        "tier": tier_vm(shown.get("tier")),
        "requested_tier": tier_vm(raw.get("requested_level")),
        "redisplay_notice": (
            plain(tr("The canonical facts changed. Review the new card before confirming again."))
            if raw["status"] == "redisplay"
            else None
        ),
        "cards": cards,
    }
    if plan_select is not None:
        vm["plan_select"] = plan_select
    if raw["intent"] == "subscription_cancel" and raw["status"] == "clarify":
        choices, version = service.subscription_choices(ws, rid)
        if choices:
            vm["subscription_select"] = {
                "choices": [
                    {
                        "key": f"c{index}",
                        "label": plain(compat.subscription_object(ref, controller=c) or str(ref)),
                    }
                    for index, ref in enumerate(choices)
                ],
                "list_version": version,
                "locked": not current or ws.untrusted,
            }
    if raw["intent"] == "clarify" and raw["status"] == "clarify":
        choices, version = service.schedule_cancel_choices(ws, rid)
        if choices:
            state = c.state
            labels = []
            for ref in choices:
                runs = sum(
                    1
                    for task in state.schedules.values()
                    if task.spa_ref == ref and task.status in {"pending", "due", "waiting_mfa"}
                )
                object_label = compat.schedule_object(ref, controller=c) or str(ref)
                labels.append(
                    plain(
                        compat.tr(
                            "chat.schedule_cancel.choice_object",
                            object=object_label,
                            runs=runs,
                        )
                    )
                )
            vm["schedule_cancel_select"] = {
                "choices": [
                    {"key": f"c{index}", "label": label} for index, label in enumerate(labels)
                ],
                "list_version": version,
                "locked": not current or ws.untrusted,
            }
    # Model suggestions under a plain clarification: system-written questions only.
    suggestion = suggest.vm(ws, rid, current=current)
    if suggestion is not None:
        vm["suggestion_select"] = suggestion
    if clarify is not None:
        vm["clarify"] = clarify
    contact_hint = contact_hint_vm(raw, contacts)
    if contact_hint is not None:
        vm["contact_hint"] = contact_hint
    if aa is not None:
        vm["aa"] = aa
    if birthday is not None:
        vm["birthday"] = birthday
    vm.update({
        "gate": gate,
        "result": result,
        "failure": failure,
        "live_notes": notices(ws, live.notes(ws, raw, PREFIX), f"{rkey}-"),
        # Only a task the device pane lists; a finished older reply's task has left it.
        "device_task": tkey if task is not None and tkey in listed else None,
        "route_hint": route_hint(raw, capabilities),
        "fallback_shortcuts": raw["intent"] == "out_of_scope",
        "cancel": cancel,
    })
    if judge():
        if cards:
            vm["card_source"] = source
        if ws.card_mismatch.get(rkey):
            vm["card_mismatch"] = wtr("web.vm.evidence.card_mismatch")
        if payload.get("scam_phrase"):
            vm["phrase_gate_note"] = plain(tr("polish.scam.phrase_gate_judge"))
        if raw["intent"] == "transfer":
            note = parse_note_lines(ws, payload)
            if note:
                vm["parse_note"] = note
        vm["technical_url"] = f"/api/evidence/responses/{rkey}"
    return vm


def turn_vm(ws, turn, current_rid: str | None, state: Any) -> dict[str, Any]:
    rkey = ws.rkey_of.get(turn.rid) if turn.rid else None
    vm: dict[str, Any] = {"id": str(turn.id), "role": turn.role}
    if rkey is not None:
        vm["rkey"] = rkey
    if turn.marker:
        marker = f"web.vm.turn.{turn.marker}"
        vm["text"] = wtr(marker) if marker in WEB_MESSAGES else plain(turn.marker)
        stopped = (ws.business.get("stop_results") or {}).get(turn.id)
        shown = result_vm(stopped, controller=ws.controller) if stopped else None
        if shown and shown.get("banner"):
            # The top-bar Stop's own result (P-DIFF-2 style): what stopped, nothing else.
            vm["text"] = f"{vm['text']} {shown['banner']['text']}"
    elif turn.role == "user":
        vm["text"] = plain(display_names.display_text(turn.hint or "", state))
    elif rkey is not None:
        hint, _ = hint_of(ws, turn.rid)
        vm["text"] = plain(
            reply_copy(
                display_response(ws, turn.rid),
                completion=turn.rid == current_rid,
                hint=hint,
            )
        )
    else:
        vm["text"] = wtr("web.vm.turn.stopped")
    if turn.role == "user" and not turn.marker:
        if turn.external_files:
            vm["attachment_note"] = plain(tr("polish.attach.sent"))
        notice = remark_notice(turn.hint or "")
        if notice:
            vm["remark_note"] = plain(notice)
    return vm


def shortcuts_vm(capabilities: dict[str, Any]) -> list[dict[str, str]]:
    prompts = dict(EXAMPLES)
    shown = []
    for key in capabilities.get("shortcuts", []):
        label = tr("polish.scam.drill_label") if key == "scam_drill" else tr(prompts[key])
        shown.append({"key": key, "label": plain(label)})
    return shown


def gated_elsewhere(ws, current_rid: str | None) -> list[dict[str, str]]:
    """``gated_response_ids``: other replies whose recipient check is not passed yet."""
    c = ws.controller
    found, seen = [], set()
    for turn in reversed(ws.transcript):
        rid = turn.rid
        if not rid or rid in seen or rid == current_rid or rid not in ws.rkey_of:
            continue
        seen.add(rid)
        identity = action_identity(ws, rid)
        if not identity or compat.has_result(c, identity):
            continue
        if compat.draft_entry(c, identity) is None and compat.pending_entry(c, identity) is None:
            continue
        if decision(c.scam_gate_status(identity)) not in (None, "proceed"):
            found.append({
                "rkey": ws.rkey_of[rid],
                "label": plain(tr("polish.scam.other_gated_button")),
            })
    return found


def chat_vm(
    ws,
    *,
    logged_in: bool,
    capabilities: dict[str, Any],
    plans: list[dict[str, Any]],
    listed: frozenset[str] = frozenset(),
):
    current_rid = ws.current.get(PREFIX) if logged_in else None
    shortcuts = shortcuts_vm(capabilities)
    drill = None
    if "scam_drill" in capabilities.get("shortcuts", []):
        drill = tr("polish.scam.drill_caption")
        if judge():
            drill += tr("polish.scam.drill_caption_judge")
        drill = plain(drill)
    recent = ws.transcript[-RECENT_TURNS:] if logged_in else []
    # One copy of the state for all the turns: ``controller.state`` rebuilds it on each read.
    state = ws.controller.state
    turns = [turn_vm(ws, turn, current_rid, state) for turn in recent]
    contacts = demo_contacts(ws, state)
    responses = {}
    for turn in recent:
        rkey = ws.rkey_of.get(turn.rid) if turn.rid else None
        if rkey is not None and rkey not in responses:
            responses[rkey] = response_vm(
                ws,
                rkey,
                turn.rid,
                current_rid=current_rid,
                capabilities=capabilities,
                contacts=contacts,
                listed=listed,
            )
    current = ws.rkey_of.get(current_rid) if current_rid else None
    return {
        "shortcuts": shortcuts,
        "drill_caption": drill,
        "greeting": None if turns else plain(tr("Hello. How can I help you today?")),
        "turns": turns,
        "responses": responses,
        "current": current if current in responses else None,
        "gated_elsewhere": gated_elsewhere(ws, current_rid) if logged_in else [],
        "task_panel": (
            task_panel_vm(ws, current_rid, capabilities, plans) if current in responses else None
        ),
        "composer": {
            "placeholder": wtr("web.vm.composer.placeholder"),
            "max_chars": 4000,
            "attach_label": wtr("web.vm.composer.attach_label"),
            "attach_note": wtr("web.vm.composer.attach_note"),
            "contacts_note": wtr(
                "web.vm.composer.contacts_note",
                names=tr("polish.scam.phrase_separator").join(contacts),
            ),
            "disabled": not logged_in,
        },
    }
