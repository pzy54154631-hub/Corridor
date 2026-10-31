"""C4: what the customer sees before a gesture is what the gesture signs (R-10).

1. A purchase review lists every preference the confirm gesture signs (account, amount,
   horizon, access days, product types, objective) in chat, on the page and in IM; the
   confirm signs exactly that snapshot, and refuses when the terms changed after review.
2. Two authorizations with the same merchant, amount and period, and two standing
   transfers with the same payee, amount and day, render to different customer text: on
   the card, on the choice buttons and in IM. The choice label equals the card's row.
3. Positive end-to-end paths with a fixture that allows them: a purchase in chat reaches a
   settled receipt through MFA, and an explicit full redemption reaches its receipt.
"""

import contextlib
import html
import re
from fractions import Fraction

import pytest

from corridor.agent.domestic_session import DomesticSession
from corridor.channels.im import IMAdapter, render_message
from corridor.i18n import reset_locale, set_locale
from corridor.schemas.domain import SubscriptionAuthorization
from corridor.ui import components, domestic_views, explanations
from corridor.ui.audience import reset_audience, set_audience
from corridor.ui.controller import DemoController, demo_state


def _session(*, showcase=False, state=None):
    controller = DemoController(initial_state=state or demo_state(showcase=showcase))
    controller.start_session()
    return DomesticSession(controller)


class _Page:
    """Records strings and button labels; a stand-in for streamlit."""

    def __init__(self):
        self.parts, self.buttons = [], []
        self.session_state = {}

    def _keep(self, *args, **_kwargs):
        self.parts.extend(str(value) for value in args if isinstance(value, str))

    markdown = caption = write = info = warning = error = success = text = _keep

    def button(self, label, *args, **kwargs):
        self.buttons.append(label)
        return False

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


@pytest.fixture
def page(monkeypatch):
    stand_in = _Page()
    for module in (components, explanations, domestic_views):
        monkeypatch.setattr(module, "st", stand_in)
    locale, audience = set_locale("zh-CN"), set_audience("customer")
    yield stand_in
    reset_audience(audience)
    reset_locale(locale)


def _text(parts):
    joined = html.unescape(re.sub(r"<[^>]+>", " ", " ".join(parts)))
    return re.sub(r"\s+", " ", joined).strip()


# --- 1. The purchase review lists what the gesture signs --------------------------------

TERMS = {
    "source_account_ref": "A_CURRENT",
    "amount": "2000/1",
    "horizon_days": 365,
    "max_access_days": 3,
    "allowed_types": ["wealth"],
    "objective": "net_return",
    "selection_mode": "explicit",
    "selected_product_ref": "P_A",
    "reason_code": "user_choice",
}


def _signed_preference(controller, pending_id):
    prepared = controller._pending[pending_id]["prepared"]
    reference = prepared.action.args.preference_ref
    value = dict(controller.state.user_inputs[reference].value)
    value.pop("id", None)
    return value


def test_the_review_lists_every_signed_preference_and_the_gesture_signs_exactly_that():
    session = _session()
    review = session.receive("帮我申购2000元稳健30天持有")
    terms = review["payload"]["purchase_terms"]
    assert {key: terms[key] for key in TERMS} == TERMS
    for words in ("365", "3", "活期", "理财"):
        assert words in review["message"], words
    card = session.confirm(review["response_id"])
    assert card["status"] == "waiting_mfa"
    signed = _signed_preference(session.controller, card["payload"]["pending_id"])
    assert {key: signed[key] for key in TERMS} == {key: terms[key] for key in TERMS}


def test_the_page_and_im_show_every_signed_preference(page):
    session = _session()
    review = session.receive("帮我申购2000元稳健30天持有")
    assert domestic_views.purchase_review(session, review)
    shown = _text(page.parts)
    account = components.reference_label("A_CURRENT", controller=session.controller)
    for words in ("365", "3", account, "理财", "2,000"):
        assert words in shown, (words, shown)
    text = render_message(review, controller=session.controller)["text"]
    for words in ("365", "3", account, "理财"):
        assert words in text, (words, text)


def test_terms_that_changed_after_review_are_never_signed(monkeypatch):
    session = _session()
    controller = session.controller
    review = session.receive("帮我申购2000元稳健30天持有")
    before = len(controller.state.user_inputs)
    changed = dict(review["payload"]["purchase_terms"], horizon_days=180)
    monkeypatch.setattr(controller, "purchase_terms", lambda *args, **kwargs: dict(changed))
    result = session.confirm(review["response_id"])
    assert result["status"] in {"clarify", "withdrawn"}, result["status"]
    assert len(controller.state.user_inputs) == before
    assert not controller._pending and controller.gateway.bank.execute_count == 0


# --- 2. Duplicate objects render differently everywhere --------------------------------


def _duplicate_subscription_state():
    state = demo_state(showcase=True)
    s1 = state.subscriptions["S1"]
    twin = SubscriptionAuthorization.model_validate({
        **s1.model_dump(mode="python"),
        "id": "S3",
        "bank_authorization_ref": "BANK_S3",
    })
    return state.model_copy(update={"subscriptions": {**state.subscriptions, "S3": twin}})


