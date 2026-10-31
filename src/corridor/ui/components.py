"""Read-only bilingual presentation of canonical gateway facts."""

import re
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_EVEN, Decimal
from fractions import Fraction
from html import escape
from typing import Any

import streamlit as st

from corridor.crypto.jcs import H, jcs_bytes
from corridor.gateway.facts import render_narrative
from corridor.gateway.output_guard import render_guarded
from corridor.i18n import get_locale
from corridor.schemas.narrative import FactRef, Narrative
from corridor.ui import display_names, fraud_hint, suitability, view_mode
from corridor.ui.dag import dag_details_html, dag_svg
from corridor.ui.explanations import render_explanation
from corridor.ui.messages import UI_MESSAGES, contact_aliases, display_value, prose, tr

STYLES = """
<style>
:root { --ink: #292437; --muted: #766f83; --line: #e9e3ee; --accent: #584174; }
.stApp { background: #f7f5fa; color: var(--ink);
font-family: Inter, "Segoe UI", "Microsoft YaHei", "PingFang SC", sans-serif; }
.block-container { max-width: 1440px; padding-top: 2rem; padding-bottom: 3rem; }
header[data-testid="stHeader"] { background:transparent; height:0; pointer-events:none; }
header[data-testid="stHeader"] button { pointer-events:auto; }
[data-testid="stToolbar"], [data-testid="stAppDeployButton"], #MainMenu { display:none; }
h1,h2,h3 { color: var(--ink); letter-spacing: -.025em; }
h1 { font-size: 1.7rem !important; } h2 { font-size: 1.35rem !important; }
.bank-header { display:flex; align-items:center; gap:14px; margin-bottom:14px; }
.bank-mark { background:#392b49; color:#f3d39c; width:46px; height:46px; border-radius:15px;
display:flex; align-items:center; justify-content:center; font-size:24px; font-weight:700; }
.bank-brand { font-size:11px; letter-spacing:2px; color:#766f83; }
.bank-name { font-size:23px; font-weight:750; letter-spacing:-.04em; }
.notice { padding:10px 14px; border:1px solid #ded4e8; border-radius:12px; background:#eee8f4;
font-size:12px; color:#554065; margin-bottom:18px; }
.hero { background:#392b49; color:#fff; padding:28px; border-radius:22px; margin:16px 0; }
.hero h2 { color:#fff; font-size:26px !important; margin:0 0 8px; }
.hero p { color:#ded2eb; max-width:650px; line-height:1.8; margin:0; }
.eyebrow { color:#f4d296; font-size:12px; letter-spacing:2px; margin-bottom:10px; }
.fact-panel { background:#fff; border:1px solid var(--line); border-radius:18px;
padding:20px; margin:12px 0; box-shadow:0 3px 10px #34244404; }
.fact-title { font-size:12px; color:var(--muted); letter-spacing:.06em; }
.fact-amount { font-size:34px; font-weight:750; margin:7px 0; letter-spacing:-.05em; }
.fact-row { display:grid; grid-template-columns:minmax(96px, .35fr) 1fr; gap:12px;
padding:9px 0; border-bottom:1px solid #f1edf4; font-size:13px; }
.fact-label { color:var(--muted); } .fact-value { overflow-wrap:anywhere; word-break:break-word; }
.tier { display:inline-block; padding:4px 10px; border-radius:999px; font-size:12px;
background:#f9ebcf; color:#735116; margin:5px 0; }
.tier-red { background:#f9e2df; color:#923a33; } .tier-green { background:#dcefe8; color:#275d49; }
.tier-black { background:#39333f; color:#fff; }
.approval-card { padding:24px; border-radius:22px; background:#fff;
border:1px solid var(--line); box-shadow:0 5px 22px #34244408; margin:12px 0; }
.approval-top { display:flex; justify-content:space-between; align-items:center;
gap:12px; flex-wrap:wrap; margin-bottom:12px; }
.approval-purpose { font-weight:650; font-size:17px; margin-top:8px; }
.approval-reason { color:#5d536b; font-size:13px; line-height:1.7; margin:10px 0 16px; }
.approval-number { font-size:12px; color:#766f83; }
.approval-card .fact-amount { margin:6px 0 16px; font-variant-numeric:tabular-nums; }
.approval-card .fact-row:last-child { border-bottom:none; }
.task-graph { width:100%; max-width:760px; margin:8px auto; overflow:hidden; }
.task-graph svg { display:block; width:100%; height:auto; max-height:510px; }
.task-graph a:hover rect, .task-graph a:focus rect { stroke:#584174; stroke-width:2.5; }
.dag-evidence { border:1px solid var(--line); border-radius:12px; margin:7px 0;
padding:9px 12px; background:#fff; font-size:12px; }
.dag-evidence summary { cursor:pointer; color:#66557a; }
.dag-evidence pre { white-space:pre-wrap; overflow-wrap:anywhere; max-height:420px; overflow:auto; }
.dag-evidence :target { scroll-margin-top:72px; }
.st-key-im_chat { background:#ededed; padding:18px; border:1px solid #dedede;
border-radius:22px; max-width:860px; margin:auto; }
.im-banner { background:#f6f6f6; color:#313131; padding:16px 20px;
border-radius:18px 18px 0 0; border-bottom:1px solid #ddd; font-weight:650; }
.st-key-im_chat [data-testid="stChatMessage"] { background:#fff; border-radius:15px;
padding:14px 18px; border:1px solid #e5e5e5; }
.st-key-im_chat [data-testid="stChatMessage"]:has([data-testid="stChatMessageAvatarUser"]) {
background:#caedab; margin-left:26px; }
.st-key-im_chat [data-testid="stChatMessage"]:has([data-testid="stChatMessageAvatarAssistant"]) {
margin-right:26px; }
.danger { border-left:3px solid #c58d46; background:#fff6e7; padding:12px 14px;
font-size:13px; line-height:1.7; margin:12px 0; border-radius:0 10px 10px 0; }
.node { border-left:2px solid #c4b5d3; margin-left:8px; padding:4px 0 14px 18px; }
.node-title { font-size:15px; font-weight:650; }
.node-status { color:var(--muted); font-size:12px; }
[data-testid="stMetric"] { background:white; border:1px solid var(--line); padding:14px;
border-radius:15px; } [data-testid="stMetricValue"] { font-size:25px; }
[data-testid="stVerticalBlockBorderWrapper"] { border-radius:16px; }
.stButton > button { border-radius:10px; min-height:42px; }
button[kind="primary"] { background:#584174; border-color:#584174; }
.stMarkdown p, .stMarkdown li, code { overflow-wrap:anywhere; }
[data-testid="stCode"] pre { white-space:pre-wrap; overflow-wrap:anywhere; }
@media(max-width:640px) {
.block-container { padding:1rem .85rem 2rem; } .bank-name { font-size:19px; }
.hero { padding:22px 18px; } .hero h2 { font-size:23px !important; }
.fact-panel { padding:16px; } .fact-amount { font-size:29px; }
.fact-row { grid-template-columns:90px 1fr; } .notice { font-size:11px; }
[data-testid="stHorizontalBlock"] { flex-wrap:wrap; gap:12px; }
[data-testid="stColumn"] { min-width:100% !important; width:100% !important; }
.approval-card { padding:18px; } .approval-card .fact-amount { font-size:32px; }
.task-graph svg { max-height:none; }
[class*="-quick-"] [data-testid="stHorizontalBlock"] { flex-wrap:nowrap; gap:8px; }
[class*="-quick-"] [data-testid="stColumn"] { min-width:0 !important;
width:calc((100% - 16px) / 3) !important; flex:1 1 0; }
[class*="-quick-"] .stButton > button { min-height:58px; padding:6px; }
[class*="-quick-"] .stButton p { font-size:13px; }
}
</style>
"""

