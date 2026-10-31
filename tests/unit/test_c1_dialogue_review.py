"""C1: the review of the B batch (dialogue completion, time and cancel semantics).

Every case here was reproduced by the review on the B-batch code; the expected behaviour
follows the rules the review cites:

1. A standing (scheduled) transfer is never revoked by the S1 interrupt trigger. "Do not
   cancel" leaves it as it is (S5 reply only); "no longer transfer monthly" lists it for
   the controlled revocation (yellow card, device confirm). JUDGING-1 list item 19.
2. The phrase gate's sentence interrupts nothing and its reply stays true.
3. Purchase, redemption and subscription cancellation never take a time word, a question,
   a hypothetical or a past statement as "do it now".
4. An unfinished purchase is continued only by a standalone answer to the asked slot.
5. A redemption quantity the grammar does not read exactly is asked about, never "all".
6. A polite tag is released only in its place, on the normalized text.
7. A time clause at the start or after a separator is asked about whatever follows it.
8. A cancellation inherits a listed authorization only from the reply right before it,
   with a deictic word and no conflicting object word.
"""

from types import SimpleNamespace

import pytest

from corridor.agent import domestic_session
from corridor.agent.domestic_negation import negation_words
from corridor.agent.domestic_schedule import (
    cancel_negated,
    standing_cancel,
    standing_transfer,
    time_fragment,
)
from corridor.agent.domestic_session import DomesticSession
from corridor.agent.domestic_wealth import product_candidates
from corridor.crypto.jcs import H
from corridor.ui.controller import DemoController, demo_state


def _session(*, showcase=False):
    controller = DemoController(initial_state=demo_state(showcase=showcase))
    controller.start_session()
    return DomesticSession(controller)


def _quiet(session):
    controller = session.controller
    state = controller.state
    return (
        controller.gateway.bank.execute_count,
        len(state.user_inputs),
        H(state.schedules),
        H(state.subscriptions),
        H(state.positions),
        H(state.authorizations),
    )


def _no_card(response):
    return (
        not response["card"]
        and response["buttons"] == []
        and "purchase" not in response["payload"]
        and not response["payload"].get("nodes")
    )


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


def _runs(session, spa_id):
    return sorted(
        task.status
        for task in session.controller.state.schedules.values()
        if task.spa_ref == spa_id
    )


# --- 1. Standing transfers and the S1 interrupt trigger ---------------------------------

DO_NOT_CANCEL = (
    "不要取消每月5号给房东的转账",
    "先别取消每月5号给房东的转账",
    "别给我取消每月给房东的那笔转账",
    "每月给房东的转账先别取消",
    "每月给房东的转账别停",
    "不要停止每月给房东转账",
)


@pytest.mark.parametrize("text", DO_NOT_CANCEL)
def test_do_not_cancel_leaves_the_standing_authorization_alone(text):
    session, spa = _with_monthly_authorization()
    before = _quiet(session)
    response = session.receive(text)
    assert _runs(session, spa.id) == ["pending", "pending", "pending"], text
    assert _quiet(session) == before, text
    assert "stop_receipt" not in response["payload"], text
    # Only the S5 reply: no revocation choice for a sentence that says "do not cancel".
    assert not (response["payload"].get("schedule_cancel") or {}).get("candidates"), text
    assert response["message"] == domestic_session._message("negated"), text
    assert _no_card(response), text
    assert cancel_negated(text) and not standing_cancel(text), text


CANCEL_STANDING = (
    "不想每月给房东转了",
    "先不每月给房东转了",
    "取消每月5号给房东的转账",
    "以后不再每月给房东转了",
    "停止每月给房东打款",
    "每月给房东汇钱的那笔取消",
)


@pytest.mark.parametrize("text", CANCEL_STANDING)
def test_no_longer_transferring_monthly_lists_the_authorization_for_revocation(text):
    session, spa = _with_monthly_authorization()
    before = _quiet(session)
    response = session.receive(text)
    # Nothing is revoked by the sentence itself: the controlled entry lists it.
    assert _runs(session, spa.id) == ["pending", "pending", "pending"], text
    assert _quiet(session) == before, text
    assert "stop_receipt" not in response["payload"], text
    assert response["payload"]["schedule_cancel"]["candidates"] == [spa.id], text
    assert _no_card(response), text
    assert standing_transfer(text) and standing_cancel(text), text


