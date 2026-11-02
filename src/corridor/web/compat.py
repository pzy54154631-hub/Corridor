"""The web package's only dependency surface on ui/, agent/ and channels/.

Pure functions and constants are re-exported unchanged, so a product revision reaches
the web pages without a second copy. Every read of a controller's or a session's private
fields lives here, as do the single private write (``attach_source_text``) and the one
read of the verifier's private state; each accessor returns copies or plain values.
"""

from copy import deepcopy
from typing import Any

from corridor.agent.domestic_session import DomesticSession
from corridor.agent.llm import LLMClient, LLMConfigurationError, LLMOutputError
from corridor.channels.im import IMAdapter, render_message
from corridor.crypto.jcs import H
from corridor.i18n import get_locale, reset_locale, set_locale
from corridor.sim.keys import stable_id
from corridor.ui import display_names, fraud_hint, scam_friction, suitability
from corridor.ui.audience import get_audience, reset_audience, set_audience
from corridor.ui.business_views import (
    beijing_time,
    remark_notice,
    subscription_projection,
    timeline_projection,
    transaction_projection,
)
from corridor.ui.components import (
    KINDS,
    LABELS,
    MONEY_FIELDS,
    REFERENCE_FIELDS,
    STATUSES,
    TECHNICAL_FIELDS,
    _technical_field,
    aa_facts,
    execution_timing,
    money,
    object_facts,
    reference_label,
    safe_text,
    schedule_facts,
    schedule_object,
    subscription_object,
    text_value,
    tier_reason,
)
from corridor.ui.controller import DemoController, demo_state
from corridor.ui.dag import NODE_STYLES, draft_snapshot, node_label
from corridor.ui.domestic_demos import prepare_demo, refusal_title
from corridor.ui.domestic_views import (
    EXAMPLES,
    birthday_event_line,
    response_copy,
    task_node_status,
    task_plans,
    transfer_parse_copy,
)
from corridor.ui.explanations import explanation, plain_reasons
from corridor.ui.insights import anomaly_projection, billing_projection, load_billing
from corridor.ui.live_config import DEPLOYMENTS, SITE_DEPLOYMENTS, LiveConfig, read_credential
from corridor.ui.live_controls import FAILURE_REASONS, _failure_source, _summary
from corridor.ui.live_runtime import (
    GLOBAL_LOCK_AFTER,
    LOCK_AFTER_FAILURES,
    PROCESS,
    RESERVE_INPUT_TOKENS,
    BudgetExceeded,
    JournalClient,
    LedgerUnavailable,
    LiveSession,
    access_proof,
)
from corridor.ui.manual_mfa import _challenge_active
from corridor.ui.messages import UI_MESSAGES, display_value, prose, source_utterance, tr
from corridor.ui.phone import MASK, _read_challenge, build_sms, sms_text
from corridor.ui.phone_outbox import OUTBOX, sms_ref
from corridor.ui.risk_questions import ANSWER_OPTIONS
from corridor.ui.scam_gate import REASON_STALE
from corridor.ui.view_mode import CUSTOMER_HIDDEN_PAGES
from corridor.ui.wealth_controller import WealthController

