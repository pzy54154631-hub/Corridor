"""B3 (risk R-10): the customer view is injective on card facts.

"What you see is what you sign" needs the converse of the renderer's purity: two canonical
cards whose consent-relevant facts differ must never render to the same customer-view
text. Consent-relevant means everything in the gateway's card except identifiers and hashes
that bind it (intent, node, plan, action and dependency hashes, the device session, the
principal token) and the internal input references (amount, preference and change inputs).

Cards come from the real preparers of one synthetic state (the interactive UI's showcase
state, plus a second subscription, card and holding added in this test only, so that every
object kind has a sibling to be confused with). Same-name contacts (two Lin) are covered by
transfers, a split bill and the clarification card. A finding fails this test; the test is
not weakened to pass.
"""

import contextlib
import html
import itertools
import re
from fractions import Fraction

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st_

from corridor.agent.domestic_session import DomesticSession
from corridor.i18n import reset_locale, set_locale
from corridor.schemas.domain import Card, Position, SubscriptionAuthorization
from corridor.ui import components, explanations
from corridor.ui.audience import reset_audience, set_audience
from corridor.ui.controller import DemoController, demo_state

# Identifiers and hashes that bind a card, never facts a customer consents to.
BINDING = {
    "action_hash",
    "intent_id",
    "node_id",
    "plan_hash",
    "dependency_hash",
    "business_context_hash",
    "device_session_id",
    "principal_token",
    "spa_hash",
}
INPUT_REFS = {"amount_ref", "preference_ref", "change_ref"}


def consent_facts(card):
    facts = {key: value for key, value in card.items() if key not in BINDING}
    objects = facts.get("objects")
    if isinstance(objects, dict):
        facts["objects"] = {key: value for key, value in objects.items() if key not in INPUT_REFS}
    return facts


class _Page:
    """Records every string the renderer would draw; a stand-in for streamlit."""

    def __init__(self):
        self.parts = []

    def _keep(self, *args, **_kwargs):
        self.parts.extend(str(value) for value in args if isinstance(value, str))

    markdown = caption = write = info = warning = error = success = text = _keep

    def json(self, *args, **_kwargs):
        self.parts.append(repr(args))

    def expander(self, *args, **_kwargs):
        self._keep(*args)
        return contextlib.nullcontext()

    def container(self, *_args, **_kwargs):
        return contextlib.nullcontext()

    def columns(self, spec, **_kwargs):
        count = spec if isinstance(spec, int) else len(spec)
        return [contextlib.nullcontext() for _ in range(count)]

    @property
    def session_state(self):
        return {}


@pytest.fixture
def customer_text(monkeypatch):
    page = _Page()
    monkeypatch.setattr(components, "st", page)
    monkeypatch.setattr(explanations, "st", page)
    locale = set_locale("zh-CN")
    audience = set_audience("customer")

    def render(card, controller):
        page.parts.clear()
        components.render_card(card, controller=controller)
        text = html.unescape(re.sub(r"<[^>]+>", " ", " ".join(page.parts)))
        return re.sub(r"\s+", " ", text).strip()

    yield render
    reset_audience(audience)
    reset_locale(locale)


def _state():
    """Showcase state plus one sibling of each object kind (test-only, data/ unchanged)."""
    state = demo_state(showcase=True)
    card = state.cards["CARD_UI_DEMO"]
    position = state.positions["POSITION_UI_DEMO"]
    subscription = state.subscriptions["S1"]
    return state.model_copy(
        update={
            "cards": {
                **state.cards,
                "CARD_UI_DEMO_2": Card.model_validate({
                    **card.model_dump(mode="python"),
                    "id": "CARD_UI_DEMO_2",
                    "card_token": "SYNTHETIC_UI_CARD_2",
                }),
            },
            "positions": {
                **state.positions,
                "POSITION_UI_DEMO_2": Position.model_validate({
                    **position.model_dump(mode="python"),
                    "id": "POSITION_UI_DEMO_2",
                    "product_ref": "P_B",
                }),
                # Same product and size as POSITION_UI_DEMO: only the holding tells them apart.
                "POSITION_UI_DEMO_3": Position.model_validate({
                    **position.model_dump(mode="python"),
                    "id": "POSITION_UI_DEMO_3",
                }),
            },
            "subscriptions": {
                **state.subscriptions,
                "S2": SubscriptionAuthorization.model_validate({
                    **subscription.model_dump(mode="python"),
                    "id": "S2",
                    "merchant_ref": "SUB_B",
                    "amount": Fraction(29),
                    "bank_authorization_ref": "BANK_S2",
                }),
            },
        }
    )


def _controller():
    controller = DemoController(initial_state=_state())
    controller.start_session()
    return controller


def _transfer(controller, text, contact=None):
    draft = controller.preview_transfer(text, request_id=f"inj:{text}:{contact}")
    if contact is not None:
        return controller.select_contact(draft["draft_id"], contact)["card"]
    assert draft["card"], text
    return draft["card"]


def _scheduled_authorization(controller, text):
    session = DomesticSession(controller, task_id="INJ:" + text)
    card = session.receive(text)
    session.confirm(card["response_id"])
    session.verify_mfa(card["response_id"])
    assert session.confirm(card["response_id"])["status"] == "controlled"
    return next(
        item.id
        for item in controller.state.authorizations.values()
        if getattr(item, "authorization_type", None) == "spa"
        and getattr(item.schedule, "day", None) == int(re.search(r"每月(\d+)号", text).group(1))
    )