LABELS = {
    "kind": "Action",
    "first_run": "First run (Beijing time)",
    "schedule_period": "Schedule",
    "participant_refs": "Participants",
    "participant_share": "Share per participant",
    "amount": "Amount",
    "available": "Available balance",
    "fees": "Fees",
    "fee_breakdown": "Fee breakdown",
    "net_proceeds": "Net proceeds",
    "return_account_ref": "Return account",
    "settlement_date": "Settlement date (UTC+8)",
    "total_before": "Daily total · before",
    "total_after": "Daily total · after",
    "objects": "Business objects",
    "source_ref": "Source account",
    "contact_ref": "Recipient contact",
    "product_ref": "Product",
    "merchant_ref": "Merchant",
    "locked_ref": "Locked account",
    "tier_reasons": "Approval tier reasons",
    "execution_window": "Execution window / due time",
    "risk": "Risk and validity",
    "best_interest": "Best-interest certificate",
    "principal_token": "Masked customer",
    "business_date": "Business date",
    "spa_hash": "Scheduled authorization binding",
    "plan_hash": "Plan hash",
    "node_id": "Node ID",
    "device_session_id": "Device session",
    "dependency_hash": "Dependency hash",
    "ccy": "Currency",
    "rank": "Rank",
    "gap": "Gap from first place",
    "candidate_ids": "All eligible products",
    "chosen_product_id": "Selected product",
    "best_choice": "First in the standard ranking",
    "rows": "Complete ranking and calculations",
    "expected_net": "Net performance benchmark (not guaranteed)",
    "total_fee": "Projected total fees",
    "fee_buy": "Purchase fee",
    "fee_hold": "Holding fee",
    "fee_sell": "Redemption fee",
    "access_days": "Holding period + redemption delay (days)",
    "product_id": "Synthetic product ID",
    "created_at": "Created at",
    "start_at": "Starts at",
    "end_at": "Ends at",
    "due_at": "Current run due at",
    "max_runs": "Maximum runs",
    "run_index": "Current run",
    "creation_tier": "Approval tier at creation",
    "target": "Fixed recipient",
    "schedule": "Fixed execution schedule",
    "amount_bound": "Fixed amount limit",
    "expires_at": "Valid until",
    "level": "Risk level",
    "scope": "Authorization scope",
    "schedule_terms": "Scheduled authorization terms",
    "spa": "Scheduled payment authorization",
    "start": "Authorization starts at",
    "end": "Authorization ends at",
    "cadence": "Frequency",
    "interval_days": "Interval (days)",
    "at": "Fixed execution time",
    "destination_ref": "Fixed destination",
    "destination_version": "Destination version",
    "risk_level": "Product risk level",
    "risk_profile": "Customer risk profile",
    "assessment_expires_at": "Risk profile valid until",
    "mfa_expires_at": "Current run MFA valid until",
    "resume_scope": "Resume authorization scope",
    "status": "Status",
    "receipt_ref": "Bank receipt ID",
    "depends_on": "Dependencies",
    "first_due_at": "First due at",
    "recurrence": "Frequency",
    "fixed_amount": "Fixed amount per run",
    "scope_hash": "Authorization scope hash",
    "expiry": "Authorization expires at",
    "type": "Schedule type",
    "amount_ref": "Device-confirmed amount source",
    "schedule_ref": "Fixed schedule ID",
    "offer_ref": "Merchant order quote",
    "purpose_ref": "Device-confirmed business scope",
    "control": "Control request",
    "ownership_epoch": "Current handoff version",
    "plan_id": "Plan ID",
    "card_change": "Card change facts",
    "card_ref": "Simulated card ID",
    "card_token": "Simulated card token",
    "account_ref": "Account",
    "before": "Before",
    "after": "After",
    "limit": "Card transaction limit",
    "restrictions": "Restricted transaction categories",
    "preference_ref": "Signed preference reference",
    "assessment_ref": "Risk assessment reference",
    "raw_level": "Signed questionnaire risk level",
    "effective_level": "Effective risk level",
    "reason_codes": "Assessment reasons",
    "clarification": "Suitability clarification",
    # B3 (R-10): the business object a cancellation, card-control or redemption acts on.
    "subscription_object": "Debit authorization",
    "card_object": "Card",
    "position_object": "Holding",
    "schedule_object": "Scheduled transfer authorization",
}
KINDS = {
    "balance_query": "Balance query",
    "transactions_query": "Transaction history",
    "bill_analyze": "Billing analysis",
    "bill_report": "Bill report",
    "anomaly_query": "Unusual transaction review",
    "sandbox_analyze": "Exploratory sandbox analysis",
    "risk_assess": "Risk assessment",
    "product_compare": "Product comparison",
    "allocation_compare": "Allocation comparison",
    "investment_income_query": "Investment income query",
    "transfer": "Transfer",
    "purchase_product": "Product purchase",
    "redeem_product": "Product redemption",
    "subscription_query": "Subscription lookup",
    "subscription_reminders": "Renewal reminders",
    "cancel_subscription": "Cancel subscription",
    "lock_funds": "Lock budget",
    "unlock_funds": "Unlock budget",
    "schedule_transfer": "Scheduled payment authorization",
    "cancel_schedule": "Cancel scheduled authorization",
    "aa_request": "Split-bill collection request",
    "merchant_order": "Simulated merchant payment",
    "cancel_order": "Cancel merchant order",
    "refund_request": "Request a refund",
    "apply_virtual_card": "Apply for a simulated virtual card",
    "freeze_card": "Freeze card",
    "unfreeze_card": "Unfreeze card",
    "report_card_lost": "Report lost card",
    "restore_card": "Restore lost card (remain frozen)",
    "lower_card_limit": "Lower card limit",
    "raise_card_limit": "Raise card limit",
    "restrict_card": "Add transaction restrictions",
    "relax_card_restriction": "Relax transaction restrictions",
    "password_change_request": "Request a password change",
    "register_contact_request": "Request contact registration",
    "set_policy": "Attempted policy override (prohibited)",
    "set_permission": "Attempted permission override (prohibited)",
    "set_risk_level": "Attempted risk-level override (prohibited)",
    "set_contact_verified": "Attempted contact-verification override (prohibited)",
    "set_mfa": "Attempted MFA override (prohibited)",
    "bypass_approval": "Attempted approval bypass (prohibited)",
    "request_secret": "Attempted secret collection (prohibited)",
}
STATUSES = {
    "pending": "Pending",
    "ready": "Ready",
    "running": "Running",
    "settled": "Settled",
    "completed": "Completed",
    "succeeded": "Completed",
    "controlled": "Scheduled authorization created",
    "interrupted": "Interrupted",
    "waiting_input": "Awaiting device input",
    "waiting_approval": "Awaiting device approval",
    "awaiting_approval": "Awaiting device approval",
    "waiting_mfa": "Awaiting MFA for this run",
    "waiting_settlement": "Awaiting settlement",
    "blocked": "Blocked",
    "failed": "Failed",
    "skipped": "Skipped",
    "unknown": "Awaiting reconciliation",
    "scheduled": "Scheduled",
    "handoff": "Operator has taken over",
    "handed_off": "Operator has taken over",
    "expired": "Expired",
    "cancelled": "Cancelled",
    "compensating": "Compensation in progress",
    "compensated": "Compensated",
    "non_reversible": "Cannot be reversed directly",
    "active": "Active",
    "frozen": "Frozen",
    "lost": "Reported lost",
}


