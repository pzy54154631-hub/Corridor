"""S5: one negation/cancel gate for money-moving chat requests (PROP-S5-NEG).

A negation, cancel or stop word anywhere in a transfer, scheduled-transfer, split-bill,
birthday-plan or refund request holds it at clarification: no new card, no dispatch, no
signed input. The gate never interrupts or withdraws anything by itself; the existing
stop handling (S1) is unchanged. Closed neutral compounds (别忘了 …) still pass.
"""

import json
from pathlib import Path

import pytest

from corridor.agent import domestic_session
from corridor.agent.domestic_intent import negation_hold, recognize
from corridor.agent.domestic_negation import (
    PLAN_CANCEL,
    compensation_negation,
    negation_words,
)
from corridor.agent.domestic_schedule import aa_candidate, schedule_candidate
from corridor.agent.domestic_session import DomesticSession
from corridor.agent.extractor import extract_intent
from corridor.i18n import reset_locale, set_locale
from corridor.ui.controller import DemoController

NOW = "2026-10-16T02:00:00Z"
DATA = Path(__file__).resolve().parents[2] / "data" / "scenarios" / "zh"


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


def _context(session, text):
    return {**session._context(), "contact_text": session.controller.gateway.vault.tokenize(text)}


def _assert_held(session, response, text):
    assert response["intent"] == "clarify", (text, response["intent"])
    assert response["status"] == "clarify" and response["needs_clarification"], text
    assert not response["card"] and response["buttons"] == [], text
    assert response["payload"]["negation"]["words"], text


def _held(session, text):
    """Receive one held request: no card, and nothing dispatched or signed by it."""
    controller = session.controller
    before = (controller.gateway.bank.execute_count, len(controller.state.user_inputs))
    response = session.receive(text)
    _assert_held(session, response, text)
    assert (controller.gateway.bank.execute_count, len(controller.state.user_inputs)) == before
    return response


# The four reproductions reported by the S4 review.
REPORTED = (
    "每月5号，停止给房东转2000块",
    "别每月5号给房东转2000块",
    "不给房东转2000块",
    "取消给房东转2000块",
)


@pytest.mark.parametrize("text", REPORTED)
def test_reported_negations_never_yield_a_card(text):
    session = _session()
    response = _held(session, text)
    if text in {"每月5号，停止给房东转2000块", "别每月5号给房东转2000块"}:
        # B2 (JUDGING-1 list item 19): stopping a standing monthly transfer is now answered
        # in two separate statements (no new payment or card; the existing authorization
        # is not cancelled) instead of the generic negation reply. Still held, no card.
        # C1: a negated sentence naming a standing transfer (别每月…转) gets it too.
        assert response["message"].startswith(domestic_session._message("schedule_cancel_lead"))
        assert response["payload"]["schedule_cancel"]["candidates"] == []
    else:
        assert response["message"] == domestic_session._message("negated")
    assert recognize(text, _context(session, text)).intent == "clarify"