__all__ = [
    "ANSWER_OPTIONS",
    "SITE_DEPLOYMENTS",
    "read_credential",
    "CUSTOMER_HIDDEN_PAGES",
    "DEPLOYMENTS",
    "EXAMPLES",
    "FAILURE_REASONS",
    "GLOBAL_LOCK_AFTER",
    "H",
    "KINDS",
    "LABELS",
    "LOCK_AFTER_FAILURES",
    "MASK",
    "MONEY_FIELDS",
    "NODE_STYLES",
    "OUTBOX",
    "PROCESS",
    "REASON_STALE",
    "REFERENCE_FIELDS",
    "RESERVE_INPUT_TOKENS",
    "STATUSES",
    "TECHNICAL_FIELDS",
    "UI_MESSAGES",
    "BudgetExceeded",
    "DemoController",
    "DomesticSession",
    "IMAdapter",
    "JournalClient",
    "LLMClient",
    "LLMConfigurationError",
    "LLMOutputError",
    "LedgerUnavailable",
    "LiveConfig",
    "LiveSession",
    "WealthController",
    "_failure_source",
    "_summary",
    "_technical_field",
    "aa_facts",
    "access_proof",
    "anomaly_projection",
    "beijing_time",
    "billing_projection",
    "birthday_event_line",
    "build_sms",
    "demo_state",
    "display_names",
    "display_value",
    "draft_snapshot",
    "execution_timing",
    "explanation",
    "fraud_hint",
    "get_audience",
    "get_locale",
    "load_billing",
    "money",
    "node_label",
    "object_facts",
    "plain_reasons",
    "prepare_demo",
    "prose",
    "reference_label",
    "refusal_title",
    "remark_notice",
    "render_message",
    "reset_audience",
    "reset_locale",
    "response_copy",
    "safe_text",
    "scam_friction",
    "schedule_facts",
    "schedule_object",
    "set_audience",
    "set_locale",
    "sms_ref",
    "sms_text",
    "source_utterance",
    "stable_id",
    "subscription_projection",
    "subscription_object",
    "suitability",
    "task_node_status",
    "task_plans",
    "text_value",
    "tier_reason",
    "timeline_projection",
    "tr",
    "transaction_projection",
    "transfer_parse_copy",
]

# Terminal envelope statuses (DomesticSession.withdraw_pending / confirm).
RETIRED = frozenset({"withdrawn", "rejected", "settled", "refused", "controlled"})


def authoritative_cards(controller, identity: str | None) -> list[dict[str, Any]]:
    """The controller's own current card(s) for an action, as a deep copy."""
    if not identity:
        return []
    return deepcopy(controller._authoritative_cards(identity))


def business_facts(card: Any) -> dict[str, Any]:
    """Every card field but the draft digest (sim.device_workflow.business_facts)."""
    return {key: value for key, value in (card or {}).items() if key != "dependency_hash"}


def pending_entry(controller, pending_id: str | None) -> dict[str, Any] | None:
    pending = controller._pending.get(pending_id) if pending_id else None
    if pending is None:
        return None
    tier = None
    if pending.get("mode") == "prepared":
        tier = pending["prepared"].proposal.verdict.tier
    return {
        "mode": pending.get("mode"),
        "challenge_id": pending.get("challenge_id"),
        "verified": pending.get("verified") is True,
        "tier": tier,
        "has_result": pending_id in controller._results,
        "closed": pending_id in controller._closed,
    }


def pending_verified(controller, pending_id: str | None) -> bool:
    return bool(pending_id) and controller._pending.get(pending_id, {}).get("verified") is True


def pending_card(controller, pending_id: str | None) -> dict[str, Any] | None:
    """The display card a red challenge belongs to (the SMS summary is built from it)."""
    pending = controller._pending.get(pending_id) if pending_id else None
    if pending is None:
        return None
    card = getattr(pending.get("display"), "card", None)
    return deepcopy(card) if isinstance(card, dict) else None


def challenges(controller) -> dict[str, dict[str, Any]]:
    """Challenge id -> {pending_id, verified} for every stored prepared action."""
    found = {}
    for key, value in controller._pending.items():
        if value.get("mode") == "prepared" and value.get("challenge_id"):
            found[value["challenge_id"]] = {
                "pending_id": key,
                "verified": value.get("verified") is True,
            }
    return found


def draft_entry(controller, draft_id: str | None) -> dict[str, Any] | None:
    draft = controller._drafts.get(draft_id) if draft_id else None
    if draft is None:
        return None
    return {"has_display": "display" in draft, "request_id": draft.get("request_id")}


def plan_entry(controller, plan_id: str | None) -> dict[str, Any] | None:
    plan = controller._plans.get(plan_id) if plan_id else None
    if plan is None:
        return None
    return {
        "node_ids": [node.node_id for node in plan["bundle"]["plan"].nodes],
        "confirmed": "confirmation" in plan,
        "source_text": plan.get("source_text"),
    }


def has_result(controller, identity: str | None) -> bool:
    return bool(identity) and identity in controller._results


