"""B1: the dependency surface of the web package (design §3.2, §6.7 item 4).

A first alarm only: every name ``compat`` re-exports exists, every callable the web
package calls keeps the explicitly reviewed parameter names, and every private field the
accessors read is still there. Product revisions add optional keywords with pinned defaults;
the accepted signature changes are recorded in review/INTEGRATION-COMPAT.md outside the
repository. A changed meaning is caught by the equivalence and safety gates, not here.
"""

import inspect

import pytest

from corridor.sim.device_workflow import business_facts as device_business_facts
from corridor.web import compat
from tests.web.test_tasks import (
    RED,
    inv22,  # noqa: F401  (INV-2.2 probe)
    logged_in,
)

FUNCTIONS = {
    "reference_label": ["reference", "controller"],
    "aa_facts": ["card", "controller"],
    "money": ["value"],
    "object_facts": ["card", "controller"],
    "subscription_object": ["ref", "controller"],
    "tier_reason": ["card", "controller"],
    "schedule_facts": ["card"],
    "execution_timing": ["card"],
    "safe_text": ["value"],
    "text_value": ["value"],
    "_technical_field": ["key"],
    "response_copy": ["response", "completion", "hint_text"],
    "transfer_parse_copy": ["payload", "completion"],
    "task_node_status": ["response"],
    "task_plans": ["response", "snapshots"],
    "birthday_event_line": ["controller", "event"],
    "node_label": ["node"],
    "draft_snapshot": ["draft"],
    "subscription_projection": ["result"],
    "transaction_projection": ["result"],
    "timeline_projection": ["records", "history"],
    "remark_notice": ["utterance"],
    "beijing_time": ["value"],
    "billing_projection": ["result", "controller"],
    "anomaly_projection": ["result", "controller"],
    "load_billing": ["controller", "period"],
    "explanation": ["card", "rule_ids", "checkpoint", "receipt"],
    "plain_reasons": ["rule_ids"],
    "build_sms": ["challenge_id", "code", "expiry", "kind", "card", "state"],
    "sms_text": ["sms", "locale"],
    "sms_ref": ["challenge_id"],
    "tr": ["key", "values"],
    "prose": ["value"],
    "display_value": ["value"],
    "source_utterance": ["key"],
    "set_audience": ["audience"],
    "reset_audience": ["token"],
    "get_audience": [],
    "set_locale": ["locale"],
    "reset_locale": ["token"],
    "get_locale": [],
    "prepare_demo": ["kind", "defer_birthday_orders"],
    "refusal_title": ["rule_ids"],
    "access_proof": ["code", "nonce"],
    "_summary": ["outcomes"],
    "_failure_source": ["summary"],
    "render_message": ["response", "controller"],
    "demo_state": ["showcase"],
    "stable_id": ["task_id", "step", "kind"],
    "H": ["value"],
    "_challenge_active": ["controller", "challenge_id"],
    "_read_challenge": ["controller", "challenge_id"],
}

METHODS = {
    ("DemoController", "start_session"): [],
    ("DemoController", "select_contact"): ["draft_id", "contact_ref"],
    ("DemoController", "confirm_transfer"): ["draft_id", "contact_ref", "request_id"],
    ("DemoController", "approve_pending"): ["pending_id"],
    ("DemoController", "request_mfa"): ["pending_id"],
    ("DemoController", "complete_mfa"): ["pending_id", "code"],
    ("DemoController", "confirm_plan"): ["plan_id", "selected_node_ids"],
    ("DemoController", "preview_birthday"): [
        "selected_indices",
        "request_id",
        "event_ref",
        "relation",
    ],
    ("DemoController", "compensate"): ["plan_id"],
    ("DemoController", "stop"): ["plan_id", "keep_standing"],
    ("DemoController", "handoff"): ["plan_id"],
    ("DemoController", "scam_gate_establish"): ["identity", "text", "external_files", "origin"],
    ("DemoController", "scam_gate_answer"): ["identity", "card_identity", "epoch", "answers"],
    ("DemoController", "scam_gate_retry"): ["identity", "card_identity", "epoch"],
    ("DemoController", "scam_gate_status"): ["identity"],
    ("DemoController", "scam_gate_rejections"): [],
    ("DemoController", "kernel_refusal"): ["pending_id"],
    ("DemoController", "snapshot"): [],
    ("DemoController", "preview_subscriptions"): ["request_id"],
    ("DemoController", "cancel_subscription"): ["ref", "request_id"],
    ("DemoController", "compare_investments"): ["objective"],
    ("DemoController", "preview_investment"): [
        "product_ref",
        "reason_code",
        "request_id",
        "amount",
        "objective",
    ],
    ("DemoController", "redeem"): ["position_ref", "amount", "request_id"],
    ("DemoController", "birthday_candidates"): [],
    ("DemoController", "at_transaction_boundary"): [],
    ("DemoController", "open_actions"): [],
    ("DemoController", "advance_clock"): ["target"],
    ("DemoController", "_authoritative_cards"): ["identity"],
    ("DemoController", "_registered_plan_ids"): [],
    ("WealthController", "start_session"): [],
    ("WealthController", "stop"): ["plan_id"],
    ("DomesticSession", "receive"): ["text", "request_id", "external_files"],
    ("DomesticSession", "confirm"): ["response_id", "selected_node_ids"],
    ("DomesticSession", "verify_mfa"): ["response_id"],
    ("DomesticSession", "withdraw_pending"): [],
    ("DomesticSession", "response"): ["response_id"],
    ("DomesticSession", "prepare_aa"): ["response_id", "participants", "request_id"],
    ("DomesticSession", "aa_roster"): [],
    ("IMAdapter", "click"): ["button", "response_id", "selected_node_ids"],
}

