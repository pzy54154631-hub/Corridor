"""Result view models: ``render_result``, ``receipt_line``, bills, subscriptions, history.

Only receipt-backed values are shown: the banner follows the gateway status, the receipt
line names the receipt's own time, number and status, and fact lines are the narrative
the gateway rendered from the verified receipt. Model text is never shown here.
"""

import re
from copy import deepcopy
from decimal import ROUND_HALF_EVEN, Decimal
from fractions import Fraction
from typing import Any

from corridor.i18n import tr as core_tr
from corridor.web.compat import (
    UI_MESSAGES,
    anomaly_projection,
    beijing_time,
    billing_projection,
    display_names,
    money,
    prose,
    reference_label,
    subscription_object,
    subscription_projection,
    tr,
    transaction_projection,
)
from corridor.web.vm.cards import judge, why_lines
from corridor.web.vm.text import plain

SUCCESS = frozenset({"settled", "complete", "executed"})
NOT_EXECUTED = frozenset({"rejected", "refused", "failed", "blocked"})
MFA = frozenset({"waiting_mfa", "escalated", "Escalate"})
# The gateway's labels for the receipt fields a result narrates (``gateway.facts._fact``).
RECEIPT_LABELS = {
    "amount": "Transfer amount",
    "balance_before": "Balance before execution",
    "balance_after": "Verified balance",
}
PRINCIPALS = {"purchase_product": "Purchase principal", "redeem_product": "Redemption principal"}
SETTLED = {
    "transfer": "Transfer settled",
    "purchase_product": "Purchase settled",
    "redeem_product": "Redemption settled",
}
# Raw identifiers and digests (``components._MACHINE_VALUE``): never in a customer line.
MACHINE_VALUE = re.compile(
    r"(?:[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}|[0-9a-f]{32,128})", re.IGNORECASE
)
REVIEW_REASONS = {
    "duplicate_candidate": (
        "Equal charges from the same merchant occurred close together; check for duplicates."
    ),
    "large_expense": (
        "This charge meets the large-expense alert threshold; confirm that you recognize it."
    ),
}


def banner(tone: str, key: str, **values: Any) -> dict[str, str]:
    return {"tone": tone, "text": plain(tr(key, **values))}


def receipt_amount(receipt: Any) -> str | None:
    """The amount a settled bank receipt itself records as debited, for the receipt's headline."""
    if not isinstance(receipt, dict) or receipt.get("receipt_type") != "bank":
        return None
    for effect in receipt.get("exact_effects") or []:
        if effect.get("effect_type") == "debit" and effect.get("amount") is not None:
            return money(effect["amount"])
    return None


def receipt_line(receipt: Any) -> str | None:
    """``components.receipt_line``: a bank receipt's own time, number and status only."""
    if not isinstance(receipt, dict) or receipt.get("receipt_type") != "bank":
        return None
    status = str(receipt.get("status", ""))
    key = f"polish.receipt.status.{status}"
    number = str(receipt.get("receipt_id", ""))
    if not judge():
        number = number.replace("-", "")[-8:].upper()
    return plain(
        tr(
            "polish.receipt.line",
            time=display_names.time_text(receipt.get("executed_at")),
            receipt=number,
            status=tr(key) if key in UI_MESSAGES else status,
        )
    )


