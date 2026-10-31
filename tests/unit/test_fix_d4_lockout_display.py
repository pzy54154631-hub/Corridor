"""Attacker-induced lockout is a visible availability cost, never a success."""

import pytest

from corridor.i18n import reset_locale, set_locale
from corridor.ui import attack_views
from tests.unit.test_dux_cards_dag import DisplayRecorder


@pytest.mark.parametrize(
    "locale,label,cost",
    [
        ("zh-CN", "锁定拦截（合法任务被拒）", "可用性代价"),
        ("en", "Contained by lockout (legitimate task refused)", "Availability cost"),
    ],
)
def test_lockout_uses_warning_and_reports_refused_legitimate_task(monkeypatch, locale, label, cost):
    display = DisplayRecorder()
    warnings, successes = [], []
    display.warning = warnings.append
    display.success = successes.append
    monkeypatch.setattr(attack_views, "st", display)
    token = set_locale(locale)
    try:
        attack_views._panel({
            "passed": False,
            "completed": False,
            "attacker_induced_lockout": True,
            "availability_cost": {"locked": True, "legitimate_task_refused": True},
            "rule_ids": ["APR-LOCK"],
            "bank_dispatches": 0,
        })
    finally:
        reset_locale(token)
    assert warnings == [label]
    assert successes == []
    assert cost in " ".join(display.visible)
