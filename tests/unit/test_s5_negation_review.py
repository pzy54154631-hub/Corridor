"""S5 review: phrasings the first negation gate let through, and positives it held.

An independent adversarial pass over PROP-S5-NEG found negated requests that still got a
money-moving card (撤消 / 关了 / 删了 / 结束 / 放弃 / 算啦 / 以后再说, an English word
written right next to Chinese text such as cancel给房东转, a word split by a bidi or other
invisible mark, a traditional 別, a refund cancelled with 取消退回) and positives it held
(，不过 "but", 不错, 特别提醒, 撤销锁定的预算). Every case here is a regression test.
"""

import pytest

from corridor.agent.domestic_intent import negation_hold, recognize
from corridor.agent.domestic_negation import compensation_negation, negation_words
from corridor.agent.domestic_session import DomesticSession
from corridor.agent.extractor import extract_intent
from corridor.ui.controller import DemoController


def _session():
    controller = DemoController()
    controller.start_session()
    return DomesticSession(controller)


def _session_with_stopped_plan():
    session = _session()
    draft = session.receive("生日安排一下")
    session.confirm(
        draft["response_id"], selected_node_ids=[node["id"] for node in draft["payload"]["nodes"]]
    )
    session.receive("停止")
    return session


def _held(session, text):
    controller = session.controller
    before = (controller.gateway.bank.execute_count, len(controller.state.user_inputs))
    response = session.receive(text)
    assert response["intent"] == "clarify", (text, response["intent"], response["status"])
    assert not response["card"] and response["buttons"] == [], text
    assert response["payload"]["negation"]["words"], text
    assert (controller.gateway.bank.execute_count, len(controller.state.user_inputs)) == before
    return response


CHINESE = (
    # Cancel verbs and their colloquial forms.
    "撤消每月5号给房东转2000块",
    "撤消给房东转2000块",
    "撤除给房东转2000块",
    "每月5号给房东转2000块，撤了吧",
    "把每月5号给房东转2000的自动转账撤了",
    "每月5号给房东转2000块那个删了",
    "每月5号给房东转2000块，删了吧",
    "把每月5号给房东转2000的定时转账关了",
    "每月5号给房东转2000块，关停",
    "结束每月5号给房东转2000块",
    "每月5号给房东转2000块的转账，结束掉",
    "解除每月5号给房东转2000块",
    "暂缓每月5号给房东转2000块",
    "每月5号给房东转2000块，先缓一缓",
    "房租先缓缓，每月5号给房东转2000",
    "没有必要每月5号给房东转2000块",
    "放弃每月5号给房东转2000块",
    "退订每月5号给房东转2000块",
    "禁止每月5号给房东转2000块",
    "每月5号给房东转2000块，拉倒吧",
    "每月5号给房东转2000块，罢了",
    "每月5号给房东转2000块，作罢",
    "我没让你每月5号给房东转2000块",
    "我反悔了，每月5号给房东转2000块",
    "收回每月5号给房东转2000块",
    "给房东转2000块，休想",
    "给房东转2000块，没门",
    "给房东转2000块，免谈",
    "明天给妈妈转50元，算啦",
    "明天给妈妈转50元，算咯",
    "明天给妈妈转50元，免啦",
    "给房东转2000块，忘了吧",
    "每月5号给房东转2000块的事忘掉吧",
    "给房东转2000块，就当我没说",
    # Putting it off.
    "给房东转2000块，等等再说",
    "给房东转2000块，以后再说吧",
    "给房东转2000块，改天吧",
    "给房东转2000块，下次吧",
    # A reminder never hides a cancel verb.
    "别忘了撤了每月5号给房东转2000",
    "别忘了把每月5号给房东转2000的定时关了",
    "别忘了结束每月5号给房东转2000",
    # Mixed traditional characters.
    "別每月5号给房东转2000块",
    "別給房东转2000块",
    "撤銷每月5号给房东转2000块",
    "刪除每月5号给房东转2000块",
    "關掉每月5号给房东转2000块",
    "終止给房东转2000块",
    "無需给房东转2000块",
    "沒必要给房东转2000块",
    "作廢给房东转2000块",
    "拒絕给房东转2000块",
    # The formal 毋 (毋须 = 无须).
    "毋须给房东转2000块",
    # Symbols.
    "每月5号给房东转2000块" + chr(0x274C),
    chr(0x1F6AB) + "给房东转2000块",
    # Split bills.
    "我和室友、同学AA 300元，撤了吧",
    "我和室友、同学AA 300元，删了",
    "我和室友、同学AA 300元，放弃",
)
ENGLISH = (
    # Right next to Chinese text or digits, where a regex word boundary does not fall.
    "cancel每月5号给房东转2000块",
    "cancel给房东转2000块",
    "stop给房东转2000块",
    "STOP每月5号给房东转2000块",
    "Ｓｔｏｐ给房东转2000块",
    "ｃａｎｃｅｌ给房东转2000块",
    "don't给房东转2000块",
    "dont给房东转2000块",
    "Don't给Chen转200元",
    "pause每月5号给房东转2000块",
    "never给房东转2000块",
    "NOT给房东转2000块",
    "cancel我和室友、同学AA 300元",
    # Words and forms the first list did not have.
    "每月5号给房东转2000块 nope",
    "每月5号给房东转2000块 No",
    "No 11月20号给房东转2000块",
    "每月5号给房东转2000块 nevermind",
    "每月5号给房东转2000块, pause",
    "I didn’t ask to 给房东转2000块",
    "cancelled: 给房东转2000块",
    "给房东转2000块 (canceled)",
    "abort 给房东转2000块",
    "skip 给房东转2000块",
    "每月5号给房东转2000块 undo",
    "revoke 每月5号给房东转2000块",
    "halt 每月5号给房东转2000块",
    "每月5号给房东转2000块 cancelling",
    "每月5号给房东转2000块 stopped",
    "每月5号给房东转2000块 shouldn't",
    "每月5号给房东转2000块 isn't needed",
    "transfer 200 to Chen. Actually, don't.",
)
# Default-ignorable code points that the first gate did not drop.
INVISIBLE = (0x034F, 0x061C, 0x115F, 0x17B4, 0x180B, 0x2066, 0x2069, 0x206A, 0x3164, 0xFE00)
INVISIBLE += (0xFE0F, 0xFFF9, 0x1D173, 0xE0020, 0x2028, 0x00A0, 0x3000)
# A stray combining mark, a private-use or an unassigned code point does not split one either.
INVISIBLE += (0x0301, 0x20DD, 0xE000, 0x0378)


