"""Readable synthetic display names; a presentation projection only.

Names come from the polish display catalog and never enter canonical cards,
authorizations, kernel verdicts, dispatch or audit. Products are named by their
catalog attribute signature (two synthetic catalogs reuse the same identifiers
with different attributes), never by identifier alone and never by risk level.
"""

import re
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from typing import Any

from corridor.ui.audience import get_audience
from corridor.ui.messages import UI_MESSAGES, contact_aliases, tr

_ISO_Z = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
_FIXED_ALIASES = {"roommate": "Roommate", "classmate": "Classmate", "neighbor": "Neighbor"}


def _judge() -> bool:
    return get_audience() == "judge"


def _number(value: Any) -> str:
    fraction = Fraction(value)
    return (
        str(fraction.numerator)
        if fraction.denominator == 1
        else f"{fraction.numerator}_{fraction.denominator}"
    )


def product_key(product: Any) -> str:
    """The display key for one catalog entry's attribute signature."""
    rate = Fraction(product.expected_annual_rate)
    return (
        f"polish.product.r{product.risk_level}.h{product.min_hold_days}"
        f".d{product.redeem_delay_days}.m{_number(product.min_amount)}"
        f".y{rate.numerator}_{rate.denominator}"
    )


def catalog_product(ref: str, state: Any) -> Any:
    catalog = getattr(state, "catalog", None)
    for product in getattr(catalog, "products", None) or []:
        if product.product_id == ref:
            return product
    return None


def _with_ref(name: str, ref: str, trace: bool) -> str:
    return tr("polish.ref.suffix", name=name, ref=ref) if trace and _judge() else name


def label(ref: Any, state: Any = None, *, trace: bool = True) -> str | None:
    """A synthetic display name for a product, account, merchant or card; None if unknown.

    The judge view appends the synthetic identifier for traceability unless ``trace``
    is false (synthetic SMS text never carries identifiers).
    """
    value = str(ref)
    product = catalog_product(value, state) if state is not None else None
    if product is not None:
        key = product_key(product)
        return _with_ref(tr(key), value, trace) if key in UI_MESSAGES else None
    for group in ("account", "merchant", "card"):
        key = f"polish.{group}.{value}"
        if key in UI_MESSAGES:
            return _with_ref(tr(key), value, trace)
    return None


def contact_parts(ref: Any, state: Any) -> tuple[str, str | None] | None:
    """Primary display name (same rule as the card's contact label) and synthetic tail."""
    value = str(ref)
    contact = getattr(state, "contacts", {}).get(value)
    if contact is None:
        return None
    aliases = [alias for alias in contact.aliases if alias]
    if contact_aliases("mother").intersection(aliases):
        primary = tr("Mom")
    elif aliases:
        primary = tr(_FIXED_ALIASES.get(aliases[0], aliases[0]))
    else:
        primary = tr("Contact")
    key = "polish.tail." + value
    return primary, (tr(key) if key in UI_MESSAGES else None)


def contact_tail(ref: Any) -> str | None:
    key = "polish.tail." + str(ref)
    return tr(key) if key in UI_MESSAGES else None


def contact_label(ref: Any, state: Any) -> str | None:
    """Customer-view contact label, for example a primary name with a synthetic tail."""
    parts = contact_parts(ref, state)
    if parts is None or parts[1] is None:
        return None
    return tr("polish.contact.label", name=parts[0], tail=parts[1])


# Registered tokens (PII_<hex>) and the vault's named tokens (NAME_*, PHONE_*). A named
# token whose contact is absent from this state (NAME_SPOUSE in the presets' base state)
# has no display value, so it gets the fixed placeholder too.
_PII_TOKEN = re.compile(
    r"(?<![A-Za-z0-9_])(?:PII_[0-9a-f]{16}|(?:NAME|PHONE)_[A-Z0-9_]+)(?![A-Za-z0-9_])"
)


def _token_labels(state: Any) -> dict[str, str]:
    """Contact name and phone tokens -> synthetic display value (name or masked tail)."""
    owners: dict[str, list[str]] = {}
    phones: dict[str, str] = {}
    for ref, contact in sorted((getattr(state, "contacts", None) or {}).items()):
        if getattr(contact, "name_token", None):
            owners.setdefault(contact.name_token, []).append(ref)
        if getattr(contact, "phone_token", None):
            phones[contact.phone_token] = ref
    labels = {}
    for token, refs in owners.items():
        names = [contact_label(ref, state) or tr("Contact") for ref in refs]
        labels[token] = (
            names[0] if len(names) == 1 else tr("polish.token.shared", names="／".join(names))
        )
    for token, ref in phones.items():
        tail = contact_tail(ref)
        labels[token] = tr("polish.token.phone", tail=tail) if tail else tr("polish.token.masked")
    return labels


def display_text(text: Any, state: Any) -> str:
    """R8: the customer view replaces vault tokens in chat text with display values.

    Contact name tokens become the synthetic contact label, phone tokens a masked tail
    and any other vault token a fixed placeholder. The judge view returns the stored
    tokenized text unchanged; the stored text, prompts and audit are never rewritten.
    """
    value = str(text)
    if _judge():
        return value
    labels = _token_labels(state)
    if labels:
        pattern = re.compile(
            r"(?<![A-Za-z0-9_])(?:"
            + "|".join(re.escape(token) for token in sorted(labels, key=len, reverse=True))
            + r")(?![A-Za-z0-9_])"
        )
        value = pattern.sub(lambda match: labels[match.group()], value)
    return _PII_TOKEN.sub(lambda _match: tr("polish.token.masked"), value)


def position_label(state: Any, position_ref: Any, index: int) -> str:
    position = getattr(state, "positions", {}).get(str(position_ref))
    product = label(position.product_ref, state) if position is not None else None
    if product is None:
        return tr("Product {name}", name=str(index))
    return tr("polish.position.label", product=product, index=index)


def beijing(value: Any) -> str:
    """Format a trusted UTC timestamp in Beijing time without consulting the wall clock."""
    if not value:
        return "—"
    return (
        datetime
        .fromisoformat(str(value).replace("Z", "+00:00"))
        .astimezone(timezone(timedelta(hours=8)))
        .strftime("%Y-%m-%d %H:%M:%S")
    )


def is_raw_time(value: Any) -> bool:
    return isinstance(value, str) and _ISO_Z.fullmatch(value) is not None


def time_text(value: Any) -> str:
    """Customer view: Beijing time; judge view: the canonical UTC value unchanged."""
    if not is_raw_time(value) or _judge():
        return str(value)
    return tr("polish.time.beijing", time=beijing(value))


def _coded(prefix: str, value: Any) -> str:
    code = str(value)
    key = prefix + code
    if key not in UI_MESSAGES:
        return code
    text = tr(key)
    if not _judge() or text == code:
        return text
    return tr("polish.with_code", label=text, code=code)


def event_label(name: Any) -> str:
    """Audit event name in plain words; the judge view keeps the raw event name."""
    return _coded("polish.event.", name)


def outcome_label(value: Any) -> str:
    """Kernel outcome in plain words; the judge view keeps the raw outcome."""
    return _coded("polish.outcome.", value)
