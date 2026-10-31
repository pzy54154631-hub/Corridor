"""Independent fact map of approval cards (design §3.5.2).

For every card kind in the parity corpus this map states which business facts the card
frame must show, in which order, under which catalog label, read from which field of the
real display card. Values are formatted with the product's own formatting functions
(``money``, ``reference_label``, ``execution_timing``...), but which facts appear and
which label each one carries is fixed here, never read from a renderer: a fact that is
dropped, relabelled, reordered or read from another field on either page fails the
three-way comparison in ``test_card_parity.py``.

Modes: ``required`` facts must be present on the card itself (a missing one is reported
as a broken card); ``optional`` facts are shown only when the field is not None;
``always`` facts are shown whatever the value (the fee row reads "as shown on the bank
receipt" when the card has no fee yet).
"""

from contextlib import contextmanager
from dataclasses import dataclass
from fractions import Fraction
from typing import Any

from corridor.i18n import reset_locale, set_locale
from corridor.ui import display_names, fraud_hint, suitability
from corridor.ui.audience import reset_audience, set_audience
from corridor.ui.components import (
    KINDS,
    aa_facts,
    execution_timing,
    money,
    reference_label,
    safe_text,
    schedule_facts,
)
from corridor.ui.messages import display_value, tr


@dataclass(frozen=True)
class Fact:
    path: str
    label: str
    value: str
    mode: str = "optional"
    source: str | None = None


def _field(card, path):
    value: Any = card
    for part in path.split("."):
        value = value.get(part) if isinstance(value, dict) else None
    return value


def _reference(card, path, controller):
    return reference_label(_field(card, path), controller=controller)


def _fee(card, path, controller):
    value = card.get("fees")
    if value is None:
        return tr("As shown on the bank receipt")
    if Fraction(value) == 0:
        return tr("{amount} (no fee)", amount=money(value))
    return money(value)


def _money(card, path, controller):
    return money(_field(card, path))


def _text(card, path, controller):
    return safe_text(_field(card, path))


def _timing(card, path, controller):
    return execution_timing(card)


def _schedule(card, path, controller):
    return schedule_facts(card)[path]


def _participants(card, path, controller):
    return aa_facts(card, controller=controller)["participant_refs"]


def _share(card, path, controller):
    return aa_facts(card, controller=controller)["participant_share"]


OBJECT_REFERENCES = {
    "subscription_object": "authorization_ref",
    "card_object": "card_ref",
    "position_object": "position_ref",
    "schedule_object": "schedule_ref",
}


def expected_object_value(card, field, controller):
    """Derive the object label from its signed reference and trusted state only.

    The ordinal domain and business fields are fixed here, independently of
    object_facts, the Streamlit renderer and the web's signed_object projection.
    Catalog translations and scalar formatters are shared, as for other facts.
    """
    reference = card["objects"][field]
    state = controller.state
    if field == "authorization_ref":
        item = state.subscriptions[reference]
        owned = sorted(
            key for key, value in state.subscriptions.items() if value.owner == item.owner
        )
        return tr(
            "object.subscription",
            merchant=reference_label(item.merchant_ref, controller=controller),
            period=tr(
                "object.period.yearly" if item.period_months == 12 else "object.period.monthly"
            ),
            amount=money(item.amount),
            index=owned.index(reference) + 1,
        )
    if field == "card_ref":
        item = state.cards[reference]
        owned = [key for key, value in state.cards.items() if value.owner == item.owner]
        return display_names.label(reference, state, trace=False) or tr(
            "object.card.ordinal", index=owned.index(reference) + 1
        )
    if field == "position_ref":
        item = state.positions[reference]
        owned = [key for key, value in state.positions.items() if value.owner == item.owner]
        index = owned.index(reference) + 1
        product = display_names.label(item.product_ref, state)
        return (
            tr("polish.position.label", product=product, index=index)
            if product is not None
            else tr("Product {name}", name=str(index))
        )
    if field == "schedule_ref":
        item = state.authorizations[reference]
        assert item.authorization_type == "spa"
        owned = sorted(
            key
            for key, value in state.authorizations.items()
            if getattr(value, "authorization_type", None) == "spa"
            and value.principal == item.principal
        )
        day = getattr(item.schedule, "day", None)
        return tr(
            "object.schedule",
            contact=reference_label(item.target.contact_ref, controller=controller),
            period=(
                tr("object.schedule.monthly", day=day)
                if day
                else tr("object.schedule.once", date=display_names.beijing(item.start))
            ),
            amount=money(item.amount),
            index=owned.index(reference) + 1,
        )
    raise KeyError(f"no trusted object map for {field!r}")


def _object(card, path, controller):
    return expected_object_value(card, OBJECT_REFERENCES[path], controller)


VALUES = {
    "reference": _reference,
    "fee": _fee,
    "money": _money,
    "text": _text,
    "timing": _timing,
    "schedule": _schedule,
    "participants": _participants,
    "share": _share,
    "object": _object,
}

