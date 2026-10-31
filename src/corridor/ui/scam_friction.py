"""Display-layer friction before a payment to a new payee; never a verdict input.

Pure functions of the canonical card, the trusted contact registry entry, the user's
own (tokenized) words and the attachment count: the same inputs as the fraud warning
whose signal codes they reuse. ``level`` grades a card 0/1/2 and ``decision`` turns the
user's two answers into ``incomplete``, ``pause`` or ``proceed``. Nothing here reads or
writes a proposal, a signature, the audit chain or the state; the answers are kept by
the product-layer session only (``ui/scam_gate.py``), bound to the action instance, the
card identity and an answering epoch. Fixed rules can ask too much or too little, and
the copy says so.
"""

from typing import Any

from corridor.crypto.jcs import H
from corridor.i18n import tr as core_tr
from corridor.ui import display_names, fraud_hint
from corridor.ui.messages import UI_MESSAGES, tr

KINDS = fraud_hint.KINDS
TEXT_CUES = fraud_hint.TEXT_SIGNALS
# Q1: was this recipient account verified through an independent trusted channel?
# Q2: is someone guiding the user through the transfer right now?
QUESTIONS = ("verified", "guided")
OPTIONS: dict[str, tuple[str, ...]] = {
    "verified": ("yes", "no", "unsure"),
    "guided": ("no", "yes"),
}
# The answers that pause a question: not verified independently (only the other party's
# information) or unsure, and someone guiding right now.
PAUSE_ANSWERS: dict[str, tuple[str, ...]] = {"verified": ("no", "unsure"), "guided": ("yes",)}
# Level 1 (no cue in the user's words, or a cue towards a known contact) pauses only when
# someone is guiding the user right now. Level 2 (new payee and an authority or external
# cue in the user's words) pauses on either question.
LEVEL_ONE_PAUSE = ("guided",)
# Only these cues raise a payment to a new payee to level 2 (rule R5): urgency or split
# in the user's words keep level 1, where an honest "not verified" may continue.
STRONG_CUES = frozenset({"authority", "external"})
DECISIONS = ("incomplete", "pause", "proceed")
# R7: words saying someone told the user to pay
# (``fraud_hint.directed``) preset the second question to "someone is guiding" (a pause).
PRESET_DIRECTED = {"guided": "yes"}
STATUS = "paused"
REASON = "scam_pause"
REASON_INCOMPLETE = "scam_incomplete"


def level(
    card: Any, *, contact: Any = None, text: str | None = None, external_files: int = 0
) -> int:
    """0: no friction; 1: recipient facts and two questions; 2: plus the stricter rule.

    Level 1 needs a new payee, or an authority/external cue in the user's words; level 2
    needs the new payee and an authority or external cue. A new payee whose words carry
    only urgency or split cues stays at level 1 (the cue sentence still names them);
    urgency or split alone never triggers, exactly as for the fraud warning.
    """
    if not isinstance(card, dict) or card.get("kind") not in KINDS:
        return 0
    codes = set(fraud_hint.signals(card, contact=contact, text=text, external_files=external_files))
    new_payee = "new_payee" in codes
    strong = bool(codes & STRONG_CUES)
    if new_payee and strong:
        return 2
    if new_payee or strong:
        return 1
    return 0


def cues(
    card: Any, *, contact: Any = None, text: str | None = None, external_files: int = 0
) -> tuple[str, ...]:
    """The text cue codes present, in the fraud warning's order."""
    codes = fraud_hint.signals(card, contact=contact, text=text, external_files=external_files)
    return tuple(code for code in codes if code in TEXT_CUES)


def directed(text: Any) -> bool:
    """True when the user's words say someone told them to make this payment (R7)."""
    return fraud_hint.directed(text)


def preset(text: Any) -> dict[str, str] | None:
    """The answers the user's own words preset (R7): "someone is guiding" when someone told
    them to pay; None otherwise. A preset pauses at once; only the user's retry clears it."""
    return dict(PRESET_DIRECTED) if directed(text) else None


def opening_cues(card: Any, text: str | None) -> tuple[str, ...]:
    """The authority or external cues of a payment card's words (R7): with any of them the
    reply no longer opens with "ready for review" but with the anti-fraud opening."""
    if not isinstance(card, dict) or card.get("kind") not in KINDS:
        return ()
    codes = fraud_hint.signals(card, text=text)
    return tuple(code for code in codes if code in STRONG_CUES)


