"""Card view models: the field assembly of ``components.render_card`` without rendering.

A card view model is built only from a card the caller passes, which for any action
with an identity is the controller's authoritative card (``compat.authoritative_cards``).
Every fact row carries the dotted path of the card field it is read from
(``objects.contact_ref``, ``fees``, ``execution_window``, ``net_proceeds``...). A row
derived from several fields uses the label name of the derived value instead:
``first_run`` (from ``spa.start``), ``schedule_period`` (from ``spa.schedule``,
``spa.max_runs`` and ``spa.expiry``) and ``participant_share`` (from ``amount`` and
``objects.participant_refs``). Row set, order and value formatting follow
``render_card`` and ``_fact_rows_html``. Notes (suitability, fraud warning, reminders)
are never facts: the page draws them outside the card frame.
"""

from copy import deepcopy
from fractions import Fraction
from typing import Any

from corridor.web.catalog import wtr
from corridor.web.compat import (
    KINDS,
    LABELS,
    MONEY_FIELDS,
    REFERENCE_FIELDS,
    STATUSES,
    _technical_field,
    aa_facts,
    display_value,
    execution_timing,
    explanation,
    fraud_hint,
    get_audience,
    money,
    object_facts,
    reference_label,
    reset_audience,
    safe_text,
    schedule_facts,
    set_audience,
    suitability,
    tier_reason,
    tr,
)
from corridor.web.vm.text import plain

TIERS = {
    "green": ("Green · automatic query", "circle"),
    "yellow": ("Yellow · device approval required", "diamond"),
    "red": ("Red · independent simulated MFA required", "triangle"),
    "black": ("Black · execution prohibited", "square"),
}
OBJECT_FIELDS = ("contact_ref", "product_ref", "merchant_ref", "source_ref", "locked_ref")
# Signed object references ``render_card`` does not draw: without them a cancel or a
# redemption card would not say which item it acts on (P-DIFF-26).
SIGNED_OBJECTS = {
    "cancel_subscription": "authorization_ref",
    "redeem_product": "position_ref",
}
CARD_CONTROL_KINDS = {
    "lower_card_limit",
    "raise_card_limit",
    "restrict_card",
    "relax_card_restriction",
    "freeze_card",
    "unfreeze_card",
}
TAIL_FIELDS = ("net_proceeds", "settlement_date", "available", "total_after")
STRUCTURED_FIELDS = ("execution_window", "risk", "card_change", "schedule_terms", "spa")
# Two rule obligations name internal objects; customers read plain words (INV-5.4).
CUSTOMER_REASONS = {
    "Approval must match the current card, owner, scope and validity window.": (
        "web.vm.reason.approval"
    ),
    "Execution requires a valid append-only audit chain.": "web.vm.reason.audit",
}
IRREVERSIBLE = (
    '<div class="danger">Irreversible: a settled payment cannot be undone directly. '
    "A refund request does not mean funds have been returned.</div>"
)
RED = (
    "Red-tier action: Complete independent simulated MFA, then approve on the device. "
    "Dispatch requires verification."
)
SCHEDULE_SCOPE = (
    "Authorization covers only the fixed scope, dates and runs shown above. Balance, "
    "destination, permissions and cumulative tier are checked at execution. A "
    "yellow-to-red change requires independent MFA for this run; without verification "
    "within 24 hours, the run expires. Previous codes are never reused."
)


def judge() -> bool:
    return get_audience() == "judge"


def customer_words(read: Any) -> str:
    """``read()`` in the customer view's wording, whatever the request's view."""
    token = set_audience("customer")
    try:
        return read()
    finally:
        reset_audience(token)


def red_words(read: Any) -> str:
    """The red tier's wording. The web draws the simulated SMS in a same-origin frame next
    to the code field, so neither view calls the check independent (P-DIFF-27)."""
    return customer_words(read) if judge() else read()


def tier_vm(code: Any) -> dict[str, str] | None:
    """The approval card's own tier label with a shape, so colour is never the only cue."""
    if code not in TIERS:
        return None
    label, glyph = TIERS[code]
    text = red_words(lambda: tr(label)) if code == "red" else tr(label)
    return {"code": str(code), "label": text, "glyph": glyph}


