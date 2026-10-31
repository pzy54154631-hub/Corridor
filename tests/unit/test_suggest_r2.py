"""The R2 confirmation reviews of the clarify suggestions (agent/domestic_suggest.py).

Each sentence below is a repro input of one of the three R2 reviews, copied as written
there: the external review's probes (p1-p6), the Opus confirmation's probes (pB, pG) and
the fuzz report's scripts (verify_fx, verify_imp, verify_final). The model output is the
reviews' own: a transfer to CONTACT_CHEN of "500" (or the contact and amount the review
named), in a fresh session with nothing before it. What each review asked for:

- a third person paying, anything said before or after the payment, another currency,
  goods, a count, a story or a question: no transfer suggestion (the whole of the words
  must be one paying request, ``payment_alias``);
- a count of payments or payees, hurry, a plea, secrecy, a scam story, impersonation
  written in traditional characters or split by a separator: held, the model is never
  asked (``held``);
- the suggestion call's tool state carries only the previous reply's intent label, and
  the materials say so.
"""

import re
from types import SimpleNamespace

import pytest

from corridor.agent import domestic_suggest
from corridor.agent.domestic_suggest import (
    _chinese_aliases,
    calls_left,
    held,
    payment_alias,
    simplified,
)
from corridor.crypto.jcs import jcs_bytes
from corridor.ui.messages import UI_MESSAGES
from tests.suggest_fakes import TRANSFER, adopt, batch, live_session

CJK = re.compile(r"[一-鿿]")


def transfer(contact="CONTACT_CHEN", amount="500"):
    return {"intent": "transfer", "slots": {"contact_ref": contact, "amount": amount}}


def other(intent):
    return {"intent": intent, "slots": {"contact_ref": None, "amount": None}}


def sentences(response):
    return [
        item["sentence"] for item in (response["payload"].get("suggestions") or {}).get("items", [])
    ]


def reasons(session):
    return [
        record["payload"]["reason"]
        for record in session.suggestions.log.records()
        if record["event"] == "suggestion_discarded"
    ]


def never_asked(text, answer=None):
    """The words are held: no model call, and the reply is byte for byte the one a session
    without suggestions gives."""
    session, client = live_session(answer or batch(TRANSFER))
    plain, _ = live_session(answer or batch(TRANSFER), suggest=False)
    response = session.receive(text)
    assert client.suggest_steps() == [], text
    assert jcs_bytes(response) == jcs_bytes(plain.receive(text)), text
    assert session.suggestions.log.records() == [], text


# --- the external review (gpt-6.1-terra), must 1: a third person pays ---------------------

THIRD_PERSON = [
    # The must's repro and probes p1.py / p4.py.
    "他想给妈妈弄500块过去",
    "爸爸先给妈妈弄500块过去",
    "他再给妈妈弄500块过去",
    "老板想给妈妈弄500块过去",
    "小王想给老妈弄500块过去",
    "他先给我妈弄500块过去",
    "他就给妈妈弄500块过去",
    "弟弟想给妈妈弄500块过去",
    "你给妈妈弄500块过去",
    "他们也想给妈妈弄500块过去",
    "让他帮我给妈妈弄500块过去",
    "妈妈帮我给弟弟弄500块过去",
    # The Opus confirmation's pB third_party_payer group.
    "她想给妈妈弄500块过去",
    "同事帮我给妈妈弄500块过去",
    "老公帮我给妈妈弄500块过去",
    "他请我给妈妈弄500块过去",
    "老板请我给妈妈弄500块过去",
    "他们想给妈妈弄500块过去",
    "你爸想给妈妈弄500块过去",
    "他先给妈妈弄500块过去",
    "他想把500块给妈妈",
    "她就把500块给妈妈",
    "哥哥先把500块弄给妈妈",
    "他给妈妈弄500块过去",
    "爸爸给妈妈弄500块过去",
]


@pytest.mark.parametrize("text", THIRD_PERSON)
def test_a_third_person_paying_is_never_offered_as_the_customer_paying(text):
    session, client = live_session(batch(TRANSFER))
    response = session.receive(text)
    assert sentences(response) == [], text
    assert client.suggest_steps() == ["suggest:1"] and reasons(session) == ["direction"], text


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("房东想给妈妈弄500块过去", "contact_ambiguous"),
        ("帮我给妈妈弄500块过去，再给妈妈弄500块", "amount"),
    ],
)
def test_the_other_third_person_probes_are_discarded_too(text, reason):
    session, _ = live_session(batch(TRANSFER))
    assert sentences(session.receive(text)) == [] and reasons(session) == [reason], text


