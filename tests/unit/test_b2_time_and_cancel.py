"""B2 (JUDGING-1 list item 19): time and cancellation semantics in chat.

1. Spaces between a day number and 号 / 日 (and around 月) are normalized before the closed
   schedule grammar reads the phrase: 每月5 号 reads as 每月5号.
2. A time fragment the grammar cannot parse (晚饭后, 睡前, 收到货后) is asked about in chat;
   it is never read as "now", so no immediate transfer card is prepared.
3. Cancelling an existing scheduled transfer gets two separate statements: no new payment
   or card was created, and the existing authorization has not been cancelled. The chat
   then lists the customer's own authorizations; a choice prepares the cancel_schedule
   card through the existing gateway path, and only the device gesture revokes it.
"""

import pytest

from corridor.agent import domestic_session
from corridor.agent.domestic_schedule import schedule_candidate, standing_cancel, time_fragment
from corridor.agent.domestic_session import DomesticSession
from corridor.crypto.jcs import H
from corridor.ui.controller import DemoController

NOW = "2026-10-16T02:00:00Z"


def _session():
    controller = DemoController()
    controller.start_session()
    return DomesticSession(controller)


def _facts(response):
    card = response["card"]
    return (
        card.get("kind"),
        card.get("amount"),
        (card.get("objects") or {}).get("contact_ref"),
        (card.get("spa") or {}).get("schedule"),
        (card.get("spa") or {}).get("start"),
    )


def _quiet(session):
    controller = session.controller
    return (
        controller.gateway.bank.execute_count,
        len(controller.state.user_inputs),
        H(controller.state.schedules),
    )


# --- 1. Spacing ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("spaced", "plain"),
    [
        ("每月5 号给房东转2000元", "每月5号给房东转2000元"),
        ("每月 5 号给房东转2000元", "每月5号给房东转2000元"),
        ("每月5　号给房东转2000元", "每月5号给房东转2000元"),
        ("10月20 号给妈妈转500元", "10月20号给妈妈转500元"),
        ("10 月 20 日给妈妈转500元", "10月20日给妈妈转500元"),
        ("１０月２０号给妈妈转500元", "10月20号给妈妈转500元"),
    ],
)
def test_spaces_around_a_day_number_read_like_the_plain_phrase(spaced, plain):
    context = {"now": NOW}
    expected = schedule_candidate(plain, context)
    assert expected["status"] == "complete", plain
    parsed = schedule_candidate(spaced, context)
    assert parsed["status"] == "complete", spaced
    assert parsed["date_text"] == expected["date_text"]
    assert parsed["monthly_day"] == expected["monthly_day"]
    spaced_card = _session().receive(spaced)
    plain_card = _session().receive(plain)
    assert spaced_card["status"] == plain_card["status"] == "awaiting_approval", spaced
    assert _facts(spaced_card) == _facts(plain_card), spaced


@pytest.mark.parametrize("text", ["房号18 号给房东转500元", "18 号楼给房东转500元"])
def test_a_spaced_room_number_is_still_not_a_date(text):
    response = _session().receive(text)
    assert response["status"] == "clarify"
    assert response["payload"]["schedule_parse"]["reason"] == "number_not_date"
    assert not response["card"]


# --- 2. Unparsed time fragments -----------------------------------------------------------

FRAGMENTS = (
    "晚饭后给房东转500元",
    "吃完晚饭以后给妈妈转200元",
    "睡前给妈妈转200元",
    "睡觉前给房东转500元",
    "收到货后给房东转500元",
    "下单前给房东转500元",
    "到家了以后给妈妈转200元",
    "开完会之后给房东转500元",
    "领了工资的时候给妈妈转200元",
    "我到公司时给妈妈转200元",
)


@pytest.mark.parametrize("text", FRAGMENTS)
def test_an_unparsed_time_fragment_is_asked_about_never_immediate(text):
    session = _session()
    before = _quiet(session)
    response = session.receive(text)
    assert response["status"] == "clarify", (text, response["status"])
    assert not response["card"] and response["buttons"] == [], text
    # 以后 / 之后 are frozen date-lexicon words (unparsed_time); the other fragments are new.
    assert response["payload"]["schedule_parse"]["reason"] in {"residual_time", "unparsed_time"}
    assert response["message"] == domestic_session._message("when"), text
    assert time_fragment(text), text
    assert _quiet(session) == before


@pytest.mark.parametrize(
    "text",
    [
        "给妈妈转500元",
        "然后给妈妈转500元",
        "房租该交了，给Chen转800元",
        "先查了余额，给妈妈转500元",
        "给前同事转500元",
    ],
)
def test_ordinary_requests_are_not_time_fragments(text):
    assert time_fragment(text) is None, text


# --- 3. Cancelling an existing scheduled transfer ----------------------------------------


