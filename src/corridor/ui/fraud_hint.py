"""Display-only fraud warning for a canonical card; it never changes any outcome.

A pure function of the canonical card, the trusted contact registry entry, the
user's own (tokenized) words and whether this turn carried an external file. It
does not block, delay or alter any action, and it is not an input to the kernel
verdict, tier, approval or bank execution. Fixed keywords can miss or misfire; the
warning says so.
"""

import re
from typing import Any

from corridor.ui import display_names
from corridor.ui.messages import UI_MESSAGES, tr

KINDS = frozenset({"transfer", "schedule_transfer"})
# Read-only kinds (queries and analyses). Every other card kind changes something; an
# unknown kind counts as a write, so the file reminder errs towards showing. risk_assess
# is not here: it replaces the customer's risk profile (the trusted core's own read-only
# set excludes it too), so a file-driven risk retake gets the reminder.
READ_KINDS = frozenset({
    "balance_query",
    "transactions_query",
    "bill_analyze",
    "bill_report",
    "anomaly_query",
    "sandbox_analyze",
    "product_compare",
    "allocation_compare",
    "investment_income_query",
    "subscription_query",
    "subscription_reminders",
})
TEXT_SIGNALS = ("authority", "external", "urgency", "split")
# S6 R7: someone told the user to pay. Words from the catalog's closed lists: a subject,
# then one of the ``directed`` verbs (told me to), then one of the ``directed_action`` verbs
# (transfer, pay) in the same clause. A verb right after a ``directed_not_after`` word (the
# user's own "please let me") or at the very start is not someone else telling them. It
# feeds the recipient check's preset only, never this warning's codes.
_CLAUSE = ",.;:!?，。；：！？、"
GATING = frozenset({"external_file", "new_payee", "authority", "external"})
ORDER = ("external_file", "new_payee", *TEXT_SIGNALS)


def cues(group: str) -> tuple[str, ...]:
    """Cue words read across every locale of the display catalog."""
    return tuple(
        sorted({
            word.strip().lower()
            for words in UI_MESSAGES[f"polish.fraud.cues.{group}"].values()
            for word in words.split("|")
            if word.strip()
        })
    )


def _matches(group: str, text: str) -> bool:
    lowered = text.lower()
    return any(re.search(re.escape(word), lowered) for word in cues(group))


def _alternation(group: str) -> str:
    return "|".join(re.escape(word) for word in sorted(cues(group), key=len, reverse=True))


def directed(text: Any) -> bool:
    """True when the user's (tokenized) words say someone told them to make this payment.

    C2: relaying words (pass on, forward, report: the catalog's ``directed_relay`` list)
    are not a payment verb: being told to pass a message on is not being told to pay.
    """
    if not isinstance(text, str) or not text:
        return False
    lowered = re.sub(
        _alternation("directed_relay"), lambda match: "□" * len(match.group()), text.lower()
    )
    guards = "".join(f"(?<!{re.escape(word)})" for word in cues("directed_not_after"))
    pattern = (
        f"(?<=[^{_CLAUSE}]){guards}(?:{_alternation('directed')})"
        f"[^{_CLAUSE}]{{0,24}}?(?:{_alternation('directed_action')})"
    )
    return bool(re.search(pattern, lowered))


def write_kind(card: Any) -> bool:
    """True for a card that would change something (R-e); never for a read-only query."""
    kind = card.get("kind") if isinstance(card, dict) else None
    return isinstance(kind, str) and bool(kind) and kind not in READ_KINDS


def signals(
    card: Any, *, contact: Any = None, text: str | None = None, external_files: int = 0
) -> tuple[str, ...]:
    """Warning codes for a card; empty unless a gating signal is present.

    R-e: this turn carried an external file and the card is a write kind. The file's
    content is never read here; only its presence counts.
    """
    if not isinstance(card, dict):
        return ()
    found = set()
    if external_files > 0 and write_kind(card):
        found.add("external_file")
    if card.get("kind") in KINDS:
        if "new_recipient" in (card.get("tier_reasons") or []) or bool(
            getattr(contact, "is_new", False)
        ):
            found.add("new_payee")
        if isinstance(text, str) and text:
            found.update(group for group in TEXT_SIGNALS if _matches(group, text))
    if not found & GATING:
        return ()
    return tuple(code for code in ORDER if code in found)


def lines(
    codes: tuple[str, ...], payee: str | None = None, *, action: str | None = None
) -> list[str]:
    """Warning sentences in signal order, then the fixed limitation footer."""
    if not codes:
        return []
    shown = [
        tr("polish.fraud.new_payee", payee=payee or tr("Contact"))
        if code == "new_payee"
        else tr("polish.fraud.external_file", action=action or tr("Business approval"))
        if code == "external_file"
        else tr("polish.fraud." + code)
        for code in codes
    ]
    # Only the file reminder: a footer that does not speak of recipients or keywords.
    footer = (
        "polish.fraud.footer_file" if set(codes) == {"external_file"} else "polish.fraud.footer"
    )
    return [*shown, tr(footer)]


def title() -> str:
    return tr("polish.fraud.title")


def judge_note(codes: tuple[str, ...] = ()) -> str:
    note = tr("polish.fraud.judge_note")
    if "external_file" in codes:
        return note + " " + tr("polish.fraud.judge_note_file")
    return note


def payee_name(card: Any, state: Any) -> str | None:
    """The recipient's primary display name from the trusted registry, if any."""
    reference = (card.get("objects") or {}).get("contact_ref") if isinstance(card, dict) else None
    parts = display_names.contact_parts(reference, state) if reference else None
    return parts[0] if parts else None