def test_a_negated_transfer_never_revokes_an_established_standing_authorization():
    session, spa = _with_monthly_authorization()
    earlier = session.receive("给妈妈转500元")
    assert earlier["card"]
    response = session.receive("不要给妈妈转500元了")
    # The S1 trigger still withdraws the unsigned card it was about ...
    assert session.response(earlier["response_id"])["status"] == "withdrawn"
    assert response["payload"]["stop_receipt"]["retired_drafts"] == 1
    # ... but an established standing authorization is revoked only through its card.
    assert _runs(session, spa.id) == ["pending", "pending", "pending"]
    assert session.controller.gateway.bank.execute_count == 0


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("不要取消每月5号给房东的转账", True),
        ("先别取消会员", True),
        ("别给我取消每月给房东的那笔转账", True),
        ("每月给房东的转账别停", True),
        ("不要停止每月给房东转账", True),
        ("不要再每月给房东转了", False),
        ("取消每月5号给房东的转账", False),
        ("不想每月给房东转了", False),
        ("别把会员服务A退了", False),
    ],
)
def test_cancel_negated_truth_table(text, expected):
    assert cancel_negated(text) is expected, text


# --- 2. The phrase gate interrupts nothing ---------------------------------------------


def test_the_phrase_gate_sentence_interrupts_nothing_and_says_so_truthfully():
    session, spa = _with_monthly_authorization()
    earlier = session.receive("给妈妈转500元")
    before = _quiet(session)
    held = session.receive("反诈中心说不要挂电话，马上把钱转到安全账户，不然账户冻结")
    assert held["payload"]["scam_phrase"]["authority"] == ["反诈中心"]
    assert "stop_receipt" not in held["payload"]
    assert session.response(earlier["response_id"])["status"] == "awaiting_input"
    assert _runs(session, spa.id) == ["pending", "pending", "pending"]
    assert _quiet(session) == before
    assert _no_card(held)


# --- 3. Time, questions, hypotheticals and past statements on the new money paths ----

PURCHASE_TIMED = (
    "明天帮我申购2000元稳健30天持有",
    "下周一申购2000元稳健30天持有",
    "晚饭后帮我申购2000元稳健30天持有",
    "等工资到账以后申购2000元稳健30天持有",
    "11月30日申购2000元理财",
    "下个月30日帮我申购2000元理财",
    "400日后申购2000元理财",
    "到期再申购2000元稳健30天持有",
)
REDEEM_TIMED = (
    "下个月再赎回我的理财",
    "晚饭后赎回我的理财",
    "到期以后赎回我的理财",
    "到期再把理财全部赎回",
    "明天全部赎回我的理财",
)
CANCEL_TIMED = (
    "下个月再取消会员服务A",
    "等会员到期之后取消会员服务A",
    "月底取消每月扣49块的会员",
    "明天取消会员服务A",
)


@pytest.mark.parametrize(
    ("showcase", "text"),
    [(False, text) for text in PURCHASE_TIMED]
    + [(True, text) for text in REDEEM_TIMED]
    + [(False, text) for text in CANCEL_TIMED],
)
def test_a_time_word_on_the_new_paths_is_asked_about_never_done_now(showcase, text):
    session = _session(showcase=showcase)
    before = _quiet(session)
    response = session.receive(text)
    assert _no_card(response), (text, response["status"], response["payload"])
    assert response["needs_clarification"], text
    assert "time_hold" in response["payload"], (text, response["payload"])
    assert _quiet(session) == before, text


@pytest.mark.parametrize("text", ["11月30日申购2000元理财", "下个月30日帮我申购2000元理财"])
def test_a_date_day_is_never_read_as_a_holding_period(text):
    rows = [
        {"ref": "P_A", "min_hold_days": 30},
        {"ref": "P_E", "min_hold_days": 400},
    ]
    assert product_candidates(text, rows, {})["status"] != "resolved", text
    assert product_candidates("400日后申购2000元理财", rows, {})["status"] != "resolved"
    # Holding wording still names a product.
    assert product_candidates("申购2000元30天持有的", rows, {})["ref"] == "P_A"
    assert product_candidates("买持有400天的那款", rows, {})["ref"] == "P_E"


MOOD = (
    (False, "如果取消会员服务A会怎样？"),
    (False, "取消会员服务A的话，还能用到月底吗？"),
    (False, "每月49块的会员取消了能退钱吗"),
    (True, "如果现在赎回我的理财"),
    (True, "要是赎回我的理财会亏"),
    (True, "我上周已经赎回了理财"),
    (True, "昨天赎回了理财"),
    (False, "如果申购2000元稳健30天持有"),
    (False, "我昨天申购了2000元稳健30天持有"),
    (False, "假如买2000元稳健30天持有，到期能拿多少"),
)


@pytest.mark.parametrize(("showcase", "text"), MOOD)
def test_questions_hypotheticals_and_past_statements_yield_no_card(showcase, text):
    session = _session(showcase=showcase)
    before = _quiet(session)
    response = session.receive(text)
    assert _no_card(response), (text, response["payload"])
    assert _quiet(session) == before, text