def result_entry(controller, identity: str | None) -> dict[str, Any] | None:
    result = controller._results.get(identity) if identity else None
    return None if result is None else deepcopy(result)


def result_status(controller, identity: str | None) -> str | None:
    result = controller._results.get(identity) if identity else None
    return None if result is None else result.get("status")


def is_closed(controller, identity: str | None) -> bool:
    return bool(identity) and identity in controller._closed


def envelope_mode(session, response_id: str) -> str | None:
    return session._modes.get(response_id)


def logged_in(controller) -> bool:
    authorization = controller.session_authorization
    return authorization is not None and authorization.expires_at > controller.state.now


def challenge_active(controller, challenge_id: str | None) -> bool:
    return bool(challenge_id) and _challenge_active(controller, challenge_id)


def read_challenge(controller, challenge_id: str) -> tuple[str, str] | None:
    """Code and expiry for the simulated SMS only; None when the challenge is gone."""
    return _read_challenge(controller, challenge_id)


def safety(controller) -> dict[str, Any]:
    return deepcopy(controller.gateway.safety_snapshot(controller.device.principal))


def vault_tokenize(controller, text: str) -> str:
    return controller.gateway.vault.tokenize(text)


def kernel_view(controller, identity: str | None) -> dict[str, Any] | None:
    """The stored proposal's hashes and verdict (judge evidence); None before a proposal."""
    pending = controller._pending.get(identity) if identity else None
    if not pending or pending.get("mode") != "prepared":
        return None
    prepared = pending["prepared"]
    verdict = prepared.proposal.verdict
    facts = getattr(verdict, "verified_facts", None) or {}
    display = pending["display"].card
    return {
        "card_core": H(facts["card_core"]) if "card_core" in facts else None,
        "action": H(prepared.action),
        "cert": H(prepared.cert),
        "proposal_card": H(prepared.proposal.card),
        "display_card": H(display),
        "same_business_facts": business_facts(prepared.proposal.card) == business_facts(display),
        "rule_ids": list(getattr(verdict, "rule_ids", [])),
        "outcome": verdict.outcome,
        "tier": getattr(verdict, "tier", None),
        "challenge": pending.get("challenge_id"),
        "verified": pending.get("verified") is True,
    }


def draft_display(controller, draft_id: str | None) -> str | None:
    """The display id of a draft's device card (the key of its confirmation record)."""
    draft = controller._drafts.get(draft_id) if draft_id else None
    return getattr((draft or {}).get("display"), "display_id", None)


def confirmed_view(controller, display_id: str | None) -> dict[str, Any] | None:
    """``kernel_view`` of a draft confirmed and dispatched in one gesture (a yellow
    transfer), read from the device workflow's own record of that confirmation."""
    workflow = getattr(controller, "workflow", None)
    if not display_id or workflow is None:
        return None
    for key, (_, confirmation) in workflow._requests.items():
        if not str(key).endswith(":" + display_id):
            continue
        prepared = getattr(confirmation, "prepared", None)
        proposal = getattr(prepared, "proposal", None)
        display = getattr(getattr(confirmation, "display", None), "card", None)
        if proposal is None or not isinstance(display, dict):
            return None
        verdict = proposal.verdict
        facts = getattr(verdict, "verified_facts", None) or {}
        return {
            "card_core": H(facts["card_core"]) if "card_core" in facts else None,
            "action": H(prepared.action),
            "cert": H(prepared.cert),
            "proposal_card": H(proposal.card),
            "display_card": H(display),
            "same_business_facts": business_facts(proposal.card) == business_facts(display),
            "rule_ids": list(getattr(verdict, "rule_ids", [])),
            "outcome": verdict.outcome,
            "tier": getattr(verdict, "tier", None),
            "challenge": None,
            "verified": False,
        }
    return None


def attach_source_text(controller, plan_id: str, text: str | None) -> None:
    """The one private write (B3): the plan keeps the user's own words, as on the page."""
    controller._plans[plan_id]["source_text"] = text


def registered_plan_ids(controller) -> list[str]:
    return list(controller._registered_plan_ids())