OUTBOX_METHODS = {
    "deliver": ["pair", "sms"],
    "mark": ["pair", "ref", "status"],
    "inbox": ["pair"],
    "register": ["pair"],
}


def parameters(value):
    return [name for name in inspect.signature(value).parameters if name != "self"]


def test_every_reexported_name_exists():
    missing = [name for name in compat.__all__ if not hasattr(compat, name)]
    assert missing == []
    assert set(FUNCTIONS) <= set(dir(compat))


@pytest.mark.parametrize("name", sorted(FUNCTIONS))
def test_functions_keep_their_parameter_names(name):
    assert parameters(getattr(compat, name)) == FUNCTIONS[name]


@pytest.mark.parametrize("owner,name", sorted(METHODS))
def test_methods_keep_their_parameter_names(owner, name):
    assert parameters(getattr(getattr(compat, owner), name)) == METHODS[(owner, name)]


def test_purchase_optional_keywords_keep_the_business_pages_defaults():
    signature = inspect.signature(compat.DemoController.preview_investment)
    product = signature.parameters["product_ref"]
    assert product.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert product.default is None
    for name in ("request_id", "amount", "objective"):
        parameter = signature.parameters[name]
        assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
        assert parameter.default is None
    reason = signature.parameters["reason_code"]
    assert reason.kind is inspect.Parameter.KEYWORD_ONLY
    assert reason.default == "user_choice"


def test_stop_keeps_global_withdrawal_as_its_explicit_button_default():
    signature = inspect.signature(compat.DemoController.stop)
    plan = signature.parameters["plan_id"]
    assert plan.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert plan.default is None
    keep_standing = signature.parameters["keep_standing"]
    assert keep_standing.kind is inspect.Parameter.KEYWORD_ONLY
    assert keep_standing.default is False


@pytest.mark.parametrize("name", ["object_facts", "subscription_object"])
def test_business_object_helpers_keep_a_required_object_and_optional_controller(name):
    signature = inspect.signature(getattr(compat, name))
    reference = signature.parameters[FUNCTIONS[name][0]]
    assert reference.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert reference.default is inspect.Parameter.empty
    controller = signature.parameters["controller"]
    assert controller.kind is inspect.Parameter.KEYWORD_ONLY
    assert controller.default is None


@pytest.mark.parametrize("name", sorted(OUTBOX_METHODS))
def test_the_outbox_keeps_its_interface(name):
    assert parameters(getattr(compat.OUTBOX, name)) == OUTBOX_METHODS[name]


def test_constants_keep_their_shape():
    assert [key for key, _ in compat.EXAMPLES][:6] == [
        "transfer",
        "billing",
        "wealth",
        "cards",
        "subscriptions",
        "birthday",
    ]
    assert compat.scam_friction.QUESTIONS == ("verified", "guided")
    assert compat.scam_friction.STATUS == "paused"
    assert compat.REASON_STALE == "scam_stale"
    assert isinstance(compat.RESERVE_INPUT_TOKENS, int)


def test_the_private_fields_the_accessors_read_still_exist():
    harness = logged_in()
    controller, session = harness.controller, harness.session
    for field in ("_pending", "_drafts", "_plans", "_results", "_closed", "_requests"):
        assert isinstance(getattr(controller, field), (dict, set)), field
    assert isinstance(session._modes, dict) and isinstance(session._responses, dict)
    verifier = controller.gateway.mfa
    assert hasattr(verifier, "_lock") and isinstance(verifier._challenges, dict)
    assert controller.session_authorization is not None
    tkey = "T" + harness.send(RED)
    harness.confirm(tkey)
    identity = harness.identity(tkey)
    entry = compat.pending_entry(controller, identity)
    assert set(entry) == {"mode", "challenge_id", "verified", "tier", "has_result", "closed"}
    assert entry["tier"] == "red" and entry["mode"] == "prepared"
    assert compat.envelope_mode(session, harness.ws.tasks[tkey].rid) == "pending"
    harness.mfa_request(tkey)
    challenge = compat.pending_entry(controller, identity)["challenge_id"]
    assert compat.challenge_active(controller, challenge)
    code, expiry = compat.read_challenge(controller, challenge)
    assert code == controller.gateway.mfa.test_code(challenge) and expiry
    assert compat.challenges(controller) == {challenge: {"pending_id": identity, "verified": False}}
    view = compat.kernel_view(controller, identity)
    assert view["same_business_facts"] is True and view["outcome"] in {"Accept", "Escalate"}
    assert view["card_core"] and len(view["proposal_card"]) == 64
    assert compat.safety(controller)["state"] == "Normal"


def test_business_facts_is_the_device_workflows_own_comparison():
    harness = logged_in()
    tkey = "T" + harness.send(RED)
    cards = compat.authoritative_cards(harness.controller, harness.identity(tkey))
    harness.confirm(tkey)
    cards += compat.authoritative_cards(harness.controller, harness.identity(tkey))
    cards.append({"dependency_hash": "x", "amount": "1/1"})
    cards.append({})
    for card in cards:
        assert compat.business_facts(card) == device_business_facts(card)
    copy = compat.authoritative_cards(harness.controller, harness.identity(tkey))
    copy[0]["amount"] = "0/1"
    assert compat.authoritative_cards(harness.controller, harness.identity(tkey)) != copy
