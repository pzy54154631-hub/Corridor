"""The recipient check (S6) as data: ``render_scam_gate`` without widgets or writes.

Level and decision come from the controller's gate record; nothing is judged here. The
questions are interactive only on the current reply, a paused gate stays latched, and
an R7 preset (the user's own words said someone told them to pay) is reported as such.
"""

from typing import Any

from corridor.web.catalog import wtr
from corridor.web.compat import scam_friction, tr
from corridor.web.vm.cards import judge
from corridor.web.vm.text import plain


def decision(status: dict[str, Any] | None) -> str | None:
    """``scam_gate_decision``: incomplete, pause, proceed or stale; None without a gate."""
    if status is None:
        return None
    return "stale" if status["stale"] else status["decision"]


def gate_vm(
    status: dict[str, Any] | None,
    *,
    card: dict[str, Any] | None,
    controller: Any,
    version: str | None,
    interactive: bool,
) -> dict[str, Any] | None:
    """GateVM for one action's gate record; None when the action was never gated."""
    if status is None:
        return None
    codes = tuple(status.get("cues") or ())
    answers = status.get("answers") or {}
    level = status.get("level", 1)
    verdict = decision(status)
    preset = bool(status.get("preset"))
    cues = scam_friction.cue_summary(codes)
    pause = None
    if verdict == "pause":
        pause = {
            "title": plain(scam_friction.pause_title(level, answers)),
            "copy": plain(
                scam_friction.pause_copy(
                    codes,
                    answers,
                    level,
                    challenge=bool(status.get("challenge_issued")),
                    preset=status.get("preset"),
                )
            ),
            "retry_label": plain(scam_friction.pause_retry_label(level, answers, codes)),
            "self_report": plain(tr("polish.scam.pause_self_report")),
            "cancel_hint": plain(tr("polish.scam.pause_cancel_hint")),
        }
    notice = {
        "stale": "polish.scam.stale",
        "incomplete": "polish.scam.incomplete",
    }.get(str(verdict))
    vm: dict[str, Any] = {
        # Without a version the questions cannot be answered from this page.
        "version": version or "0" * 16,
        "title": plain(scam_friction.title()),
        "subtitle": plain(tr("polish.scam.not_signed")),
        "facts_title": plain(tr("polish.scam.facts_title")),
        "facts": [
            plain(line) for line in scam_friction.facts(card, getattr(controller, "state", None))
        ],
        "cues": plain(cues) if cues else None,
        "questions_title": plain(tr("polish.scam.questions_title")),
        "questions": [
            {
                "name": question,
                "label": plain(scam_friction.question_label(question)),
                "options": [
                    {"value": option, "label": plain(scam_friction.option_label(question, option))}
                    for option in scam_friction.OPTIONS[question]
                ],
                "answer": answers.get(question),
            }
            for question in scam_friction.QUESTIONS
        ],
        "decision": verdict,
        "locked": status.get("decision") == "pause",
        "preset": preset,
        "interactive": bool(interactive and version),
        "pause": pause,
        "notice": plain(tr(notice)) if notice else None,
        "self_report_note": wtr("web.vm.gate.self_report"),
        "footer": plain(scam_friction.footer()),
    }
    if judge():
        vm["judge"] = {
            "note": plain(scam_friction.judge_note()),
            "record": plain(scam_friction.record_line(answers, verdict)),
            "preset_note": plain(tr("polish.scam.preset_judge")) if preset else None,
        }
    return vm
