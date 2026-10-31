"""S5 review: fuzz the phrasings the first gate missed, and the positives it held.

Negated sentences fuzz the cancel verbs and English words added after the review, written
with or without a space next to the Chinese text, and a negation split by any invisible
mark: none yields a card, a dispatch or a signed input. Near-miss positives (，不过…, 不错,
特别提醒) keep exactly the card the plain sentence gets.
"""

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from corridor.agent.domestic_negation import negation_words
from corridor.agent.domestic_session import DomesticSession
from corridor.ui.controller import DemoController

WORDS = (
    "撤消",
    "撤了",
    "删了",
    "关了",
    "关停",
    "结束",
    "解除",
    "暂缓",
    "缓一缓",
    "缓缓",
    "没有必要",
    "放弃",
    "退订",
    "禁止",
    "拉倒",
    "罢了",
    "作罢",
    "没让",
    "反悔",
    "收回",
    "算啦",
    "算咯",
    "免啦",
    "忘了吧",
    "以后再说",
    "等等再说",
    "改天吧",
    "当我没说",
    "別",
)
ENGLISH = (
    "cancel",
    "Cancel",
    "CANCEL",
    "cancelled",
    "stop",
    "STOP",
    "stopped",
    "pause",
    "don't",
    "dont",
    "do not",
    "didn't",
    "never",
    "nevermind",
    "not",
    "nope",
    "abort",
    "skip",
    "undo",
    "revoke",
    "halt",
    "won't",
    "can't",
)
SPLIT = ("取消", "算了", "撤销", "作废", "终止", "拒绝", "删除", "关掉", "无需", "放弃", "结束")
MARKS = (0x00AD, 0x034F, 0x061C, 0x115F, 0x17B4, 0x180B, 0x200B, 0x200D, 0x2060, 0x2066)
MARKS += (0x2069, 0x206A, 0x3164, 0xFE0F, 0xFEFF, 0xFFA0, 0xFFF9, 0x1D173, 0xE0001, 0xE0020)
PHRASES = ("", "明天", "下周五", "每月5号", "月底", "11月20号")
ALIASES = ("妈妈", "房东", "室友", "Chen")
PUNCT = ("，", ",", "。", "；", "！", " ", "", "、")
NEGATED = (
    "{w}{d}给{a}转{m}元",
    "{d}{w}给{a}转{m}元",
    "{d}给{a}转{m}元{p}{w}",
    "{w}{p}{d}给{a}转{m}元",
    "我和{a}、同学AA {m}元{p}{w}",
)
POSITIVE = (
    "{d}给{a}转{m}元",
    "{d}给{a}转{m}元{p}不过备注写房租",
    "不过{p}{d}给{a}转{m}元",
    "不错{p}{d}给{a}转{m}元",
    "特别提醒{p}{d}给{a}转{m}元",
)
AMOUNTS = st.integers(min_value=1, max_value=50000).map(str)


def _session():
    controller = DemoController()
    controller.start_session()
    return controller, DomesticSession(controller)


def _assert_no_card(text):
    controller, chat = _session()
    response = chat.receive(text)
    assert not response["card"] and response["buttons"] == [], (ascii(text), response["status"])
    assert response["needs_clarification"] or response["intent"] == "stop", ascii(text)
    assert controller.gateway.bank.execute_count == 0
    assert not controller.state.user_inputs
    assert not controller._pending and not controller._plans


@settings(deadline=None, max_examples=100, suppress_health_check=[HealthCheck.too_slow])
@given(
    template=st.sampled_from(NEGATED),
    word=st.one_of(st.sampled_from(WORDS), st.sampled_from(ENGLISH)),
    phrase=st.sampled_from(PHRASES),
    alias=st.sampled_from(ALIASES),
    amount=AMOUNTS,
    punct=st.sampled_from(PUNCT),
)
def test_added_words_hold_in_any_position(template, word, phrase, alias, amount, punct):
    text = template.format(w=word, d=phrase, a=alias, m=amount, p=punct)
    assert negation_words(text), ascii(text)
    _assert_no_card(text)


@settings(deadline=None, max_examples=60, suppress_health_check=[HealthCheck.too_slow])
@given(
    word=st.sampled_from(SPLIT),
    mark=st.sampled_from(MARKS),
    phrase=st.sampled_from(PHRASES),
    alias=st.sampled_from(ALIASES),
    amount=AMOUNTS,
)
def test_no_invisible_mark_splits_a_negation(word, mark, phrase, alias, amount):
    split = word[:1] + chr(mark) + word[1:]
    text = f"{split}{phrase}给{alias}转{amount}元"
    assert negation_words(text), ascii(text)
    _assert_no_card(text)


@settings(deadline=None, max_examples=60, suppress_health_check=[HealthCheck.too_slow])
@given(
    template=st.sampled_from(POSITIVE[1:]),
    phrase=st.sampled_from(PHRASES),
    alias=st.sampled_from(ALIASES),
    amount=AMOUNTS,
    punct=st.sampled_from(("，", ",", "：", "。")),
)
def test_new_neutral_words_get_the_plain_sentences_card(template, phrase, alias, amount, punct):
    text = template.format(d=phrase, a=alias, m=amount, p=punct)
    plain = POSITIVE[0].format(d=phrase, a=alias, m=amount)
    assert negation_words(text) == (), text
    _, reference = _session()
    controller, chat = _session()
    expected = reference.receive(plain)
    response = chat.receive(text)
    assert "negation" not in response["payload"], text
    facts = [
        ((item["card"] or {}).get("kind"), (item["card"] or {}).get("amount"))
        for item in (expected, response)
    ]
    assert facts[0] == facts[1], (text, plain)
    assert controller.gateway.bank.execute_count == 0