def _banner(result: dict[str, Any], status: str, receipt: Any, exploratory: bool):
    if status == "advanced":
        return {
            "tone": "info",
            "text": plain(
                f"{tr('Simulated clock advanced to ')}{result.get('clock', '—')} · "
                f"{result.get('events', 0)}{tr(' due events processed')}"
            ),
        }
    if status in SUCCESS and receipt:
        if exploratory:
            return banner(
                "info",
                "Exploratory analysis ran · question semantics are unverified. This cannot "
                "authorize a payment.",
            )
        if result.get("analysis_receipt"):
            return banner("success", "Analysis complete · computation receipt verified")
        return banner("success", "Executed · bank receipt verified")
    if status in {"unknown", "pending"}:
        return banner(
            "warning",
            "Awaiting reconciliation · do not pay again. Wait for the bank receipt or "
            "operator review.",
        )
    if status in {"handoff", "handed_off"}:
        return banner("warning", "Operator handoff · automatic execution paused")
    if status == "interrupted":
        return banner("warning", "Interrupted · automatic execution paused")
    if status == "resumed":
        return banner(
            "info",
            "Plan resumed within the device-approved scope. Every subsequent action still "
            "requires gateway checks.",
        )
    if status == "no_active_plan":
        return banner("info", "No active plan. No plan was paused and no funds moved.")
    if status == "no_compensation":
        return banner(
            "info",
            "No reversible compensation can be generated automatically. Settled payments "
            "cannot be undone directly.",
        )
    if status == "controlled":
        return banner(
            "info",
            "Control request recorded · see node status and bank receipts to confirm "
            "whether and when a payment occurred.",
        )
    if status in {"redisplay", "drift"}:
        return banner(
            "warning",
            "Business facts changed · the previous approval is invalid. Review the new "
            "card below and approve again.",
        )
    if status in NOT_EXECUTED:
        return banner("error", "Action not executed · review the reasons before approving again")
    if status == "outside_lock_month":
        return banner("warning", "birthday.outside_lock_month")
    if result.get("needs_mfa") or status in MFA:
        return banner(
            "warning", "Awaiting MFA · complete simulated verification on the Device page"
        )
    return banner("info", "Awaiting approval · execution has not completed")


def fact_text(fact: dict[str, Any], receipt: dict[str, Any]) -> Any:
    """One verified receipt field in this locale, with the gateway's own core templates;
    any other field keeps the line rendered when the receipt arrived."""
    field, kind = fact.get("field"), receipt.get("kind")
    if field == "status":
        settled = fact.get("exact_value") == "settled"
        display = core_tr(
            SETTLED.get(str(kind), "Query completed") if settled else "Bank confirmation incomplete"
        )
        return core_tr("{display}.", display=display)
    if field in RECEIPT_LABELS and fact.get("display_value") is not None:
        label = RECEIPT_LABELS[field]
        if field == "amount":
            label = PRINCIPALS.get(str(kind), label)
        return core_tr(
            "{label}: {display}{unit}.",
            label=core_tr(label),
            display=fact["display_value"],
            unit=" CNY",
        )
    return fact.get("text")


def facts_lines(result: dict[str, Any]) -> list[str]:
    """The narrative of a verified bank receipt, re-rendered in this locale.

    An analysis receipt's narrative names raw transaction references; its figures are
    shown by the bill and subscription views instead, as on the Streamlit pages.
    """
    receipt = result.get("receipt")
    projection = result.get("facts")
    if (
        not isinstance(receipt, dict)
        or receipt.get("receipt_type") != "bank"
        or not isinstance(projection, dict)
        or projection.get("status") != "rendered"
    ):
        return []
    lines = [
        plain(fact_text(fact, receipt))
        for fact in projection.get("facts", [])
        if isinstance(fact, dict) and fact.get("text")
    ]
    if judge():
        return lines
    return [line for line in lines if not MACHINE_VALUE.search(line)]