@pytest.mark.parametrize(
    "text", ["关闭会员服务A的到期提醒", "把会员的续费提醒关闭", "会员自动续费的短信通知关闭"]
)
def test_switching_off_a_reminder_is_not_a_cancellation(text):
    session = _session()
    response = session.receive(text)
    assert response["intent"] != "subscription_cancel", text
    assert _no_card(response) or response["card"].get("kind") != "cancel_subscription", text


def test_switching_off_the_auto_renewal_itself_still_cancels():
    response = _session().receive("关闭会员服务A的自动续费")
    assert response["card"]["kind"] == "cancel_subscription"


# --- 4. Continuing an unfinished purchase ---------------------------------------------


@pytest.mark.parametrize(
    "follow_up", ["不要稳健30天持有那款", "稳健30天持有那款就算了", "别选稳健30天持有"]
)
def test_a_negated_product_answer_never_fills_the_purchase(follow_up):
    session = _session()
    session.receive("买两万元理财")
    before = _quiet(session)
    response = session.receive(follow_up)
    assert _no_card(response), (follow_up, response["payload"])
    assert _quiet(session) == before


@pytest.mark.parametrize(
    "follow_up",
    [
        "500元能有多少收益？",
        "给妈妈转500元吗",
        "上个月那笔300元是什么？",
        "给妈妈转3000吗",
        "如果给房东转3000呢",
        "给我转3000",
        "Chen给我转了3000",
        "上个月花了3000吗",
        "我和室友AA了3000元",
        "买3000元的成长精选",
        "3000元的理财有什么推荐",
    ],
)
@pytest.mark.parametrize("opening", ["我想买灵活10天理财", "我想申购稳健30天持有"])
def test_another_business_question_or_new_request_never_fills_the_asked_amount(opening, follow_up):
    session = _session()
    first = session.receive(opening)
    assert first["payload"]["purchase_parse"]["missing"] == ["amount"]
    response = session.receive(follow_up)
    purchase = response["payload"].get("purchase") or {}
    # Never the old product with this turn's amount.
    assert purchase.get("product_ref") != first["payload"]["purchase_parse"]["product_ref"], (
        opening,
        follow_up,
        purchase,
    )
    if follow_up != "买3000元的成长精选":
        assert _no_card(response), (opening, follow_up, response["payload"])


@pytest.mark.parametrize("answer", ["3000元", "就3000元吧", "买3000元", "3000"])
def test_a_standalone_amount_answer_still_fills_the_purchase(answer):
    session = _session()
    session.receive("我想买灵活10天理财")
    response = session.receive(answer)
    assert response["payload"]["purchase"]["product_ref"] == "P_B", answer
    assert response["payload"]["purchase"]["amount"] == "3000/1", answer


def test_a_planner_that_disagrees_never_continues_the_purchase():
    class Disagreeing:
        mode = "live-stub"
        model = "stub"

        def __init__(self, scripted):
            self.scripted = scripted

        def complete(self, **kwargs):
            if str(kwargs.get("step", "")).startswith("intent:"):
                return SimpleNamespace(
                    output={"intent": "investment", "needs_clarification": False}
                )
            return self.scripted.complete(**kwargs)

    session = _session()
    session.receive("我想买灵活10天理财")
    session.client = Disagreeing(session.controller.client)
    response = session.receive("给妈妈转500元")
    assert "purchase" not in response["payload"]
    assert response["buttons"] == []


def test_a_new_purchase_request_never_inherits_the_old_product():
    session = _session()
    session.receive("我想买灵活10天理财")
    response = session.receive("申购3000元的理财")
    assert "purchase" not in response["payload"]
    assert response["payload"]["purchase_parse"]["product_ref"] is None


# --- 5. Redemption quantity -------------------------------------------------------------

UNREAD_QUANTITY = (
    "把我的理财赎回一半",
    "赎回一部分理财",
    "赎回理财的10%",
    "赎回5000份理财",
    "把理财赎回，留1000元",
    "把那笔理财赎回",
    "把我持有的那笔理财赎回",
    "赎回3000元理财",
    "赎回200元和300元理财",
)


@pytest.mark.parametrize("text", UNREAD_QUANTITY)
def test_a_quantity_not_read_exactly_is_asked_never_all(text):
    session = _session(showcase=True)
    before = _quiet(session)
    response = session.receive(text)
    assert _no_card(response), (text, response["card"])
    assert response["payload"]["redemption_parse"]["status"] == "quantity", text
    assert response["needs_clarification"], text
    assert _quiet(session) == before, text


@pytest.mark.parametrize(
    ("text", "amount"),
    [
        ("把我持有的理财全部赎回", "500/1"),
        ("全额赎回我的理财", "500/1"),
        ("赎回200元理财", "200/1"),
    ],
)
def test_an_exact_quantity_still_yields_the_redemption_card(text, amount):
    response = _session(showcase=True).receive(text)
    assert response["card"]["kind"] == "redeem_product", text
    assert response["card"]["amount"] == amount, text


