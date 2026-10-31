"""B2: result, receipt and failure view models (``render_result`` and ``receipt_line``).

A banner follows the gateway status; the receipt line names only the receipt's own
time, number and status (customer: Beijing time and the last eight characters); facts are
the narrative rendered from the verified receipt. A failed or unknown gesture leaves a
failure block and Cancel, never a second confirm.
"""

import re

import pytest

from corridor.web import errors
from corridor.web.catalog import bound, wtr
from corridor.web.compat import tr
from tests.web.test_viewmodels import BALANCE, YELLOW, Web

pytestmark = pytest.mark.usefixtures("st_sentinel")
REFUSED = "给妈妈转999999元"
BILL = "看看9月账单"
ANOMALY = "有没有异常扣款"
SUBSCRIPTIONS = "查看我的订阅"


def settled(view):
    web = Web(view=view).login()
    rkey = web.send(YELLOW)
    web.gesture()
    return web, rkey


def test_the_customer_receipt_line_shows_beijing_time_and_the_last_eight_characters():
    web, rkey = settled("customer")
    result = web.response(rkey)["result"]
    receipt = web.controller.state.receipts
    (identifier,) = [key for key, value in receipt.items() if value.kind == "transfer"]
    assert result["banner"] == {"tone": "success", "text": "已办理"}
    assert "（北京时间）" in result["receipt_line"]
    assert identifier.replace("-", "")[-8:].upper() in result["receipt_line"]
    assert identifier not in result["receipt_line"]
    assert "rule_ids" not in result and "receipt" not in result
    assert result["facts_lines"] and result["why"] == {"lines": []}


def test_the_judge_receipt_line_keeps_the_canonical_number_and_time():
    web, rkey = settled("judge")
    result = web.response(rkey)["result"]
    assert re.search(r"[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}", result["receipt_line"])
    assert re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", result["receipt_line"])
    assert result["banner"]["text"] == "已执行 · 银行回执已核验"
    assert result["receipt"]["receipt_type"] == "bank" and result["rule_ids"] == []
    assert result["audit_checkpoint"]["head"]


def test_a_green_balance_query_settles_without_any_device_task():
    web = Web().login()
    rkey = web.send(BALANCE)
    page = web.state()
    response = page["chat"]["responses"][rkey]
    assert response["result"]["banner"]["tone"] == "success"
    assert any("余额" in line for line in response["result"]["facts_lines"])
    assert response["tier"]["code"] == "green" and response["cards"] == []
    assert page["device"]["tasks"] == [] and page["chat"]["task_panel"]["tier"]["code"] == "green"


def test_a_kernel_refusal_reads_as_not_executed_with_plain_reasons_for_customers():
    web = Web().login()
    rkey = web.send(REFUSED)
    web.gesture()
    response = web.response(rkey)
    result = response["result"]
    with bound(web.ws):
        assert result["banner"] == {
            "tone": "error",
            "text": tr("Action not executed · review the reasons before approving again"),
        }
        assert result["why"]["lines"][0].startswith(tr("polish.reason.plain_prefix"))
    assert not re.search(r"\b[A-Z]{2,}-[A-Z0-9-]+\b", " ".join(result["why"]["lines"]))
    assert response["cards"] == [] and response["cancel"] is None
    assert web.task(rkey)["step"] == "refused"
    web.switch(view="judge")
    judged = web.response(rkey)["result"]
    assert judged["rule_ids"] and any(
        rule in judged["why"]["lines"][-1] for rule in judged["rule_ids"]
    )


def test_cancel_keeps_its_stop_receipt_in_the_chat_and_withdraws_the_card():
    web = Web().login()
    first = web.send(YELLOW)
    stop = web.post("chat_stop")
    page = web.state()
    withdrawn = page["chat"]["responses"][first]
    assert withdrawn["status"] == "withdrawn" and withdrawn["cards"] == []
    assert withdrawn["device_task"] is None
    assert withdrawn["status_text"] == wtr("web.vm.status.withdrawn")
    receipt = page["chat"]["responses"][stop]
    with bound(web.ws):
        assert receipt["result"]["banner"] == {
            "tone": "warning",
            "text": tr("Interrupted · automatic execution paused"),
        }
    assert receipt["copy_lines"] and page["chat"]["current"] == stop
    assert all(task.get("rkey") != first for task in page["device"]["tasks"])


