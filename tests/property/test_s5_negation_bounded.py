"""S5: the negation gate holds every generated negated request and keeps near-miss positives.

Negated sentences fuzz the word, its position (before or after the date phrase, its own
clause before or after, the very end), the recipient, the amount, the date phrase and the
punctuation: none yields a card, a dispatch or a signed input. Near-miss positives (a
reminder, politeness, parking, arithmetic) keep exactly the card the plain sentence gets.
"""

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from corridor.agent.domestic_intent import negation_hold, recognize
from corridor.agent.domestic_negation import negation_words
from corridor.agent.domestic_session import DomesticSession
from corridor.ui.controller import DemoController

WORDS = (
    "不",
    "别",
    "不要",
    "先别",
    "不想",
    "先不",
    "停止",
    "取消",
    "停掉",
    "暂停",
    "不用",
    "不必",
    "甭",
    "无需",
    "没必要",
    "撤销",
    "作废",
    "关掉",
    "删除",
    "去掉",
)
TAILS = ("算了", "不用了", "免了", "不要了", "取消", "停掉吧", "还是算了", "别转了吧")
PHRASES = ("", "明天", "下周五", "每月5号", "每月15号", "月底", "11月20号", "2026-11-03", "20号")
ALIASES = ("妈妈", "老妈", "房东", "室友", "同学", "Chen", "新联系人", "小王")
PUNCT = ("，", ",", "。", "；", "！", " ", "、")
NEGATED = (
    "{w}{d}给{a}转{m}元",
    "{d}{w}给{a}转{m}元",
    "{d}给{a}{w}转{m}元",
    "{w}{p}{d}给{a}转{m}元",
    "{d}{p}{w}给{a}转{m}元",
    "{d}给{a}转{m}元{p}{t}",
    "{t}{p}{d}给{a}转{m}元",
    "{d}给{a}汇款{m}元{p}{t}",
    "{w}{d}帮我给{a}打钱{m}块",
    "我和{a}{w}AA {m}元",
    "昨天和{a}聚餐一共花了{m}元{p}{w}AA",
)
POSITIVE = (
    "{d}给{a}转{m}元",
    "别忘了{d}给{a}转{m}元",
    "不要忘了{d}给{a}转{m}元",
    "可别忘了{d}给{a}转{m}元",
    "不好意思{p}{d}给{a}转{m}元",
    "对不起{p}{d}给{a}转{m}元",
    "我算了一下{p}{d}给{a}转{m}元",
)
AMOUNTS = st.one_of(
    st.integers(min_value=1, max_value=50000).map(str),
    st.tuples(st.integers(min_value=1, max_value=999), st.integers(min_value=1, max_value=99)).map(
        lambda pair: f"{pair[0]}.{pair[1]:02d}"
    ),
)


def _session():
    controller = DemoController()
    controller.start_session()
    return controller, DomesticSession(controller)


def _card_facts(response):
    card = response["card"] or {}
    return (
        card.get("kind"),
        card.get("amount"),
        (card.get("objects") or {}).get("contact_ref"),
        (card.get("spa") or {}).get("schedule"),
        (card.get("spa") or {}).get("start"),
    )


@settings(deadline=None, max_examples=120, suppress_health_check=[HealthCheck.too_slow])
@given(
    template=st.sampled_from(NEGATED),
    word=st.sampled_from(WORDS),
    tail=st.sampled_from(TAILS),
    phrase=st.sampled_from(PHRASES),
    alias=st.sampled_from(ALIASES),
    amount=AMOUNTS,
    punct=st.sampled_from(PUNCT),
)
def test_negated_money_requests_never_yield_a_card(
    template, word, tail, phrase, alias, amount, punct
):
    text = template.format(w=word, t=tail, d=phrase, a=alias, m=amount, p=punct)
    assert negation_words(text), text
    controller, chat = _session()
    response = chat.receive(text)
    assert not response["card"] and response["buttons"] == [], (text, response["status"])
    assert response["needs_clarification"] or response["intent"] == "stop", text
    if response["intent"] != "stop":
        assert response["intent"] == "clarify", text
        assert response["payload"]["negation"]["words"], text
    assert controller.gateway.bank.execute_count == 0
    assert not controller.state.user_inputs
    assert not controller._pending and not controller._plans


@settings(deadline=None, max_examples=80, suppress_health_check=[HealthCheck.too_slow])
@given(
    template=st.sampled_from(POSITIVE[1:]),
    phrase=st.sampled_from(PHRASES),
    alias=st.sampled_from(ALIASES),
    amount=AMOUNTS,
    punct=st.sampled_from(PUNCT),
)
def test_near_miss_positives_get_the_plain_sentences_card(template, phrase, alias, amount, punct):
    text = template.format(d=phrase, a=alias, m=amount, p=punct)
    plain = POSITIVE[0].format(d=phrase, a=alias, m=amount)
    assert negation_words(text) == (), text
    _, reference = _session()
    controller, chat = _session()
    expected = reference.receive(plain)
    response = chat.receive(text)
    assert "negation" not in response["payload"], text
    if expected["card"]:
        # Only the neutral words differ, so the card says exactly what the plain request says.
        assert _card_facts(response) == _card_facts(expected), (text, plain)
    else:
        assert not response["card"], (text, plain)
    assert controller.gateway.bank.execute_count == 0


@settings(deadline=None, max_examples=200)
@given(
    text=st.text(
        alphabet=st.sampled_from(
            list("给转汇款打钱元块明天每月5号房东妈妈AA我和")
            + list("不别停取消算了免用要想先，。 生日蛋糕退回预算锁")
        ),
        max_size=24,
    )
)
def test_the_gate_only_ever_removes_a_money_moving_intent(text):
    """Whatever the text, a held request is clarify, and a money intent carries no negation."""
    for previous in (None, "birthday"):
        context = {"previous_intent": previous}
        intent = recognize(text, context).intent
        if negation_hold(text, context):
            assert intent == "clarify", text
        if intent == "transfer":
            assert negation_words(text) == (), text
