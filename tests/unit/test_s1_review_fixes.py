"""S1 review: counterexamples found by the adversarial verifier, pinned as regressions."""

import pytest

from corridor.agent.domestic_intent import compensation_request, history_listing, recognize
from corridor.agent.domestic_schedule import residual_time, schedule_candidate
from corridor.agent.domestic_session import DomesticSession
from corridor.ui.controller import DemoController

NOW = "2026-10-16T02:00:00Z"


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


def _statuses(session):
    return sorted(
        node["status"]
        for plan in session.controller.snapshot()["plans"]
        for node in plan["nodes"].values()
    )


@pytest.mark.parametrize(
    "text",
    [
        "房东说不用了，给他转500补偿",
        "给房东转500块补偿",
        "转账失败了，银行能补偿吗",
        "算了不转给Chen了，把锁住的钱退回来",
    ],
)
def test_paying_someone_as_compensation_never_interrupts_the_running_plan(text):
    assert not compensation_request(text)
    session = _session_with_plan()
    before = _statuses(session)
    reply = session.receive(text)
    assert reply["intent"] != "birthday"
    assert (reply["card"] or {}).get("kind") != "unlock_funds"
    assert _statuses(session) == before
    assert not any(plan["blocked"] for plan in session.controller.snapshot()["plans"])


@pytest.mark.parametrize(
    "text", ["把锁住的1000块退回活期", "把预算转回活期", "生日的预算退给我", "补偿一下"]
)
def test_returning_locked_funds_is_still_compensation(text):
    assert compensation_request(text)


@pytest.mark.parametrize(
    "text",
    [
        "房号18号给房东转500元",
        "房号18号转500元给房东",
        "门牌18号给房东转500元",
        "301室18号给房东转500元",
        "给房东转500元，房号:18号",
        "给房东转500元，住在18号",
    ],
)
def test_identifier_noun_before_the_number_wins_over_the_transfer_verb(text):
    parsed = schedule_candidate(text, {"now": NOW})
    assert (parsed["status"], parsed["reason"]) == ("unresolved", "number_not_date"), parsed
    reply = _session().receive(text)
    assert reply["status"] == "clarify" and not reply["card"], text


@pytest.mark.parametrize("text", ["18号给房东转500元", "给房东转500元，28号", "在20号给妈妈转200"])
def test_day_numbers_with_date_context_still_schedule(text):
    assert schedule_candidate(text, {"now": NOW})["status"] == "complete"


@pytest.mark.parametrize(
    "text",
    [
        "明儿给房东转500元",
        "这两天给房东转500元",
        "这几天给房东转500元",
        "抽空给房东转500元",
        "给房东转五百块，等发了工资",
        "Tomorrow给房东转500元",
        "给房东转500元 next week",
    ],
)
def test_colloquial_time_words_never_preview_an_immediate_transfer(text):
    assert residual_time(text)
    reply = _session().receive(text)
    assert not reply["card"] and not reply["buttons"], text


@pytest.mark.parametrize("text", ["上个月账单明细", "九月的流水", "2025年的交易记录"])
def test_a_named_period_keeps_the_period_report(text):
    assert not history_listing(text)
    reply = _session().receive(text)
    assert reply["payload"]["analysis_receipt"]["function_id"] == "bill_summary_v1"


def test_explaining_details_is_not_a_history_request():
    assert recognize("请说明细节").intent == "clarify"


@pytest.mark.parametrize("text", ["那个停掉", "这些都停掉", "停掉", "取消那个"])
def test_subscription_follow_up_is_not_an_interruption(text):
    for previous in ("subscription_query", "subscription_cancel"):
        assert recognize(text, {"previous_intent": previous}).intent == "subscription_cancel"
    assert recognize("等等", {"previous_intent": "subscription_query"}).intent == "stop"
    assert recognize("那个计划停掉", {"previous_intent": "subscription_query"}).intent == "stop"


@pytest.mark.parametrize("text", ["取消生日计划", "生日计划取消"])
def test_cancelling_the_birthday_plan_interrupts_instead_of_drafting_a_new_one(text):
    session = _session_with_plan()
    reply = session.receive(text)
    assert reply["intent"] == "stop" and "nodes" not in reply["payload"]
    assert all(plan["blocked"] for plan in session.controller.snapshot()["plans"])
    assert "已执行：锁定预算" in reply["message"]