def money(value: Any) -> str:
    try:
        fraction = Fraction(value)
        display = (Decimal(fraction.numerator) / Decimal(fraction.denominator)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_EVEN
        )
        prefix = "¥" if get_locale() == "zh-CN" else "CNY "
        return f"{prefix}{display:,.2f}"
    except (ValueError, TypeError, ZeroDivisionError):
        return "—"


def text_value(value: Any) -> str:
    if isinstance(value, bool):
        return tr("Yes") if value else tr("No")
    if value is None:
        return tr("Not applicable")
    if isinstance(value, (dict, list)):
        return jcs_bytes(value).decode("utf-8")
    return tr(str(value))


MONEY_FIELDS = {
    "amount",
    "available",
    "total_before",
    "total_after",
    "fees",
    "fixed_amount",
    "expected_net",
    "total_fee",
    "fee_buy",
    "fee_hold",
    "fee_sell",
    "amount_bound",
    "net_proceeds",
    "limit",
}
REFERENCE_FIELDS = {
    "source_ref",
    "contact_ref",
    "product_ref",
    "merchant_ref",
    "locked_ref",
    "return_account_ref",
    "account_ref",
    "product_id",
    "chosen_product_id",
}
TECHNICAL_FIELDS = {
    "principal_token",
    "depends_on",
    "receipt_ref",
    "candidate_ids",
    "card_token",
    "amount_ref",
    "offer_ref",
    "purpose_ref",
    "schedule_ref",
    "preference_ref",
    "assessment_ref",
    "destination_ref",
    "card_ref",
    "tier_reasons",
    "business_ref",
}
_MACHINE_VALUE = re.compile(
    r"(?:[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}|[0-9a-f]{32,128})",
    re.IGNORECASE,
)


def _technical_field(key: str) -> bool:
    return key in TECHNICAL_FIELDS or key.endswith(("_hash", "_id")) and key != "product_id"


def _controller_for(card: dict[str, Any] | None = None) -> Any:
    for name in ("controller", "wealth_controller"):
        controller = st.session_state.get(name)
        if isinstance(getattr(controller, "session", None), str) and (
            not card or getattr(controller, "session", None) == card.get("device_session_id")
        ):
            return controller
    return None


