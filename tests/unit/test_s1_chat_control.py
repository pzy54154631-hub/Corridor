"""S1: natural interruption with truthful receipts, history query and chat compensation."""

import pytest

from corridor.agent import domestic_session
from corridor.agent.domestic_intent import natural_stop, recognize
from corridor.agent.domestic_session import DomesticSession
from corridor.bench.domestic_runner import run_task
from corridor.ui import business_views
from corridor.ui.controller import DemoController

PLAN = {"task": "生日安排一下", "simulated_device_events": ["confirm_all_nodes"]}
FALSE_DENIAL = "目前没有执行操作"


def _session():
    controller = DemoController()
    controller.start_session()
    return DomesticSession(controller)


def _session_with_plan():
    session = _session()
    draft = session.receive("生日安排一下")
    session.confirm(
        draft["response_id"], selected_node_ids=[node["id"] for node in draft["payload"]["nodes"]]
    )
    return session


def _nodes(result):
    (plan,) = result["plans"]
    tools = {node["node_id"]: node["tool"] for node in plan["plan"]["nodes"]}
    return plan, {tools[key]: value["status"] for key, value in plan["nodes"].items()}


@pytest.mark.parametrize(
    "text",
    [
        "停一下，先别执行了",
        "停一下",
        "先别执行了",
        "暂停",
        "暂停一下",
        "别做了",
        "先停",
        "等一下别转",
        "别执行了",
        "算了，别做了",
        "先把计划停一下吧",
        "stop",
    ],
)
def test_natural_interruption_stops_the_plan_and_lists_the_executed_lock(text):
    # DIAG-Q60-PLAN: the lock already executed when the user interrupts in their own words.
    result = run_task({"task": text, "context": [PLAN]})
    assert (result["intent"], result["final_status"]) == ("stop", "interrupted")
    plan, statuses = _nodes(result)
    assert plan["blocked"] is True
    assert statuses.pop("lock_funds") == "succeeded"
    assert set(statuses.values()) == {"cancelled"}
    assert result["dispatch_count"] == 1
    reply = result["responses"][-1]
    receipt = reply["payload"]["stop_receipt"]
    (entry,) = receipt["plans"]
    assert [item["tool"] for item in entry["executed"]] == ["lock_funds"]
    assert len(entry["not_executed"]) == 3
    assert {item["status"] for item in entry["not_executed"]} == {"cancelled"}
    message = reply["message"]
    assert "已执行：锁定预算" in message
    assert "未执行、已取消：到期解锁预算、订购鲜花、订购蛋糕" in message
    assert FALSE_DENIAL not in message and "没有执行任何操作" not in message


def test_diag_q60_plan_scenario_matches_the_exact_phrase_path():
    natural = run_task({"task": "停一下，先别执行了", "context": [PLAN]})
    exact = run_task({"task": "先别执行了", "context": [PLAN]})
    assert _nodes(natural)[1] == _nodes(exact)[1]
    assert natural["responses"][-1]["message"] == exact["responses"][-1]["message"]


@pytest.mark.parametrize(
    "text,intent",
    [
        ("银行卡先暂停", "card_freeze"),
        ("这张卡先别用了", "card_freeze"),
        ("那个会员停掉", "subscription_cancel"),
        ("还是算了吧", "clarify"),
        ("等一下，给Chen转200元", "transfer"),
        ("停车费帮我查一下", "clarify"),
    ],
)
def test_interruption_needs_the_whole_utterance_to_be_stop_wording(text, intent):
    assert not natural_stop(text)
    assert recognize(text).intent == intent


@pytest.mark.parametrize("text", ["不转了", "别转了", "停止转账", "取消这笔转账", "取消后续"])
def test_existing_stop_words_and_transfer_regexes_are_kept(text):
    assert recognize(text).intent == "stop"


def test_stop_without_plan_or_card_says_nothing_ran():
    reply = _session().receive("停一下")
    assert reply["status"] == "no_active_plan"
    assert reply["message"] == domestic_session._message("stop_none")


def test_stop_reports_withdrawn_unsigned_cards():
    session = _session()
    pending = session.receive("给房东转500元")
    reply = session.receive("暂停")
    assert reply["payload"]["stop_receipt"] == {"plans": [], "retired_drafts": 1}
    assert "已撤回1张待确认的卡片" in reply["message"]
    assert session.confirm(pending["response_id"])["status"] == "withdrawn"


def test_negated_transfer_that_interrupts_a_plan_reports_what_ran():
    session = _session_with_plan()
    reply = session.receive("先别给老妈转800元")
    assert reply["status"] == "clarify" and not reply["card"]
    (entry,) = reply["payload"]["stop_receipt"]["plans"]
    assert entry["blocked"] and [item["tool"] for item in entry["executed"]] == ["lock_funds"]
    assert reply["message"].startswith("未生成转账卡片。计划已中断。已执行：锁定预算")