def opening(codes: tuple[str, ...]) -> str:
    names = [tr("polish.scam.cue." + code) for code in codes if code in STRONG_CUES]
    return tr("polish.scam.opening", cues=tr("polish.scam.cue_separator").join(names))


def clean_answers(answers: Any) -> dict[str, str | None]:
    """Keep only the two known questions with a listed option; anything else is unanswered."""
    given = answers if isinstance(answers, dict) else {}
    return {
        question: given.get(question) if given.get(question) in OPTIONS[question] else None
        for question in QUESTIONS
    }


def triggers(grade: int, answers: Any) -> tuple[str, ...]:
    """The answer codes that pause at this level, in question order; empty when none."""
    if grade <= 0:
        return ()
    clean = clean_answers(answers)
    questions = QUESTIONS if grade >= 2 else LEVEL_ONE_PAUSE
    found = []
    for question in questions:
        value = clean[question]
        if value is not None and value in PAUSE_ANSWERS[question]:
            found.append(f"{question}_{value}")
    return tuple(found)


def decision(grade: int, answers: Any) -> str:
    """incomplete until both questions are answered; pause on a triggering answer.

    Level 2 pauses when the account is not independently verified (``no``) or the user
    is unsure, or when someone is guiding; level 1 pauses only when someone is guiding
    (``LEVEL_ONE_PAUSE``). Level 0 always proceeds.
    """
    if grade <= 0:
        return "proceed"
    clean = clean_answers(answers)
    if any(clean[question] is None for question in QUESTIONS):
        return "incomplete"
    if triggers(grade, clean):
        return "pause"
    return "proceed"


def card_identity(card: Any) -> str | None:
    """The identity the answers are bound to: every canonical fact but the draft digest.

    ``dependency_hash`` is the digest of the certificate's dependency hashes (it differs
    between a draft preview and the prepared action of the same intent); every other
    field is kept, so a different payee, amount, tier, kind, intent, node or action
    hash gives a different identity. The device workflow compares cards the same way
    (``business_facts``).
    """
    if not isinstance(card, dict) or not card:
        return None
    return H({key: value for key, value in card.items() if key != "dependency_hash"})


def cards_identity(cards: Any) -> str | None:
    """One identity for an action's authoritative card set; None when it is unusable.

    A single card gives its own identity; several (a plan) give the hash of their sorted
    identities. An empty set, a non-dict or an empty card yields None, which the gate
    treats as stale, never as "no gate".
    """
    if not isinstance(cards, (list, tuple)) or not cards:
        return None
    identities = [card_identity(card) for card in cards]
    if any(identity is None for identity in identities):
        return None
    if len(identities) == 1:
        return identities[0]
    return H(sorted(identities))


def is_new_payee(card: Any, contact: Any = None) -> bool:
    """True when the card or the registry entry marks the payee as new (a registration
    label, not a statement about payment history)."""
    if not isinstance(card, dict):
        return False
    return "new_recipient" in (card.get("tier_reasons") or []) or bool(
        getattr(contact, "is_new", False)
    )


def facts(card: Any, state: Any) -> list[str]:
    """Registered recipient facts from trusted state, then their source and the advice.

    A known (not new) contact gets its registration line instead of an empty block, so
    the questions asked on an authority or external cue never appear without facts.
    """
    if not isinstance(card, dict):
        return []
    reference = (card.get("objects") or {}).get("contact_ref")
    contacts = getattr(state, "contacts", None)
    contact = contacts.get(reference) if isinstance(contacts, dict) and reference else None
    parts = display_names.contact_parts(reference, state) if reference else None
    lines = []
    if parts is not None and parts[1] is not None:
        lines.append(tr("polish.scam.fact_payee", payee=parts[0], tail=parts[1]))
    elif parts is not None:
        lines.append(tr("polish.scam.fact_payee_plain", payee=parts[0]))
    if is_new_payee(card, contact):
        lines.append(tr("polish.scam.fact_new"))
        lines.append(
            tr("polish.scam.fact_registered")
            if contact is not None
            else tr("polish.scam.fact_unregistered")
        )
    elif contact is not None:
        lines.append(tr("polish.scam.fact_known"))
    else:
        lines.append(tr("polish.scam.fact_unregistered"))
    lines.append(tr("polish.scam.fact_source"))
    lines.append(tr("polish.scam.advice"))
    return lines


