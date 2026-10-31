"""UI-POLISH P7/P9/P10: customer view end to end; the judge view keeps every piece of evidence."""

import re

import pytest

from corridor.ui.app import PAGES
from corridor.ui.audience import reset_audience, set_audience
from corridor.ui.messages import UI_MESSAGES, tr
from tests.e2e.test_demo_ui import navigate
from tests.e2e.test_phone_flow import unstarted_app

HEX64 = re.compile(r"\b[0-9a-f]{64}\b")
UUID = re.compile(r"[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}", re.IGNORECASE)
RULE_ID = re.compile(r"\b[A-Z]{2,}(?:-[A-Z0-9]+)+\b")
FORBIDDEN = (
    "Token",
    "SPA",
    "MFA grant",
    "kernel_verdict",
    "overseas",
    "Normal",
    "Watch",
    "Locked",
    "网关规范审批卡",
    "Press Enter",
    "联系人 CHEN",
)


@pytest.fixture
def customer(monkeypatch):
    """Customer default view; AppTest replays selectbox format functions on the test
    thread, so the test thread mirrors the script thread's display audience."""
    monkeypatch.setenv("CORRIDOR_DEFAULT_VIEW", "customer")
    monkeypatch.setenv("CORRIDOR_LOCALE", "zh-CN")
    token = set_audience("customer")
    yield
    reset_audience(token)


def element_texts(page):
    texts = []
    for elements in (
        page.markdown,
        page.caption,
        page.text,
        page.info,
        page.warning,
        page.success,
        page.error,
        page.code,
        page.metric,
        page.subheader,
        page.header,
        page.title,
    ):
        for item in elements:
            texts.append(str(getattr(item, "value", "")))
            texts.append(str(getattr(item, "label", "")))
    for widgets in (page.button, page.selectbox, page.text_input, page.checkbox, page.expander):
        for item in widgets:
            texts.append(str(getattr(item, "label", "")))
    for box in page.selectbox:
        texts.extend(str(option) for option in box.options)
    for frame in page.dataframe:
        texts.append(frame.value.to_string())
    return [text for text in texts if text]


def start(**params):
    page = unstarted_app()
    for key, value in params.items():
        page.query_params[key] = value
    return page.run()


def test_customer_home_keeps_only_the_fixed_notice_and_hides_evidence_pages(customer):
    page = start()
    assert not page.exception
    markdown = "\n".join(str(item.value) for item in page.markdown)
    assert "simulation-persistent" in markdown and tr("simulation_notice") in markdown
    assert 'class="bank-subtitle"' not in markdown
    page.button(key="enter").click().run()
    footer = UI_MESSAGES[
        "Financial effects come from verified receipts · synthetic data · "
        "simulated authentication · no real money · append-only audit"
    ]["zh-CN"]
    assert footer not in element_texts(page)
    options = page.selectbox(key="page").options
    assert tr("nav.audit") not in options and tr("nav.dag") not in options
    assert len(options) == len(PAGES) - 2


def test_judge_query_parameter_opens_the_full_judge_view(monkeypatch):
    monkeypatch.setenv("CORRIDOR_DEFAULT_VIEW", "customer")
    monkeypatch.setenv("CORRIDOR_LOCALE", "zh-CN")
    page = start(view="judge")
    page.button(key="enter").click().run()
    assert page.session_state["polish_view"] == "judge"
    assert page.button(key="view_toggle")
    assert len(page.selectbox(key="page").options) == len(PAGES)
    assert any("评委视图" in text for text in element_texts(page))
    markdown = "\n".join(str(item.value) for item in page.markdown)
    assert 'class="bank-subtitle"' in markdown
    navigate(page, "audit")
    assert not page.exception
    joined = "\n".join(element_texts(page))
    assert "真实模型尚未录制" not in joined
    assert "计划 16 项" not in joined
    assert "domestic-freeze-10" in joined


def test_phone_route_renders_only_the_phone(customer):
    page = start(phone="fresh-unpaired-token")
    assert not page.exception
    assert "controller" not in page.session_state
    assert not [button for button in page.button if button.key == "enter"]
    shown = "\n".join(str(item.value) for item in page.markdown)
    assert "模拟手机" in shown and "未配对" in shown


def _scan(page, where):
    assert not page.exception, where
    assert not page.json, where
    for text in element_texts(page):
        assert not HEX64.search(text), (where, text)
        assert not UUID.search(text), (where, text)
        assert not RULE_ID.search(text), (where, text)
        for word in FORBIDDEN:
            assert word not in text, (where, word, text)