def test_a_third_person_who_just_paid_is_held():
    """Probe p6.py: 刚 (a payment already made) holds the words before any model call."""
    assert held("他刚想给妈妈弄500块过去") == "completed"
    never_asked("他刚想给妈妈弄500块过去")


def test_payment_alias_reads_the_whole_words_with_the_customer_as_the_payer():
    aliases = ["妈妈", "老妈", "我妈"]
    for text in THIRD_PERSON:
        assert payment_alias(text, aliases, "500") is None, text
    for text in ("帮我给妈妈弄500块过去", "你帮我给妈妈弄500块过去", "我想给妈妈弄500块过去"):
        assert payment_alias(text, aliases, "500") == "妈妈", text


# --- must 2: counts and multiples ------------------------------------------------------------

COUNTED = {
    "schedule": [
        # The must's repro (probe p6.py).
        "给妈妈弄500块过去两次",
        "给妈妈弄500块过去三遍",
        "给妈妈弄500块过去两趟",
        "给妈妈弄500块过去，弄两回",
        "给妈妈弄500块过去，一共两回",
        "给妈妈弄500块过去，连续两天",
        "给妈妈弄500块过去，每人",
        # Probe p5.py, and the Opus confirmation's pB count_or_total group.
        "给妈妈弄500块过去两回",
        "给妈妈弄两次500块过去",
        "帮我给妈妈弄500块过去两次",
        "帮我给妈妈弄500块过去，弄三次",
        "给妈妈弄500块过去，一共两笔",
        "帮我给妈妈弄500块过去，连续三天",
    ],
    "split": ["给妈妈弄500块过去，给两个人", "帮我给妈妈弄500块，两个人的"],
}


@pytest.mark.parametrize(
    ("reason", "text"), [(reason, text) for reason, texts in COUNTED.items() for text in texts]
)
def test_a_count_of_payments_or_payees_never_reaches_the_model(reason, text):
    assert held(text) == reason, text
    never_asked(text)


# The same amount written twice, the external review's suggestion 1 (probe p5.py): the
# trusted amount grammar reads no single amount, so nothing is offered.
SAME_AMOUNT_TWICE = [
    "给妈妈弄500块过去，再给妈妈弄500块",
    "帮我给妈妈弄500块过去，再给妈妈弄500块",
    "给妈妈弄500块过去，再给妈妈弄500元",
    "给妈妈弄500块过去，给妈妈弄500块过去",
    "给妈妈弄500块过去，另外再弄500块",
    "给妈妈弄500块过去，还有500块",
    "给妈妈弄500块过去，500块",
    "给妈妈弄500块过去和500块",
    "给妈妈弄500块+500块过去",
    "给妈妈弄500块过去再弄500块",
    "给妈妈弄500块过去，再弄500块过去",
]


@pytest.mark.parametrize("text", SAME_AMOUNT_TWICE)
def test_the_same_amount_written_twice_gets_no_suggestion(text):
    session, _ = live_session(batch(TRANSFER))
    assert sentences(session.receive(text)) == [] and reasons(session) == ["amount"], text


# --- must 3 (external) and must 1 (Opus): hurry, a plea, secrecy, a scam story ----------------

