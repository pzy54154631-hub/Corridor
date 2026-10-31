"""Product line: the customer view speaks like a bank's service, and a receipt reads like a receipt.

1. The birthday suggestion and its reminders say "这只是提醒，您还没有办理任何业务" in the
   customer view (the judge view keeps "只读 / 尚未授权任何操作").
2. A clarification that nobody could mistake for a completed action no longer ends with
   "本条消息没有执行任何操作" in the customer view; one that follows a cancel word or a
   discarded draft keeps it.
3. A settled bank receipt is titled "已办理" in the customer view and shows the receipt's own
   time, number and status in both views; nothing that is not in the receipt.
4. The task panel names a split-bill reply "AA 收款请求", not "转账".
5. The wealth page's marketing-metadata twin check is shown in the judge view only.
"""

import ast
from pathlib import Path

from corridor.ui import display_names
from corridor.ui.messages import UI_MESSAGES
from tests.e2e.test_demo_ui import app, navigate

ROOT = Path(__file__).resolve().parents[2]
ZH = "zh-CN"
NOTHING = "本条消息没有执行任何操作"
QUESTIONS = (
    "chat.schedule.unresolved",
    "chat.schedule.monthly_day",
    "chat.schedule.when",
    "chat.schedule.number_not_date",
    "chat.aa.unresolved",
    "chat.aa.ambiguous",
    "chat.aa.complete_prompt",
)


def msg(key, locale=ZH, **values):
    text = UI_MESSAGES[key][locale]
    return text.format(**values) if values else text


def page_for(monkeypatch, view, locale=ZH):
    monkeypatch.setenv("CORRIDOR_DEFAULT_VIEW", view)
    monkeypatch.setenv("CORRIDOR_LOCALE", locale)
    page = app()
    page.button(key="enter").click().run()
    assert not page.exception
    return page


def text_of(page):
    groups = (page.markdown, page.caption, page.warning, page.info, page.success, page.error)
    return "\n".join(str(item.value) for group in groups for item in group)


def send(page, text):
    page.text_input(key="domestic_text").set_value(text)
    page.button(key="domestic_send").click().run()
    assert not page.exception


# --- 1. the birthday suggestion ------------------------------------------------------------------


def test_the_birthday_suggestion_is_a_plain_reminder_in_the_customer_view(monkeypatch):
    customer = text_of(page_for(monkeypatch, "customer"))
    assert "这只是提醒" in customer and "您还没有办理任何业务" in customer
    assert "只读生日建议" not in customer and "尚未授权任何操作" not in customer
    judge = text_of(page_for(monkeypatch, "judge"))
    assert "只读" in judge and "尚未授权任何操作" in judge
    for key in (
        "Read-only birthday suggestion; no operation authorized. Due: ",
        "birthday.reminder_only",
        "birthday.not_eligible",
        "birthday.outside_lock_month",
    ):
        plain = UI_MESSAGES["customer:" + key]
        assert "您还没有办理任何业务" in plain[ZH] or "您也没有办理任何业务" in plain[ZH], key
        assert "授权" not in plain[ZH] and "只读" not in plain[ZH], key
        assert "authorized" not in plain["en"].lower(), key


# --- 2. clarifications ---------------------------------------------------------------------------


def test_customer_clarifications_ask_without_the_nothing_executed_line(monkeypatch):
    for key in QUESTIONS:
        plain = UI_MESSAGES["customer:" + key]
        assert NOTHING not in plain[ZH] and "executed no operation" not in plain["en"], key
        # The judge view keeps the evidence sentence.
        assert NOTHING in msg(key), key
    # Where the user may think something was done, the sentence stays in both views.
    for key in ("chat.negated", "chat.card_mismatch"):
        assert "customer:" + key not in UI_MESSAGES
        assert NOTHING in msg(key)
    customer = page_for(monkeypatch, "customer")
    send(customer, "每月30号给房东转2000元")
    shown = text_of(customer) + "\n".join(str(item.value) for item in customer.markdown)
    assert UI_MESSAGES["customer:chat.schedule.monthly_day"][ZH] in shown
    assert NOTHING not in shown
    negated = page_for(monkeypatch, "customer")
    send(negated, "别给房东转2000元")
    assert msg("chat.negated") in text_of(negated)