def reference_label(reference: Any, *, controller: Any = None) -> str:
    """Resolve presentation names only from existing public structured state."""
    value = str(reference)
    controller = controller if controller is not None else _controller_for()
    # Synthetic display names (products by catalog signature; accounts, merchants and
    # cards by identifier) are a projection only; unknown references keep the old label.
    named = display_names.label(value, controller.state if controller is not None else None)
    if named is not None:
        return named
    if controller is not None:
        contact = controller.state.contacts.get(value)
        if contact is not None:
            if not view_mode.is_judge():
                customer = display_names.contact_label(value, controller.state)
                if customer is not None:
                    return customer
            # Public aliases describe the contact; they never choose the recipient.
            aliases = [alias for alias in contact.aliases if alias]
            primary = (
                tr("Mom")
                if contact_aliases("mother").intersection(aliases)
                else safe_text(aliases[0])
                if aliases
                else tr("Contact")
            )
            identifier = safe_text(value.removeprefix("CONTACT_"))
            return tr("{name} (Contact {identifier})", name=primary, identifier=identifier)
        merchant = controller.state.merchants.get(value)
        if merchant is not None:
            return safe_text(merchant.display_alias)
        account = controller.state.accounts.get(value)
        if account is not None:
            return tr("Current account" if account.kind == "current" else "Locked budget account")
    fixed = {
        "A_CURRENT": "Current account",
        "A_LOCKED": "Locked budget account",
        "CONTACT_CHEN": "Roommate",
        "CONTACT_LIN_A": "Classmate",
        "CONTACT_LIN_B": "Neighbor",
    }
    if value in fixed:
        return tr(fixed[value])
    if value.startswith("P_"):
        return tr("Product {name}", name=safe_text(value.removeprefix("P_").replace("_", " ")))
    return safe_text(value)


def safe_text(value: Any) -> str:
    """Keep raw structured data and canonical bindings out of ordinary fact rows."""
    if isinstance(value, dict):
        return tr("Technical evidence")
    if isinstance(value, list):
        return " · ".join(safe_text(item) for item in value) or "—"
    text = text_value(value)
    if _MACHINE_VALUE.search(text):
        return tr("Technical evidence")
    if display_names.is_raw_time(text):
        # Customer view: Beijing time; the judge view keeps the canonical UTC value.
        return display_names.time_text(text)
    if re.fullmatch(r"-?\d+/[1-9]\d*", text):
        fraction = Fraction(text)
        rounded = Decimal(fraction.numerator) / Decimal(fraction.denominator)
        return f"{rounded:,.4f}".rstrip("0").rstrip(".")
    aliases = {"roommate": "Roommate", "classmate": "Classmate", "neighbor": "Neighbor"}
    return tr(aliases.get(text, text))


def _fact_rows_html(facts: dict[str, Any], *, controller: Any = None) -> str:
    rows = []
    for key, value in facts.items():
        if _technical_field(key) or isinstance(value, dict):
            continue
        label = display_value(LABELS.get(key, key))
        if key == "fees":
            shown = (
                tr("As shown on the bank receipt")
                if value is None
                else tr("{amount} (no fee)", amount=money(value))
                if Fraction(value) == 0
                else money(value)
            )
        elif key in MONEY_FIELDS:
            shown = money(value) if value is not None else tr("Not provided")
        elif key in REFERENCE_FIELDS and value is not None:
            shown = reference_label(value, controller=controller)
        elif key == "kind":
            shown = display_value(KINDS.get(str(value), tr("Business task")))
        elif key == "status":
            shown = tr(STATUSES.get(str(value), str(value)))
        elif key == "reason_codes" and isinstance(value, list):
            shown = " · ".join(tr("reason." + str(reason)) for reason in value)
        else:
            shown = safe_text(value)
        rows.append(
            f'<div class="fact-row"><div class="fact-label">{escape(label)}</div>'
            f'<div class="fact-value">{escape(shown)}</div></div>'
        )
    return "".join(rows)


def fact_rows(facts: dict[str, Any]) -> None:
    html = _fact_rows_html(facts, controller=_controller_for())
    if html:
        st.markdown('<div class="fact-panel">' + html + "</div>", unsafe_allow_html=True)


def localized_facts(
    facts: list[dict[str, Any]], controller: Any, *, guarded: bool = True
) -> list[dict[str, Any]]:
    """Re-render original authenticated references without mutating state or audit."""
    if not facts:
        return []
    renderer = render_guarded if guarded else render_narrative
    result = renderer(
        Narrative(
            fact_refs=[
                FactRef(
                    receipt_ref=fact["receipt_ref"],
                    field=fact["field"],
                    expected=fact["exact_value"],
                )
                for fact in facts
            ]
        ),
        controller.state,
        controller.device.principal,
        controller.gateway.vault,
    )
    return [fact.model_dump(mode="json") for fact in result.facts]


def render_fact_projection(projection: dict[str, Any], controller: Any) -> None:
    """Show trusted narrative facts as readable text, with raw evidence on demand."""
    # Bank projections retain their original renderer and exploratory warnings.
    facts = localized_facts(projection.get("facts", []), controller, guarded=False)
    for fact in facts:
        st.write(fact["text"])
    if projection.get("facts") and not facts:
        st.warning(tr("The requested facts could not be verified."))
    with st.expander(tr("Binding identifiers and complete canonical JSON"), expanded=False):
        st.json(projection)


