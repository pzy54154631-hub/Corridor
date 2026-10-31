"""Five suitability lines for a purchase card; a read-only projection of its own facts.

The match line only cites the gateway's comparison certificate (built by the gateway
and re-checked by the kernel); the display layer never compares risk levels itself,
and the kernel verdict always prevails.
"""

from decimal import ROUND_HALF_EVEN, Decimal
from fractions import Fraction
from typing import Any

from corridor.i18n import get_locale
from corridor.ui import display_names
from corridor.ui.messages import UI_MESSAGES, tr
from corridor.ui.wealth_statement import EXCLUSIONS


def exclusion_text(reason: Any) -> str:
    code = str(reason)
    if code in EXCLUSIONS:
        return tr(EXCLUSIONS[code])
    key = "polish.exclusion." + code
    return tr(key) if key in UI_MESSAGES else code


def _separator() -> str:
    return "、" if get_locale() == "zh-CN" else ", "


def percent(value: Any) -> str:
    """A rational annual rate as a percentage, never as a raw fraction string."""
    rate = Fraction(value) * 100
    shown = (Decimal(rate.numerator) / Decimal(rate.denominator)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_EVEN
    )
    return f"{shown:f}".rstrip("0").rstrip(".") + "%"


def _horizon(card: dict[str, Any], state: Any) -> Any:
    reference = (card.get("objects") or {}).get("preference_ref")
    item = getattr(state, "user_inputs", {}).get(reference) if reference else None
    value = getattr(item, "value", None)
    if isinstance(value, dict):
        return value.get("horizon_days")
    return getattr(value, "horizon_days", None)


def lines(card: dict[str, Any], state: Any) -> list[str]:
    """Exactly five lines for a purchase card carrying risk and certificate facts, else []."""
    risk = card.get("risk") if isinstance(card, dict) else None
    certificate = card.get("best_interest") if isinstance(card, dict) else None
    if (
        not isinstance(card, dict)
        or card.get("kind") != "purchase_product"
        or not isinstance(risk, dict)
        or not isinstance(certificate, dict)
    ):
        return []
    reference = str((card.get("objects") or {}).get("product_ref") or "")
    product = display_names.catalog_product(reference, state)
    horizon = _horizon(card, state)
    if product is None or horizon is None or risk.get("effective_level") is None:
        return [tr("polish.suit.incomplete")]
    if reference in (certificate.get("candidate_ids") or []):
        match = tr("polish.suit.match_in")
    else:
        reasons = (certificate.get("exclusions") or {}).get(reference) or []
        reasons = reasons if isinstance(reasons, list) else [reasons]
        match = tr(
            "polish.suit.match_out",
            reasons=_separator().join(exclusion_text(reason) for reason in reasons) or "—",
        )
    return [
        tr("polish.suit.product", risk=product.risk_level),
        tr(
            "polish.suit.customer",
            level=risk["effective_level"],
            expires=display_names.time_text(risk.get("expires_at") or "—"),
        ),
        match,
        tr(
            "polish.suit.liquidity",
            hold=product.min_hold_days,
            delay=product.redeem_delay_days,
            horizon=horizon,
        ),
        tr("polish.suit.returns", rate=percent(product.expected_annual_rate)),
    ]


def footer() -> str:
    return tr("polish.suit.footer")


def refusal_facts(demo: dict[str, Any]) -> dict[str, Any]:
    """The isolated R4 refusal's verdict facts, for display only."""
    r4 = demo.get("r4") or {}
    return {
        "outcome": r4.get("outcome"),
        "rule_ids": list(r4.get("rule_ids") or []),
        "execute_count": r4.get("execute_count", 0),
        "challenge_id": r4.get("challenge_id"),
    }