def _pool():
    controller = _controller()
    cards = {
        "transfer mom 500": _transfer(controller, "给妈妈转500元"),
        "transfer mom 800": _transfer(controller, "给妈妈转800元"),
        "transfer landlord 500": _transfer(controller, "给房东转500元"),
        "transfer new 500": _transfer(controller, "给新联系人转500元"),
        "transfer Lin A 200": _transfer(controller, "给Lin转200元", "CONTACT_LIN_A"),
        "transfer Lin B 200": _transfer(controller, "给Lin转200元", "CONTACT_LIN_B"),
        "transfer classmate 300": _transfer(controller, "给同学转300元"),
        "transfer neighbor 300": _transfer(controller, "给邻居转300元"),
    }
    for name, day, monthly in (
        ("schedule landlord monthly 5", "2026-11-05", 5),
        ("schedule landlord monthly 6", "2026-11-06", 6),
        ("schedule landlord once 11-05", "2026-11-05", None),
    ):
        cards[name] = controller.preview_scheduled_transfer(
            None, "CONTACT_LANDLORD", "2000/1", f"inj:{name}", date_text=day, monthly_day=monthly
        )["card"]
    for name, participants in (
        ("aa chen lin A", ["CONTACT_CHEN", "CONTACT_LIN_A"]),
        ("aa chen lin B", ["CONTACT_CHEN", "CONTACT_LIN_B"]),
    ):
        cards[name] = controller.preview_aa_collection(
            "300/1", participants, request_id=f"inj:{name}"
        )["card"]
    for ref in ("S1", "S2"):
        cards[f"cancel subscription {ref}"] = controller.cancel_subscription(
            ref, request_id=f"inj:cancel:{ref}"
        )["card"]
    for ref in ("CARD_UI_DEMO", "CARD_UI_DEMO_2"):
        cards[f"freeze {ref}"] = controller.preview_card_operation(
            "freeze_card", card_ref=ref, request_id=f"inj:freeze:{ref}"
        )["card"]
    for ref in ("POSITION_UI_DEMO", "POSITION_UI_DEMO_2", "POSITION_UI_DEMO_3"):
        cards[f"redeem {ref}"] = controller.redeem(ref, "500/1", request_id=f"inj:redeem:{ref}")[
            "card"
        ]
    for ref in ("P_A", "P_B"):
        cards[f"purchase {ref}"] = controller.preview_investment(
            ref, request_id=f"inj:purchase:{ref}"
        )["card"]
    first = _scheduled_authorization(controller, "每月5号给房东转2000元")
    second = _scheduled_authorization(controller, "每月6号给房东转2000元")
    for name, ref in (("cancel schedule 5", first), ("cancel schedule 6", second)):
        cards[name] = controller.cancel_schedule(ref, request_id=f"inj:{name}")["card"]
    return controller, cards


def test_cards_with_different_consent_facts_render_differently(customer_text):
    controller, cards = _pool()
    texts = {name: customer_text(card, controller) for name, card in cards.items()}
    collisions = [
        (left, right)
        for left, right in itertools.combinations(sorted(cards), 2)
        if consent_facts(cards[left]) != consent_facts(cards[right]) and texts[left] == texts[right]
    ]
    assert not collisions, collisions


def test_the_pool_has_a_sibling_for_every_object_kind():
    _, cards = _pool()
    kinds = [card["kind"] for card in cards.values()]
    for kind in (
        "transfer",
        "schedule_transfer",
        "aa_request",
        "cancel_subscription",
        "freeze_card",
        "redeem_product",
        "purchase_product",
        "cancel_schedule",
    ):
        assert kinds.count(kind) >= 2, kind


def test_same_name_contacts_render_with_their_distinct_tails(customer_text):
    controller, cards = _pool()
    left = customer_text(cards["transfer Lin A 200"], controller)
    right = customer_text(cards["transfer Lin B 200"], controller)
    assert left != right
    assert "5502" in left and "8816" in right


CONTACTS = ("CONTACT_CHEN", "CONTACT_LIN_A", "CONTACT_LIN_B", "CONTACT_NEW", "CONTACT_LANDLORD")


@settings(max_examples=25, deadline=None)
@given(
    first=st_.tuples(st_.sampled_from(CONTACTS), st_.integers(min_value=1, max_value=900)),
    second=st_.tuples(st_.sampled_from(CONTACTS), st_.integers(min_value=1, max_value=900)),
)
def test_scheduled_transfers_to_any_two_recipients_and_amounts_render_differently(first, second):
    # Same-name contacts included; the scheduled form takes the recipient explicitly.
    if first == second:
        return
    controller = _controller()
    page = _Page()
    original_components, original_explanations = components.st, explanations.st
    components.st, explanations.st = page, page
    locale, audience = set_locale("zh-CN"), set_audience("customer")
    try:
        texts = []
        for contact, amount in (first, second):
            card = controller.preview_scheduled_transfer(
                None,
                contact,
                f"{amount}/1",
                f"inj:hyp:{contact}:{amount}",
                date_text="2026-11-05",
                monthly_day=5,
            )["card"]
            page.parts.clear()
            components.render_card(card, controller=controller)
            texts.append(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", " ".join(page.parts))))
    finally:
        components.st, explanations.st = original_components, original_explanations
        reset_audience(audience)
        reset_locale(locale)
    assert texts[0] != texts[1], (first, second)
