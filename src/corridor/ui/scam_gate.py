"""S6 gate: an explicit per-action state machine kept by the product-layer controller.

One entry per action instance (a draft, a pending or a plan identity). Nothing here reads
Streamlit, the trusted core, the signing path or the clock: the controller computes the
card identity from the action's authoritative card and the level from a context snapshot (the user's
words, the attachment count and the contact facts at that time), and this module only
keeps and checks the record. Every write carries the action identity, the card identity
and the epoch it was made for; a mismatching (stale) event changes no entry and only
leaves a rejection record. An established gate is never removed: its answers can be
invalidated (a re-grade or a retry) but the action stays gated, and its level never
drops below ``MIN_LEVEL``: a re-grade that finds no trigger any more (the payee switched
to a known contact, the words went missing) still asks both questions again, so a gate
is never lifted by a re-grade alone. When the controller replaces the action instance a
gate protects (a redisplay under a new identity), ``inherit`` carries the gate over:
the new instance starts ``incomplete`` before any gesture can reach it.

``decision`` of one entry:

* ``incomplete`` -- a question is unanswered: right after the gate is established, after
  a retry, after a re-grade (card or context change) and right after an inheritance;
* ``pause`` -- the answers trigger a pause for this level. The pause latches: answers
  for this epoch are refused, only a retry opens a new epoch;
* ``proceed`` -- both answered and no pause rule triggered.

The refusal reported at an entry point also knows ``stale``: the action's current card
is missing, has no computable identity or differs from the card the answers were given
on. A stale gate never counts as "no gate".
"""

from copy import deepcopy
from typing import Any

from corridor.ui import scam_friction

REASON_STALE = "scam_stale"
REASONS = (scam_friction.REASON_INCOMPLETE, scam_friction.REASON, REASON_STALE)
# Why a write was refused; recorded, never acted on.
REJECT_NO_GATE = "no_gate"
REJECT_CARD = "card_mismatch"
REJECT_EPOCH = "epoch_mismatch"
REJECT_LATCHED = "latched"
REJECT_NOT_PAUSED = "not_paused"
REJECTIONS_KEPT = 64
# An established gate always asks its two questions: level 0 means "no gate", and the
# controller never establishes one for it, so a stored level is never below 1.
MIN_LEVEL = 1


def _gated_level(level: Any) -> int:
    return max(MIN_LEVEL, int(level))


def _blank_answers() -> dict[str, str | None]:
    return {question: None for question in scam_friction.QUESTIONS}


def _preset(preset: Any) -> tuple[dict[str, str | None], dict[str, str]]:
    """The starting answers and the preset itself (R7: answers the user's own words gave,
    such as "someone is guiding" when someone told them to pay)."""
    given = {
        question: value
        for question, value in scam_friction.clean_answers(preset).items()
        if value is not None
    }
    answers = _blank_answers()
    answers.update(given)
    return answers, given


def _start(grade: int, answers: dict[str, str | None], preset: dict[str, str]) -> str:
    """A preset that triggers a pause pauses at once (the user's own words said it, and
    the pause latches until the user's own retry); otherwise the ordinary rule."""
    if preset and scam_friction.triggers(grade, answers):
        return "pause"
    return scam_friction.decision(grade, answers)