def tier_reason(card: dict[str, Any], *, controller: Any = None) -> str:
    """Explain the gateway's tier from its actual facts; never choose a new tier."""
    reasons = list(card.get("tier_reasons", []))
    kind = str(card.get("kind", ""))
    operation = tr(KINDS.get(kind, "Business approval"))
    amount = money(card["amount"]) if card.get("amount") is not None else ""
    if kind in {"lock_funds", "unlock_funds"} and card.get("tier") == "yellow":
        purpose = tr("Birthday plan") if card.get("purpose") == "birthday" else tr("this plan")
        return tr(
            (
                "Lock {amount} from the current account for {purpose}; "
                "yellow-tier confirmation is required."
            )
            if kind == "lock_funds"
            else (
                "Release {amount} of the remaining locked budget to the current account; "
                "yellow-tier confirmation is required."
            ),
            amount=amount,
            purpose=purpose,
        )
    non_daily = [reason for reason in reasons if reason not in {"daily_limit", "small_transfer"}]
    if non_daily:
        return " ".join(
            tr(
                "{operation} {amount} is a sensitive operation; "
                "red-tier independent simulated MFA is required.",
                operation=operation,
                amount=amount,
            )
            if reason == "sensitive_operation"
            else tr(
                "{operation} {amount}: {reason}",
                operation=operation,
                amount=amount,
                reason=tr("tier_reason." + str(reason)),
            )
            for reason in non_daily
        )
    if card.get("kind") in {"transfer", "merchant_order"} and card.get("total_after") is not None:
        total = card["total_after"]
        if controller is not None:
            limit = controller.state.policy.transfer_yellow_limit
            exceeds = Fraction(total) > limit
            if card.get("tier") == ("red" if exceeds else "yellow"):
                template = (
                    "Daily total {total} > {limit}; independent simulated MFA is required."
                    if exceeds
                    else "Daily total {total} ≤ {limit}; your confirmation is required."
                )
                return tr(template, total=money(total), limit=money(limit))
        if reasons:
            return " ".join(tr("tier_reason." + str(reason)) for reason in reasons)
        return tr("Daily total {total}; review the approval tier shown above.", total=money(total))
    if card.get("tier") == "green":
        return tr(
            "{operation} is read-only and moves no funds; green-tier automatic execution.",
            operation=operation,
        )
    if card.get("tier") == "yellow":
        descriptions = {
            "subscription_query": "Read subscription authorizations and recurring charges",
            "subscription_reminders": "Read the selected subscriptions' upcoming renewal dates",
            "cancel_subscription": (
                "Cancel future debits under the selected subscription authorization"
            ),
            "apply_virtual_card": (
                "Create a simulated virtual card for the selected current account"
            ),
            "freeze_card": "Freeze the selected card to prevent new transactions",
            "lower_card_limit": "Reduce the selected card's spending limit",
            "restrict_card": "Add transaction restrictions to the selected card",
            "cancel_schedule": "Cancel the selected authorization's remaining scheduled runs",
            "aa_request": "Create a split-bill collection request without moving funds",
            "cancel_order": "Request cancellation of the selected merchant order",
            "refund_request": (
                "Request a refund for the selected payment; funds are not yet returned"
            ),
            "redeem_product": "Redeem the selected holding under its settlement terms",
        }
        description = descriptions.get(kind)
        if description:
            return tr(
                "{description}; yellow-tier confirmation is required.",
                description=tr(description),
            )
        return tr(
            "{operation} {amount} changes or accesses the selected banking service; "
            "yellow-tier confirmation is required.",
            operation=operation,
            amount=amount,
        )
    return tr("Review the required approval tier before continuing.")


def _beijing(value: Any) -> str:
    """Display a canonical UTC timestamp in Beijing time; never consults the wall clock."""
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return tr("Execution time unverified")
    return moment.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")


def schedule_facts(card: dict[str, Any]) -> dict[str, str]:
    """First run and period read only from the canonical card's signed SPA scope."""
    spa = card.get("spa")
    window = card.get("execution_window")
    if card.get("kind") != "schedule_transfer" or not isinstance(spa, dict):
        return {}
    if not isinstance(window, dict) or window.get("start") != spa.get("start"):
        return {}
    schedule = spa.get("schedule") or {}
    until = _beijing(spa.get("expiry"))
    period = (
        tr(
            "schedule.period.monthly",
            day=schedule.get("day"),
            runs=spa.get("max_runs"),
            until=until,
        )
        if schedule.get("type") == "monthly"
        else tr("schedule.period.once", until=until)
    )
    return {"first_run": _beijing(spa.get("start")), "schedule_period": period}


def object_facts(card: dict[str, Any], *, controller: Any = None) -> dict[str, str]:
    """B3 (R-10): name the business object a card acts on, read from trusted state.

    Cancellation, card-control and redemption cards carry their object only as a reference
    (a debit authorization, a card, a holding or a scheduled authorization). Without these
    rows two such cards for different objects rendered to the same customer-view text, so
    the customer could not tell which one the device gesture would sign. Display only: the
    rows never enter the card, an input, a verdict or the audit.
    """
    state = getattr(controller, "state", None)
    objects = card.get("objects") or {}
    if state is None or not isinstance(objects, dict):
        return {}
    rows: dict[str, str] = {}
    ref = objects.get("authorization_ref")
    subscription_row = subscription_object(ref, controller=controller) if ref else None
    if subscription_row is not None:
        rows["subscription_object"] = subscription_row
    ref = objects.get("card_ref")
    if ref and ref in getattr(state, "cards", {}):
        owner = state.cards[ref].owner
        owned = [key for key, value in state.cards.items() if value.owner == owner]
        rows["card_object"] = display_names.label(ref, state, trace=False) or tr(
            "object.card.ordinal", index=owned.index(ref) + 1
        )
    ref = objects.get("position_ref")
    if ref and ref in getattr(state, "positions", {}):
        owner = state.positions[ref].owner
        owned = [key for key, value in state.positions.items() if value.owner == owner]
        rows["position_object"] = display_names.position_label(state, ref, owned.index(ref) + 1)
    ref = objects.get("schedule_ref")
    schedule_row = schedule_object(ref, controller=controller) if ref else None
    if schedule_row is not None:
        rows["schedule_object"] = schedule_row
    return rows