PRESSURE = {
    "phrase_gate": [
        # The external review's must 3 repro (probe p2.py).
        "妈妈急着用，给妈妈弄500块过去",
        "给妈妈弄500块过去救命",
        "妈妈被骗子盯上，帮我给妈妈弄500块过去",
        "保密，帮我给妈妈弄500块过去",
        "快点帮我给妈妈弄500块过去",
        "妈妈住院，给妈妈弄500块过去",
        "妈妈在医院，帮我给妈妈弄500块过去",
        "抓紧给妈妈弄500块过去",
        "赶快给妈妈弄500块过去",
        "尽快给妈妈弄500块过去",
        "火速给妈妈弄500块过去",
        "速度给妈妈弄500块过去",
        "快快给妈妈弄500块过去",
        "十万火急给妈妈弄500块过去",
        "警察在旁边，帮我给妈妈弄500块过去",
        "老师在等，帮我给妈妈弄500块过去",
        "妈妈在等着，帮我给妈妈弄500块过去",
        "偷偷给妈妈弄500块过去",
        # The Opus confirmation's must 1 (pB urgency and scam_context groups).
        "赶快帮我给妈妈弄500块过去",
        "快帮我给妈妈弄500块过去",
        "抓紧帮我给妈妈弄500块过去",
        "立马给妈妈弄500块过去",
        "即刻给妈妈弄500块过去",
        "加急给妈妈弄500块过去",
        "紧急！帮我给妈妈弄500块过去",
        "很急，帮我给妈妈弄500块过去",
        "十万火急，给妈妈弄500块过去",
        "越快越好，给妈妈弄500块过去",
        "第一时间给妈妈弄500块过去",
        "速给妈妈弄500块过去",
        "限时给妈妈弄500块过去",
        "现在就给妈妈弄500块过去",
        "急，帮我给妈妈弄500块过去",
        "妈妈着急，帮我给妈妈弄500块过去",
        "妈妈那边很急，帮我给妈妈弄500块过去",
        "麻溜给妈妈弄500块过去",
        "帮我给妈妈弄500块过去当保证金",
        "帮我给妈妈弄500块过去刷单",
        "帮我给妈妈弄500块过去返利",
        "帮我给妈妈弄500块过去交解冻费",
        "帮我给妈妈弄500块过去激活",
        "帮我给妈妈弄500块过去做任务",
        "帮我给妈妈弄500块过去，保密",
        "帮我给妈妈弄500块过去，她被骗了",
        "帮我给妈妈弄500块过去，她在派出所",
        "帮我给妈妈弄500块过去，妈妈的新账户",
    ],
    "impersonation": [
        "妈妈手机坏掉，帮我给妈妈弄500块过去",
        "网上认识的妈妈，帮我给妈妈弄500块过去",
        "我是妈妈，帮我给妈妈弄500块过去",
    ],
    "negation": [
        "别告诉别人，给妈妈弄500块过去",
        "刻不容缓，给妈妈弄500块过去",
        "等不及了，给妈妈弄500块过去",
        "帮我给妈妈弄500块过去，别告诉别人",
        "帮我给妈妈弄500块过去，不要声张",
    ],
    "time": ["一会儿给妈妈弄500块过去", "等下给妈妈弄500块过去"],
    "mood": ["如需帮我给妈妈弄500块过去", "要是行就给妈妈弄500块过去"],
}


@pytest.mark.parametrize(
    ("reason", "text"), [(reason, text) for reason, texts in PRESSURE.items() for text in texts]
)
def test_hurry_secrecy_and_scam_stories_never_reach_the_model(reason, text):
    assert held(text) == reason, text
    never_asked(text)


# --- the fuzz report, must 2: traditional characters and split words -------------------------

TRADITIONAL_OR_SPLIT = [
    ("妈妈用新號碼联系我，帮我给妈妈弄500块过去", "impersonation"),
    ("有人自稱妈妈，帮我给妈妈弄500块过去", "impersonation"),
    ("有人假裝是妈妈，帮我给妈妈弄500块过去", "impersonation"),
    ("妈妈換了新號，帮我给妈妈弄500元过去", "impersonation"),
    ("冒-充妈妈的人让帮我给妈妈弄500块过去", "impersonation"),
    ("有人自·称是妈妈，帮我给妈妈弄500块过去", "impersonation"),
    ("对方自稱房东，帮我给新联系人弄800块过去", "impersonation"),
    ("新號碼联系我自稱房东，给新联系人弄800元", "impersonation"),
    ("妈妈用新號碼联系我，帮我给妈妈弄2000块过去", "impersonation"),
    ("有人自稱是妈妈，帮我给妈妈弄500块过去", "impersonation"),
    ("有人冒-充妈妈，帮我给妈妈弄800块过去", "impersonation"),
    ("有人自稱是房东，给房东弄2000块过去", "impersonation"),
    ("公安局讓我把500块弄给妈妈", "phrase_gate"),
    # The simplified controls the report compared them with.
    ("妈妈用新号码联系我，帮我给妈妈弄500块过去", "impersonation"),
    ("有人自称妈妈，帮我给妈妈弄500块过去", "impersonation"),
    ("警察让我马上给妈妈弄500块过去", "phrase_gate"),
    ("反诈中心让我给妈妈弄500块过去", "phrase_gate"),
]