def test_customer_view_never_shows_hashes_tokens_or_rule_ids(customer):
    page = start()
    page.button(key="enter").click().run()
    page.text_input(key="domestic_text").set_value("给妈妈转八百元")
    page.button(key="domestic_send").click().run()
    _scan(page, "card")
    page.button(key="domestic_confirm").click().run()
    controller = page.session_state["controller"]
    assert controller.gateway.bank.execute_count == 1
    _scan(page, "receipt")
    for route in [name for name in PAGES if name not in {"dag", "audit"}]:
        navigate(page, route)
        _scan(page, route)


@pytest.mark.parametrize("example", ["billing", "subscriptions"])
def test_customer_view_chat_reports_carry_no_receipt_json_or_source_ids(customer, example):
    """The billing and subscription shortcuts end in receipts; their evidence is judge-only."""
    page = start()
    page.button(key="enter").click().run()
    page.button(key=f"domestic_example_{example}").click().run()
    _scan(page, example)
    if [button for button in page.button if button.key == "domestic_confirm"]:
        page.button(key="domestic_confirm").click().run()
        _scan(page, example + ":confirmed")


def test_customer_view_pages_after_actions_carry_no_evidence(customer):
    page = start()
    page.button(key="enter").click().run()
    navigate(page, "billing")
    page.button(key="bill_report").click().run()
    _scan(page, "billing:report")
    navigate(page, "subscriptions")
    page.button(key="preview_subscriptions").click().run()
    page.button(key="approve_subscriptions").click().run()
    _scan(page, "subscriptions:approved")
    navigate(page, "birthday")
    for mode in ("interrupt", "failure", "handoff"):
        page.button(key=f"recovery_{mode}").click().run()
        _scan(page, "birthday:recovery_" + mode)


def test_customer_view_notification_demo_hides_rule_ids_digests_and_raw_refs(customer):
    page = start()
    page.button(key="enter").click().run()
    page.selectbox(key="demo_preset").select("notification_injection").run()
    page.button(key="load_demo_preset").click().run()
    page.button(key="notification_hijacked_run").click().run()
    _scan(page, "notification_injection")
    assert not any("CONTACT_" in text for text in element_texts(page))


def test_red_card_has_one_grey_line_instead_of_placeholder_methods(monkeypatch):
    monkeypatch.setenv("CORRIDOR_LOCALE", "zh-CN")
    page = start()
    page.button(key="enter").click().run()
    page.text_input(key="domestic_text").set_value("给妈妈转1500元")
    page.button(key="domestic_send").click().run()
    assert not page.exception
    assert not page.radio
    assert any("其他强验证方式未接入" in str(item.value) for item in page.caption)
    card_label = UI_MESSAGES["Red · independent simulated MFA required"]["zh-CN"]
    panel = "\n".join(str(item.value) for item in page.markdown)
    assert panel.count(card_label) >= 2
    assert UI_MESSAGES["Red · strong verification"]["zh-CN"] not in panel


def test_timeline_events_are_named_and_every_audit_event_has_a_label(monkeypatch):
    monkeypatch.setenv("CORRIDOR_LOCALE", "zh-CN")
    page = start()
    page.button(key="enter").click().run()
    page.text_input(key="domestic_text").set_value("给妈妈转八百元")
    page.button(key="domestic_send").click().run()
    page.button(key="domestic_confirm").click().run()
    controller = page.session_state["controller"]
    event = next(item for item in controller.birthday_candidates() if item["lock_window_open"])
    draft = controller.preview_birthday(event_ref=event["event_ref"])
    controller.confirm_plan(draft["plan_id"], [node["id"] for node in draft["nodes"]])
    navigate(page, "audit")
    assert not page.exception
    timeline = page.dataframe[0].value.to_string()
    assert "内核裁决（kernel_verdict）" in timeline
    events = {row["event"] for row in controller.gateway.audit.records()}
    assert {"kernel_verdict", "plan_registered", "bank_receipt_verified"} <= events
    missing = sorted(event for event in events if "polish.event." + event not in UI_MESSAGES)
    assert not missing


def test_every_audit_event_name_written_by_the_code_has_a_label():
    """Static guard: gateway, agent and allocation code paths that name an audit event."""
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "src/corridor"
    names = set()
    for path in root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            writer = node.func.attr == "_audit" or (
                node.func.attr == "append" and "audit" in ast.unparse(node.func.value)
            )
            first = node.args[0] if node.args else None
            if writer and isinstance(first, ast.Constant) and isinstance(first.value, str):
                names.add(first.value)
    assert {"plan_registered", "unknown", "user_utterance"} <= names
    assert sorted(name for name in names if "polish.event." + name not in UI_MESSAGES) == []