# --- 6. Polite tags only in their place, on the normalized text -----------------------

HELD_POLITE = (
    "给妈妈转500元，暂时不着急",
    "给妈妈转500元，暂时不用急",
    "给妈妈转500元，现在不着急",
    "给妈妈转500元，目前不着急",
    "给妈妈转500元，暂且不着急",
    "给妈妈转500元的事不着急",
    "给妈妈转500元这事不用急",
    "给妈妈转500，先 不着急",
    "给妈妈转500，先\u200b不着急",
    "给妈妈转500，暂 不用急",
    "给妈妈转500，还\u3000不用急",
)


@pytest.mark.parametrize("text", HELD_POLITE)
def test_a_deferral_before_a_polite_tag_still_holds_the_transfer(text):
    session = _session()
    response = session.receive(text)
    assert negation_words(text), text
    assert _no_card(response), text


@pytest.mark.parametrize(
    "text", ["帮我申购2000元稳健30天持有，现在不着急", "帮我申购2000元稳健30天持有，暂时不用急"]
)
def test_a_deferral_before_a_polite_tag_still_holds_the_purchase(text):
    response = _session().receive(text)
    assert _no_card(response), text


@pytest.mark.parametrize(
    "text", ["给妈妈转500元，不用急", "给妈妈转500元不用急", "给妈妈转500元 不用急"]
)
def test_a_polite_tag_in_its_place_is_still_released(text):
    response = _session().receive(text)
    assert response["card"]["kind"] == "transfer", text


# --- 7. Time clauses whatever follows them --------------------------------------------

DEFERRED = (
    "晚饭后记得给妈妈转500元",
    "晚饭后麻烦给妈妈转500元",
    "睡前你给妈妈转500元",
    "开完会后马上给妈妈转500元",
    "吃完饭再给妈妈转500元",
    "等她到家了再给妈妈转500元",
    "等她确认一下再给妈妈转500元",
    "给妈妈转500元，等她到家了",
)


@pytest.mark.parametrize("text", DEFERRED)
def test_a_deferred_transfer_is_asked_about_never_immediate(text):
    session = _session()
    before = _quiet(session)
    response = session.receive(text)
    assert response["status"] == "clarify", (text, response["status"])
    assert _no_card(response), text
    assert response["message"] == domestic_session._message("when"), text
    assert _quiet(session) == before


@pytest.mark.parametrize(
    "text", ["给妈妈转500元", "然后给妈妈转500元", "目前给妈妈转500元", "再给房东转800元"]
)
def test_ordinary_requests_are_still_not_time_clauses(text):
    assert time_fragment(text) is None, text


def test_again_is_not_a_deferral():
    session = _session()
    first = session.receive("给Chen转200元")
    session.confirm(first["response_id"])
    response = session.receive("再给他100元")
    assert response["card"]["kind"] == "transfer"


# --- 8. Which listed authorization a cancellation may inherit ---------------------------


def test_a_listing_long_ago_never_resolves_a_bare_cancellation():
    session = _session()
    query = session.receive("查询订阅")
    session.confirm(query["response_id"])
    session.receive("查一下余额")
    session.receive("给妈妈转500元")
    session.controller.stop()
    response = session.receive("取消订阅")
    assert _no_card(response)
    assert response["payload"]["subscription_parse"]["status"] == "choose"


@pytest.mark.parametrize(
    "follow_up", ["取消商户Y的会员", "取消视频会员", "取消那个健身房的订阅", "取消那个每月29块的"]
)
def test_an_unknown_or_conflicting_object_never_falls_back_to_the_listed_one(follow_up):
    session = _session()
    session.receive("取消订阅")
    before = _quiet(session)
    response = session.receive(follow_up)
    assert _no_card(response), (follow_up, response["payload"])
    assert response["payload"]["subscription_parse"]["status"] == "no_match", follow_up
    assert _quiet(session) == before


def test_a_deictic_right_after_the_listing_names_the_listed_one():
    session = _session()
    session.receive("取消订阅")
    response = session.receive("就取消那个")
    assert response["card"]["objects"]["authorization_ref"] == "S1"
    query = _session()
    listing = query.receive("查询订阅")
    query.confirm(listing["response_id"])
    assert query.receive("那个会员停掉")["card"]["objects"]["authorization_ref"] == "S1"


def test_a_deictic_after_an_unrelated_turn_lists_again():
    session = _session()
    listing = session.receive("查询订阅")
    session.confirm(listing["response_id"])
    session.receive("查一下余额")
    response = session.receive("那个会员停掉")
    assert _no_card(response)