@pytest.mark.parametrize(("text", "reason"), TRADITIONAL_OR_SPLIT)
def test_cue_words_in_traditional_characters_or_split_apart_are_held(text, reason):
    assert held(text) == reason, text
    never_asked(text)


def test_the_gate_folds_traditional_characters_and_drops_separators_inside_words():
    pairs = domestic_suggest._TRADITIONAL_PAIRS
    assert len(pairs) % 2 == 0
    table = dict(zip(pairs[0::2], pairs[1::2], strict=True))
    assert all(source != target and target not in table for source, target in table.items())
    assert (
        simplified("新號碼 自稱 假裝 換了 趕緊 馬上 盡快 後天 幣")
        == "新号码自称假装换了赶紧马上尽快后天币"
    )
    for split in ("自·称", "自・称", "冒-充", "冒.充", "冒_充", "自1称", "冒a充"):
        assert held(f"有人{split}妈妈，帮我给妈妈弄500块过去") == "impersonation", split
    # Traditional forms of the S6 urgency words hold as their simplified ones do.
    for words in ("趕緊幫我给妈妈弄500块过去", "馬上给妈妈弄500块过去", "盡快给妈妈弄500块过去"):
        assert held(words) == "phrase_gate", words


# --- the fuzz report, must 1, and the Opus pB currency group: another currency --------------

CURRENCY = [
    # fuzz/scripts/verify_fx.py and verify_final.py.
    "给妈妈弄500块越南盾过去",
    "给妈妈弄500块马币过去",
    "给妈妈弄500块澳门币过去",
    "给妈妈弄500块加拿大元过去",
    "给妈妈弄500块日本钱过去",
    "给妈妈弄500块的卢比过去",
    "给妈妈弄500块令吉过去",
    "给妈妈弄500块比索过去",
    "给妈妈弄500块迪拉姆过去",
    "给妈妈弄500块里拉过去",
    "给妈妈弄500块盾过去",
    "给妈妈弄500块的坚戈过去",
    "给妈妈弄500块Q币过去",
    "给妈妈弄500块金币过去",
    "给妈妈弄500块游戏币过去",
    "给妈妈弄500块代币过去",
    "给妈妈弄500美元过去",
    "给妈妈弄500块美金过去",
    "给妈妈弄500块港币过去",
    "给妈妈弄500块欧元过去",
    "帮我给妈妈弄500块马币过去",
    "帮我给妈妈弄500块卢比过去",
    "帮我给妈妈弄1000块澳门币过去",
    "帮我给妈妈弄500块的瑞郎过去",
    "帮我给妈妈弄500块Q币过去",
    "帮我给妈妈弄500块软妹币过去",
    # The Opus confirmation's pB currency group.
    "帮我给妈妈弄500元澳门币过去",
    "帮我给妈妈弄澳门币500元过去",
    "帮我给妈妈弄500块澳门币过去",
    "帮我给妈妈弄500元新加坡币过去",
    "帮我给妈妈弄500元马币过去",
    "帮我给妈妈弄500元越南盾过去",
    "帮我给妈妈弄500元卢比过去",
    "帮我给妈妈弄500元泰币过去",
    "帮我给妈妈弄500元比索过去",
    "帮我给妈妈弄500元瑞郎过去",
    "帮我给妈妈弄500元Q币过去",
    "帮我给妈妈弄500元泰达币过去",
    "帮我给妈妈弄500元以太币过去",
    "帮我给妈妈弄500元葡币过去",
    "帮我给妈妈弄新台币500元过去",
    "帮我给妈妈弄港币500元过去",
    "帮我给妈妈弄美金500元过去",
    "帮我给妈妈弄500元美金过去",
    "帮我给妈妈弄500元外币过去",
    "帮我给妈妈弄500元的澳门币过去",
]


def _literal(text):
    return re.search(r"[0-9]+", text).group()


