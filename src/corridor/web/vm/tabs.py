"""Tab view models: operator (always open), accounts, wealth and plans (ext items).

Each tab reads the controller's public snapshot, the session's replies and the business
slots the service keeps (``ws.business``); choice keys ``c<i>`` index the same lists the
service picks from, so a key the page sends always names what the page showed.
"""

from typing import Any

from corridor.web import compat, tasks
from corridor.web.catalog import bound, wtr
from corridor.web.compat import (
    KINDS,
    LABELS,
    STATUSES,
    H,
    beijing_time,
    display_names,
    display_value,
    money,
    reference_label,
    tr,
)
from corridor.web.vm import chat
from corridor.web.vm.app import capabilities_vm, features_of
from corridor.web.vm.cards import card_vm, fact_value, judge
from corridor.web.vm.results import (
    bill_vm,
    result_vm,
    subscriptions_vm,
)
from corridor.web.vm.text import plain

RECEIPTS_SHOWN = 5
INTENT_LABELS = {
    "transfer": "transfer",
    "balance_query": "balance_query",
    "bill_report": "bill_report",
    "investment": "investment",
    "card_apply": "card",
    "card_lost": "card",
    "card_freeze": "card",
    "card_limit": "card",
    "card_restriction": "card",
    "card_unlock": "card",
    "subscription_query": "subscription",
    "subscription_cancel": "subscription",
    "birthday": "birthday",
    "stop": "stop",
    "handoff": "handoff",
}
OBJECTIVES = (
    ("net_return", "Net performance benchmark (not guaranteed)"),
    ("total_fee", "Total fees"),
    ("liquidity", "Liquidity"),
)
COMPARED = ("expected_net", "total_fee", "access_days", "gap")
CATALOGUE = (
    "Synthetic product catalogue · performance benchmark (not guaranteed), for comparison "
    "only; not realized income."
)


