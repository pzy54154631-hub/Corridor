"""Distinct signed objects stay distinct in customer cards and selection lists.

Twins share every displayed merchant/product attribute and amount. Only their trusted
object identities differ. The web must preserve the product UI's object ordinal without
changing either the canonical approval card or business state.
"""

from copy import deepcopy

import pytest

from corridor.agent.domestic_session import DomesticSession
from corridor.i18n import reset_locale, set_locale
from corridor.ui import components
from corridor.ui.audience import reset_audience, set_audience
from corridor.ui.controller import DemoController, demo_state
from corridor.web import catalog
from corridor.web.state import new_web_session
from corridor.web.vm import cards, results, tabs


@pytest.fixture(params=["zh-CN", "en"])
def customer_locale(request):
    locale, audience = set_locale(request.param), set_audience("customer")
    yield request.param
    reset_audience(audience)
    reset_locale(locale)


def twin_controller(*, inactive_position=False):
    state = demo_state(showcase=True)
    subscription = state.subscriptions["S1"]
    position = state.positions["POSITION_UI_DEMO"]
    subscriptions = {
        **state.subscriptions,
        "S3": subscription.model_copy(update={"id": "S3", "bank_authorization_ref": "BANK_S3"}),
    }
    positions = {
        **state.positions,
        "POSITION_TWIN": position.model_copy(update={"id": "POSITION_TWIN"}),
    }
    if inactive_position:
        positions = {
            "POSITION_OLD": position.model_copy(update={"id": "POSITION_OLD", "status": "closed"}),
            **positions,
        }
    controller = DemoController(
        initial_state=state.model_copy(
            update={"subscriptions": subscriptions, "positions": positions}
        )
    )
    controller.start_session()
    return controller


def object_value(card, controller, field):
    vm = cards.card_vm(card, controller=controller, key="review-card")
    return next(fact["value"] for fact in vm["facts"] if fact["key"] == "objects." + field)


def test_twin_subscription_cards_are_distinct_and_match_the_choice_labels(customer_locale):
    controller = twin_controller()
    previews = {
        ref: controller.cancel_subscription(ref, request_id="cancel:" + ref)["card"]
        for ref in ("S1", "S3")
    }
    state_before = deepcopy(controller.state.model_dump(mode="python"))
    cards_before = deepcopy(previews)
    # A projection fixture, not a signed receipt or an end-to-end query claim. The real
    # detector refuses to match completely identical authorizations to one charge series;
    # the card preparers above independently accept each explicit authorized object.
    result = {
        "status": "settled",
        "cancellable": ["S1", "S3"],
        "analysis_receipt": {"computed_at": controller.state.now, "results": {}},
    }
    listing = results.subscriptions_vm(result, controller=controller)
    choice_labels = {
        result["cancellable"][int(choice["key"][1:])]: choice["label"]
        for choice in listing["cancel_choices"]
    }
    shown = {
        ref: object_value(card, controller, "authorization_ref") for ref, card in previews.items()
    }
    assert shown["S1"] != shown["S3"]
    assert choice_labels["S1"] != choice_labels["S3"]
    for ref, value in shown.items():
        assert value == choice_labels[ref]
        assert value == components.subscription_object(ref, controller=controller)
        assert ref not in value
    assert controller.state.model_dump(mode="python") == state_before
    assert previews == cards_before


@pytest.mark.parametrize("inactive_position", [False, True])
def test_twin_holding_cards_and_choices_use_the_same_owner_ordinal(
    customer_locale, inactive_position
):
    controller = twin_controller(inactive_position=inactive_position)
    previews = {
        ref: controller.redeem(ref, request_id="redeem:" + ref)["card"]
        for ref in ("POSITION_UI_DEMO", "POSITION_TWIN")
    }
    ws = new_web_session("customer", customer_locale, browser_id="b" * 43, tab_id="t" * 32)
    ws.controller = controller
    ws.session = DomesticSession(controller, ws.wealth)
    state_before = deepcopy(controller.state.model_dump(mode="python"))
    cards_before = deepcopy(previews)
    with catalog.bound(ws):
        listing = tabs.wealth_vm(ws, {}, controller.snapshot())
        active = [
            position["id"]
            for position in controller.snapshot()["positions"]
            if position["owner"] == controller.device.principal and position["status"] == "active"
        ]
        choice_labels = {active[int(row["key"][1:])]: row["label"] for row in listing["positions"]}
        shown = {
            ref: object_value(card, controller, "position_ref") for ref, card in previews.items()
        }
    assert shown["POSITION_UI_DEMO"] != shown["POSITION_TWIN"]
    for ref, value in shown.items():
        assert value == choice_labels[ref]
        assert (
            value
            == components.object_facts(previews[ref], controller=controller)["position_object"]
        )
        assert ref not in value
    assert controller.state.model_dump(mode="python") == state_before
    assert previews == cards_before


def visible_card(card, controller):
    vm = cards.card_vm(card, controller=controller, key="same-display-key")
    return (
        vm["title"],
        vm["purpose"],
        vm["amount_text"],
        tuple((fact["label"], fact["value"]) for fact in vm["facts"]),
    )


def test_different_cards_have_distinct_visible_approval_facts(customer_locale):
    state = demo_state(showcase=True)
    original = state.cards["CARD_UI_DEMO"]
    twin = original.model_copy(update={"id": "CARD_SECOND", "card_token": "SYNTHETIC_SECOND"})
    controller = DemoController(
        initial_state=state.model_copy(update={"cards": {**state.cards, twin.id: twin}})
    )
    controller.start_session()
    previews = [
        controller.preview_card_operation("freeze_card", card_ref=ref, request_id="freeze:" + ref)[
            "card"
        ]
        for ref in (original.id, twin.id)
    ]
    before = deepcopy(previews)
    assert previews[0]["objects"]["card_ref"] != previews[1]["objects"]["card_ref"]
    assert visible_card(previews[0], controller) != visible_card(previews[1], controller)
    assert previews == before


def test_twin_schedule_revocation_cards_have_distinct_visible_facts(customer_locale):
    controller = twin_controller()
    for index in range(2):
        preview = controller.preview_scheduled_transfer(
            "A_CURRENT",
            "CONTACT_LANDLORD",
            "2000",
            f"twin-schedule:{index}",
            date_text="2026-11-05",
            monthly_day=5,
        )
        pending = preview["pending_id"]
        controller.approve_pending(pending)
        if controller._pending.get(pending, {}).get("mode") == "prepared":
            challenge = controller.request_mfa(pending)["challenge_id"]
            code = controller.gateway.mfa.test_code(challenge)
            controller.complete_mfa(pending, code=code)
            controller.approve_pending(pending)
    refs = [
        ref
        for ref, item in controller.state.authorizations.items()
        if getattr(item, "authorization_type", None) == "spa"
    ]
    assert len(refs) == 2
    previews = [controller.cancel_schedule(ref, request_id="revoke:" + ref)["card"] for ref in refs]
    before = deepcopy(previews)
    assert previews[0]["objects"]["schedule_ref"] != previews[1]["objects"]["schedule_ref"]
    assert visible_card(previews[0], controller) != visible_card(previews[1], controller)
    assert previews == before