@pytest.mark.parametrize("text", CURRENCY)
def test_another_currency_is_never_written_as_yuan(text):
    session, client = live_session(batch(transfer(amount=_literal(text))))
    response = session.receive(text)
    assert sentences(response) == [] and client.suggest_steps() == ["suggest:1"], text
    assert reasons(session) == ["amount"], text


@pytest.mark.parametrize(
    ("text", "contact", "amount"),
    [
        ("帮我给房东弄2000块令吉过去", "CONTACT_LANDLORD", "2000"),
        ("帮我给新联系人弄800块越南盾过去", "CONTACT_NEW", "800"),
        ("帮我给新联系人弄800块马币过去", "CONTACT_NEW", "800"),
        ("帮我给妈妈弄2000块马币过去", "CONTACT_CHEN", "2000"),
    ],
)
def test_another_currency_is_never_written_as_yuan_for_any_contact(text, contact, amount):
    session, _ = live_session(batch(transfer(contact, amount)))
    assert sentences(session.receive(text)) == [] and reasons(session) == ["amount"], text


# --- the Opus confirmation, must 2: only the whole paying request is offered ----------------

NOT_THE_WHOLE_REQUEST = {
    "goods_or_other_payee": [
        "帮我给妈妈送500块的花",
        "帮我给妈妈寄500块的特产",
        "帮我给妈妈带500块的水果",
        "帮我给妈妈弄500块的东西过去",
        "帮我给妈妈弄500块的礼物过去",
        "帮我给妈妈送500元购物卡",
        "帮我给妈妈送500元油卡",
        "帮我给妈妈发500元优惠券",
        "帮我给妈妈交500块医药费",
        "帮我给妈妈付500块房租",
        "帮我给妈妈交500块学费",
        "帮我给妈妈交500块水电费",
        "帮我给妈妈付500块的快递",
        "帮我给妈妈买500块的花",
        # The external review's suggestion 3 (probe p5.py).
        "给妈妈送500块现金过去",
        "帮我给妈妈弄500块现金过去",
    ],
    "past_reference": [
        "给妈妈弄500块过去的那笔",
        "给妈妈弄500块过去那回",
        "给妈妈弄500块过去失败",
        "给妈妈弄500块过去的记录",
        "给妈妈弄500块过去那个查一下",
        "给妈妈弄500块过去是哪天",
        "给妈妈弄500块过去这事",
        "给妈妈弄500块过去这件事",
        "给妈妈弄500块过去的事",
    ],
    "incoming_or_other_dir": [
        "给妈妈那边弄500块过来",
        "给妈妈弄500块过来",
        "帮我从妈妈那弄500块过来",
        "妈妈那边500块弄过来",
        "让妈妈弄500块过来",
        "帮妈妈把500块弄回来",
        "给妈妈弄500块回来",
        "帮我给妈妈弄500块回来",
        "给妈妈弄500块拿回来",
        "妈妈想给我弄500块过来",
    ],
    "question_or_info": [
        "给妈妈弄500块过去多久能到",
        "给妈妈弄500块过去安全吗",
        "给妈妈弄500块过去会被扣钱吗",
        "给妈妈弄500块过去有限额吗",
        "给妈妈弄500块过去需要多久",
        "给妈妈弄500块过去要多久",
        "帮我给妈妈弄500块过去行不行",
        "帮我给妈妈弄500块过去好不好",
    ],
    "other": [
        "现在给妈妈弄500块过去",
        # The Opus confirmation's suggestion 9: a card the words name is not dropped.
        "给妈妈弄500块过去，卡号尾号500",
    ],
}


@pytest.mark.parametrize(
    "text", [text for texts in NOT_THE_WHOLE_REQUEST.values() for text in texts]
)
def test_words_that_are_not_one_paying_request_get_no_transfer(text):
    session, client = live_session(batch(TRANSFER))
    response = session.receive(text)
    assert sentences(response) == [], text
    if held(text) is None:
        asked = client.suggest_steps()
        assert asked in ([], ["suggest:1"]), text  # [] when the lexical rules read it
        if asked:
            assert reasons(session) in (["direction"], ["context"], ["amount"]), text
    else:
        assert client.suggest_steps() == [], text


