"""S4: 自动 as a schedule filler and a past-tense narrative before an explicit AA request.

CAP-Q04 (每月5号自动给房东转2000块房租) reaches the chat scheduled-transfer card and CAP-Q05
(昨天和室友、同学聚餐一共花了300元，帮我发起AA收款) reaches the named split-bill card. The
grammar stays closed: every near miss below is unresolved (or none) and never yields a card
other than the form's own.
"""

import pytest

from corridor.agent.domestic_schedule import aa_candidate, residual_time, schedule_candidate
from corridor.agent.domestic_session import DomesticSession
from corridor.agent.domestic_slots import trusted_slot_context
from corridor.crypto.jcs import H
from corridor.ui.controller import DemoController

NOW = "2026-10-16T02:00:00Z"
Q04 = "每月5号自动给房东转2000块房租"
Q05 = "昨天和室友、同学聚餐一共花了300元，帮我发起AA收款"


def _controller():
    controller = DemoController()
    controller.start_session()
    return controller


def _context(controller, text):
    return {
        **trusted_slot_context(controller.state, controller.device.principal),
        "contact_text": controller.gateway.vault.tokenize(text),
    }


@pytest.mark.parametrize(
    "text",
    [Q04, "自动每月5号给房东转2000块", "每月5号 自动 给房东转2000块", "明天自动给妈妈转50"],
)
def test_auto_next_to_the_one_schedule_phrase_is_a_filler(text):
    result = schedule_candidate(text, {"now": NOW})
    assert result["status"] == "complete", result
    assert result["phrase"] in {"每月5号", "明天"}


@pytest.mark.parametrize(
    "text,reason",
    [
        # 自动 alone names no time: ask "when?", never an immediate card.
        ("自动给房东转2000块", "residual_time"),
        ("给房东自动转2000块", "residual_time"),
        # Not touching the phrase: still residual. A second 自动 is not the verb phrase,
        # so the phrase itself is not recognised (每月 then reads as a recurrence).
        ("每月5号给房东自动转2000块", "residual_time"),
        ("每月5号自动自动给房东转2000块", "recurrence_unsupported"),
        ("自动明天自动给妈妈转50", "residual_time"),
        # 自动 never widens the grammar: other recurrences and conflicts stay unresolved.
        ("每周五自动给妈妈转500", "recurrence_unsupported"),
        ("每月自动给妈妈转500", "recurrence_unsupported"),
        ("每月31号自动给妈妈转500", "monthly_day_unsupported"),
        ("明天和下周五自动给妈妈转50", "unparsed_time"),
        ("每月5号自动给房东转2000持续一年", "unparsed_time"),
        # Verifier: 自动 must itself end at the verb phrase or a clause boundary. Stop,
        # cancel and negation words after it (and 18号自动售货机, a machine number) leave
        # the phrase unrecognised exactly as before the filler, never a transfer card.
        ("每月5号自动停止给房东转2000块", "recurrence_unsupported"),
        ("每月5号自动取消给房东转2000块", "recurrence_unsupported"),
        ("每月5号自动不给房东转2000块", "recurrence_unsupported"),
        ("每月5号自动地给房东转2000块", "recurrence_unsupported"),
        ("明天自动停止给妈妈转50", "unparsed_time"),
        ("明天自动取消给妈妈转50", "unparsed_time"),
        ("18号自动售货机给房东转500", "unparsed_time"),
    ],
)
def test_auto_near_misses_stay_unresolved(text, reason):
    result = schedule_candidate(text, {"now": NOW})
    assert result["status"] == "unresolved" and result["reason"] == reason, result
    controller = _controller()
    response = DomesticSession(controller).receive(text)
    assert not response["card"] and response["needs_clarification"]
    assert controller.gateway.bank.execute_count == 0 and not controller.state.user_inputs


def test_auto_is_a_residual_word_only_outside_a_phrase():
    assert residual_time("自动给房东转2000块")
    assert schedule_candidate("给房东转2000块", {"now": NOW}) == {"status": "none"}


def test_q04_reaches_the_unsigned_monthly_card_from_the_form_preparer():
    controller = _controller()
    before = H(controller.state)
    response = DomesticSession(controller).receive(Q04)
    card = response["card"]
    assert response["status"] == "awaiting_approval" and response["buttons"]
    assert card["kind"] == "schedule_transfer" and card["amount"] == "2000/1"
    assert card["objects"]["contact_ref"] == "CONTACT_LANDLORD"
    assert card["spa"]["schedule"] == {"type": "monthly", "day": 5}
    assert card["spa"]["max_runs"] == 3
    assert response["payload"]["schedule_echo"]["phrase"] == "每月5号"
    assert H(controller.state) == before
    assert controller.gateway.bank.execute_count == 0 and not controller.state.user_inputs