def signed_object(field: str, value: Any, controller: Any) -> str:
    """The product UI's object label, including its ordinal among the owner's objects.

    Merchant/product names and amounts alone do not distinguish twin objects. Read the
    shared projection using the signed reference, never the current selection index.
    """
    key = "subscription_object" if field == "authorization_ref" else "position_object"
    shown = object_facts({"objects": {field: value}}, controller=controller).get(key)
    if shown is None:
        shown = safe_text(value)
    return f"{shown} · {value}" if judge() else shown


def fact_value(field: str, value: Any, controller: Any) -> str:
    """``_fact_rows_html``'s formatting of one row value."""
    if field in SIGNED_OBJECTS.values():
        return signed_object(field, value, controller)
    if field == "fees":
        if value is None:
            return tr("As shown on the bank receipt")
        if Fraction(value) == 0:
            return tr("{amount} (no fee)", amount=money(value))
        return money(value)
    if field in MONEY_FIELDS:
        return money(value) if value is not None else tr("Not provided")
    if field in REFERENCE_FIELDS and value is not None:
        return reference_label(value, controller=controller)
    if field == "kind":
        return display_value(KINDS.get(str(value), tr("Business task")))
    if field == "status":
        return tr(STATUSES.get(str(value), str(value)))
    if field == "reason_codes" and isinstance(value, list):
        return " · ".join(tr("reason." + str(reason)) for reason in value)
    return safe_text(value)


def fact_rows(rows: list[tuple[str, str, Any]], controller: Any) -> list[dict[str, str]]:
    """(path, field, value) rows -> FactVM, skipping what ``_fact_rows_html`` skips."""
    return [
        {
            "key": path,
            "label": (
                wtr(f"web.vm.{path}")
                if path.startswith("card_change.")
                else (
                    wtr(f"web.vm.fact.{field}")
                    if field in SIGNED_OBJECTS.values()
                    else plain(display_value(LABELS.get(field, field)))
                )
            ),
            "value": plain(
                card_change_value(field, value, controller)
                if path.startswith("card_change.")
                else fact_value(field, value, controller)
            ),
        }
        for path, field, value in rows
        if not _technical_field(field) and not isinstance(value, dict)
    ]


def card_change_value(field: str, value: Any, controller: Any) -> str:
    """Format controls from the approval card, never the form or current controls."""
    if field == "status" and value in {"active", "frozen", "lost"}:
        return wtr(f"web.vm.cards.status.{value}")
    if field == "restrictions" and isinstance(value, list):
        if not value:
            return wtr("web.vm.cards.restrictions.none")
        return " · ".join(
            wtr(f"web.vm.cards.category.{item}")
            if item in {"cash", "online", "overseas", "entertainment"}
            else safe_text(item)
            for item in value
        )
    return fact_value(field, value, controller)


def card_rows(card: dict[str, Any], controller: Any) -> list[tuple[str, str, Any]]:
    """The rows ``render_card`` passes to ``_fact_rows_html``, with their field paths."""
    kind = card.get("kind")
    objects = card.get("objects") or {}
    signed = SIGNED_OBJECTS.get(str(kind))
    rows = [
        (f"objects.{field}", field, objects[field])
        for field in ((signed,) if signed else ()) + OBJECT_FIELDS
        if objects.get(field) is not None
    ]
    if kind != "password_change_request":
        rows.append(("fees", "fees", card.get("fees")))
    if kind == "transfer":
        rows.append(("execution_window", "execution_window", execution_timing(card)))
    elif kind == "schedule_transfer":
        schedule = schedule_facts(card)
        if schedule:
            rows.extend((field, field, schedule[field]) for field in schedule)
        else:
            rows.append(("execution_window", "execution_window", execution_timing(card)))
    shares = aa_facts(card, controller=controller)
    if shares:
        rows.append(("objects.participant_refs", "participant_refs", shares["participant_refs"]))
        rows.append(("participant_share", "participant_share", shares["participant_share"]))
    # Product revisions added card and standing-authorization identities to the card
    # frame. Keep those shared projections too; the two signed-reference rows above
    # already cover subscription and holding identities without repeating them.
    rows.extend(
        (field, field, value)
        for field, value in object_facts(card, controller=controller).items()
        if field not in {"subscription_object", "position_object"}
    )
    rows.extend(
        (field, field, card[field])
        for field in TAIL_FIELDS
        if card.get(field) is not None
        and not (kind == "password_change_request" and field == "total_after")
    )
    change = card.get("card_change")
    if kind in CARD_CONTROL_KINDS and isinstance(change, dict):
        for phase in ("before", "after"):
            controls = change.get(phase)
            if isinstance(controls, dict):
                rows.extend(
                    (f"card_change.{phase}.{field}", field, controls[field])
                    for field in ("limit", "restrictions", "status")
                    if field in controls
                )
    return rows


