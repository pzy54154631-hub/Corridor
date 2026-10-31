"""Read-only explanations use the gateway's actual facts and rule identities."""

import streamlit as st

from corridor.kernel.rules import RULES
from corridor.ui.audience import get_audience
from corridor.ui.messages import tr

_DESCRIPTIONS = {
    "APR": "Approval must match the current card, owner, scope and validity window.",
    "BRK": "The safety lock blocks money actions until owner device unlock succeeds.",
    "AUTH": "The device signature must authorize this exact action and scope.",
    "PROV": "Business values must resolve to current trusted sources.",
    "MFA": "Independent MFA must be valid and unused for this action.",
    "CTRL": "This control operation requires independent device verification.",
    "TIER": "The deterministic policy selects the required approval tier.",
    "BAL": "Available trusted funds must cover the action.",
    "DOMAIN": "The action must satisfy the current business state and schedule.",
    "BIC": "The signed catalog, suitability and deterministic comparison must agree.",
    "ALLOC": "The allocation must satisfy signed policy and portfolio constraints.",
    "RISK": "Risk answers and review must be current, signed and within policy.",
    "PLAN": "Plan dependencies, ownership and execution scope must remain valid.",
    "IDEM": "An execution identity cannot produce a second payment.",
    "PII": "Private values must remain inside the trusted boundary.",
    "RCPT": "Only a verified matching receipt can establish an execution fact.",
    "BANK": "Only a verified matching receipt can establish an execution fact.",
    "AUD": "Execution requires a valid append-only audit chain.",
    "SCHEMA": "The proposal must satisfy the canonical typed schema.",
    "CERT": "The certificate must match the exact action hash.",
    "READ": "The session must authorize this query scope.",
    "TOOL": "Only supported banking tools can be invoked.",
    "SBX": "Exploratory computation must remain within sandbox limits.",
}


def explanation(*, card=None, rule_ids=(), checkpoint=None, receipt=None):
    card = card or {}
    return {
        "tier": card.get("tier"),
        "tier_reasons": list(card.get("tier_reasons", [])),
        "rules": {
            rule: tr(_DESCRIPTIONS.get(RULES[rule].obligation, "Review the recorded rule outcome."))
            if rule in RULES
            else tr("Review the recorded rule outcome.")
            for rule in rule_ids
        },
        "fact_sources": {
            key: card[key]
            for key in (
                "objects",
                "dependency_hash",
                "business_context_hash",
                "action_hash",
                "device_session_id",
            )
            if key in card
        },
        "receipt_ref": receipt.get("id", receipt.get("receipt_ref")) if receipt else None,
        "audit_checkpoint": checkpoint,
    }


def plain_reasons(rule_ids):
    """Distinct plain-language obligations for the reported rules; no rule IDs."""
    reasons = []
    for rule in rule_ids:
        text = (
            tr(_DESCRIPTIONS.get(RULES[rule].obligation, "Review the recorded rule outcome."))
            if rule in RULES
            else tr("Review the recorded rule outcome.")
        )
        if text not in reasons:
            reasons.append(text)
    return reasons


def render_explanation(*, card=None, rule_ids=(), checkpoint=None, receipt=None):
    if get_audience() == "customer":
        # Customer view: one plain sentence, no rule IDs, hashes or checkpoints.
        reasons = plain_reasons(rule_ids)
        if reasons:
            st.caption(tr("polish.reason.plain_prefix") + " ".join(reasons))
        return
    if checkpoint is None and card:
        for key in ("controller", "wealth_controller"):
            controller = st.session_state.get(key)
            session = getattr(controller, "session", None)
            if session and session == card.get("device_session_id"):
                checkpoint = controller.gateway.audit.checkpoint()
                break
    value = explanation(card=card, rule_ids=rule_ids, checkpoint=checkpoint, receipt=receipt)
    with st.expander(tr("Why this action · rules, facts and audit")):
        tier = value["tier"]
        label = {
            "green": tr("Green · automatic query"),
            "yellow": tr("Yellow · device approval required"),
            "red": tr("Red · independent simulated MFA required"),
            "black": tr("Black · execution prohibited"),
        }.get(str(tier), str(tier))
        # Judge view keeps the raw enum in parentheses next to the localized tier name.
        st.caption(tr("Approval tier: ") + (f"{label} ({tier})" if tier else tr("Not assigned")))
        for reason in value["tier_reasons"]:
            st.write(tr("tier_reason." + reason))
        for rule, description in value["rules"].items():
            st.write(f"{rule} · {description}")
        if not value["rules"]:
            st.write(tr("No rejection or escalation rule was reported for this view."))
        st.caption(tr("Trusted source bindings and verified receipt reference"))
        st.json({"fact_sources": value["fact_sources"], "receipt_ref": value["receipt_ref"]})
        st.caption(tr("Current session audit checkpoint; a preview does not authorize execution."))
        st.json(value["audit_checkpoint"] or {})