@pytest.mark.parametrize(
    "text,participants,collect",
    [
        (Q05, ["CONTACT_CHEN", "CONTACT_LIN_A"], "200/1"),
        (
            "和室友、同学聚餐一共花了300元，帮我发起AA收款",
            ["CONTACT_CHEN", "CONTACT_LIN_A"],
            "200/1",
        ),
        ("上周五和室友一起吃饭花了三百元，帮我AA一下", ["CONTACT_CHEN"], "150/1"),
        ("前天跟同学看电影花了60块，发个AA", ["CONTACT_LIN_A"], "30/1"),
        ("刚才我和室友打车总共花了80元。请AA", ["CONTACT_CHEN"], "40/1"),
    ],
)
def test_narrative_split_bill_reaches_the_named_card(text, participants, collect):
    controller = _controller()
    result = aa_candidate(text, _context(controller, text))
    assert result["status"] == "complete", result
    assert result["participants"] == participants and result["collect_total"] == collect
    response = DomesticSession(controller).receive(text)
    assert response["status"] == "awaiting_approval"
    assert response["card"]["kind"] == "aa_request"
    assert response["card"]["objects"]["participant_refs"] == participants
    assert response["card"]["amount"] == collect
    assert controller.gateway.bank.execute_count == 0 and not controller.state.user_inputs


@pytest.mark.parametrize(
    "text,reason",
    [
        # A future or extra time word anywhere: chat does not schedule collections.
        ("明天和室友、同学聚餐一共花300元，帮我发起AA收款", "time_not_supported"),
        ("昨天和室友、同学聚餐一共花了300元，明天帮我发起AA收款", "time_not_supported"),
        ("昨天和室友、同学聚餐一共花了300元，帮我发起AA收款，下周一收", "time_not_supported"),
        ("昨天下午和室友、同学聚餐一共花了300元，帮我发起AA收款", "time_not_supported"),
        ("下周和室友、同学聚餐一共花了300元，帮我发起AA收款", "time_not_supported"),
        # Not past tense (no 了), or an extra clause: outside the closed narrative.
        ("昨天和室友、同学聚餐一共花300元，帮我发起AA收款", "time_not_supported"),
        ("昨天和室友、同学聚餐一共花了300元，每人100，帮我发起AA", "time_not_supported"),
        ("昨天和室友、同学聚餐一共花了300元，帮我发起AA收款，我们四个人", "time_not_supported"),
        # Unknown or ambiguous people are never guessed.
        ("昨天和室友、小王聚餐一共花了300元，帮我发起AA收款", "time_not_supported"),
        ("昨天和Lin聚餐一共花了300元，帮我发起AA收款", "ambiguous"),
        ("上次和室友、同学聚餐一共花了300元，帮我发起AA收款", "participants"),
        # A finished split, a per-person phrase or an uneven share.
        ("昨天和室友、同学一起AA了300元", "time_not_supported"),
        ("和室友、同学聚餐一共花了300元，每人100，帮我发起AA", "per_person_phrase"),
        ("昨天和室友、同学聚餐一共花了100元，帮我发起AA收款", "uneven"),
        ("昨天和室友、同学聚餐一共花了300元，帮我发起AA收款", None),
    ],
)
def test_narrative_near_misses_are_unresolved(text, reason):
    controller = _controller()
    result = aa_candidate(text, _context(controller, text))
    if reason is None:
        assert result["status"] == "complete"
        return
    assert result["status"] == "unresolved" and result["reason"] == reason, result
    response = DomesticSession(controller).receive(text)
    assert not response["card"] and response["needs_clarification"]
    assert controller.gateway.bank.execute_count == 0 and not controller.state.user_inputs


@pytest.mark.parametrize(
    "text",
    [
        "昨天室友付了300元，帮我和同学发起AA",
        "昨天室友垫付了300元，我和室友、同学聚餐，帮我发起AA收款",
        "昨天和室友、同学聚餐一共花了300元，室友买单，帮我发起AA收款",
        "昨天和室友、同学聚餐一共花了300元，帮我转给室友AA款",
    ],
)
def test_narrative_never_collects_from_someone_who_paid(text):
    controller = _controller()
    assert aa_candidate(text, _context(controller, text)) == {"status": "none"}
    response = DomesticSession(controller).receive(text)
    assert not response["card"]
    assert controller.gateway.bank.execute_count == 0


def test_narrative_headcount_is_completed_from_the_verified_roster_p2():
    controller = _controller()
    chat = DomesticSession(controller)
    source = chat.receive("昨天我们四个人聚餐一共花了460元，帮我发起AA收款")
    assert source["status"] == "awaiting_device_selection" and not source["card"]
    assert source["payload"]["aa"] == {"participants": 4, "per_person": "115/1"}
    assert source["payload"]["aa_parse"] == {"status": "unresolved", "reason": "participants"}
    chosen = ["CONTACT_CHEN", "CONTACT_LANDLORD", "CONTACT_LIN_A"]
    completed = chat.prepare_aa(source["response_id"], chosen)
    assert completed["card"]["kind"] == "aa_request" and completed["card"]["amount"] == "345/1"
    assert completed["card"]["objects"]["participant_refs"] == chosen
    # A headcount narrative with a future time word is not offered the roster card.
    timed = chat.receive("昨天我们四个人聚餐一共花了460元，明天帮我发起AA收款")
    assert timed["payload"]["aa_parse"]["reason"] == "time_not_supported"
    with pytest.raises(ValueError):
        chat.prepare_aa(timed["response_id"], chosen)
    assert controller.gateway.bank.execute_count == 0