def cue_summary(codes: tuple[str, ...]) -> str | None:
    names = [tr("polish.scam.cue." + code) for code in codes if code in TEXT_CUES]
    if not names:
        return None
    return tr("polish.scam.cues", cues=tr("polish.scam.cue_separator").join(names))


def pause_lead(grade: int, answers: Any, preset: Any = None) -> str:
    """The first sentence of the pause block, naming only the answers that triggered it.

    A second question preset from the user's own words (R7) is named as such, never as
    an answer the user gave.
    """
    if (
        isinstance(preset, dict)
        and preset.get("guided") == "yes"
        and clean_answers(answers)["guided"] == "yes"
    ):
        return tr("polish.scam.pause_lead_preset")
    names = [tr("polish.scam.trigger." + code) for code in triggers(grade, answers)]
    if not names:
        return tr("polish.scam.pause_lead_plain")
    return tr("polish.scam.pause_lead", triggers=tr("polish.scam.trigger_separator").join(names))


def guided_only(grade: int, answers: Any) -> bool:
    """True when "someone guiding right now" is the only answer that pauses at this level."""
    return triggers(grade, answers) == ("guided_yes",)


def pause_title(grade: int, answers: Any) -> str:
    """The pause block's title: end the guidance when guiding alone paused it (there is
    nothing for that user to verify); otherwise verify the recipient first."""
    if guided_only(grade, answers):
        return tr("polish.scam.pause_title_guided")
    return tr("polish.scam.pause_title")


def pause_retry_label(grade: int, answers: Any, codes: tuple[str, ...] = ()) -> str:
    """The answer-again button: "decided on my own" after a pause by guiding alone,
    "verified through an official channel" after a verification trigger; either one is
    only the user's own statement. When guiding alone paused it but the user's words
    carried an authority cue, the action sentence also asks for the official-number
    check, so the button covers both statements (R6)."""
    if guided_only(grade, answers):
        if "authority" in codes:
            return tr("polish.scam.pause_retry_guided_authority")
        return tr("polish.scam.pause_retry_guided")
    return tr("polish.scam.pause_retry")


def pause_copy(
    codes: tuple[str, ...],
    answers: Any = None,
    grade: int = 2,
    *,
    challenge: bool = False,
    preset: Any = None,
) -> str:
    """The pause block: the triggering answers, an action sentence, future tense only.

    A pause by "someone guiding" alone asks the user to end the guidance and decide
    alone (rule R5); any verification trigger asks for verification through contact
    details the user saved. The "call the official number" line appears only when the
    user's words carried an authority cue; the fraud warning on the same screen already
    states that banks, police and anti-fraud centres never ask for a transfer, so it is
    not repeated. With an issued challenge the block says that its validity window keeps
    running.
    """
    authority = "authority" in codes
    if guided_only(grade, answers):
        key = "polish.scam.pause_guided_authority" if authority else "polish.scam.pause_guided"
    else:
        key = "polish.scam.pause_authority" if authority else "polish.scam.pause"
    body = tr(key, lead=pause_lead(grade, answers, preset))
    if challenge:
        body += tr("polish.scam.pause_challenge")
    return body


def question_label(question: str) -> str:
    return tr("polish.scam.q_" + question)


def option_label(question: str, option: Any, *, locale: str | None = None) -> str:
    """The label of an option code; ``locale`` pins the language a widget was drawn in,
    so its labels stay the same whenever the page serializes it later."""
    key = (
        f"polish.scam.q_{question}.{option}"
        if option in OPTIONS.get(question, ())
        else "polish.scam.unanswered"
    )
    if locale is None:
        return tr(key)
    return core_tr(key, locale=locale, catalog=UI_MESSAGES)


def record_line(answers: Any, verdict: str) -> str:
    """One judge-view line of the interaction; display only, never audited."""
    clean = clean_answers(answers)
    return tr(
        "polish.scam.judge_record",
        verified=option_label("verified", clean["verified"]),
        guided=option_label("guided", clean["guided"]),
        decision=tr("polish.scam.decision." + verdict),
    )


def title() -> str:
    return tr("polish.scam.title")


def footer() -> str:
    return tr("polish.scam.footer")


def judge_note() -> str:
    return tr("polish.scam.judge_note")