def subscription_object(ref: Any, *, controller: Any = None) -> str | None:
    """C1 (R-10): one debit authorization as the customer sees it, with its ordinal among
    the owner's authorizations, so two twins (same merchant, amount and period) never read
    the same. The chat's choice buttons use this same text."""
    state = getattr(controller, "state", None)
    subscriptions = getattr(state, "subscriptions", {}) if state is not None else {}
    subscription = subscriptions.get(str(ref)) if ref else None
    if subscription is None:
        return None
    owned = sorted(key for key, value in subscriptions.items() if value.owner == subscription.owner)
    return tr(
        "object.subscription",
        merchant=reference_label(subscription.merchant_ref, controller=controller),
        period=tr(
            "object.period.yearly" if subscription.period_months == 12 else "object.period.monthly"
        ),
        amount=money(subscription.amount),
        index=owned.index(subscription.id) + 1,
    )


def schedule_object(ref: Any, *, controller: Any = None) -> str | None:
    """One standing transfer authorization as the customer sees it, with its ordinal among
    the owner's authorizations (the chat's revocation buttons use this same text)."""
    state = getattr(controller, "state", None)
    authorizations = getattr(state, "authorizations", {}) if state is not None else {}
    spa = authorizations.get(str(ref)) if ref else None
    if spa is None or getattr(spa, "authorization_type", None) != "spa":
        return None
    owned = sorted(
        key
        for key, value in authorizations.items()
        if getattr(value, "authorization_type", None) == "spa" and value.principal == spa.principal
    )
    day = getattr(spa.schedule, "day", None)
    return tr(
        "object.schedule",
        contact=reference_label(getattr(spa.target, "contact_ref", ""), controller=controller),
        period=(
            tr("object.schedule.monthly", day=day)
            if day
            else tr("object.schedule.once", date=_beijing(spa.start))
        ),
        amount=money(spa.amount),
        index=owned.index(spa.id) + 1,
    )


def aa_facts(card: dict[str, Any], *, controller: Any = None) -> dict[str, str]:
    """Participants and the gateway's equal share, read only from the canonical card."""
    participants = (card.get("objects") or {}).get("participant_refs")
    if card.get("kind") != "aa_request" or not participants or card.get("amount") is None:
        return {}
    share = Fraction(card["amount"]) / len(participants)
    return {
        "participant_refs": "、".join(
            reference_label(ref, controller=controller) for ref in participants
        ),
        "participant_share": tr("aa.share_value", share=money(share), count=len(participants)),
    }


def execution_timing(card: dict[str, Any]) -> str:
    """Display only timing explicitly represented by the canonical card."""
    if "execution_window" not in card:
        return tr("Execution time unverified")
    window = card["execution_window"]
    if window is None and card.get("kind") != "schedule_transfer":
        return tr("Execute immediately")
    if isinstance(window, dict) and window:
        return " · ".join(
            f"{display_value(LABELS.get(key, key))}: {safe_text(value)}"
            for key, value in window.items()
        )
    return tr("Execution time unverified")


def render_card(
    card: dict[str, Any],
    *,
    canonical: bool = True,
    position: int | None = None,
    total: int | None = None,
    controller: Any = None,
    contextual_recipient: bool = False,
    hint_text: str | None = None,
    external_files: int = 0,
) -> None:
    if not card:
        st.info(tr("No business facts are ready for approval."))
        return
    title = (
        tr("Gateway approval card")
        if canonical
        else tr("Facts for device review · not yet authorized")
    )
    tier = str(card.get("tier", "yellow"))
    tier_text = {
        "green": tr("Green · automatic query"),
        "yellow": tr("Yellow · device approval required"),
        "red": tr("Red · independent simulated MFA required"),
        "black": tr("Black · execution prohibited"),
    }.get(tier, tier)
    amount = (
        money(card["amount"])
        if card.get("amount") is not None
        else display_value(KINDS.get(card.get("kind"), tr("Business approval")))
    )
    controller = controller or _controller_for(card)
    objects = card.get("objects") or {}
    rows = {
        key: objects[key]
        for key in ("contact_ref", "product_ref", "merchant_ref", "source_ref", "locked_ref")
        if objects.get(key) is not None
    }
    if card.get("kind") != "password_change_request":
        rows["fees"] = card.get("fees")
    if card.get("kind") == "transfer":
        rows["execution_window"] = execution_timing(card)
    elif card.get("kind") == "schedule_transfer":
        # Beijing-time first run and period; the raw UTC window stays in the expander below.
        rows.update(schedule_facts(card) or {"execution_window": execution_timing(card)})
    rows.update(aa_facts(card, controller=controller))
    rows.update(object_facts(card, controller=controller))
    rows.update({
        key: card[key]
        for key in ("net_proceeds", "settlement_date", "available", "total_after")
        if card.get(key) is not None
        and not (card.get("kind") == "password_change_request" and key == "total_after")
    })
    number = (
        tr("Payment {position} of {total}", position=position, total=total)
        if position is not None and total is not None and total > 1
        else ""
    )
    purpose = tr(KINDS.get(str(card.get("kind")), "Business approval"))
    st.markdown(
        '<section class="approval-card"><div class="approval-top">'
        f'<div class="fact-title">{escape(title)}</div>'
        f'<span class="approval-number">{escape(number)}</span></div>'
        f'<div class="approval-purpose">{escape(purpose)}</div>'
        f'<div class="fact-amount">{escape(amount)}</div>'
        f'<span class="tier tier-{escape(tier)}">{escape(tier_text)}</span>'
        f'<p class="approval-reason">{escape(tier_reason(card, controller=controller))}</p>'
        + _fact_rows_html(rows, controller=controller)
        + "</section>",
        unsafe_allow_html=True,
    )
    render_card_notes(
        card, controller=controller, hint_text=hint_text, external_files=external_files
    )
    if contextual_recipient:
        st.caption(tr("Using the previous recipient"))
    if card.get("irreversible"):
        st.markdown(
            tr(
                '<div class="danger">Irreversible: a settled payment cannot be und'
                "one directly. A refund request does not mean funds have been retu"
                "rned.</div>"
            ),
            unsafe_allow_html=True,
        )
    if tier == "red":
        st.warning(
            tr(
                "Red-tier action: Complete independent simulated MFA, then approve"
                " on the device. Dispatch requires verification."
            )
        )
    for key in ("execution_window", "risk", "card_change", "schedule_terms", "spa"):
        if isinstance(card.get(key), dict) and view_mode.is_judge():
            with st.expander(display_value(LABELS.get(key, key)), expanded=False):
                render_structured(card[key])
    if card.get("kind") == "schedule_transfer":
        st.caption(
            tr(
                "Authorization covers only the fixed scope, dates and runs shown a"
                "bove. Balance, destination, permissions and cumulative tier are c"
                "hecked at execution. A yellow-to-red change requires independent "
                "MFA for this run; without verification within 24 hours, the run e"
                "xpires. Previous codes are never reused."
            )
        )
    if view_mode.is_judge():
        # Customer view: the five suitability lines above replace the certificate table.
        if isinstance(card.get("best_interest"), dict):
            with st.expander(
                tr("#### All eligible products and selection evidence"), expanded=False
            ):
                render_bic(card["best_interest"])
        with st.expander(tr("Technical evidence"), expanded=False):
            if card.get("amount") is not None:
                st.caption(tr("Amount") + tr(" · canonical value ") + str(card["amount"]))
            st.caption(tr("permission_table"))
            render_contact_evidence([objects.get("contact_ref")], controller=controller)
            st.json(card)
    render_explanation(card=card)