ORDINARY = [
    # The Opus confirmation's prototype (probes/pG_anchor_proto.py LEGIT): still offered.
    ("帮我给妈妈弄500块过去", "CONTACT_CHEN", "500", "给妈妈转500元"),
    ("给妈妈弄500块过去", "CONTACT_CHEN", "500", "给妈妈转500元"),
    ("请帮我给妈妈打500块过去", "CONTACT_CHEN", "500", "给妈妈转500元"),
    ("麻烦给我妈汇500元", "CONTACT_CHEN", "500", "给我妈转500元"),
    ("我要给老妈弄300块过去", "CONTACT_CHEN", "300", "给老妈转300元"),
    ("我想给妈妈弄500块过去", "CONTACT_CHEN", "500", "给妈妈转500元"),
    ("帮我把500块弄给妈妈", "CONTACT_CHEN", "500", "给妈妈转500元"),
    ("把500块给妈妈", "CONTACT_CHEN", "500", "给妈妈转500元"),
    ("帮我给妈妈弄500块过去吧", "CONTACT_CHEN", "500", "给妈妈转500元"),
    # SG-3: one exclamation mark or tilde is still a paying request; a question mark (吧？)
    # no longer is (tests/unit/test_suggest_sg2.py).
    ("帮我给妈妈弄500块过去！", "CONTACT_CHEN", "500", "给妈妈转500元"),
    ("给妈妈弄500块过去～", "CONTACT_CHEN", "500", "给妈妈转500元"),
    ("再给妈妈弄500块过去", "CONTACT_CHEN", "500", "给妈妈转500元"),
    ("给房东弄800块钱过去", "CONTACT_LANDLORD", "800", "给房东转800元"),
    ("帮我给妈妈弄500.5块过去", "CONTACT_CHEN", "500.5", "给妈妈转500.5元"),
    ("帮我给妈妈整500块", "CONTACT_CHEN", "500", "给妈妈转500元"),
    ("你帮我给妈妈弄500块过去", "CONTACT_CHEN", "500", "给妈妈转500元"),
    # The live run's transfer sentence (live-01, S1).
    ("给老妈弄300块过去", "CONTACT_CHEN", "300", "给老妈转300元"),
    ("帮我给妈妈弄500块过去，谢谢", "CONTACT_CHEN", "500", "给妈妈转500元"),
]


@pytest.mark.parametrize(("text", "contact", "amount", "expected"), ORDINARY)
def test_an_ordinary_paying_request_is_still_offered(text, contact, amount, expected):
    session, _ = live_session(batch(transfer(contact, amount)))
    response = session.receive(text)
    assert sentences(response) == [expected], text
    adopted = adopt(session, response["response_id"], 0)
    assert adopted["card"]["kind"] == "transfer" and adopted["status"] != "settled"
    assert session.controller.gateway.bank.execute_count == 0


# --- the Opus confirmation, must 3: what the suggestion call sends ----------------------------


def test_the_suggestion_call_sends_only_the_previous_intent_label_and_the_materials_say_so():
    """The suggestion call's tool state is the previous reply's intent label (None at the
    start), as every intent call's is: a label the session wrote, never the words. The
    live note, the judge evidence and the materials say so."""
    session, client = live_session(batch(transfer("CONTACT_LANDLORD", "800")))
    first = session.receive("给妈妈转500元")
    assert first["intent"] == "transfer"
    session.receive("帮我给房东弄800块过去")
    sent = {
        step: state
        for step, state in zip(client.steps, client.tool_states, strict=True)
        if step.startswith("suggest:")
    }
    assert sent == {"suggest:2": {"previous_intent": "transfer"}}
    fresh, fresh_client = live_session(batch(TRANSFER))
    fresh.receive("帮我给妈妈弄500块过去")
    assert fresh_client.tool_states == [{"previous_intent": None}]
    for state in [*client.tool_states, *fresh_client.tool_states]:
        assert set(state) == {"previous_intent"}
        assert state["previous_intent"] is None or not CJK.search(state["previous_intent"])
    for key in ("live.reason.on", "suggest.judge.source"):
        assert "上一轮回复的意图标签" in UI_MESSAGES[key]["zh-CN"], key
        assert "previous reply's intent label" in UI_MESSAGES[key]["en"], key


# --- the cheaper suggestions taken from the three reviews ------------------------------------


