"""Dependency graph rendered from gateway snapshots without execution authority."""

import json
import re
from html import escape
from textwrap import wrap
from typing import Any

from corridor.ui.audience import get_audience
from corridor.ui.messages import UI_MESSAGES, tr

NODE_STYLES = {
    "pending": ("#eef0f4", "Pending"),
    "ready": ("#eef0f4", "Ready"),
    "scheduled": ("#eef0f4", "Scheduled"),
    "waiting_input": ("#fff0c7", "Awaiting device input"),
    "waiting_approval": ("#fff0c7", "Awaiting device approval"),
    "awaiting_approval": ("#fff0c7", "Awaiting device approval"),
    "waiting_mfa": ("#fff0c7", "Awaiting MFA for this run"),
    # S6: the display-layer recipient check waits for the user's answers, not a device.
    "paused": ("#fff1f1", "Awaiting the recipient check answers"),
    "running": ("#d9ebff", "Running"),
    "waiting_settlement": ("#d9ebff", "Awaiting settlement"),
    "succeeded": ("#dcefe8", "Completed"),
    "settled": ("#dcefe8", "Completed"),
    "completed": ("#dcefe8", "Completed"),
    "controlled": ("#dcefe8", "Scheduled authorization created"),
    "failed": ("#fbe1e1", "Failed"),
    "blocked": ("#fbe1e1", "Blocked"),
    "unknown": ("#fde7cc", "Awaiting reconciliation"),
    "interrupted": ("#e8e8ed", "Interrupted"),
    "expired": ("#e8e8ed", "Expired"),
    "skipped": ("#e8e8ed", "Skipped"),
    "cancelled": ("#e8e8ed", "Cancelled"),
    "compensating": ("#efe1ff", "Compensation in progress"),
    "compensated": ("#efe1ff", "Compensated"),
    "handoff": ("#fde7cc", "Operator has taken over"),
    "handed_off": ("#fde7cc", "Operator has taken over"),
    "non_reversible": ("#fbe1e1", "Cannot be reversed directly"),
}
NODE_LABELS = {
    "lock_funds": "Lock birthday budget",
    "unlock_funds": "Unlock when due",
    "merchant_order": "Merchant order",
    "transfer": "Transfer",
    "schedule_transfer": "Scheduled transfer",
    "bill_analyze": "Billing analysis",
    "bill_report": "Bill report",
    "subscription_query": "Subscription lookup",
    "cancel_subscription": "Cancel subscription",
    "purchase_product": "Product purchase",
    "redeem_product": "Product redemption",
    "report_card_lost": "Report lost card",
    "refund_request": "Request a refund",
    "cancel_order": "Cancel merchant order",
}


def node_label(node: dict[str, Any]) -> str:
    """Only fixed bilingual business labels may become graph titles."""
    label = node.get("label")
    if isinstance(label, str) and label in UI_MESSAGES:
        return tr(label)
    if node.get("tool") == "merchant_order":
        merchant = node.get("args", {}).get("merchant_ref", {}).get("object_ref")
        if merchant in {"FLOWERS", "CAKE"}:
            return tr("Order flowers" if merchant == "FLOWERS" else "Order cake")
    return tr(NODE_LABELS.get(node.get("tool", ""), "Business task"))


def node_anchor(key: str, index: int) -> str:
    return f"dag-{re.sub(r'[^a-zA-Z0-9_-]', '-', key)}-node-{index}"


