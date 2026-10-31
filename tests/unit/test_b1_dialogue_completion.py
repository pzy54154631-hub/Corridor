"""B1: finish three services in chat.

1. Cancelling a subscription: a request that names one of the customer's own active debit
   authorizations (its amount, its synthetic merchant name, its identifier, or the one item
   a previous reply displayed) yields the subscription form's own yellow card in chat; the
   device gesture signs and executes it. A request that names none lists the candidates in
   chat and waits; it never guesses.
2. Purchase and redemption: the chat fills the product and amount slots across turns.
   A complete purchase is shown for review; only the device gesture signs the preference,
   through the wealth page's own preparer, and the red card then needs MFA and a final
   approval exactly as on the wealth page. A redemption names one of the customer's own
   holdings and uses the redemption form's own preparer.
3. Both wealth requests and the cancellation pass through the S5 negation gate: a negated
   request yields no card, no review and no signed input.
"""

from fractions import Fraction

import pytest

from corridor.agent.domestic_intent import negation_hold, recognize
from corridor.agent.domestic_session import DomesticSession
from corridor.channels.im import IMAdapter
from corridor.crypto.jcs import H
from corridor.ui.controller import DemoController, demo_state


def _session(*, showcase=False):
    controller = DemoController(initial_state=demo_state(showcase=showcase))
    controller.start_session()
    return DomesticSession(controller)


def _quiet(session):
    controller = session.controller
    return (
        controller.gateway.bank.execute_count,
        len(controller.state.user_inputs),
        H(controller.state.subscriptions),
        H(controller.state.positions),
    )


# --- 1. Cancelling a subscription -------------------------------------------------------


def test_naming_the_amount_yields_the_yellow_cancellation_card_and_confirm_executes():
    session = _session()
    before = _quiet(session)
    response = session.receive("把每月扣49块的那个会员取消掉")
    assert response["intent"] == "subscription_cancel"
    assert response["status"] == "awaiting_approval"
    assert not response["needs_clarification"]
    assert response["card"]["kind"] == "cancel_subscription"
    assert response["card"]["objects"]["authorization_ref"] == "S1"
    assert response["tier"] == "yellow"
    assert [button["id"] for button in response["buttons"]] == ["confirm"]
    # Receiving prepares an unsigned card only.
    assert _quiet(session) == before
    settled = session.confirm(response["response_id"])
    assert settled["status"] == "settled"
    assert session.controller.state.subscriptions["S1"].status == "cancelled"
    assert session.controller.gateway.bank.execute_count == 1


@pytest.mark.parametrize(
    "text",
    ["取消会员服务A", "帮我把会员服务A的自动扣费关掉", "取消订阅S1", "每月49元的会员不想续了"],
)
def test_a_merchant_name_identifier_or_amount_selects_the_one_authorization(text):
    response = _session().receive(text)
    assert response["intent"] == "subscription_cancel", text
    assert response["card"]["kind"] == "cancel_subscription", text
    assert response["card"]["objects"]["authorization_ref"] == "S1", text


@pytest.mark.parametrize(
    "text", ["取消订阅", "把视频会员退了", "把自动扣费关掉", "会员不要续了", "不想续会员了"]
)
def test_a_request_naming_no_authorization_lists_the_candidates_in_chat(text):
    session = _session()
    before = _quiet(session)
    response = session.receive(text)
    assert response["intent"] == "subscription_cancel", text
    assert response["status"] == "clarify" and response["needs_clarification"], text
    assert not response["card"] and response["buttons"] == [], text
    assert response["payload"]["subscription_choices"] == ["S1"], text
    assert "49" in response["message"], text
    assert _quiet(session) == before


def test_choosing_from_the_listed_candidates_yields_the_card_in_chat():
    session = _session()
    listed = session.receive("取消订阅")
    chosen = session.prepare_subscription_cancel(listed["response_id"], "S1")
    assert chosen["response_id"] != listed["response_id"]
    assert chosen["card"]["kind"] == "cancel_subscription"
    assert chosen["card"]["objects"]["authorization_ref"] == "S1"
    # Idempotent per choice; the source reply is never rewritten.
    assert session.prepare_subscription_cancel(listed["response_id"], "S1") == chosen
    assert session.response(listed["response_id"])["status"] == "clarify"
    with pytest.raises(ValueError):
        session.prepare_subscription_cancel(listed["response_id"], "S2")
    assert session.confirm(chosen["response_id"])["status"] == "settled"