def _with_monthly_authorization():
    session = _session()
    card = session.receive("每月5号给房东转2000元")
    session.confirm(card["response_id"])
    session.verify_mfa(card["response_id"])
    assert session.confirm(card["response_id"])["status"] == "controlled"
    [spa] = [
        item
        for item in session.controller.state.authorizations.values()
        if getattr(item, "authorization_type", None) == "spa"
    ]
    return session, spa


def _active_runs(session, spa_id):
    return [
        task
        for task in session.controller.state.schedules.values()
        if task.spa_ref == spa_id and task.status in {"pending", "due", "waiting_mfa"}
    ]


def test_cancelling_a_monthly_transfer_answers_in_two_separate_statements():
    session, spa = _with_monthly_authorization()
    before = _quiet(session)
    response = session.receive("取消每月5号给房东的转账")
    assert response["intent"] == "clarify" and response["status"] == "clarify"
    assert not response["card"] and response["buttons"] == []
    assert response["payload"]["negation"]["words"]
    assert response["payload"]["schedule_cancel"]["candidates"] == [spa.id]
    message = response["message"]
    assert "本条消息没有发起新付款" in message
    assert "已有的定时转账授权尚未取消" in message
    assert message.index("本条消息没有发起新付款") < message.index("已有的定时转账授权尚未取消")
    assert _quiet(session) == before
    assert len(_active_runs(session, spa.id)) == 3


def test_choosing_the_listed_authorization_prepares_and_executes_the_revocation():
    session, spa = _with_monthly_authorization()
    listed = session.receive("每月5号，停止给房东转2000块")
    assert listed["payload"]["schedule_cancel"]["candidates"] == [spa.id]
    card = session.prepare_schedule_cancel(listed["response_id"], spa.id)
    assert card["card"]["kind"] == "cancel_schedule"
    assert card["card"]["objects"]["schedule_ref"] == spa.id
    assert card["tier"] == "yellow"
    assert len(_active_runs(session, spa.id)) == 3
    result = session.confirm(card["response_id"])
    assert result["status"] == "controlled"
    assert _active_runs(session, spa.id) == []
    # The same choice again after the revocation is refused (R2-E15): the current standing
    # authorizations are checked before the cache, so neither a second card nor the old
    # finished reply comes back.
    with pytest.raises(ValueError):
        session.prepare_schedule_cancel(listed["response_id"], spa.id)
    with pytest.raises(ValueError):
        session.prepare_schedule_cancel(listed["response_id"], "NOT-LISTED")


def test_without_an_authorization_the_reply_still_says_both_things():
    response = _session().receive("取消每月5号给房东的转账")
    message = response["message"]
    assert "本条消息没有发起新付款" in message
    assert "已有的定时转账授权尚未取消" not in message
    assert "您目前没有进行中的定时转账授权" in message
    assert response["payload"]["schedule_cancel"]["candidates"] == []


@pytest.mark.parametrize("text", ["不给房东转2000块", "取消给房东转2000块"])
def test_a_negated_new_request_keeps_the_s5_reply(text):
    assert not standing_cancel(text), text
    response = _session().receive(text)
    assert response["message"] == domestic_session._message("negated")
    assert "schedule_cancel" not in response["payload"]


def test_a_negated_monthly_transfer_gets_the_standing_listing():
    # C1 (review of B2, pr1-review p15 / p17): a negated sentence that names a standing
    # transfer (每月 … 转) goes to the controlled revocation listing; it was the S5 reply
    # above. Still no card; without an authorization the reply says there is none.
    text = "别每月5号给房东转2000块"
    assert standing_cancel(text)
    response = _session().receive(text)
    assert response["payload"]["schedule_cancel"]["candidates"] == []
    assert not response["card"] and response["buttons"] == []


@pytest.mark.parametrize(
    "text",
    [
        "取消每月5号给房东的转账",
        "每月5号，停止给房东转2000块",
        "把自动给房东的转账撤销",
        "不再每月给房东转了",
    ],
)
def test_naming_a_standing_transfer_with_a_cancel_word_is_a_cancellation(text):
    assert standing_cancel(text), text


def test_a_negated_auto_debit_cancellation_is_not_a_scheduled_transfer():
    # Review fix: 自动扣费 is a subscription debit, not a scheduled transfer; only a request
    # that names a transfer gets the two-statement revocation reply.
    text = "先别取消自动扣费"
    assert not standing_cancel(text)
    response = _session().receive(text)
    assert response["payload"]["negation"]["words"]
    assert "schedule_cancel" not in response["payload"]
    assert response["message"] == domestic_session._message("negated")


def test_a_one_time_authorization_is_listed_by_its_beijing_date():
    session = _session()
    card = session.receive("11月5号给房东转500元")
    session.confirm(card["response_id"])
    if session.response(card["response_id"])["status"] == "waiting_mfa":
        session.verify_mfa(card["response_id"])
        session.confirm(card["response_id"])
    assert session.response(card["response_id"])["status"] == "controlled"
    listed = session.receive("取消定时给房东的转账")
    assert listed["payload"]["schedule_cancel"]["candidates"]
    assert "单次（2026-11-05）" in listed["message"], listed["message"]