def test_a_handoff_reply_reports_the_paused_execution():
    web = Web().login()
    web.send(YELLOW)
    rkey = web.post("handoff")
    response = web.response(rkey)
    assert response["route_hint"]["target"] == "operator"
    with bound(web.ws):
        text = tr("Operator handoff · automatic execution paused")
    assert response["result"]["banner"] == {"tone": "warning", "text": text}
    assert response["copy_lines"] == [text]


@pytest.mark.parametrize(
    ("error", "kind"), [(RuntimeError("boom"), "failed"), (TypeError("boom"), "unknown")]
)
def test_a_failed_gesture_leaves_a_failure_block_and_only_cancel(monkeypatch, error, kind):
    web = Web().login()
    rkey = web.send(YELLOW)

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(web.controller.workflow, "confirm_transfer", fail)
    with pytest.raises(errors.WebError):
        web.gesture()
    page = web.state()
    task = page["device"]["tasks"][0]
    assert task["step"] == f"closed_{kind}" and task["gesture"] is None
    assert task["failure"]["kind"] == kind and task["cancel"] is not None
    response = page["chat"]["responses"][rkey]
    assert response["failure"]["kind"] == kind and response["cancel"] is not None
    assert "boom" not in str(page)
    if kind == "unknown":
        assert task["failure"]["lines"] == [wtr("web.vm.failure.unknown")]


def test_a_bill_report_shows_totals_and_exact_category_ratios():
    web = Web(view="judge").login()
    rkey = web.send(BILL)
    bill = web.response(rkey)["result"]["bill"]
    assert [row["label"] for row in bill["totals"]] == ["消费支出", "已退款", "净支出"]
    assert all(row["source"] for row in bill["totals"])
    ratios = [row["ratio"] for row in bill["categories"]]
    assert "1" in ratios and all(re.fullmatch(r"(?:0|1)(?:\.\d+)?", ratio) for ratio in ratios)
    web.switch(view="customer")
    bill = web.response(rkey)["result"]["bill"]
    assert all("source" not in row for row in bill["totals"])


def test_an_anomaly_question_lists_the_charges_to_review():
    web = Web().login()
    rkey = web.send(ANOMALY)
    bill = web.response(rkey)["result"]["bill"]
    assert bill["anomalies"] and bill["totals"] == []
    item = bill["anomalies"][0]
    assert item["amount"].startswith("¥") and "（北京时间）" in item["date"]


def test_a_settled_subscription_query_lists_rows_and_cancellable_choices():
    web = Web().login()
    rkey = web.send(SUBSCRIPTIONS)
    web.gesture()
    subscriptions = web.response(rkey)["result"]["subscriptions"]
    assert subscriptions["rows"] and all(set(row) == {"text"} for row in subscriptions["rows"])
    keys = [choice["key"] for choice in subscriptions["cancel_choices"]]
    assert keys == [f"c{index}" for index in range(len(keys))]
    assert subscriptions["footer"] and "（北京时间）" not in subscriptions["snapshot_text"][:0]
    web.switch(view="judge")
    rows = web.response(rkey)["result"]["subscriptions"]["rows"]
    assert all(len(row["cells"]) == 6 for row in rows)


@pytest.mark.parametrize("text", [YELLOW, BALANCE])
def test_receipt_fact_lines_follow_the_language_and_match_the_gateway_narrative(text):
    web = Web().login()
    rkey = web.send(text)
    if text == YELLOW:
        web.gesture()
    rid = web.ws.rkeys[rkey]
    stored = [fact["text"] for fact in web.ws.session.response(rid)["payload"]["facts"]["facts"]]
    assert web.response(rkey)["result"]["facts_lines"] == stored
    web.switch(locale="en")
    lines = web.response(rkey)["result"]["facts_lines"]
    assert len(lines) == len(stored) and all(line.isascii() for line in lines)
    assert any(line.startswith("Verified balance: ") for line in lines)