@pytest.mark.parametrize("text", CHINESE + ENGLISH)
def test_negated_phrasings_never_yield_a_card(text):
    assert negation_words(text), text
    _held(_session(), text)


@pytest.mark.parametrize("code", INVISIBLE)
def test_no_invisible_character_splits_a_negation(code):
    mark = chr(code)
    for text in (
        f"取{mark}消给房东转2000块",
        f"每月5号给房东转2000块，算{mark}了",
        f"撤{mark}销每月5号给房东转2000块",
        f"can{mark}cel给房东转2000块" if code not in (0x2028, 0x00A0, 0x3000) else None,
    ):
        if text is not None:
            assert negation_words(text), ascii(text)
            _held(_session(), text)


@pytest.mark.parametrize(
    "text",
    [
        "妈妈生日计划结束吧",
        "妈妈生日放弃了",
        "妈妈生日计划撤了吧",
        "妈妈生日计划删了",
        "妈妈生日计划关了吧",
        "妈妈生日计划作罢",
        "妈妈生日计划暂缓",
        "妈妈生日计划cancel",
        "cancel妈妈生日计划",
        "今年妈妈生日不过了",
        "今年妈妈生日不过",
    ],
)
def test_negated_birthday_plans_draft_nothing(text):
    session = _session()
    _held(session, text)
    assert not session.controller._plans


def test_conversation_page_parser_reads_the_same_words():
    """The page parses privacy-tokenized text (一 reads as 1); the gate still holds it."""
    for text in ("每月5号给房东转2000块，先缓一缓", "cancel给Chen转200元", "給Chen转200元，別转"):
        controller = DemoController()
        controller.start_session()
        draft = controller.preview_transfer(text, request_id="s5-review")
        assert draft["status"] == "clarify" and not draft["card"], text
    labels = {"CONTACT_CHEN": ["CONTACT_CHEN", "NAME_CHEN", "Chen"]}
    assert extract_intent("给Chen转200元，缓1缓", labels).kind == "clarify"