def render_card_notes(
    card: dict[str, Any],
    *,
    controller: Any = None,
    hint_text: str | None = None,
    external_files: int = 0,
) -> None:
    """Suitability lines and the fraud warning; both read the card and never change it."""
    state = getattr(controller, "state", None)
    lines = suitability.lines(card, state)
    if lines:
        st.markdown(
            '<div class="suit-block">'
            f'<div class="suit-title">{escape(tr("polish.suit.title"))}</div><ul>'
            + "".join(f"<li>{escape(line)}</li>" for line in lines)
            + f'</ul><div class="suit-footer">{escape(suitability.footer())}</div></div>',
            unsafe_allow_html=True,
        )
    contacts = getattr(state, "contacts", None)
    reference = (card.get("objects") or {}).get("contact_ref")
    contact = contacts.get(reference) if isinstance(contacts, dict) and reference else None
    codes = fraud_hint.signals(card, contact=contact, text=hint_text, external_files=external_files)
    if not codes:
        return
    *shown, footer = fraud_hint.lines(
        codes,
        fraud_hint.payee_name(card, state),
        action=tr(KINDS.get(str(card.get("kind")), "Business approval")),
    )
    note = (
        f'<div class="fraud-judge">{escape(fraud_hint.judge_note(codes))}</div>'
        if view_mode.is_judge()
        else ""
    )
    st.markdown(
        '<div class="fraud-hint">'
        f'<div class="fraud-title">{escape(fraud_hint.title())}</div><ul>'
        + "".join(f"<li>{escape(line)}</li>" for line in shown)
        + f'</ul><div class="fraud-footer">{escape(footer)}</div>{note}</div>',
        unsafe_allow_html=True,
    )


def render_contact_evidence(references: list[Any], *, controller: Any = None) -> None:
    """Display source aliases only inside a caller's technical evidence expander."""
    if controller is None:
        return
    contacts = {
        reference: {"aliases": list(controller.state.contacts[reference].aliases)}
        for reference in references
        if reference in controller.state.contacts
    }
    if contacts:
        st.caption(tr("Recipient aliases from the trusted contact registry"))
        st.json(contacts)


def render_bic(bic: dict[str, Any]) -> None:
    st.markdown(tr("#### All eligible products and selection evidence"))
    facts = {
        key: bic[key]
        for key in ("candidate_ids", "chosen_product_id", "best_choice", "rank", "gap")
        if key in bic
    }
    fact_rows(facts)
    if bic.get("best_choice") is False:
        st.warning(
            tr(
                "The selected product is not first in the standard ranking. Review"
                " its rank and gap. Continuing requires your explicit, active choi"
                "ce."
            )
        )
    for row in bic.get("rows", []):
        st.write(
            f"{tr('Rank ')}{row.get('rank', '—')} · " + reference_label(row.get("product_id", "—"))
        )
        fact_rows({key: value for key, value in row.items() if key != "product_id"})


def render_structured(facts: dict[str, Any]) -> None:
    """Keep nested business facts readable without hiding them in raw JSON."""
    flat = {key: value for key, value in facts.items() if not isinstance(value, dict)}
    if flat:
        fact_rows(flat)
    for key, value in facts.items():
        if isinstance(value, dict) and not _technical_field(key):
            st.write(display_value(LABELS.get(key, key)))
            render_structured(value)


def render_dag(snapshot: dict[str, Any], *, key: str = "task", technical: bool = True) -> None:
    """Present a real graph and linked, collapsed evidence from the same snapshot."""
    if not snapshot.get("plan", {}).get("nodes"):
        st.caption(tr("No task yet. Start with a banking request."))
        return
    st.markdown(
        '<div class="task-graph">' + dag_svg(snapshot, key=key) + "</div>", unsafe_allow_html=True
    )
    if technical and view_mode.is_judge():
        st.caption(tr("Select a node to inspect its technical evidence."))
        st.markdown(dag_details_html(snapshot, key=key), unsafe_allow_html=True)


def render_plans(plans: list[dict[str, Any]], *, key: str = "active") -> None:
    for index, snapshot in enumerate(plans):
        st.markdown(f"{tr('#### Plan ')}{index + 1}")
        if snapshot.get("blocked"):
            st.warning(tr("Automatic execution has stopped for this plan."))
        render_dag(snapshot, key=f"{key}-plan-{index + 1}")
        if view_mode.is_judge():
            with st.expander(
                f"{tr('Plan ')}{index + 1}{tr(': canonical state and audit checkpoint')}"
            ):
                st.json(snapshot)