def test_a_shown_alias_is_chinese_characters_only():
    """Opus suggestion 7 / external review suggestion: no control, direction or markup
    character in a sentence the page renders."""
    names = ["妈‮妈", "*妈*", "[妈妈]", "妈 妈", "妈妈", "王·小明", "Chen", "ＭＯＭ", "妈妈​"]
    assert _chinese_aliases(names) == ["妈妈", "王·小明"]


@pytest.mark.parametrize(
    ("text", "intent"),
    [
        ("我的卡找到了", "card_lost"),
        ("卡找回来了", "card_lost"),
        ("卡又找着", "card_lost"),
        ("把卡打开", "card_freeze"),
        ("卡注销掉", "card_apply"),
    ],
)
def test_words_that_undo_an_intent_never_get_its_template(text, intent):
    """Opus suggestion 1: 卡找到了 is no lost card (and so on)."""
    session, client = live_session(batch(other(intent)))
    response = session.receive(text)
    assert client.suggest_steps() == ["suggest:1"]
    assert sentences(response) == [] and reasons(session) == ["context"], text


@pytest.mark.parametrize(
    ("text", "intent", "expected"),
    [
        ("卡掉了", "card_lost", "我的银行卡丢了"),
        ("卡先锁上", "card_freeze", "冻结我的银行卡"),
        ("那个视频网站的月费帮我弄掉", "subscription_cancel", "取消订阅"),
        ("把这个月的单子调出来瞧瞧", "subscription_query", "查看我的订阅"),
    ],
)
def test_the_live_runs_other_sentences_still_reach_the_model(text, intent, expected):
    session, _ = live_session(batch(other(intent)))
    assert sentences(session.receive(text)) == [expected], text


def test_a_slot_missing_from_the_output_fails_closed_as_the_wire_schema_requires_it():
    """External review suggestion (§16-1): the local schema requires both slots, as the
    strict wire schema does; an output without one fails closed as a whole."""
    from corridor.agent.domestic_suggest import SuggestionBatch, wire_schema
    from corridor.agent.llm import _strict_schema

    slots = _strict_schema(wire_schema(["CONTACT_CHEN"]))["properties"]["suggestions"]["items"]
    assert slots["properties"]["slots"]["required"] == ["contact_ref", "amount"]
    for partial in ({"contact_ref": None}, {"amount": None}, {}):
        output = {"suggestions": [{"intent": "card_freeze", "slots": partial}]}
        with pytest.raises(ValueError):
            SuggestionBatch.model_validate(output)
        session, client = live_session(output)
        plain, _ = live_session(output, suggest=False)
        response = session.receive("那张东西帮我处理一下")
        assert jcs_bytes(response) == jcs_bytes(plain.receive("那张东西帮我处理一下"))
        assert client.suggest_steps() == ["suggest:1"] and reasons(session) == ["schema"]


def test_a_live_client_whose_cap_cannot_be_read_gets_no_suggestion():
    """External review suggestion 5: a client with a live session whose call count or cap
    cannot be read is treated as having no call to spare."""
    assert calls_left(SimpleNamespace(_live_session=SimpleNamespace())) == 0
    assert calls_left(SimpleNamespace(_live_session=None)) == 0
    assert calls_left(object()) is None
    session, client = live_session(batch(TRANSFER))
    client._live_session = SimpleNamespace(_calls="7", config=SimpleNamespace(session_calls=40))
    response = session.receive("帮我给妈妈弄500块过去")
    assert client.suggest_steps() == [] and sentences(response) == []
    assert reasons(session) == ["budget_reserved"]


def test_a_prompt_that_cannot_be_built_is_not_counted_as_asked(monkeypatch):
    """External review suggestion 4: the per-session count moves only with a call."""

    def broken(*args, **kwargs):
        raise RuntimeError("injected")

    session, client = live_session(batch(TRANSFER))
    monkeypatch.setattr(domestic_suggest, "prompt_contacts", broken)
    session.receive("帮我给妈妈弄500块过去")
    assert session.suggestions.asked == 0 and client.suggest_steps() == []