def result_vm(result: Any, *, controller: Any) -> dict[str, Any] | None:
    """``render_result`` as data; None when there is nothing to show."""
    if not isinstance(result, dict) or not result:
        return None
    status = str(result.get("status", "waiting"))
    receipt = result.get("receipt") or result.get("analysis_receipt")
    exploratory = isinstance(receipt, dict) and receipt.get("trust") == "exploratory"
    rows = result.get("results")
    if rows:
        rows = rows.values() if isinstance(rows, dict) else rows
        vm: dict[str, Any] = {
            "banner": banner("info", "Execution results by node"),
            "receipt_line": None,
            "nodes": [
                item
                for item in (result_vm(row, controller=controller) for row in rows)
                if item is not None
            ],
            "facts_lines": [],
            "message_lines": [],
            "why": {"lines": []},
        }
    else:
        message = result.get("message")
        vm = {
            "banner": _banner(result, status, receipt, exploratory),
            "receipt_line": (
                receipt_line(receipt)
                if status in SUCCESS
                and receipt
                and not exploratory
                and not result.get("analysis_receipt")
                else None
            ),
            "facts_lines": facts_lines(result),
            "message_lines": (
                [line for line in plain(prose(message)).splitlines() if line.strip()]
                if isinstance(message, str) and message and not exploratory
                else []
            ),
            "why": {"lines": why_lines(result.get("card"), result.get("rule_ids", []))},
        }
        amount = receipt_amount(receipt) if status in SUCCESS and not exploratory else None
        if amount is not None:
            vm["amount_text"] = amount
    if judge():
        vm["rule_ids"] = list(result.get("rule_ids", []))
        vm["receipt"] = deepcopy(receipt) if isinstance(receipt, dict) else None
        vm["audit_checkpoint"] = deepcopy(result.get("audit_checkpoint"))
    return vm


def ratio(value: Any, maximum: Fraction) -> str:
    """An exact share of the largest bar as a decimal string between 0 and 1."""
    if not maximum:
        return "0"
    share = abs(Fraction(value)) / maximum
    shown = (Decimal(share.numerator) / Decimal(share.denominator)).quantize(
        Decimal("0.0001"), rounding=ROUND_HALF_EVEN
    )
    return format(shown.normalize(), "f")


def bill_vm(result: dict[str, Any], *, controller: Any) -> dict[str, Any] | None:
    """``render_bill_result`` and the billing page's review cards; None without a report."""
    view = billing_projection(result, controller)
    reviews = anomaly_projection(result, controller)
    receipt = result.get("analysis_receipt") or {}
    flagged = "anomaly_tx_ids" in receipt.get("results", {})
    if not view["totals"] and not reviews and not flagged:
        return None
    labels = {
        "gross_spend": tr("Gross spending"),
        "refunds": tr("Refunds"),
        "net_spend": tr("Net spending"),
    }
    totals = []
    for key, value in view["totals"].items():
        row = {"label": plain(labels.get(key, key)), "value": money(value["value"])}
        if judge():
            row["source"] = f"{value['receipt_ref']} · {value['field']}"
        totals.append(row)
    maximum = max((abs(Fraction(item["value"])) for item in view["categories"]), default=0)
    categories = [
        {
            "label": plain(tr(item["key"].replace("_", " ").title())),
            "value": money(item["value"]),
            "ratio": ratio(item["value"], Fraction(maximum)),
        }
        for item in view["categories"]
    ]
    anomalies = [
        {
            "title": plain(f"{tr('Charge to review')} {index} · {money(item['amount'])}"),
            "amount": money(item["amount"]),
            "date": display_names.time_text(item["date"]),
            "reasons": plain(
                " ".join(
                    [
                        tr(
                            REVIEW_REASONS.get(
                                reason, "The analysis flagged this charge for review."
                            )
                        )
                        for reason in item["reasons"]
                    ]
                    + [tr("A review flag does not establish fraud.")]
                )
            ),
        }
        for index, item in enumerate(reviews, 1)
    ]
    empty = None
    if view["totals"] and not categories:
        empty = plain(tr("No settled spending in this period."))
    elif not view["totals"] and not anomalies:
        empty = plain(tr("No suspicious charges were flagged in this period."))
    return {"totals": totals, "categories": categories, "anomalies": anomalies, "empty_text": empty}