def receipt_line(receipt: Any) -> None:
    """A bank receipt's own time, number and status (JUDGING-1 list item 17): nothing
    that is not in the receipt. The customer view shows the time in Beijing time and the
    number as its last eight characters (it never shows a raw identifier); the judge view
    keeps both canonical values."""
    if not isinstance(receipt, dict) or receipt.get("receipt_type") != "bank":
        return
    status = str(receipt.get("status", ""))
    key = f"polish.receipt.status.{status}"
    number = str(receipt.get("receipt_id", ""))
    if not view_mode.is_judge():
        number = number.replace("-", "")[-8:].upper()
    st.caption(
        tr(
            "polish.receipt.line",
            time=display_names.time_text(receipt.get("executed_at")),
            receipt=number,
            status=tr(key) if key in UI_MESSAGES else status,
        )
    )


def render_result(result: dict[str, Any] | None) -> None:
    if not result:
        return
    status = str(result.get("status", "waiting"))
    receipt = result.get("receipt") or result.get("analysis_receipt")
    exploratory = isinstance(receipt, dict) and receipt.get("trust") == "exploratory"
    if status == "advanced":
        st.info(
            f"{tr('Simulated clock advanced to ')}"
            f"{result.get('clock', '—')}"
            " · "
            f"{result.get('events', 0)}"
            f"{tr(' due events processed')}"
        )
    if status in {"redisplay", "drift"}:
        st.warning(
            tr(
                "Business facts changed · the previous approval is invalid. Review"
                " the new card below and approve again."
            )
        )
    if result.get("results"):
        rows = result["results"]
        rows = rows.values() if isinstance(rows, dict) else rows
        st.write(tr("Execution results by node"))
        for row in rows:
            if isinstance(row, dict):
                render_result(row)
        return
    if status in {"settled", "complete", "executed"} and receipt:
        if exploratory:
            st.info(
                tr(
                    "Exploratory analysis ran · question semantics are unverified. Thi"
                    "s cannot authorize a payment."
                )
            )
        elif result.get("analysis_receipt"):
            st.success(tr("Analysis complete · computation receipt verified"))
        else:
            st.success(tr("Executed · bank receipt verified"))
            receipt_line(receipt)
    elif status in {"unknown", "pending"}:
        st.warning(
            tr(
                "Awaiting reconciliation · do not pay again. Wait for the bank rec"
                "eipt or operator review."
            )
        )
    elif status in {"handoff", "handed_off"}:
        st.warning(tr("Operator handoff · automatic execution paused"))
    elif status == "interrupted":
        st.warning(tr("Interrupted · automatic execution paused"))
    elif status == "resumed":
        st.info(
            tr(
                "Plan resumed within the device-approved scope. Every subsequent a"
                "ction still requires gateway checks."
            )
        )
    elif status == "no_active_plan":
        st.info(tr("No active plan. No plan was paused and no funds moved."))
    elif status == "no_compensation":
        st.info(
            tr(
                "No reversible compensation can be generated automatically. Settle"
                "d payments cannot be undone directly."
            )
        )
    elif status == "controlled":
        st.info(
            tr(
                "Control request recorded · see node status and bank receipts to c"
                "onfirm whether and when a payment occurred."
            )
        )
    elif status in {"redisplay", "drift"}:
        if result.get("card"):
            render_card(result["card"])
    elif status in {"rejected", "refused", "failed", "blocked"}:
        st.error(tr("Action not executed · review the reasons before approving again"))
    elif status == "outside_lock_month":
        st.warning(tr("birthday.outside_lock_month"))
    elif result.get("needs_mfa") or status in {"waiting_mfa", "escalated", "Escalate"}:
        st.warning(tr("Awaiting MFA · complete simulated verification on the Device page"))
    elif status != "advanced":
        st.info(tr("Awaiting approval · execution has not completed"))
    if result.get("rule_ids") and view_mode.is_judge():
        st.caption(tr("Rules: ") + " · ".join(result["rule_ids"]))
    render_explanation(
        card=result.get("card"),
        rule_ids=result.get("rule_ids", []),
        checkpoint=result.get("audit_checkpoint"),
        receipt=receipt,
    )
    if result.get("message") and not exploratory:
        st.write(prose(result["message"]))
    if receipt:
        st.markdown('<div id="receipt"></div>', unsafe_allow_html=True)
        if exploratory:
            projection = result.get("facts", {})
            if isinstance(projection, dict):
                if projection.get("status") == "rendered":
                    for fact in projection.get("facts", []):
                        st.write(fact["text"])
                if projection.get("note") or result.get("message"):
                    with st.expander(tr("Assistant note (unverified)"), expanded=True):
                        if projection.get("note"):
                            st.write(projection["note"])
                        if result.get("message"):
                            st.write(result["message"])
            if view_mode.is_judge():
                with st.expander(
                    tr("Raw exploratory evidence (semantics unverified)"), expanded=False
                ):
                    st.caption(
                        tr(
                            "Program labels and question semantics are unverified; the signatu"
                            "re authenticates only the computation source."
                        )
                    )
                    st.json(receipt)
        elif view_mode.is_judge():
            with st.expander(tr("Verified receipt"), expanded=False):
                st.json(receipt)
        if view_mode.is_judge():
            st.markdown(tr("[This receipt](#receipt) · audit chain on the Evidence & Audit page"))
            st.button(tr("View audit chain"), key=f"audit-link:{H(receipt)}", on_click=open_audit)
    if result.get("audit_checkpoint") and view_mode.is_judge():
        with st.expander(tr("Related audit checkpoint")):
            st.json(result["audit_checkpoint"])


def open_audit() -> None:
    st.session_state["page"] = "audit"
    st.session_state["active_page"] = "audit"