def test_a_follow_up_names_the_single_listed_candidate():
    session = _session()
    session.receive("取消订阅")
    response = session.receive("就取消那个")
    assert response["intent"] == "subscription_cancel"
    assert response["card"]["objects"]["authorization_ref"] == "S1"


def test_an_amount_that_matches_no_authorization_lists_them_and_prepares_nothing():
    session = _session()
    response = session.receive("取消每月29块的那个会员")
    assert response["status"] == "clarify"
    assert not response["card"]
    assert response["payload"]["subscription_parse"]["status"] == "no_match"
    assert response["payload"]["subscription_choices"] == ["S1"]


def test_the_listing_flow_of_the_frozen_suite_is_unchanged():
    session = _session()
    listing = session.receive("查询订阅")
    session.confirm(listing["response_id"])
    response = session.receive("那个会员停掉")
    assert response["card"]["objects"]["authorization_ref"] == "S1"
    assert session.confirm(response["response_id"])["status"] == "settled"


def test_without_an_active_authorization_nothing_is_offered():
    session = _session()
    card = session.receive("把每月扣49块的那个会员取消掉")
    session.confirm(card["response_id"])
    again = session.receive("取消订阅")
    assert again["status"] == "clarify"
    assert again["payload"]["subscription_choices"] == []
    assert not again["card"]


@pytest.mark.parametrize(
    "text",
    [
        "先别取消会员",
        "不要取消订阅",
        "我不想取消那个49块的会员",
        "取消会员，算了",
        "别把会员服务A退了",
        "不要取消续费",
    ],
)
def test_a_negated_cancellation_is_held_without_a_card(text):
    session = _session()
    before = _quiet(session)
    response = session.receive(text)
    assert response["intent"] == "clarify", text
    assert response["payload"]["negation"]["words"], text
    assert not response["card"] and response["buttons"] == [], text
    assert negation_hold(text), text
    assert _quiet(session) == before


@pytest.mark.parametrize("text", ["会员不要续了", "不想续会员了", "别续费了", "把会员不再续订"])
def test_a_cancel_phrase_built_on_a_negation_is_not_held(text):
    assert recognize(text).intent == "subscription_cancel", text
    assert negation_hold(text) == (), text


def test_the_im_channel_lists_the_candidates_and_confirms_the_card():
    session = _session()
    adapter = IMAdapter(session)
    listed = adapter.receive("取消订阅")
    assert "49" in listed["text"]
    card = adapter.receive("把每月扣49块的那个会员取消掉")
    assert card["facts_complete"] and card["buttons"]
    assert adapter.click("confirm", card["response_id"])["status"] == "settled"


# --- 2. Purchase ----------------------------------------------------------------------


def test_a_complete_purchase_is_reviewed_in_chat_and_signed_only_by_the_device_gesture(
    monkeypatch,
):
    session = _session()
    controller = session.controller
    calls = []
    original = controller.preview_investment

    def watched(*args, **kwargs):
        calls.append((args, kwargs))
        return original(*args, **kwargs)

    monkeypatch.setattr(controller, "preview_investment", watched)
    before = _quiet(session)
    review = session.receive("帮我申购2000元稳健30天持有")
    assert review["intent"] == "investment"
    assert review["status"] == "awaiting_input" and not review["needs_clarification"]
    assert review["payload"]["purchase"]["product_ref"] == "P_A"
    assert review["payload"]["purchase"]["amount"] == "2000/1"
    assert not review["card"]
    assert [button["id"] for button in review["buttons"]] == ["confirm"]
    # Receiving signs nothing and does not call the wealth page's signing preparer.
    assert calls == [] and _quiet(session) == before
    card = session.confirm(review["response_id"])
    assert len(calls) == 1
    assert card["status"] == "waiting_mfa"
    assert card["card"]["kind"] == "purchase_product"
    assert card["card"]["tier"] == "red"
    assert card["card"]["amount"] == "2000/1"
    assert card["card"]["objects"]["product_ref"] == "P_A"
    assert controller.gateway.bank.execute_count == 0
    verified = session.verify_mfa(review["response_id"])
    assert verified["status"] == "mfa_verified"
    settled = session.confirm(review["response_id"])
    assert settled["status"] == "settled"
    assert controller.gateway.bank.execute_count == 1
    [position] = controller.state.positions.values()
    assert position.product_ref == "P_A" and position.principal_amount == Fraction(2000)