def _select(value: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    return {key: value[key] for key in keys if key in value}


def handoff_snapshot(response: dict[str, Any]) -> dict[str, Any]:
    """``app::handoff``'s redacted snapshot, field for field."""
    payload = response.get("payload", {})
    redacted = _select(response, ("response_id", "intent", "status"))
    redacted["session_snapshot"] = [
        _select(row, ("utterance_hash", "response_id", "intent", "status"))
        for row in payload.get("session_snapshot", [])
    ]
    redacted["dag_snapshot"] = [
        {
            **_select(item["plan"], ("plan_id", "intent_id")),
            "plan_hash": H(item["plan"]),
            **_select(item, ("ownership_epoch", "blocked")),
            "nodes": {
                node_id: _select(node, ("status", "receipt_ref"))
                for node_id, node in item.get("nodes", {}).items()
            },
            "audit_checkpoint": _select(item.get("audit_checkpoint", {}), ("head", "seq")),
        }
        for item in payload.get("dag_snapshot", [])
    ]
    execution = payload.get("wealth", {}).get("execution")
    if execution:
        shown = _select(
            execution,
            ("allocation_ref", "plan_hash", "biac_hash", "status", "control_status", "rule_ids"),
        )
        shown["items"] = [
            _select(
                item,
                (
                    "item_ref",
                    "product_ref",
                    "status",
                    "receipt_ref",
                    "decision_key",
                    "child_plan_id",
                ),
            )
            for item in execution.get("items", [])
        ]
        shown["audit_checkpoint"] = _select(execution.get("audit_checkpoint", {}), ("head", "seq"))
        redacted["wealth"] = {"execution": shown}
    return redacted


def snapshot_lines(redacted: dict[str, Any]) -> list[str]:
    """The redacted snapshot in plain words: one line per reply, then the plans."""
    lines = [plain(tr("Latest conversation handoff snapshot"))]
    for index, row in enumerate(redacted.get("session_snapshot", []), 1):
        intent = INTENT_LABELS.get(str(row.get("intent")), "other")
        lines.append(
            wtr(
                "web.vm.operator.row",
                index=index,
                intent=wtr(f"web.vm.intent.{intent}"),
                status=chat.status_text(str(row.get("status"))),
            )
        )
    plans = redacted.get("dag_snapshot", [])
    if plans:
        blocked = sum(1 for item in plans if item.get("blocked"))
        lines.append(wtr("web.vm.operator.plans", count=len(plans), blocked=blocked))
    return lines


def task_key(ws, source: str, identity: str | None = None) -> str | None:
    """The newest business task of a source (and identity), as the service registered it."""
    found = [
        task.key
        for task in ws.tasks.values()
        if task.source == source and (identity is None or task.identity == identity)
    ]
    return max(found, key=lambda key: int(key[2:])) if found else None


def pending_result(snapshot: dict[str, Any], pending_id: str | None) -> dict[str, Any] | None:
    for row in snapshot.get("pending", []):
        if row.get("pending_id") == pending_id:
            return row.get("result")
    return None


def operator_vm(ws, capabilities: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
    session = ws.session
    latest = next(
        (row for row in reversed(session.history) if row.get("intent") == "handoff"), None
    )
    vm: dict[str, Any] = {
        "intro": plain(
            tr(
                "Handoff pauses automatic execution. The advisor sees redacted evidence; later "
                "financial actions still require authorization and gateway checks."
            )
        )
    }
    if latest is not None:
        response = session.response(latest["response_id"])
        redacted = handoff_snapshot(response)
        vm["result"] = result_vm(response.get("payload"), controller=ws.controller)
        vm["snapshot_lines"] = snapshot_lines(redacted)
        if judge():
            vm["snapshot"] = redacted
    else:
        vm["result"] = None
        vm["snapshot_lines"] = [wtr("web.vm.operator.none")]
    compensation = None
    if capabilities.get("compensate"):
        compensation = {
            "label": plain(tr("Prepare reversible compensation and a new approval card")),
            **chat.compensate_vm(ws, snapshot.get("plans", [])),
        }
        stored = ws.business.get("compensation")
        key = task_key(ws, "compensation")
        if key is not None:
            compensation["task"] = key
        if isinstance(stored, dict) and not stored.get("card"):
            compensation["result"] = result_vm(stored, controller=ws.controller)
        elif isinstance(stored, dict):
            found = pending_result(snapshot, stored.get("pending_id"))
            if found:
                compensation["result"] = result_vm(found, controller=ws.controller)
    vm["compensation"] = compensation
    vm["note"] = plain(
        tr(
            "Settled transfers cannot be directly reversed. Reversible compensation needs a new "
            "approval; a refund request is not a received refund."
        )
    )
    return vm


def latest_reply(ws, intent: str, *, settled: bool = True) -> dict[str, Any] | None:
    for row in reversed(ws.session.history):
        if row.get("intent") != intent:
            continue
        response = ws.session.response(row["response_id"])
        if not settled or response["status"] == "settled":
            return response
    return None


def subscription_result(ws, snapshot: dict[str, Any]) -> dict[str, Any] | None:
    """The settled query whose list the cancel choices index: the same one the service
    picks ``c<i>`` from (a cancel card registered after it does not hide it)."""
    return tasks.subscription_listing(ws)


def accounts_vm(ws, capabilities: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
    c = ws.controller
    if not snapshot.get("logged_in"):
        return {
            "balances": [],
            "cards": [],
            "receipts": [],
            "subscriptions": None,
            "sub_query": None,
            "billing": None,
            "empty_text": wtr("web.vm.accounts.empty"),
        }
    balances = [
        {"label": plain(reference_label(ref, controller=c)), "amount": money(value)}
        for ref, value in snapshot.get("balances", {}).items()
    ]
    cards = [
        {
            "label": plain(display_names.label(card["id"], c.state) or tr("Simulated card ID")),
            "status_text": plain(
                tr(STATUSES.get(str(card.get("status")), str(card.get("status"))))
            ),
        }
        for card in snapshot.get("cards", [])
    ]
    bank = [item for item in snapshot.get("receipts", []) if item.get("receipt_type") == "bank"]
    receipts = []
    for item in reversed(bank[-RECEIPTS_SHOWN:]):
        debit = next(
            (effect for effect in item.get("exact_effects", []) if effect.get("amount")), None
        )
        receipts.append({
            "title": plain(tr(KINDS.get(str(item.get("kind")), "Business task"))),
            "amount": money(debit["amount"]) if debit else "—",
            "time": display_names.time_text(item.get("executed_at")),
            "status": plain(tr(STATUSES.get(str(item.get("status")), str(item.get("status"))))),
        })
    stored = subscription_result(ws, snapshot)
    subscriptions = subscriptions_vm(stored, controller=c) if stored else None
    if subscriptions is not None:
        # A cancel choice holds only for the list shown here (R1-E2).
        subscriptions["cancel_version"] = tasks.list_version(
            ws, "subscriptions", list(stored.get("cancellable") or [])
        )
    sub_query = None
    if "subscriptions" in capabilities.get("shortcuts", []):
        sub_query = {"label": plain(tr("Prepare subscription query approval"))}
        key = task_key(ws, "subscriptions")
        if key is not None:
            sub_query["task"] = key
    billing = None
    stored = ws.business.get("billing")
    if isinstance(stored, dict) and isinstance(stored.get("report"), dict):
        billing = bill_vm(stored["report"], controller=c)
    else:
        reply = latest_reply(ws, "bill_report")
        billing = bill_vm(reply["payload"], controller=c) if reply else None
    return {
        "balances": balances,
        "cards": cards,
        "receipts": receipts,
        "subscriptions": subscriptions,
        "sub_query": sub_query,
        "billing": billing,
        "empty_text": None,
    }


def plans_vm(ws, capabilities: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
    plans = [
        {
            "key": f"p{index}",
            "title": plain(f"{tr('#### Plan ')}{index + 1}"),
            "dag": chat.dag_vm(item),
            "blocked_text": (
                plain(tr("Automatic execution has stopped for this plan."))
                if item.get("blocked")
                else None
            ),
        }
        for index, item in enumerate(snapshot.get("plans", []))
    ]
    draft = None
    stored = ws.business.get("birthday_draft")
    if capabilities.get("plan_select") and isinstance(stored, dict) and stored.get("nodes"):
        key = task_key(ws, "plans", stored.get("plan_id"))
        if key is not None:
            draft = {
                "task": key,
                "select": chat.plan_select_vm(ws, key, stored["nodes"], stored.get("plan_id")),
            }
    return {"plans": plans, "draft": draft, "empty_text": wtr("web.vm.plan.empty")}


def refusal_vm(
    card: dict[str, Any],
    controller: Any,
    *,
    outcome: Any,
    rule_ids: list[str],
    dispatches: int,
    isolated: bool,
) -> dict[str, Any]:
    """``render_refusal_card``: a kernel Reject is final, so no gesture is ever offered."""
    vm: dict[str, Any] = {
        "title": plain(compat.refusal_title(rule_ids)),
        "lead": plain(tr("chat.rejected")),
        "card": card_vm(card, controller=controller, key="W-R"),
        "reasons": [plain(line) for line in compat.plain_reasons(rule_ids)],
        "dispatches_text": plain(
            tr("polish.r4.result" if isolated else "polish.refusal.result", count=dispatches)
        ),
        "isolation": plain(tr("polish.r4.isolation" if isolated else "polish.refusal.next")),
    }
    if judge():
        vm["outcome"] = str(outcome)
        vm["rule_ids"] = list(rule_ids)
    return vm


def product_name(product: Any, controller: Any) -> str:
    return plain(display_names.label(product, controller.state) or str(product))


def comparison_vm(stored: dict[str, Any], controller: Any) -> dict[str, Any]:
    """``app::investments``' comparison: ranked rows, exclusions and the explicit choice."""
    bic = stored.get("bic") or {}
    shown = result_vm(stored, controller=controller) or {}
    rows = []
    for index, row in enumerate(stored.get("rows") or bic.get("rows") or []):
        cells = [
            {
                "label": plain(display_value(LABELS.get(key, key))),
                "value": plain(fact_value(key, value, controller)),
            }
            for key, value in row.items()
            if key in COMPARED
        ]
        rows.append({
            "key": f"c{index}",
            "name": plain(f"{tr('Rank ')}{row.get('rank', '—')} · ")
            + product_name(row.get("product_id"), controller),
            "cells": cells,
            "chosen": row.get("product_id") == bic.get("chosen_product_id"),
        })
    excluded = stored.get("exclusions") or {}
    items = excluded.items() if isinstance(excluded, dict) else []
    vm: dict[str, Any] = {
        "banner": (shown.get("banner") or {}).get("text") or plain(tr(CATALOGUE)),
        "rows": rows,
        "exclusions": [
            {
                "name": product_name(product, controller),
                "reasons": plain(
                    " · ".join(
                        compat.suitability.exclusion_text(reason)
                        for reason in (reasons if isinstance(reasons, list) else [reasons])
                    )
                ),
            }
            for product, reasons in items
        ],
        "candidates": [
            {"key": f"c{index}", "label": product_name(product, controller)}
            for index, product in enumerate(stored.get("candidate_ids") or [])
        ],
    }
    if judge() and bic:
        vm["bic"] = bic
    return vm


def pending_card(ws, stored: Any, tkey: str | None, key: str) -> dict[str, Any] | None:
    """A business page's stored preview, shown from the controller's own card."""
    if not isinstance(stored, dict) or not isinstance(stored.get("card"), dict):
        return None
    cards = compat.authoritative_cards(ws.controller, stored.get("pending_id")) or [stored["card"]]
    shown: dict[str, Any] = {"card": card_vm(cards[0], controller=ws.controller, key=key)}
    if tkey is not None:
        shown["task"] = tkey
    return shown


def wealth_vm(ws, capabilities: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
    c = ws.controller
    now = c.state.now
    objective = str(ws.business.get("wealth_objective", "net_return"))
    positions = []
    for index, item in enumerate(
        position
        for position in snapshot.get("positions", [])
        if position.get("owner") == c.device.principal and position.get("status") == "active"
    ):
        redeemable = str(item.get("redeemable_at") or "") <= now
        positions.append({
            "key": f"c{index}",
            "label": plain(
                compat.object_facts({"objects": {"position_ref": item["id"]}}, controller=c)[
                    "position_object"
                ]
            ),
            "redeemable": redeemable,
            "caption": plain(
                tr(
                    "Position eligible for redemption"
                    if redeemable
                    else "Position still within its minimum holding period"
                )
            ),
            "from_text": plain(
                tr("Redeemable from (Beijing time): ") + beijing_time(item.get("redeemable_at"))
            ),
        })
    stored = ws.business.get("investment_pending")
    purchase = pending_card(
        ws, stored, task_key(ws, "wealth", (stored or {}).get("pending_id")), "W-0"
    )
    refusal = c.kernel_refusal(stored.get("pending_id")) if purchase else None
    if purchase is not None and refusal is not None:
        purchase["refusal"] = refusal_vm(
            stored["card"],
            c,
            outcome=refusal["outcome"],
            rule_ids=refusal["rule_ids"],
            dispatches=refusal["dispatches"],
            isolated=False,
        )
    stored = ws.business.get("redemption_pending")
    redemption = pending_card(
        ws, stored, task_key(ws, "wealth", (stored or {}).get("pending_id")), "W-1"
    )
    results = []
    for task in ws.tasks.values():
        if task.source == "wealth":
            found = pending_result(snapshot, task.identity)
            if found:
                results.append(result_vm(found, controller=c))
    demo = ws.demos.get("suitability_r4")
    r4 = demo.get("r4") if isinstance(demo, dict) else None
    comparison = ws.business.get("wealth_comparison")
    shown = comparison_vm(comparison, c) if isinstance(comparison, dict) else None
    if shown is not None:
        shown["candidates_version"] = tasks.list_version(ws, "wealth", tasks.wealth_listing(ws))
    vm: dict[str, Any] = {
        "intro": plain(tr(CATALOGUE)),
        "scope_note": wtr("web.vm.route.wealth_note"),
        "objective": {
            "value": objective,
            "options": [{"value": value, "label": plain(tr(label))} for value, label in OBJECTIVES],
        },
        "comparison": shown,
        "purchase": purchase,
        "positions": positions,
        "positions_version": tasks.list_version(ws, "positions", tasks.positions(ws)),
        "redemption": redemption,
        "results": results,
        "r4": (
            refusal_vm(
                r4["card"],
                demo.get("controller") or c,
                outcome=r4.get("outcome"),
                rule_ids=list(r4.get("rule_ids") or []),
                dispatches=int(r4.get("execute_count") or 0),
                isolated=True,
            )
            if isinstance(r4, dict) and isinstance(r4.get("card"), dict)
            else None
        ),
    }
    if isinstance(c.client, compat.JournalClient):
        # The wealth advisor stays scripted while the banking chat plans live (INV-10.8).
        vm["live_scope_note"] = plain(tr("live.scope.wealth", deployment=c.client.deployment.label))
    return vm


TABS = {"accounts": accounts_vm, "wealth": wealth_vm, "plans": plans_vm, "operator": operator_vm}


def tab(ws, name: str) -> dict[str, Any]:
    """TabPayload.vm of one tab; the handler checks the name against the capabilities."""
    with bound(ws):
        capabilities = capabilities_vm(features_of(ws))
        return TABS[name](ws, capabilities, ws.controller.snapshot())