def test_a_log_commit_that_fails_takes_the_offer_and_the_payload_back(monkeypatch):
    """External review suggestion 4: the offer and the payload go in before the commit,
    which comes last; a commit that fails takes both back out."""
    seen = {}
    original = domestic_suggest.SuggestionLog.commit

    def failing(log, records):
        session = seen["session"]
        (identifier,) = session.suggestions._offers
        seen["during"] = "suggestions" in session._responses[identifier]["payload"]
        raise RuntimeError("injected")

    session, _ = live_session(batch(TRANSFER))
    plain, _ = live_session(batch(TRANSFER), suggest=False)
    seen["session"] = session
    monkeypatch.setattr(domestic_suggest.SuggestionLog, "commit", failing)
    response = session.receive("帮我给妈妈弄500块过去")
    monkeypatch.setattr(domestic_suggest.SuggestionLog, "commit", original)
    assert seen["during"] is True
    assert jcs_bytes(response) == jcs_bytes(plain.receive("帮我给妈妈弄500块过去"))
    assert session.suggestions._offers == {} and session.suggestions.log.records() == []
    assert session.response(response["response_id"]) == response


def test_the_streamlit_notice_follows_what_the_session_received(monkeypatch):
    """Opus suggestion 2 / external review B3: a send that fails after the session received
    the sentence says it was sent (the desk adopted it); a second click on a suggestion
    already sent adds no "expired" notice."""
    from corridor.ui import suggest_views

    state = {}
    monkeypatch.setattr(suggest_views, "st", SimpleNamespace(session_state=state))
    session, _ = live_session(batch(TRANSFER))
    first = session.receive("帮我给妈妈弄500块过去")
    identifier = first["response_id"]

    def received_then_failed(session, sentence, prefix, route=True):
        session.receive(sentence)
        raise OSError("the page failed while showing the reply")

    suggest_views.adopt(session, identifier, 0, "p", received_then_failed, True)
    assert state["p_suggestion_error"] == (identifier, "suggest.sent_then_failed")
    assert session.suggestions.view(session, identifier)[0]["state"] == "adopted"
    assert UI_MESSAGES["suggest.sent_then_failed"]["zh-CN"]

    def never_called(*args, **kwargs):
        raise AssertionError("a suggestion already sent is never sent again")

    suggest_views.adopt(session, identifier, 0, "p", never_called, True)
    assert "p_suggestion_error" not in state

    # Before the session received it: the suggestion stays offered and the notice says so.
    fresh, _ = live_session(batch(TRANSFER))
    reply = fresh.receive("帮我给妈妈弄500块过去")

    def failed(session, sentence, prefix, route=True):
        raise OSError("not sent")

    suggest_views.adopt(fresh, reply["response_id"], 0, "q", failed, True)
    assert state["q_suggestion_error"] == (reply["response_id"], "suggest.send_failed")
    assert fresh.suggestions.view(fresh, reply["response_id"])[0]["state"] == "offered"


# Opus suggestion 6: the gate holds these Chinese S6 cue words whatever the display catalog
# says (a translation change to polish.fraud.cues.* cannot weaken it), and someone telling
# the customer to pay.
S6_WORDS = (
    "公安",
    "反诈中心",
    "客服",
    "工作人员",
    "检察院",
    "法院",
    "警察",
    "专户",
    "发来的",
    "安全账户",
    "对方提供",
    "指定账户",
    "链接",
    "不然",
    "冻结",
    "分钟内",
    "否则",
    "尽快",
    "立刻",
    "立即",
    "赶紧",
    "马上",
    "分两笔",
    "分笔",
    "拆分",
    "第二笔",
    "避开限额",
)


DIRECTED_WORDS = (
    "催我",
    "叫我",
    "吩咐我",
    "命令我",
    "安排我",
    "指导我",
    "教我",
    "要我",
    "让我",
    "逼我",
)


@pytest.mark.parametrize("empty", [False, True])
def test_the_chinese_s6_cue_words_are_held_whatever_the_catalog_says(monkeypatch, empty):
    from corridor.ui import fraud_hint

    if empty:
        monkeypatch.setattr(fraud_hint, "cues", lambda group: ())
        monkeypatch.setattr(fraud_hint, "directed", lambda text: False)
    for word in S6_WORDS:
        for text in (f"{word}，帮我给妈妈弄500块过去", f"帮我给妈妈弄500块过去，{word}"):
            assert held(text) == "phrase_gate", text
    for word in DIRECTED_WORDS:
        text = f"老板{word}给妈妈弄500块过去"
        assert held(text) in ("phrase_gate", "direction", "relay"), text