def test_the_self_test_purchase_names_its_product_and_the_kernel_refuses_the_amount():
    session = _session()
    review = session.receive("帮我申购2万元那款30天持有的稳健理财")
    assert review["payload"]["purchase"] == {
        "product_ref": "P_A",
        "amount": "20000/1",
        "source_ref": "A_CURRENT",
        "objective": "net_return",
    }
    refused = session.confirm(review["response_id"])
    assert refused["status"] == "rejected"
    assert "BAL-AVAILABLE" in refused["rule_ids"]
    assert session.controller.gateway.bank.execute_count == 0


def test_a_missing_product_is_asked_in_chat_and_filled_on_the_next_turn():
    session = _session()
    first = session.receive("买两万元理财")
    assert first["status"] == "awaiting_device_selection"
    parse = first["payload"]["purchase_parse"]
    assert parse["missing"] == ["product"] and parse["amount"] == "20000/1"
    assert "稳健30天持有" in first["message"]
    second = session.receive("稳健30天持有那款")
    assert second["intent"] == "investment"
    assert second["status"] == "awaiting_input"
    assert second["payload"]["purchase"]["product_ref"] == "P_A"
    assert second["payload"]["purchase"]["amount"] == "20000/1"


def test_a_missing_amount_is_asked_in_chat_and_filled_on_the_next_turn():
    session = _session()
    first = session.receive("我想买灵活10天理财")
    assert first["payload"]["purchase_parse"]["missing"] == ["amount"]
    assert first["payload"]["purchase_parse"]["product_ref"] == "P_B"
    second = session.receive("3000元")
    assert second["payload"]["purchase"] == {
        "product_ref": "P_B",
        "amount": "3000/1",
        "source_ref": "A_CURRENT",
        "objective": "net_return",
    }


def test_an_unrelated_turn_drops_the_unfinished_purchase():
    session = _session()
    session.receive("买两万元理财")
    session.receive("查一下余额")
    response = session.receive("稳健30天持有那款")
    assert response["status"] != "awaiting_input"
    assert "purchase" not in response["payload"]


def test_a_holding_period_shared_by_two_products_never_selects_one():
    response = _session().receive("10天的理财买2000元")
    assert response["status"] == "awaiting_device_selection"
    parse = response["payload"]["purchase_parse"]
    assert parse["missing"] == ["product"] and parse["product_ref"] is None
    # Three catalog products hold for 10 days (the synthetic catalog's own attributes).
    assert parse["candidates"] == ["P_B", "P_C", "P_D"]


def test_a_family_word_alone_never_selects_a_product():
    # 稳健 also reads as a risk appetite; even when it narrows the catalogue to one
    # product, the chat asks instead of selecting it.
    response = _session().receive("想买稳健的产品")
    assert response["status"] == "awaiting_device_selection"
    parse = response["payload"]["purchase_parse"]
    assert "product" in parse["missing"] and parse["product_ref"] is None


def test_the_frozen_wealth_cases_keep_their_outcomes():
    session = _session()
    session.receive("想比较理财产品")
    reference = session.receive("那只买两万")
    assert reference["status"] == "awaiting_device_selection"
    assert reference["needs_clarification"] and reference["tier"] is None
    for text in ("五万闲钱，稳一点", "有笔闲钱一直没安排", "有五万闲钱想理财"):
        response = _session().receive(text)
        assert response["status"] == "awaiting_device_selection", text
        assert "purchase" not in response["payload"], text