def test_clarification_after_an_executed_node_never_denies_execution():
    session = _session_with_plan()
    reply = session.receive("嗯")
    assert reply["status"] == "clarify"
    assert FALSE_DENIAL not in reply["message"]
    assert all(FALSE_DENIAL not in pair[0] for pair in domestic_session._MESSAGES.values())


@pytest.mark.parametrize(
    "text",
    ["查一下我最近的交易记录", "最近的流水给我看看", "账单明细查一下", "看看交易明细"],
)
def test_transaction_history_wording_reaches_the_green_query(text):
    result = run_task({"task": text})
    assert (result["intent"], result["final_status"], result["tier"]) == (
        "bill_report",
        "settled",
        "green",
    )
    payload = result["responses"][-1]["payload"]
    assert payload["receipt"]["kind"] == "transactions_query"
    assert not result["approval_events"] and not result["mfa_events"]
    controller = DemoController()
    controller.start_session()
    rows = business_views.transaction_projection(business_views.query_transactions(controller))
    assert business_views.transaction_projection(payload) == rows and rows


def test_history_with_analysis_wording_keeps_the_period_report():
    result = run_task({"task": "九月交易明细分类统计一下"})
    payload = result["responses"][-1]["payload"]
    assert payload["analysis_receipt"]["function_id"] == "bill_summary_v1"
    assert (payload.get("receipt") or {}).get("kind") != "transactions_query"


def test_compensation_request_interrupts_the_cancelled_plan_and_prepares_its_card():
    # DIAG-Q61-PLAN: the request must not become a new birthday draft.
    session = _session_with_plan()
    controller = session.controller
    plans_before = controller._registered_plan_ids()
    locked = controller.snapshot()["balances"]["A_LOCKED"]
    reply = session.receive("生日计划取消了，把锁住的1000块退回活期账户")
    assert reply["intent"] == "birthday" and "nodes" not in reply["payload"]
    assert reply["card"]["kind"] == "unlock_funds" and reply["buttons"]
    assert reply["payload"]["compensation"] == {"plan_id": plans_before[-1]}
    (entry,) = reply["payload"]["stop_receipt"]["plans"]
    assert entry["blocked"] and [item["tool"] for item in entry["executed"]] == ["lock_funds"]
    assert reply["message"].startswith("计划已中断。已执行：锁定预算")
    assert "退回锁定的预算" not in reply["message"]
    assert reply["message"].endswith(domestic_session._message("compensation_card"))
    assert controller._registered_plan_ids() == plans_before
    assert controller.gateway.bank.execute_count == 1
    final = session.confirm(reply["response_id"])
    assert final["status"] == "settled"
    assert controller.snapshot()["balances"]["A_LOCKED"] != locked
    # The existing compensation flow registers its own one-node unlock plan on approval.
    birthday, compensation = controller.snapshot()["plans"]
    assert birthday["plan"]["plan_id"] == plans_before[-1]
    lock = birthday["plan"]["nodes"][0]
    assert lock["tool"] == "lock_funds"
    assert birthday["nodes"][lock["node_id"]]["status"] == "compensated"
    assert [node["tool"] for node in compensation["plan"]["nodes"]] == ["unlock_funds"]


def test_compensation_on_a_running_plan_without_cancel_wording_asks_to_stop_first():
    session = _session_with_plan()
    reply = session.receive("把锁住的1000块退回活期")
    assert reply["status"] == "clarify" and not reply["card"]
    assert reply["payload"]["plan"]["blocked"] is False
    assert reply["message"].startswith("计划仍在进行。已执行：锁定预算")
    assert "尚未执行：到期解锁预算、订购鲜花、订购蛋糕" in reply["message"]
    assert reply["message"].endswith(domestic_session._message("compensation_active"))
    assert "退回锁定的预算" not in reply["message"]
    assert session.controller.gateway.bank.execute_count == 1


def test_compensation_without_a_plan_is_not_a_new_birthday_draft():
    result = run_task({"task": "生日计划取消了，把锁住的1000块退回活期账户"})
    assert (result["intent"], result["final_status"]) == ("birthday", "no_active_plan")
    assert not result["plans"] and not result["actions"] and result["dispatch_count"] == 0


@pytest.mark.parametrize("text", ["把卡解锁", "帮我先锁1000块预算", "这个月先帮我锁1000块"])
def test_card_unlock_and_new_budget_requests_are_not_compensation(text):
    assert not domestic_session.compensation_request(text)