# --- 3. receipts ---------------------------------------------------------------------------------


def _settle(page):
    send(page, "给妈妈转500元")
    page.button(key="domestic_confirm").click().run()
    assert not page.exception
    session = page.session_state["domestic_session"]
    response = session.response(page.session_state["domestic_response_id"])
    assert response["status"] == "settled"
    return response["payload"]["receipt"]


def test_a_settled_receipt_reads_like_a_receipt(monkeypatch):
    customer = page_for(monkeypatch, "customer")
    receipt = _settle(customer)
    assert receipt["receipt_type"] == "bank" and receipt["status"] == "settled"
    shown = text_of(customer)
    assert UI_MESSAGES["customer:Executed · bank receipt verified"][ZH] == "已办理"
    assert "已办理" in [str(item.value) for item in customer.success]
    # The customer view never shows a raw identifier (tests/e2e/test_polish_views.py): the
    # receipt number is its last eight characters, named as such; the judge view keeps
    # the whole identifier.
    tail = receipt["receipt_id"].replace("-", "")[-8:].upper()
    line = msg(
        "customer:polish.receipt.line",
        time=msg("polish.time.beijing", time=display_names.beijing(receipt["executed_at"])),
        receipt=tail,
        status=msg("polish.receipt.status.settled"),
    )
    assert line in shown and receipt["receipt_id"] not in shown
    assert "末 8 位" in msg("customer:polish.receipt.line")
    judge = page_for(monkeypatch, "judge")
    receipt = _settle(judge)
    shown = text_of(judge)
    assert msg("Executed · bank receipt verified") in shown
    assert (
        msg(
            "polish.receipt.line",
            time=receipt["executed_at"],
            receipt=receipt["receipt_id"],
            status=msg("polish.receipt.status.settled"),
        )
        in shown
    )
    for status in ("settled", "pending", "failed", "unknown"):
        assert set(UI_MESSAGES[f"polish.receipt.status.{status}"]) == {"en", ZH}


# --- 4. the task panel's split-bill node ---------------------------------------------------------


def test_the_task_panel_names_a_split_bill_reply_as_a_collection_request(monkeypatch):
    page = page_for(monkeypatch, "customer")
    send(page, "我和Chen、房东 AA 300元")
    session = page.session_state["domestic_session"]
    response = session.response(page.session_state["domestic_response_id"])
    assert response["card"]["kind"] == "aa_request"
    # The task panel's graph (the card itself always showed its kind).
    graphs = [str(item.value) for item in page.markdown if "<svg" in str(item.value)]
    assert len(graphs) == 1
    assert f"1. {msg('Split-bill collection request')}" in graphs[0]
    assert f"1. {msg('Transfer')}" not in graphs[0]


# --- 5. the wealth page --------------------------------------------------------------------------


def test_the_marketing_twin_check_is_judge_view_only(monkeypatch):
    label = msg("Marketing metadata twin (isolated)")
    judge = page_for(monkeypatch, "judge")
    navigate(judge, "wealth")
    judge.button(key="compare_investments").click().run()
    assert not judge.exception
    assert [item for item in judge.expander if str(item.label) == label]
    assert [item for item in judge.toggle if item.key == "sponsored_twin"]
    assert "preview_investment" in [button.key for button in judge.button]
    # AppTest cannot take the customer view of this page past a comparison (a selectbox
    # label quirk of the test tool, the same on the previous commit), so the customer side
    # is read from the source: the twin expander is drawn only under view_mode.is_judge().
    source = (ROOT / "src/corridor/ui/app.py").read_text(encoding="utf-8")
    function = next(
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.FunctionDef) and node.name == "investments"
    )

    def is_twin(node):
        return isinstance(node, ast.With) and "Marketing metadata twin" in ast.unparse(node)

    guarded = [
        node
        for branch in ast.walk(function)
        if isinstance(branch, ast.If) and ast.unparse(branch.test) == "view_mode.is_judge()"
        for statement in branch.body
        for node in ast.walk(statement)
        if is_twin(node)
    ]
    assert len(guarded) == 1
    assert len([node for node in ast.walk(function) if is_twin(node)]) == 1