@pytest.mark.parametrize(
    ("text", "kind", "contact", "amount"),
    [
        # "But" opening a clause, praise, and 别 meaning "other" or "apart".
        (
            "每月5号给房东转2000块，不过备注写房租",
            "schedule_transfer",
            "CONTACT_LANDLORD",
            "2000/1",
        ),
        ("给房东转2000块，不过要备注房租", "transfer", "CONTACT_LANDLORD", "2000/1"),
        ("不过，给房东转2000块吧", "transfer", "CONTACT_LANDLORD", "2000/1"),
        ("不错，那就每月5号给房东转2000块", "schedule_transfer", "CONTACT_LANDLORD", "2000/1"),
        ("跟房东关系不错，给房东转2000块", "transfer", "CONTACT_LANDLORD", "2000/1"),
        ("特别提醒：每月5号给房东转2000块", "schedule_transfer", "CONTACT_LANDLORD", "2000/1"),
        # Words next to the new lexicon entries that are not negations.
        ("关于房租，每月5号给房东转2000块", "schedule_transfer", "CONTACT_LANDLORD", "2000/1"),
        ("没问题，每月5号给房东转2000块", "schedule_transfer", "CONTACT_LANDLORD", "2000/1"),
        ("还没交房租，帮我给房东转2000块", "transfer", "CONTACT_LANDLORD", "2000/1"),
        ("我忘了给房东转2000块，现在帮我转", "transfer", "CONTACT_LANDLORD", "2000/1"),
        ("再说一遍，给房东转2000块", "transfer", "CONTACT_LANDLORD", "2000/1"),
        # English letters that only contain a negation word inside another word or a number.
        ("给房东转2000块，备注No.5房租", "transfer", "CONTACT_LANDLORD", "2000/1"),
        ("给Chen转200元，notify me", "transfer", "CONTACT_CHEN", "200/1"),
        ("给Chen转200元 Nothing else", "transfer", "CONTACT_CHEN", "200/1"),
        ("给Chen转200元 note 房租", "transfer", "CONTACT_CHEN", "200/1"),
        ("给Chen转200元，no problem", "transfer", "CONTACT_CHEN", "200/1"),
    ],
)
def test_neutral_words_keep_their_card(text, kind, contact, amount):
    assert negation_words(text) == (), text
    session = _session()
    response = session.receive(text)
    card = response["card"]
    assert card and card["kind"] == kind, (text, response["status"])
    assert card["amount"] == amount and card["objects"]["contact_ref"] == contact
    assert "negation" not in response["payload"]
    assert session.controller.gateway.bank.execute_count == 0


def test_a_neutral_time_clause_is_not_a_negation_but_asks_when():
    # 结束后 is still not a negation word (S5 review). B2 (JUDGING-1 list item 19) changed
    # what follows: "after the meeting ends" is an unparsed time fragment, so the chat asks
    # "when?" instead of previewing an immediate transfer card, as this test once fixed.
    text = "会议结束后给房东转2000块"
    assert negation_words(text) == (), text
    session = _session()
    response = session.receive(text)
    assert not response["card"] and response["status"] == "clarify"
    assert "negation" not in response["payload"]
    assert response["payload"]["schedule_parse"]["reason"] == "residual_time"
    assert session.controller.gateway.bank.execute_count == 0


@pytest.mark.parametrize(
    "text",
    [
        "给房东转2000，不过先别转",
        "不过，不要给房东转2000块",
        "特别不想给房东转2000块",
        "不错，但是别给房东转2000块",
        "no problem, don't transfer 200 to Chen",
        "给房东转2000块，不过算了",
    ],
)
def test_new_neutral_words_never_hide_a_negation(text):
    assert negation_words(text), text
    _held(_session(), text)


def test_privacy_tokens_and_fixture_handles_carry_no_english_negation():
    session = _session()
    vault = session.controller.gateway.vault
    for text in ("给Chen转200元", "给房东转13800001234元", "给妈妈转50元，备注a@b.example.com"):
        safe = vault.tokenize(text)
        assert negation_words(safe) == (), safe
    for handle in ("NAME_CHEN", "PHONE_LANDLORD", "NEW_PAYEE", "LIN_A", "CONTACT_NEW", "MOM"):
        assert negation_words(f"给{handle}转200元") == (), handle


def test_refund_release_words_keep_the_refund_card():
    for text in (
        "撤销锁定的预算",
        "解除锁定，把预算退回来",
        "放弃生日计划，把锁定的预算退回来",
        "结束计划，把预算退回来",
        "计划撤了，把预算退回来",
        "取消计划把预算退回来",
        "把锁定的预算收回来，退回活期",
        "补偿一下",
        "取消计划，补偿一下",
    ):
        assert compensation_negation(text) == (), text
        session = _session_with_stopped_plan()
        response = session.receive(text)
        assert response["intent"] == "birthday", text
        assert response["card"]["kind"] == "unlock_funds", text


def test_a_cancelled_refund_holds_the_refund_card():
    session = _session_with_stopped_plan()
    pending = dict(session.controller._pending)
    for text in (
        "取消退回锁定的预算",
        "停止退回预算",
        "取消解锁预算",
        "放弃把锁定的预算退回",
        "取消补偿",
        "停止补偿",
        "撤销补偿",
        "放弃补偿",
    ):
        assert compensation_negation(text), text
        _held(session, text)
        assert session.controller._pending == pending, text


def test_held_lines_route_like_before_when_not_money_moving():
    # The new words do not touch intents outside the money-moving branches.
    assert recognize("取消订阅").intent == "subscription_cancel"
    assert recognize("这张卡先别用了").intent == "card_freeze"
    assert recognize("停一下").intent == "stop"
    for text in ("取消订阅", "这张卡先别用了", "会员关了吧", "查一下余额"):
        assert negation_hold(text) == (), text