def test_twin_subscriptions_render_differently_on_cards_buttons_and_im(page):
    session = _session(state=_duplicate_subscription_state())
    controller = session.controller
    texts = {}
    for ref in ("S1", "S3"):
        card = controller.cancel_subscription(ref, request_id=f"twin:{ref}")["card"]
        page.parts.clear()
        components.render_card(card, controller=controller)
        texts[ref] = _text(page.parts)
    assert texts["S1"] != texts["S3"]
    listed = session.receive("取消订阅")
    assert listed["payload"]["subscription_choices"] == ["S1", "S3"]
    page.buttons.clear()
    domestic_views.subscription_choices(session, listed, "domestic")
    assert len(set(page.buttons)) == 2, page.buttons
    # The choice label is the card's own object row.
    for label in page.buttons:
        assert label in texts["S1"] or label in texts["S3"], label
    im_texts = set()
    for ref in ("S1", "S3"):
        chosen = session.prepare_subscription_cancel(listed["response_id"], ref)
        im_texts.add(render_message(chosen, controller=controller)["text"])
    assert len(im_texts) == 2


def test_twin_standing_authorizations_have_different_choice_labels(page):
    session = _session()
    controller = session.controller
    for index in range(2):
        pending = controller.preview_scheduled_transfer(
            "A_CURRENT",
            "CONTACT_LANDLORD",
            "2000",
            f"twin-spa-{index}",
            date_text="2026-11-05",
            monthly_day=5,
        )
        controller.approve_pending(pending["pending_id"])
        if controller._pending.get(pending["pending_id"], {}).get("mode") == "prepared":
            controller.request_mfa(pending["pending_id"])
            controller.complete_mfa(pending["pending_id"])
            controller.approve_pending(pending["pending_id"])
    listed = session.receive("取消每月5号给房东的转账")
    candidates = listed["payload"]["schedule_cancel"]["candidates"]
    assert len(candidates) == 2
    page.buttons.clear()
    domestic_views.schedule_cancel_choices(session, listed, "domestic")
    assert len(set(page.buttons)) == 2, page.buttons
    cards = [
        session.prepare_schedule_cancel(listed["response_id"], ref)["card"] for ref in candidates
    ]
    texts = []
    for card in cards:
        page.parts.clear()
        components.render_card(card, controller=controller)
        texts.append(_text(page.parts))
    assert texts[0] != texts[1]


# --- 3. Positive end-to-end paths ---------------------------------------------------------


def test_a_purchase_in_chat_reaches_a_settled_receipt():
    session = _session()
    review = session.receive("帮我申购2000元稳健30天持有")
    assert session.confirm(review["response_id"])["status"] == "waiting_mfa"
    assert session.verify_mfa(review["response_id"])["status"] == "mfa_verified"
    settled = session.confirm(review["response_id"])
    assert settled["status"] == "settled"
    receipt = settled["payload"]["receipt"]
    assert receipt["kind"] == "purchase_product" and receipt["status"] == "settled"
    [position] = session.controller.state.positions.values()
    assert position.principal_amount == Fraction(2000)


def test_an_explicit_full_redemption_reaches_its_receipt():
    session = _session(showcase=True)
    card = session.receive("把我持有的理财全部赎回")
    assert card["card"]["kind"] == "redeem_product"
    settled = session.confirm(card["response_id"])
    assert settled["status"] == "settled"
    assert settled["payload"]["receipt"]["kind"] == "redeem_product"


def test_the_im_channel_confirms_a_purchase_after_listing_all_terms():
    session = _session()
    adapter = IMAdapter(session)
    review = adapter.receive("帮我申购2000元稳健30天持有")
    assert review["facts_complete"] and "365" in review["text"]
    assert adapter.click("confirm", review["response_id"])["status"] == "waiting_mfa"


# --- 4. The revocation card: signed only by the gesture; a refused id is not taken -------


def test_a_refused_revocation_never_occupies_the_request_id():
    session = _session()
    controller = session.controller
    card = session.receive("每月5号给房东转2000元")
    session.confirm(card["response_id"])
    session.verify_mfa(card["response_id"])
    session.confirm(card["response_id"])
    [spa] = [
        ref
        for ref, item in controller.state.authorizations.items()
        if getattr(item, "authorization_type", None) == "spa"
    ]
    with pytest.raises(ValueError):
        controller.cancel_schedule("NOT-AN-AUTHORIZATION", request_id="revoke-1")
    # The same request id still serves the valid choice afterwards.
    assert controller.cancel_schedule(spa, request_id="revoke-1")["card"]["kind"] == (
        "cancel_schedule"
    )


def test_the_revocation_preview_signs_nothing_and_the_gesture_signs_once(monkeypatch):
    session = _session()
    controller = session.controller
    card = session.receive("每月5号给房东转2000元")
    session.confirm(card["response_id"])
    session.verify_mfa(card["response_id"])
    session.confirm(card["response_id"])
    listed = session.receive("取消每月5号给房东的转账")
    [ref] = listed["payload"]["schedule_cancel"]["candidates"]
    signed = []
    original = controller.device.confirm_input

    def counting(**kwargs):
        signed.append(kwargs["slot"])
        return original(**kwargs)

    monkeypatch.setattr(controller.device, "confirm_input", counting)
    revocation = session.prepare_schedule_cancel(listed["response_id"], ref)
    assert revocation["card"]["kind"] == "cancel_schedule"
    assert signed == []
    assert session.confirm(revocation["response_id"])["status"] == "controlled"
    assert signed == ["schedule_cancel"]