class ScamGate:
    """Product-layer session state only; see the module docstring for the transitions."""

    def __init__(self) -> None:
        self._entries: dict[str, dict[str, Any]] = {}
        self._rejections: list[dict[str, Any]] = []

    # --- reading -------------------------------------------------------------------------

    def identities(self) -> list[str]:
        return list(self._entries)

    def entry(self, identity: str) -> dict[str, Any] | None:
        """A copy of the record; the stored object is never handed out."""
        entry = self._entries.get(identity)
        return None if entry is None else deepcopy(entry)

    def rejections(self) -> list[dict[str, Any]]:
        return deepcopy(self._rejections)

    def refusal_reason(self, identity: str, current_card_identity: str | None) -> str | None:
        """None when the action is not gated or may continue; otherwise the reason.

        ``current_card_identity`` is the identity of the action's authoritative card right
        now (None when the card is missing or has no computable identity). An established
        gate whose card changed, vanished or cannot be identified refuses as stale.
        """
        entry = self._entries.get(identity)
        if entry is None:
            return None
        if current_card_identity is None or current_card_identity != entry["card_identity"]:
            return REASON_STALE
        if entry["decision"] == "pause":
            return scam_friction.REASON
        if entry["decision"] == "incomplete":
            return scam_friction.REASON_INCOMPLETE
        return None

    # --- transitions ---------------------------------------------------------------------

    def establish(
        self,
        identity: str,
        *,
        card_identity: str,
        level: int,
        context: dict[str, Any],
        cues: tuple[str, ...] = (),
        origin: dict[str, Any] | None = None,
        preset: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Create the gate for an action, or re-grade it; idempotent for the same inputs.

        The same card identity and the same context leave the entry untouched (the level
        passed in is ignored then: an ordinary redraw can never lower a level). A changed
        card identity or context is an explicit re-grade: the level is replaced (never
        below ``MIN_LEVEL``), the answers are invalidated, the epoch advances and the
        decision is ``incomplete``. A level 0 passed in is raised to ``MIN_LEVEL``: an
        established gate is never turned into a blank "proceed" record. A ``preset`` (R7)
        starts the answers from the user's own words and is recorded as such; a preset that
        triggers a pause pauses at once.
        """
        entry = self._entries.get(identity)
        if (
            entry is not None
            and entry["card_identity"] == card_identity
            and entry["context"] == context
        ):
            if origin is not None:
                entry["origin"] = deepcopy(origin)
            return deepcopy(entry)
        if entry is None:
            entry = {
                "required": True,
                "epoch": 0,
                "retries": 0,
                "regrades": 0,
                "origin": deepcopy(origin) if origin is not None else None,
            }
            self._entries[identity] = entry
        else:
            entry["epoch"] += 1
            entry["regrades"] += 1
            if origin is not None:
                entry["origin"] = deepcopy(origin)
        grade = _gated_level(level)
        answers, given = _preset(preset)
        entry.update({
            "card_identity": card_identity,
            "level": grade,
            "context": deepcopy(context),
            "cues": tuple(cues),
            "answers": answers,
            "preset": given,
            "decision": _start(grade, answers, given),
        })
        return deepcopy(entry)

    def inherit(
        self, source: str, identity: str, *, card_identity: str | None
    ) -> dict[str, Any] | None:
        """Carry an established gate over to the action instance that replaces its action.

        Used when the controller replaces the instance a gate protects under a new
        identity (a redisplay previews the same intent again). The new entry keeps the
        source's level, cues, context and origin, is bound to the replacement's card
        identity, starts at epoch 0 with blank answers and the decision ``incomplete``,
        and records ``inherited_from``. The source entry is left as it is (its identity
        is never reused; it reads as stale from then on). Without a source gate nothing
        is created (None): a path that never showed the questions stays as it was. An
        identity that already has an entry keeps it.
        """
        parent = self._entries.get(source)
        if parent is None:
            return None
        existing = self._entries.get(identity)
        if existing is not None:
            return deepcopy(existing)
        grade = _gated_level(parent["level"])
        answers, given = _preset(parent.get("preset"))
        entry = {
            "required": True,
            "epoch": 0,
            "retries": 0,
            "regrades": 0,
            "origin": deepcopy(parent["origin"]),
            "inherited_from": source,
            "card_identity": card_identity,
            "level": grade,
            "context": deepcopy(parent["context"]),
            "cues": tuple(parent["cues"]),
            "answers": answers,
            "preset": given,
            "decision": _start(grade, answers, given),
        }
        self._entries[identity] = entry
        return deepcopy(entry)

    def _check(self, identity: str, card_identity: Any, epoch: Any) -> str | None:
        entry = self._entries.get(identity)
        if entry is None:
            return self._reject(identity, REJECT_NO_GATE, card_identity=card_identity, epoch=epoch)
        if card_identity != entry["card_identity"]:
            return self._reject(identity, REJECT_CARD, card_identity=card_identity, epoch=epoch)
        if epoch != entry["epoch"]:
            return self._reject(identity, REJECT_EPOCH, card_identity=card_identity, epoch=epoch)
        return None

    def answer(self, identity: str, *, card_identity: Any, epoch: Any, answers: Any) -> str | None:
        """Merge answered questions into the entry; None when accepted, else the reason.

        Only an event for the current action, card identity and epoch is applied; an
        unanswered question never erases a recorded answer (the page's widgets are a
        projection that Streamlit drops on a page change). While the decision is
        ``pause`` the entry is latched and every answer event is refused.
        """
        rejected = self._check(identity, card_identity, epoch)
        if rejected is not None:
            return rejected
        entry = self._entries[identity]
        clean = scam_friction.clean_answers(answers)
        changes = {
            question: value
            for question, value in clean.items()
            if value is not None and value != entry["answers"][question]
        }
        if not changes:
            # The page's per-run sync of unchanged (or blank) widgets is not an event.
            return None
        if entry["decision"] == "pause":
            return self._reject(identity, REJECT_LATCHED, card_identity=card_identity, epoch=epoch)
        entry["answers"].update(changes)
        entry["decision"] = scam_friction.decision(entry["level"], entry["answers"])
        return None

    def retry(self, identity: str, *, card_identity: Any, epoch: Any) -> str | None:
        """The user's "answer again" gesture: a new epoch with blank answers; never a removal."""
        rejected = self._check(identity, card_identity, epoch)
        if rejected is not None:
            return rejected
        entry = self._entries[identity]
        if entry["decision"] != "pause":
            return self._reject(
                identity, REJECT_NOT_PAUSED, card_identity=card_identity, epoch=epoch
            )
        entry["epoch"] += 1
        entry["retries"] += 1
        entry["answers"] = _blank_answers()
        # The retry is the user's own statement: a preset from their words is cleared too.
        entry["preset"] = {}
        entry["decision"] = scam_friction.decision(entry["level"], None)
        return None

    def _reject(self, identity: str, reason: str, **detail: Any) -> str:
        self._rejections.append({"identity": identity, "reason": reason, **deepcopy(detail)})
        del self._rejections[:-REJECTIONS_KEPT]
        return reason
