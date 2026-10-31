"""UI-POLISH P3: customer and judge views are a display preference, never authority."""

import re
from string import Formatter

import pytest

from corridor.crypto.jcs import H
from corridor.i18n import reset_locale, set_locale
from corridor.i18n import tr as core_tr
from corridor.ui import dag, explanations
from corridor.ui.audience import get_audience, reset_audience, set_audience
from corridor.ui.messages import UI_MESSAGES, tr
from tests.e2e.test_demo_ui import app, navigate
from tests.unit.test_ui_polish_copy import Recorder

RULE_ID = re.compile(r"\b[A-Z]{2,}(?:-[A-Z0-9]+)+\b")


def _fields(template):
    return {name for _, name, _, _ in Formatter().parse(template) if name}


def test_every_customer_override_has_a_base_key_with_the_same_placeholders():
    overrides = [key for key in UI_MESSAGES if key.startswith("customer:")]
    # 28: the S2 review added one override (the wealth attack reason without rule IDs).
    # 40: S3 screenshot QA added twelve: ten device-page pointers named 「设备审批/MFA」 while the
    # customer nav calls that page 「设备确认」, the wealth attack caption without English jargon,
    # and the new-recipient tier reason in the same SMS wording as the tier badge.
    # 79: S4 added 39: device wording 「设备确认」 for task-node
    # states, the device page title and expander, every remaining customer-visible "MFA"
    # or 设备审批/可信设备 sentence, the wealth page's evidence-mode line in plain Chinese,
    # and two screenshot-review fixes (unlock result and empty device list wording).
    # 80: S6 R5 added the plain customer version of the recipient check's isolation line
    # (polish.scam.not_signed): no 门控 / 规范卡 / 签名载荷 / 审计链 in the customer view.
    # 83: S6 R6 added plain customer titles for the recipient check (polish.scam.title,
    # facts_title, device_title): "页面提示" instead of 展示层 / display-layer.
    # 96: JUDGING-1 list item 17 (messages-voice.json) added thirteen service-tone versions:
    # the birthday suggestion and three birthday reminders ("您还没有办理任何业务"), seven
    # clarifying questions without "本条消息没有执行任何操作", "已办理" for a receipt, and
    # the receipt line that names its number as the last eight characters.
    assert len(overrides) == 96
    for key in overrides:
        base = key.removeprefix("customer:")
        assert base in UI_MESSAGES, key
        for locale in ("zh-CN", "en"):
            assert _fields(UI_MESSAGES[key][locale]) == _fields(UI_MESSAGES[base][locale]), key


@pytest.mark.parametrize("locale", ["zh-CN", "en"])
def test_default_audience_renders_every_key_exactly_as_before(locale):
    assert get_audience() == "judge"
    token = set_locale(locale)
    try:
        for key, translations in UI_MESSAGES.items():
            values = {name: "X" for name in _fields(translations[locale])}
            assert tr(key, **values) == core_tr(key, catalog=UI_MESSAGES, **values), key
    finally:
        reset_locale(token)


def test_customer_audience_uses_plain_overrides_and_hides_rule_evidence(monkeypatch):
    locale = set_locale("zh-CN")
    audience = set_audience("customer")
    try:
        assert tr("Gateway approval card") == "请核对办理信息"
        recorder = Recorder()
        monkeypatch.setattr(explanations, "st", recorder)
        explanations.render_explanation(rule_ids=["APR-SCOPE", "BAL-AVAILABLE", "APR-SCOPE"])
        shown = " ".join(value for _, value in recorder.calls)
        assert not RULE_ID.search(shown)
        assert shown.startswith(UI_MESSAGES["polish.reason.plain_prefix"]["zh-CN"])
        assert not [name for name, _ in recorder.calls if name in {"json", "expander"}]
        snapshot = {
            "plan": {"nodes": [{"node_id": "n1", "tool": "transfer", "label": "Transfer"}]},
            "nodes": {"n1": {"status": "settled"}},
        }
        assert dag.dag_details_html(snapshot) == ""
    finally:
        reset_audience(audience)
        reset_locale(locale)
    assert dag.dag_details_html(snapshot)


def _expander_labels(page):
    return [item.label for item in page.expander]


def test_customer_default_hides_evidence_pages_and_toggle_changes_display_only(monkeypatch):
    monkeypatch.setenv("CORRIDOR_DEFAULT_VIEW", "customer")
    monkeypatch.setenv("CORRIDOR_LOCALE", "zh-CN")
    page = app()
    page.button(key="enter").click().run()
    options = page.selectbox(key="page").options
    assert "nav.dag" not in options
    labels = set(options)
    assert tr("nav.audit") not in labels and tr("nav.dag") not in labels
    assert tr("nav.conversation") in labels
    page.text_input(key="domestic_text").set_value("给妈妈转八百元")
    page.button(key="domestic_send").click().run()
    assert not page.exception
    assert "技术证据" not in _expander_labels(page)
    controller = page.session_state["controller"]
    before = (
        H(controller.state),
        controller.gateway.audit.checkpoint(),
        controller.gateway.bank.execute_count,
    )
    page.button(key="view_toggle").click().run()
    assert not page.exception
    assert page.query_params["view"] == ["judge"] or page.query_params["view"] == "judge"
    assert page.session_state["polish_view"] == "judge"
    assert "技术证据" in _expander_labels(page)
    assert (
        H(controller.state),
        controller.gateway.audit.checkpoint(),
        controller.gateway.bank.execute_count,
    ) == before
    navigate(page, "audit")
    assert not page.exception
