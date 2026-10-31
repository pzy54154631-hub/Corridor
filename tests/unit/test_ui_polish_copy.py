"""UI-POLISH P1: copy that contradicted the screen or the gateway is corrected."""

from contextlib import contextmanager

import pytest

from corridor.i18n import reset_locale, set_locale
from corridor.ui import safety
from corridor.ui.messages import UI_MESSAGES
from tests.e2e.test_demo_ui import app, navigate


@pytest.mark.parametrize("locale", ["zh-CN", "en"])
def test_kernel_rejection_reason_names_both_lock_paths(locale):
    token = set_locale(locale)
    try:
        view = safety.safety_projection({"state": "Locked", "reason": "kernel_rejections"})
        session = safety.safety_projection({"state": "Watch", "consecutive_money_rejections": 2})
    finally:
        reset_locale(token)
    (reason,) = view["reasons"]
    (signal,) = session["reasons"]
    if locale == "zh-CN":
        assert "累计" in reason and "不要求连续" in reason and "本会话" in reason
        assert "连续内核拒绝" not in reason
        assert "累计" not in signal
        assert "自上一笔通过内核检查的动钱提案起计" in signal
    else:
        assert "cumulative" in reason and "need not be consecutive" in reason
        assert "this session" in reason
        assert "cumulative" not in signal
    assert signal.endswith(" · 2")


class Recorder:
    def __init__(self):
        self.session_state = {}
        self.calls = []

    @contextmanager
    def expander(self, label, **kwargs):
        self.calls.append(("expander", str(label)))
        yield

    def __getattr__(self, name):
        def record(value=None, *args, **kwargs):
            self.calls.append((name, str(value)))

        return record


def test_lamps_have_no_english_subtitle_and_unlock_time_is_beijing(monkeypatch):
    recorder = Recorder()
    monkeypatch.setattr(safety, "st", recorder)
    token = set_locale("zh-CN")
    try:
        safety._render_snapshot({
            "state": "Locked",
            "reason": "kernel_rejections",
            "unlock_after": "2026-10-16T02:30:00Z",
        })
    finally:
        reset_locale(token)
    shown = " ".join(value for _, value in recorder.calls)
    for english in (">Normal<", ">Watch<", ">Locked<"):
        assert english not in shown
    assert "2026-10-16 10:30:00" in shown
    assert "2026-10-16 02:30:00" not in shown


def _texts(page):
    return [
        str(item.value)
        for elements in (
            page.markdown,
            page.caption,
            page.info,
            page.warning,
            page.success,
            page.error,
            page.text,
        )
        for item in elements
    ]


def test_unselected_plan_shows_plan_size_not_zero_and_unsigned_notice(monkeypatch):
    monkeypatch.setenv("CORRIDOR_LOCALE", "zh-CN")
    page = app()
    page.button(key="enter").click().run()
    page.button(key="birthday_suggestion").click().run()
    assert not page.exception
    nodes = page.session_state["birthday_draft"]["nodes"]
    metric = next(item for item in page.metric if item.label == "本链需逐项审批")
    assert metric.value == f"共 {len(nodes)} 项 · 已勾选 0"
    texts = _texts(page)
    assert UI_MESSAGES["u1.chain.44"]["zh-CN"] not in texts
    assert UI_MESSAGES["polish.chain.unsigned_notice"]["zh-CN"] in texts
    assert not [item for item in page.metric if item.label == "本链预计需审批"]


def test_operator_area_after_completed_compensation_says_nothing_is_locked(monkeypatch):
    monkeypatch.setenv("CORRIDOR_LOCALE", "zh-CN")
    page = app()
    page.button(key="enter").click().run()
    page.selectbox(key="demo_preset").select("birthday_unknown").run()
    page.button(key="load_demo_preset").click().run()
    page.button(key="demo_birthday_orders").click().run()
    page.button(key="demo_recovery_task_stop").click().run()
    page.button(key="demo_reconcile").click().run()
    page.button(key="demo_recovery_cooldown").click().run()
    page.button(key="demo_recovery_unlock").click().run()
    page.button(key="demo_recovery_task_back").click().run()
    page.button(key="demo_recovery_compensate_confirm").click().run()
    page.button(key="demo_recovery_task_handoff").click().run()
    assert not page.exception
    texts = _texts(page)
    assert not any("补偿是新的操作" in text for text in texts)
    assert any("¥0.00" in text and "坐席核查不会生成补偿" in text for text in texts)
    check = UI_MESSAGES["polish.operator.check"]["zh-CN"]
    assert page.button(key="demo_operator_prepare").label == check
    page.button(key="demo_operator_prepare").click().run()
    assert page.session_state["demo_scenario"]["operator_result"]["status"] == "no_compensation"


def test_recorded_tab_and_wealth_pages_have_no_pending_or_not_run_copy(monkeypatch):
    monkeypatch.setenv("CORRIDOR_LOCALE", "zh-CN")
    page = app()
    page.button(key="enter").click().run()
    shown = []
    for route in ("audit", "wealth"):
        navigate(page, route)
        assert not page.exception
        shown.extend(_texts(page))
    joined = "\n".join(shown)
    assert "正式录制待指定" not in joined
    assert "not_run" not in joined
    assert "真实模型尚未录制" not in joined
    assert UI_MESSAGES["polish.recorded.pending"]["zh-CN"] in shown
    assert UI_MESSAGES["polish.wealth.evidence_mode"]["zh-CN"] in shown