RECIPIENT = Fact("objects.contact_ref", "Recipient contact", "reference", "required")
RECIPIENT_IF_ANY = Fact("objects.contact_ref", "Recipient contact", "reference")
PRODUCT = Fact("objects.product_ref", "Product", "reference", "required")
MERCHANT = Fact("objects.merchant_ref", "Merchant", "reference")
SOURCE = Fact("objects.source_ref", "Source account", "reference", "required")
SOURCE_IF_ANY = Fact("objects.source_ref", "Source account", "reference")
LOCKED = Fact("objects.locked_ref", "Locked account", "reference", "required")
LOCKED_IF_ANY = Fact("objects.locked_ref", "Locked account", "reference")
FEES = Fact("fees", "Fees", "fee", "always")
WINDOW = Fact("execution_window", "Execution window / due time", "timing", "always")
FIRST_RUN = Fact("first_run", "First run (Beijing time)", "schedule", "always")
PERIOD = Fact("schedule_period", "Schedule", "schedule", "always")
PARTICIPANTS = Fact("objects.participant_refs", "Participants", "participants", "required")
SHARE = Fact("participant_share", "Share per participant", "share", "always")
NET = Fact("net_proceeds", "Net proceeds", "money")
SETTLEMENT = Fact("settlement_date", "Settlement date (UTC+8)", "text")
AVAILABLE = Fact("available", "Available balance", "money")
TOTAL_AFTER = Fact("total_after", "Daily total · after", "money")
BALANCES = (AVAILABLE, TOTAL_AFTER)
SUBSCRIPTION_OBJECT = Fact(
    "subscription_object", "Debit authorization", "object", "required", "objects.authorization_ref"
)
CARD_OBJECT = Fact("card_object", "Card", "object", "required", "objects.card_ref")
POSITION_OBJECT = Fact("position_object", "Holding", "object", "required", "objects.position_ref")
SCHEDULE_OBJECT = Fact(
    "schedule_object",
    "Scheduled transfer authorization",
    "object",
    "required",
    "objects.schedule_ref",
)

FACT_MAP = {
    "transfer": (RECIPIENT, SOURCE, FEES, WINDOW, *BALANCES),
    "schedule_transfer": (
        RECIPIENT_IF_ANY,
        MERCHANT,
        SOURCE,
        LOCKED_IF_ANY,
        FEES,
        FIRST_RUN,
        PERIOD,
        *BALANCES,
    ),
    "report_card_lost": (FEES, CARD_OBJECT, *BALANCES),
    "lock_funds": (SOURCE, LOCKED, FEES, *BALANCES),
    "unlock_funds": (SOURCE_IF_ANY, LOCKED, FEES, *BALANCES),
    "purchase_product": (PRODUCT, SOURCE, FEES, *BALANCES),
    "redeem_product": (FEES, POSITION_OBJECT, NET, SETTLEMENT, *BALANCES),
    "subscription_query": (FEES, *BALANCES),
    "cancel_subscription": (MERCHANT, FEES, SUBSCRIPTION_OBJECT, *BALANCES),
    "cancel_schedule": (FEES, SCHEDULE_OBJECT, *BALANCES),
    "aa_request": (RECIPIENT_IF_ANY, SOURCE_IF_ANY, FEES, PARTICIPANTS, SHARE, *BALANCES),
}


class BrokenCard(AssertionError):
    """The card itself lacks a business fact its kind must carry."""


@contextmanager
def in_view(audience, locale):
    """Read labels and values as one audience and one language would see them."""
    audience_token, locale_token = set_audience(audience), set_locale(locale)
    try:
        yield
    finally:
        reset_locale(locale_token)
        reset_audience(audience_token)


def expected_facts(card, controller):
    """[(field path, label, value)] the card frame must show, in order."""
    kind = card.get("kind")
    if kind not in FACT_MAP:
        raise KeyError(f"no fact map for card kind {kind!r}")
    rows = []
    for fact in FACT_MAP[kind]:
        source = fact.source or fact.path
        present = fact.mode == "always" or _field(card, source) is not None
        if fact.mode == "required" and not present:
            raise BrokenCard(f"{kind} card has no {source}")
        if fact.path in ("first_run", "schedule_period") and present:
            present = bool(schedule_facts(card))
        if present:
            value = VALUES[fact.value](card, fact.path, controller)
            rows.append((fact.path, display_value(fact.label), value))
    return rows


def expected_amount(card):
    """The amount line: the card's own amount, or the kind's name when it has none."""
    if card.get("amount") is not None:
        return money(card["amount"])
    return display_value(KINDS.get(card.get("kind"), tr("Business approval")))


def expected_note_kinds(card, controller, *, hint_text=None, external_files=0, context=False):
    """Notes drawn outside the frame, in order: suitability lines, the fraud warning, the
    previous-recipient reminder, the irreversibility warning, the red-tier warning and the
    scheduled-authorization scope."""
    state = getattr(controller, "state", None)
    contacts = getattr(state, "contacts", None) or {}
    contact = contacts.get(_field(card, "objects.contact_ref"))
    signals = fraud_hint.signals(
        card, contact=contact, text=hint_text, external_files=external_files
    )
    flags = (
        ("suitability", bool(suitability.lines(card, state))),
        ("fraud", bool(signals)),
        ("recipient_context", context),
        ("irreversible", bool(card.get("irreversible"))),
        ("red", str(card.get("tier", "yellow")) == "red"),
        ("schedule_scope", card.get("kind") == "schedule_transfer"),
    )
    return [kind for kind, shown in flags if shown]