@pytest.mark.parametrize(
    "text",
    [
        # Before the date phrase, between it and the verb, after the amount.
        "别每月5号给房东转2000块",
        "每月5号别给房东转2000块",
        "每月5号给房东别转2000块",
        "明天不给妈妈转50元",
        "下周五不要给室友转300元",
        "月底先别给房东转2000块",
        # A clause of its own, before or after, with any punctuation.
        "每月5号，停止给房东转2000块",
        "不，每月5号给房东转2000块",
        "别，明天给妈妈转50元",
        "每月5号给房东转2000块，算了",
        "每月5号给房东转2000块。不用了",
        "每月5号给房东转2000块；免了",
        "给房东转2000块！取消",
        "给房东转2000块 停掉",
        "明天给妈妈转50元、不要了",
        # Every lexicon word, immediate transfer.
        "不想给房东转2000块",
        "先不给房东转2000块",
        "不用给房东转2000块",
        "不必给房东转2000块",
        "不需要给房东转2000块",
        "没必要给房东转2000块",
        "无需给房东转2000块",
        "甭给房东转2000块了",
        "勿给房东转2000块",
        "暂停给房东转2000块",
        "停掉给房东转2000块",
        "撤销给房东转2000块",
        "撤回给房东转2000块",
        "作废给房东转2000块",
        "终止给房东转2000块",
        "拒绝给房东转2000块",
        # Removing or switching off a standing transfer.
        "关掉每月5号给房东转2000块",
        "把每月5号给房东转2000块关闭",
        "删除每月5号给房东转2000块",
        "每月5号给房东转2000块，删掉",
        "去掉明天给妈妈转50元",
        "撤掉明天给妈妈转50元",
        "Remove the transfer of 200 yuan to Chen",
        # Split bills and the other money words.
        "我和室友、同学不用AA 300元",
        "别跟室友AA 300元",
        "昨天和室友、同学聚餐一共花了300元，不用AA",
        "这个月房租不交了",
        "不用还钱给房东了",
        # English.
        "Don't transfer 200 yuan to Chen",
        "do not transfer 200 to Chen tomorrow",
        "Transfer 200 yuan to Chen, cancel it",
    ],
)
def test_negation_anywhere_holds_the_request(text):
    _held(_session(), text)


@pytest.mark.parametrize(
    "text",
    [
        "取\u200b消给房东转2000块",
        "取 消给房东转2000块",
        "不 给房东转2000块",
        "ＤＯＮ’Ｔ transfer 200 to Chen",
    ],
)
def test_spacing_invisible_characters_and_width_cannot_split_a_negation(text):
    assert negation_words(text), text
    _held(_session(), text)


def test_correction_with_a_negated_recipient_clarifies_and_drops_nothing_silently():
    session = _session()
    for text in (
        "不要转给张三，转给李四",
        "不要转给妈妈，转给房东200元",
        "别给妈妈转，给房东转200元",
    ):
        _held(session, text)
    assert not session.controller._drafts and not session.controller._pending


@pytest.mark.parametrize(
    ("text", "kind", "contact", "amount"),
    [
        # A reminder is a positive request: 别忘了 X asks for X.
        ("别忘了每月5号给房东转2000", "schedule_transfer", "CONTACT_LANDLORD", "2000/1"),
        ("不要忘了每月5号给房东转2000", "schedule_transfer", "CONTACT_LANDLORD", "2000/1"),
        ("可别忘了明天给妈妈转50元", "schedule_transfer", "CONTACT_CHEN", "50/1"),
        ("别忘记下周五给室友转300元", "schedule_transfer", "CONTACT_CHEN", "300/1"),
        ("别忘了给房东转2000块", "transfer", "CONTACT_LANDLORD", "2000/1"),
        # Politeness, parking, arithmetic: not negations.
        ("不好意思，给房东转2000块", "transfer", "CONTACT_LANDLORD", "2000/1"),
        ("对不起，明天给妈妈转50元", "schedule_transfer", "CONTACT_CHEN", "50/1"),
        ("给房东转停车费300元", "transfer", "CONTACT_LANDLORD", "300/1"),
        ("我算了一下，给房东转2000块", "transfer", "CONTACT_LANDLORD", "2000/1"),
    ],
)
def test_neutral_compounds_keep_their_card(text, kind, contact, amount):
    assert negation_words(text) == (), text
    session = _session()
    response = session.receive(text)
    card = response["card"]
    assert card and card["kind"] == kind, (text, response["status"])
    assert card["amount"] == amount
    assert card["objects"]["contact_ref"] == contact
    assert "negation" not in response["payload"]
    assert session.controller.gateway.bank.execute_count == 0