def note(kind: str, lines: list[str], *, title: str | None = None, footer: str | None = None):
    return {"kind": kind, "title": title, "lines": lines, "footer": footer}


def card_notes(
    card: dict[str, Any],
    controller: Any,
    *,
    hint_text: str | None = None,
    external_files: int = 0,
    contextual_recipient: bool = False,
) -> list[dict[str, Any]]:
    """``render_card_notes`` and the reminders ``render_card`` draws below the frame."""
    notes = []
    state = getattr(controller, "state", None)
    lines = suitability.lines(card, state)
    if lines:
        notes.append(
            note(
                "suitability",
                [plain(line) for line in lines],
                title=plain(tr("polish.suit.title")),
                footer=plain(suitability.footer()),
            )
        )
    contacts = getattr(state, "contacts", None)
    reference = (card.get("objects") or {}).get("contact_ref")
    contact = contacts.get(reference) if isinstance(contacts, dict) and reference else None
    codes = fraud_hint.signals(card, contact=contact, text=hint_text, external_files=external_files)
    if codes:
        *shown, footer = fraud_hint.lines(
            codes,
            fraud_hint.payee_name(card, state),
            action=tr(KINDS.get(str(card.get("kind")), "Business approval")),
        )
        footer = plain(footer)
        if judge():
            footer = f"{footer} {plain(fraud_hint.judge_note(codes))}"
        notes.append(
            note(
                "fraud",
                [plain(line) for line in shown],
                title=plain(fraud_hint.title()),
                footer=footer,
            )
        )
    if contextual_recipient:
        notes.append(note("recipient_context", [plain(tr("Using the previous recipient"))]))
    if card.get("irreversible"):
        notes.append(note("irreversible", [plain(tr(IRREVERSIBLE))]))
    if str(card.get("tier", "yellow")) == "red":
        notes.append(note("red", [plain(red_words(lambda: tr(RED)))]))
    if card.get("kind") == "schedule_transfer":
        notes.append(note("schedule_scope", [plain(tr(SCHEDULE_SCOPE))]))
    return notes


def why_lines(card: dict[str, Any] | None, rule_ids: Any = ()) -> list[str]:
    """``render_explanation``: one plain sentence for customers; tier and rules for judges."""
    rules = list(rule_ids or ())
    value = explanation(card=card, rule_ids=rules)
    if not judge():
        plain_words = {tr(source): wtr(key) for source, key in CUSTOMER_REASONS.items()}
        reasons = []
        for text in value["rules"].values():
            text = plain_words.get(text, text)
            if text not in reasons:
                reasons.append(text)
        if not reasons:
            return []
        return [plain(tr("polish.reason.plain_prefix") + " ".join(reasons))]
    tier = value["tier"]
    shown = tier_vm(tier)
    label = shown["label"] if shown else str(tier)
    lines = [plain(tr("Approval tier: ") + (f"{label} ({tier})" if tier else tr("Not assigned")))]
    lines.extend(plain(tr("tier_reason." + reason)) for reason in value["tier_reasons"])
    lines.extend(
        plain(wtr("web.vm.evidence.rule_line", rule=rule, text=text))
        for rule, text in value["rules"].items()
    )
    if not value["rules"]:
        lines.append(plain(tr("No rejection or escalation rule was reported for this view.")))
    return lines


def structured_lines(facts: dict[str, Any], controller: Any) -> list[str]:
    """``render_structured``: flat rows, then each nested non-technical block."""
    rows = [(key, key, value) for key, value in facts.items() if not isinstance(value, dict)]
    lines = [f"{row['label']}: {row['value']}" for row in fact_rows(rows, controller)]
    for key, value in facts.items():
        if isinstance(value, dict) and not _technical_field(key):
            lines.append(plain(display_value(LABELS.get(key, key))))
            lines.extend(structured_lines(value, controller))
    return lines


