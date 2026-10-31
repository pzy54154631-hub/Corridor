"""S3 screenshot QA: copy and rendering defects found by viewing the re-captured screens."""

import ast
from pathlib import Path

from corridor.i18n import reset_locale, set_locale
from corridor.ui import explanations, view_mode
from corridor.ui.audience import reset_audience, set_audience
from corridor.ui.messages import UI_MESSAGES, tr
from tests.unit.test_ui_polish_copy import Recorder

ROOT = Path(__file__).resolve().parents[2]
RED_RULE = (
    "Red-tier action: Complete independent simulated MFA, then approve on the device. "
    "Dispatch requires verification."
)


def test_app_script_has_no_bare_expression_that_streamlit_magic_would_render():
    # app.py is the Streamlit main script: a bare non-call expression is written to the page
    # (the subscription page showed a stray "None" from `f(x) if x else None`).
    tree = ast.parse((ROOT / "src/corridor/ui/app.py").read_text(encoding="utf-8"))
    bare = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr)
        and not isinstance(node.value, ast.Call | ast.Constant | ast.Await | ast.Yield)
    ]
    assert bare == []


def test_customer_red_tier_note_stays_true_after_verification_and_execution():
    # The note is rendered on every red card, including verified and executed ones,
    # so it must state the rule rather than instruct the user to type a code first.
    text = UI_MESSAGES["customer:" + RED_RULE]["zh-CN"]
    assert "请先" not in text
    assert "扣款" not in text
    assert "才会办理" in text


def test_customer_device_pointers_name_the_page_the_customer_navigation_shows():
    customer_nav = UI_MESSAGES["customer:nav.device"]["zh-CN"]
    for key, value in UI_MESSAGES.items():
        if key.startswith(("customer:", "nav.")):
            continue
        if "设备审批/MFA" in value["zh-CN"] or "可信设备审批页" in value["zh-CN"]:
            override = UI_MESSAGES.get("customer:" + key)
            assert override is not None, key
            text = override["zh-CN"]
            assert "MFA" not in text and "可信设备审批页" not in text, key
            assert "「" not in text or f"「{customer_nav}」" in text, key


def test_judge_explanation_names_the_tier_in_words_and_keeps_the_raw_value(monkeypatch):
    locale = set_locale("zh-CN")
    audience = set_audience("judge")
    try:
        recorder = Recorder()
        monkeypatch.setattr(explanations, "st", recorder)
        explanations.render_explanation(card={"tier": "yellow"})
        captions = [value for name, value in recorder.calls if name == "caption"]
        assert any(
            tr("Yellow · device approval required") in value and "(yellow)" in value
            for value in captions
        )
    finally:
        reset_audience(audience)
        reset_locale(locale)


def test_customer_chat_column_metrics_wrap_instead_of_truncating():
    # The 420 px customer chat column cut "¥2,812.00" to "¥2,812.…" in three-column metrics.
    audience = set_audience("customer")
    try:
        css = view_mode.page_css()
    finally:
        reset_audience(audience)
    assert "max-width:420px" in css
    assert '[data-testid="stMetricValue"]' in css
    assert "text-overflow:clip" in css


def test_chain_demo_kernel_outcome_is_localized_in_the_judge_view():
    source = (ROOT / "src/corridor/ui/chain_demo.py").read_text(encoding="utf-8")
    assert 'outcome=display_names.outcome_label(result["kernel_outcome"])' in source
    locale = set_locale("zh-CN")
    audience = set_audience("judge")
    try:
        from corridor.ui import display_names

        assert display_names.outcome_label("Reject") == "拒绝（Reject）"
    finally:
        reset_audience(audience)
        reset_locale(locale)


def test_customer_subscription_result_fits_the_chat_column_and_shows_no_stray_values(monkeypatch):
    from tests.e2e.test_demo_ui import navigate
    from tests.e2e.test_polish_views import element_texts, start

    monkeypatch.setenv("CORRIDOR_DEFAULT_VIEW", "customer")
    monkeypatch.setenv("CORRIDOR_LOCALE", "zh-CN")
    audience = set_audience("customer")
    try:
        page = start()
        page.button(key="enter").click().run()
        navigate(page, "subscriptions")
        page.button(key="preview_subscriptions").click().run()
        page.button(key="approve_subscriptions").click().run()
        assert not page.exception
        texts = element_texts(page)
        # Streamlit magic used to write a stray "None" below the subscription result.
        assert "None" not in [str(item.value) for item in page.code]
        assert "None" not in texts
        assert not [item for item in page.json]
        # Customer rows are wrapped lines (no clipped grid), in Beijing time, named merchants.
        assert not page.dataframe
        joined = "\n".join(texts)
        assert "会员服务A（合成）" in joined
        assert "T02:00:00Z" not in joined
        assert "S1" not in [option for box in page.selectbox for option in box.options]
    finally:
        reset_audience(audience)