def test_the_fraud_sentence_keeps_its_card_for_the_reminder():
    # 不然 ("otherwise") is a conjunction; S2's fraud reminder needs this card.
    # R7: with an impersonated public authority as well
    # ("反诈中心让我立即……") the phrase gate holds the sentence (no card, test_ui_scam_r7.py);
    # the negation gate itself still never holds it.
    held = "反诈中心让我立即给新联系人转480元，不然账户冻结"
    assert negation_words(held) == ()
    assert not _session().receive(held)["card"]
    text = "工作人员说立即给新联系人转480元，不然账户冻结"
    assert negation_words(text) == ()
    assert _session().receive(text)["card"]


@pytest.mark.parametrize(
    "text",
    [
        "别忘了别给房东转2000",
        "别忘了取消每月5号给房东转2000",
        "不好意思，不要给房东转2000块",
        "我算了一下，不给房东转了2000块",
        "给房东转停车费300元，算了",
        "算了下次再给房东转2000",
    ],
)
def test_a_neutral_compound_never_hides_another_negation(text):
    assert negation_words(text), text
    _held(_session(), text)


def test_unrelated_words_keep_their_intents():
    assert recognize("取消订阅").intent == "subscription_cancel"
    assert recognize("那个会员停掉", {"previous_intent": "subscription_query"}).intent == (
        "subscription_cancel"
    )
    assert recognize("这张卡先别用了").intent == "card_freeze"
    assert recognize("停一下").intent == "stop"
    assert recognize("算了，不转了").intent == "stop"
    assert recognize("等一下别转").intent == "stop"
    assert recognize("取消这笔转账").intent == "stop"
    assert recognize("有不认识的扣款吗").intent == "bill_report"
    for text in ("取消订阅", "停一下", "这张卡先别用了", "有不认识的扣款吗"):
        assert negation_hold(text) == (), text


def test_natural_stop_still_interrupts_and_withdraws():
    session = _session_with_plan()
    reply = session.receive("停一下")
    assert reply["intent"] == "stop"
    assert all(plan["blocked"] for plan in session.controller.snapshot()["plans"])


def test_the_gate_itself_withdraws_and_interrupts_nothing():
    session = _session_with_plan()
    pending = session.receive("给房东转500元")
    assert pending["buttons"]
    before = session.controller.snapshot()["plans"]
    reply = _held(session, "取消给房东转500元")
    assert "stop_receipt" not in reply["payload"]
    assert session.controller.snapshot()["plans"] == before
    assert session.response(pending["response_id"])["buttons"]
    assert pending["payload"]["draft_id"] in session.controller._drafts
    # The reply names the existing control; saying it withdraws the card as before.
    assert "停止" in reply["message"]
    session.receive("停止")
    assert session.confirm(pending["response_id"])["status"] == "withdrawn"


def test_the_existing_negation_trigger_still_interrupts():
    session = _session_with_plan()
    reply = session.receive("先别给老妈转800元")
    assert reply["payload"]["stop_receipt"]["plans"][0]["blocked"]
    assert reply["payload"]["negation"]["words"] == ["先别"]
    assert reply["message"].startswith("未生成转账卡片。计划已中断。")


@pytest.mark.parametrize("text", ["妈妈生日不用安排了", "生日计划不要了", "妈妈生日别订蛋糕了"])
def test_negated_birthday_requests_draft_no_plan(text):
    session = _session()
    _held(session, text)
    assert not session.controller._plans


def test_birthday_follow_up_and_reminder():
    session = _session()
    assert session.receive("别忘了妈妈生日")["status"] == "draft"
    follow = session.receive("那个不用了")
    assert follow["status"] == "clarify" and follow["payload"]["negation"]["words"] == ["不用"]


def test_refund_requests_keep_plan_cancel_words_but_not_a_negated_return():
    session = _session_with_plan()
    session.receive("停止")
    pending = dict(session.controller._pending)
    for text in ("不要退回锁定的预算", "锁定的预算先别退回", "不用把锁住的钱退回来"):
        _held(session, text)
        assert session.controller._pending == pending, text
    response = session.receive("预算不用了，退回来")
    assert response["intent"] == "birthday" and response["card"]["kind"] == "unlock_funds"