def bic_lines(bic: dict[str, Any], controller: Any) -> list[str]:
    """``render_bic``: the certificate's ranking facts, one line per row."""
    keys = ("candidate_ids", "chosen_product_id", "best_choice", "rank", "gap")
    rows = [(key, key, bic[key]) for key in keys if key in bic]
    lines = [f"{row['label']}: {row['value']}" for row in fact_rows(rows, controller)]
    if bic.get("best_choice") is False:
        lines.append(
            plain(
                tr(
                    "The selected product is not first in the standard ranking. Review its "
                    "rank and gap. Continuing requires your explicit, active choice."
                )
            )
        )
    for row in bic.get("rows", []):
        product = reference_label(row.get("product_id", "—"), controller=controller)
        lines.append(plain(f"{tr('Rank ')}{row.get('rank', '—')} · {product}"))
        rest = [(key, key, value) for key, value in row.items() if key != "product_id"]
        lines.extend(f"{item['label']}: {item['value']}" for item in fact_rows(rest, controller))
    return lines


def technical(card: dict[str, Any], controller: Any) -> dict[str, Any]:
    reference = (card.get("objects") or {}).get("contact_ref")
    contacts = getattr(getattr(controller, "state", None), "contacts", None) or {}
    contact = contacts.get(reference) if reference else None
    return {
        "card": deepcopy(card),
        "amount_raw": str(card["amount"]) if card.get("amount") is not None else None,
        "contact_aliases": [alias for alias in getattr(contact, "aliases", ()) or () if alias],
    }


def structured(card: dict[str, Any], controller: Any) -> list[dict[str, Any]]:
    blocks = [
        {
            "label": plain(display_value(LABELS.get(key, key))),
            "lines": structured_lines(card[key], controller),
        }
        for key in STRUCTURED_FIELDS
        if isinstance(card.get(key), dict)
    ]
    if isinstance(card.get("best_interest"), dict):
        blocks.append({
            "label": plain(tr("#### All eligible products and selection evidence")),
            "lines": bic_lines(card["best_interest"], controller),
        })
    return blocks


def card_vm(
    card: dict[str, Any],
    *,
    controller: Any,
    key: str,
    canonical: bool = True,
    position: int | None = None,
    total: int | None = None,
    contextual_recipient: bool = False,
    hint_text: str | None = None,
    external_files: int = 0,
) -> dict[str, Any]:
    """One CardVM from one card dict; the caller decides which card that is."""
    tier = str(card.get("tier", "yellow"))
    kind = card.get("kind")
    amount = (
        money(card["amount"])
        if card.get("amount") is not None
        else display_value(KINDS.get(kind, tr("Business approval")))
    )
    vm: dict[str, Any] = {
        "key": key,
        "title": plain(
            tr("Gateway approval card")
            if canonical
            else tr("Facts for device review · not yet authorized")
        ),
    }
    if position is not None and total is not None and total > 1:
        vm["position"] = plain(tr("Payment {position} of {total}", position=position, total=total))
    vm.update({
        "purpose": plain(tr(KINDS.get(str(kind), "Business approval"))),
        "amount_text": plain(amount),
        "tier": tier_vm(tier) or {"code": "black", "label": plain(tr(tier)), "glyph": "square"},
        "tier_reason": plain(
            red_words(lambda: tier_reason(card, controller=controller))
            if tier == "red"
            else tier_reason(card, controller=controller)
        ),
        "facts": fact_rows(card_rows(card, controller), controller),
        "notes": card_notes(
            card,
            controller,
            hint_text=hint_text,
            external_files=external_files,
            contextual_recipient=contextual_recipient,
        ),
        "not_signed_label": wtr("web.vm.not_signed"),
        "why": {"lines": why_lines(card)},
    })
    if judge():
        vm["technical"] = technical(card, controller)
        vm["structured"] = structured(card, controller)
    return vm


def cards_vm(cards: list[dict[str, Any]], *, prefix: str, controller: Any, **options: Any):
    """CardVMs keyed ``<prefix>-<index>``; several cards are numbered like plan nodes."""
    total = len(cards)
    return [
        card_vm(
            card,
            controller=controller,
            key=f"{prefix}-{index}",
            position=index + 1 if total > 1 else None,
            total=total if total > 1 else None,
            **options,
        )
        for index, card in enumerate(cards)
        if isinstance(card, dict) and card
    ]