def subscriptions_vm(result: dict[str, Any], *, controller: Any) -> dict[str, Any] | None:
    """``render_subscription_result``; choices ``c<i>`` follow the receipt's cancellable list."""
    if result.get("status") != "settled" or not result.get("analysis_receipt"):
        return None
    view = subscription_projection(result)
    receipt = result.get("analysis_receipt") or {}
    computed = str(receipt.get("computed_at", ""))
    snapshot = tr("Approved query snapshot: ") + (
        computed if judge() else f"{beijing_time(computed)} · " + tr("Beijing time")
    )
    merchants = {
        row["authorization_ref"]: tr(reference_label(row["merchant_ref"], controller=controller))
        for row in view["rows"]
        if row["authorization_ref"]
    }
    renewals = []
    for item in view["renewals"]:
        reference = item["authorization_ref"]
        # The customer view names the merchant instead of the raw authorization reference.
        name = reference if judge() else merchants.get(reference, reference)
        renewals.append(
            plain(
                tr("Upcoming renewal · ")
                + f"{name} · {beijing_time(item['next_due'])} · "
                + tr("Beijing time")
            )
        )
    rows = []
    for row in view["rows"]:
        facts = {
            "merchant": tr(reference_label(row["merchant_ref"], controller=controller)),
            "amount": money(row["amount"]),
            "cancellable": tr("Yes") if row["cancellable"] else tr("No"),
        }
        text = (
            tr("polish.subscription.row", **facts, next_due=beijing_time(row["next_due"]))
            if row["next_due"]
            else tr("polish.subscription.row_no_next", **facts)
        )
        item: dict[str, Any] = {"text": plain(text)}
        if judge():
            item["cells"] = [
                facts["merchant"],
                facts["amount"],
                row["authorization_ref"] or tr("Detected recurring charge only"),
                facts["cancellable"],
                beijing_time(row["next_due"]),
                ", ".join(row["source_tx_ids"]),
            ]
        rows.append(item)
    cancellable = [ref for ref in result.get("cancellable") or [] if isinstance(ref, str)]
    return {
        "snapshot_text": plain(snapshot),
        "renewals": renewals,
        "rows": rows,
        "empty_text": (
            None if rows else plain(tr("No recurring charges were identified in this query."))
        ),
        "footer": plain(
            tr(
                "Renewals use this analysis receipt's seven-day reminder field. "
                "Cancellation requires a separate approval."
            )
        ),
        "cancel_choices": [
            {
                "key": f"c{index}",
                "label": plain(
                    subscription_object(ref, controller=controller)
                    or merchants.get(ref)
                    or tr("Cancel subscription")
                ),
            }
            for index, ref in enumerate(cancellable)
        ],
    }


def transactions_vm(result: dict[str, Any], *, controller: Any) -> dict[str, Any] | None:
    """``render_transaction_rows``: the verified query receipt's rows only."""
    receipt = result.get("receipt") or {}
    if result.get("status") != "settled" or receipt.get("kind") != "transactions_query":
        return None
    shown = judge()
    columns = [
        tr("Time (UTC)"),
        tr("Direction"),
        tr("Amount"),
        tr("Category"),
        tr("Counterparty reference"),
    ]
    if shown:
        columns.insert(0, tr("Transaction reference"))
    rows = []
    for row in transaction_projection(result):
        counterparty = row.get("merchant_ref") or row.get("contact_ref")
        category = "u1.category." + str(row.get("category_code"))
        cells = [
            display_names.time_text(row.get("booked_at")),
            tr("Debit") if row.get("direction") == "debit" else tr("Credit"),
            money(row.get("amount")),
            tr(category) if category in UI_MESSAGES else str(row.get("category_code")),
            (counterparty if shown else reference_label(counterparty, controller=controller))
            if counterparty
            else "—",
        ]
        if shown:
            cells.insert(0, str(row.get("id")))
        rows.append([plain(cell) for cell in cells])
    return {"columns": [plain(column) for column in columns], "rows": rows}