@pytest.mark.parametrize(
    "text",
    [
        "生日计划取消了，把锁住的1000块退回活期账户",
        "算了，把锁住的钱退回来",
        "不办了，预算退回",
        "生日不过了，退回预算",
        "这个计划停了，把钱退回活期，预算也退回",
        "停一下，把预算退回来",
    ],
)
def test_refund_plan_cancel_words_are_not_negations(text):
    assert compensation_negation(text) == (), text


def test_plan_cancel_list_is_shared_with_the_refund_flow():
    assert domestic_session._CANCEL_PLAN is PLAN_CANCEL


def test_negated_split_bill_offers_no_roster_completion():
    session = _session()
    response = _held(session, "我们四个人吃饭花了400，不用AA")
    with pytest.raises(ValueError):
        session.prepare_aa(response["response_id"], ["CONTACT_CHEN", "CONTACT_LANDLORD"])


def test_parsers_hold_negations_themselves():
    context = {**_session()._context()}
    held = schedule_candidate("别每月5号给房东转2000块", context)
    assert held["status"] == "unresolved" and held["reason"] == "negated"
    assert held["words"] == ["别"]
    assert schedule_candidate("每月5号给房东转2000块", context)["status"] == "complete"
    assert schedule_candidate("别忘了每月5号给房东转2000块", context)["status"] == "complete"
    # Checked last: an incomplete parse keeps its own reason (S4 near-misses unchanged).
    assert schedule_candidate("每月5号自动停止给房东转2000块", context)["reason"] == (
        "recurrence_unsupported"
    )
    aa = aa_candidate(
        "我和室友、同学不用AA 300元", {**context, "contact_text": "我和室友、同学不用AA 300元"}
    )
    assert aa["status"] == "unresolved" and aa["reason"] == "negated"
    assert aa_candidate("我和室友、同学AA 300元", context)["status"] == "complete"


def test_conversation_page_parser_shares_the_gate():
    labels = {"CONTACT_CHEN": ["CONTACT_CHEN", "NAME_CHEN", "Chen"]}
    assert extract_intent("给Chen转200元", labels).contact_refs == ["CONTACT_CHEN"]
    for text in ("不给Chen转200元", "取消给Chen转200元", "给Chen转200元，算了"):
        assert extract_intent(text, labels).kind == "clarify", text
    controller = DemoController()
    controller.start_session()
    draft = controller.preview_transfer("取消给Chen转200元", request_id="s5-legacy")
    assert draft["status"] == "clarify" and not draft["card"]


def test_reply_copy_in_chat_and_im():
    from corridor.channels.im import render_message
    from corridor.ui.domestic_views import response_copy
    from corridor.ui.messages import tr

    token = set_locale("zh-CN")
    try:
        response = _session().receive("不给房东转2000块")
        assert response_copy(response) == tr("chat.negated")
        assert "没有生成新的卡片" in tr("chat.negated")
        assert domestic_session._message("negated") in render_message(response)["text"]
    finally:
        reset_locale(token)


def test_frozen_suite_utterances_are_never_held():
    """Equivalence by construction: the gate holds no frozen zh-suite or intent-eval line."""
    # Each line with the frozen context it is evaluated in: intent-eval rows carry their
    # previous intent; a zh-suite turn follows the intent of the turn before it.
    lines = []
    for line in (DATA / "colloquial.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            lines.append((row["text"], (row.get("context") or {}).get("previous_intent")))
    for row in json.loads((DATA / "cases.json").read_text(encoding="utf-8")):
        previous = None
        for turn in [*row["request"]["context"], row["request"]]:
            lines.append((turn["task"], previous))
            previous = recognize(turn["task"], {"previous_intent": previous}).intent
    assert len(lines) > 150
    held = [text for text, previous in lines if negation_hold(text, {"previous_intent": previous})]
    assert held == []