def dag_svg(snapshot: dict[str, Any], *, key: str = "task", compact: bool = False) -> str:
    """Render bounded, escaped SVG, keeping explicit dependencies and actual states."""
    nodes = snapshot.get("plan", {}).get("nodes", [])
    ids = {node["node_id"] for node in nodes}
    ranks: dict[str, int] = {}
    for _ in nodes:
        for node in nodes:
            deps = [ref for ref in node.get("depends_on", []) if ref in ids]
            if node["node_id"] not in ranks and all(ref in ranks for ref in deps):
                ranks[node["node_id"]] = max((ranks[ref] + 1 for ref in deps), default=0)
    columns: dict[int, list[str]] = {}
    for node in nodes:
        rank = ranks.get(node["node_id"], 0)
        columns.setdefault(rank, []).append(node["node_id"])
    width = max(340, max((len(group) for group in columns.values()), default=1) * 230 + 24)
    row_height = 112 if compact else 124
    height = max(112, (max(columns, default=0) + 1) * row_height + 16)
    positions = {
        identifier: ((width - len(group) * 230) // 2 + index * 230 + 7, 12 + rank * row_height)
        for rank, group in columns.items()
        for index, identifier in enumerate(group)
    }
    ordinals = {node["node_id"]: index for index, node in enumerate(nodes, start=1)}
    marker = node_anchor(key, 0) + "-arrow"
    svg = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'role="img" aria-label="{escape(tr("Task dependency graph"))}" '
        'style="width:100%;height:auto;font-family:Segoe UI,Microsoft YaHei,sans-serif">',
        f'<defs><marker id="{marker}" viewBox="0 0 10 10" refX="9" refY="5" '
        'markerWidth="6" markerHeight="6" orient="auto-start-reverse">'
        '<path d="M 0 0 L 10 5 L 0 10 z" fill="#766f83"/></marker></defs>',
    ]
    for node in nodes:
        x2, y2 = positions[node["node_id"]]
        for dependency in node.get("depends_on", []):
            if dependency not in positions:
                continue
            x1, y1 = positions[dependency]
            svg.append(
                f'<path data-edge="{ordinals[dependency]}:{ordinals[node["node_id"]]}" '
                f'd="M {x1 + 108} {y1 + 84} C {x1 + 108} {y1 + 104}, '
                f'{x2 + 108} {y2 - 20}, {x2 + 108} {y2}" '
                f'stroke="#a496b4" stroke-width="2" fill="none" marker-end="url(#{marker})"/>'
            )
    for index, node in enumerate(nodes):
        identifier = node["node_id"]
        status = snapshot.get("nodes", {}).get(identifier, {}).get("status", "pending")
        color, status_label = NODE_STYLES.get(status, ("#eef0f4", status))
        x, y = positions[identifier]
        label = node_label(node)
        label_lines = wrap(f"{index + 1}. {label}", width=27)[:2]
        status_lines = wrap(tr(status_label), width=30)[:2]
        title = escape(f"{index + 1}. {label} · {tr(status_label)}")
        svg.append(
            f'<a href="#{node_anchor(key, index + 1)}" aria-label="{title}" '
            'style="text-decoration:none;color:inherit">'
            f'<g data-status="{escape(status)}"><title>{title}</title>'
            f'<rect x="{x}" y="{y}" width="216" height="84" rx="14" '
            f'fill="{color}" stroke="#c4b5d3"/>'
            f'<text x="{x + 14}" y="{y + 24}" font-size="13" font-weight="600" fill="#292437">'
            + "".join(
                f'<tspan x="{x + 14}" dy="{0 if line == 0 else 16}">{escape(text)}</tspan>'
                for line, text in enumerate(label_lines)
            )
            + "</text>"
            f'<text x="{x + 14}" y="{y + 59}" font-size="11" fill="#554065">'
            + "".join(
                f'<tspan x="{x + 14}" dy="{0 if line == 0 else 13}">{escape(text)}</tspan>'
                for line, text in enumerate(status_lines)
            )
            + "</text></g></a>"
        )
    svg.append("</svg>")
    return "".join(svg)


def dag_details_html(snapshot: dict[str, Any], *, key: str = "task") -> str:
    """Anchor targets inside closed details reveal the exact selected node evidence."""
    if get_audience() == "customer":
        return ""
    details = []
    for index, node in enumerate(snapshot.get("plan", {}).get("nodes", []), start=1):
        state = snapshot.get("nodes", {}).get(node["node_id"], {})
        source = json.dumps({"node": node, "state": state}, ensure_ascii=False, indent=2)
        summary = f"{tr('Technical evidence')} · {index}. {node_label(node)}"
        details.append(
            '<details class="dag-evidence">'
            f'<summary>{escape(summary)}</summary><div id="{node_anchor(key, index)}">'
            f"<pre>{escape(source)}</pre></div></details>"
        )
    return "".join(details)


def draft_snapshot(draft: dict[str, Any]) -> dict[str, Any]:
    """Project unsigned preview rows as waiting approval, never as completed."""
    nodes = [
        {
            "node_id": node["id"],
            "tool": node.get("card", {}).get("kind", ""),
            "label": node.get("label", "Node"),
            "depends_on": node.get("depends_on", []),
        }
        for node in draft.get("nodes", [])
    ]
    return {
        "plan": {"nodes": nodes},
        "nodes": {node["node_id"]: {"status": "waiting_approval"} for node in nodes},
    }