@pytest.mark.parametrize(
    "text",
    [
        "我有5万闲钱，想买点稳健的理财，有什么推荐",
        "我买的理财现在收益多少了",
        "稳健30天持有和灵活10天理财买哪个好？",
    ],
)
def test_advice_questions_and_past_purchases_keep_the_advisory_path(text):
    response = _session().receive(text)
    assert response["intent"] == "investment", text
    assert response["status"] == "awaiting_device_selection", text
    assert "purchase_parse" not in response["payload"], text
    assert "purchase" not in response["payload"], text


def test_the_im_channel_shows_the_review_and_its_confirm_signs_the_preference():
    session = _session()
    adapter = IMAdapter(session)
    review = adapter.receive("帮我申购2000元稳健30天持有")
    assert review["facts_complete"] and review["buttons"]
    assert "2,000" in review["text"]
    card = adapter.click("confirm", review["response_id"])
    assert card["status"] == "waiting_mfa"
    assert card["card"]["kind"] == "purchase_product"


# --- 2b. Redemption -------------------------------------------------------------------


def test_redeeming_the_one_holding_yields_the_yellow_redemption_card():
    # C1 (review of B1, item p2 E): the one holding no longer
    # stands in for the quantity, so the request names it ("全部"); without a quantity the
    # chat asks (tests/unit/test_c1_dialogue_review.py).
    session = _session(showcase=True)
    before = _quiet(session)
    response = session.receive("把我持有的那笔理财全部赎回")
    assert response["intent"] == "investment"
    assert response["card"]["kind"] == "redeem_product"
    assert response["card"]["objects"]["position_ref"] == "POSITION_UI_DEMO"
    assert response["card"]["amount"] == "500/1"
    assert response["tier"] == "yellow"
    assert _quiet(session) == before
    assert session.confirm(response["response_id"])["status"] == "settled"
    assert session.controller.gateway.bank.execute_count == 1


def test_a_named_amount_redeems_part_of_the_holding():
    response = _session(showcase=True).receive("赎回200元理财")
    assert response["card"]["amount"] == "200/1"


def test_without_a_holding_redemption_says_so_and_prepares_nothing():
    session = _session()
    before = _quiet(session)
    response = session.receive("我想赎回理财")
    assert response["status"] == "clarify" and response["needs_clarification"]
    assert response["payload"]["redemption_parse"]["status"] == "none"
    assert not response["card"] and _quiet(session) == before


# --- 3. The negation gate on wealth requests ----------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "不要帮我申购稳健30天持有2000元",
        "先别买理财",
        "我不想赎回理财",
        "申购2000元稳健30天持有，算了",
        "取消申购理财",
    ],
)
def test_a_negated_purchase_or_redemption_is_held(text):
    session = _session(showcase=True)
    before = _quiet(session)
    response = session.receive(text)
    assert response["intent"] == "clarify", text
    assert response["payload"]["negation"]["words"], text
    assert not response["card"] and response["buttons"] == [], text
    assert "purchase" not in response["payload"], text
    assert _quiet(session) == before


def test_a_polite_tag_does_not_hold_a_purchase():
    response = _session().receive("帮我申购2000元稳健30天持有，可以吗")
    assert response["status"] == "awaiting_input"


def test_a_held_turn_drops_the_unfinished_purchase():
    session = _session()
    session.receive("买两万元理财")
    held = session.receive("不买了")
    assert held["intent"] == "clarify"
    response = session.receive("稳健30天持有那款")
    assert "purchase" not in response["payload"]


def test_redeeming_a_product_the_customer_does_not_hold_redeems_nothing():
    # Review fix: a named product that matches no holding must never fall back to the one
    # holding the customer does have (that would be a card for a different product).
    session = _session(showcase=True)
    before = _quiet(session)
    response = session.receive("赎回灵活10天理财")
    assert not response["card"] and response["buttons"] == []
    assert response["status"] == "awaiting_device_selection"
    assert response["payload"]["redemption_parse"]["status"] == "no_match"
    assert _quiet(session) == before
