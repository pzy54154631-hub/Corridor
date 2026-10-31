"""S5 R8: five polite tags are not negations in two places.

不用急 / 不着急 / 好不好 / 行不行 / 可以吗, standing as their own clause after a comma or ending
the sentence (with a particle and end punctuation), leave a transfer request exactly as the
plain request: the same card, no negation. Anywhere else (opening the sentence, followed by
more of their own clause, right after 先 / 暂 / 还) they hold the request as before, and a
polite tag never releases any other negation in the sentence.
"""

from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from corridor.agent.domestic_intent import recognize
from corridor.agent.domestic_negation import negation_words, without_polite_tags
from tests.property.test_s5_negation_bounded import (
    ALIASES,
    AMOUNTS,
    NEGATED,
    PUNCT,
    TAILS,
    WORDS,
    _card_facts,
    _session,
)

TAGS = ("不用急", "不着急", "好不好", "行不行", "可以吗")
NEGATING_TAGS = TAGS[:4]
PARTICLES = ("", "吧", "呀", "啊", "呢")
ENDS = ("", "。", "！", "？", "?", "~", "…", "。。")
SEPARATORS = ("，", ",", "、", "；")
BASES = ("{d}给{a}转{m}元", "{d}帮我给{a}打钱{m}块", "{d}给{a}汇款{m}元")
PHRASES = ("", "明天", "每月5号", "下周五")
SLOW = dict(deadline=None, suppress_health_check=[HealthCheck.too_slow])


def _plain(base, phrase, alias, amount):
    return base.format(d=phrase, a=alias, m=amount)


@settings(max_examples=120, **SLOW)
@given(
    base=st.sampled_from(BASES),
    phrase=st.sampled_from(PHRASES),
    alias=st.sampled_from(ALIASES),
    amount=AMOUNTS,
    tag=st.sampled_from(TAGS),
    particle=st.sampled_from(PARTICLES),
    end=st.sampled_from(ENDS),
    separator=st.sampled_from(SEPARATORS + ("",)),
    follow=st.booleans(),
)
def test_a_polite_tag_in_its_place_leaves_the_plain_requests_card(
    base, phrase, alias, amount, tag, particle, end, separator, follow
):
    plain = _plain(base, phrase, alias, amount)
    if follow and separator:
        # Its own clause between two separators, the request going on after it.
        text = f"{plain}{separator}{tag}{particle}{separator}谢谢{end}"
        expected_plain = f"{plain}{separator}{separator}谢谢{end}"
    else:
        text = f"{plain}{separator}{tag}{particle}{end}"
        expected_plain = plain
    assert negation_words(text) == negation_words(expected_plain) == (), text
    assert recognize(text).intent == recognize(expected_plain).intent, text
    _, reference = _session()
    controller, chat = _session()
    expected = reference.receive(expected_plain)
    response = chat.receive(text)
    assert "negation" not in response["payload"], text
    assert _card_facts(response) == _card_facts(expected), (text, expected_plain)
    assert bool(response["card"]) == bool(expected["card"]), text
    assert controller.gateway.bank.execute_count == 0


@settings(max_examples=120, **SLOW)
@given(
    base=st.sampled_from(BASES),
    phrase=st.sampled_from(PHRASES),
    alias=st.sampled_from(ALIASES),
    amount=AMOUNTS,
    tag=st.sampled_from(TAGS),
    particle=st.sampled_from(PARTICLES),
    separator=st.sampled_from(SEPARATORS),
    place=st.sampled_from(("opening", "continued", "not_yet")),
)
def test_a_polite_tag_anywhere_else_still_holds_the_request(
    base, phrase, alias, amount, tag, particle, separator, place
):
    plain = _plain(base, phrase, alias, amount)
    text = {
        "opening": f"{tag}{particle}{separator}{plain}",
        "continued": f"{plain}{separator}{tag}着转",
        "not_yet": f"{plain}先{tag}",
    }[place]
    controller, chat = _session()
    response = chat.receive(text)
    assert not response["card"] and response["buttons"] == [], (text, response["status"])
    if tag in NEGATING_TAGS:
        assert negation_words(text), text
    assert controller.gateway.bank.execute_count == 0


@settings(max_examples=120, **SLOW)
@given(
    template=st.sampled_from(NEGATED),
    word=st.sampled_from(WORDS),
    tail=st.sampled_from(TAILS),
    phrase=st.sampled_from(("", "明天", "每月5号")),
    alias=st.sampled_from(ALIASES),
    amount=AMOUNTS,
    punct=st.sampled_from(PUNCT),
    tag=st.sampled_from(TAGS),
    separator=st.sampled_from(SEPARATORS + ("",)),
    end=st.sampled_from(ENDS),
)
def test_a_polite_tag_never_releases_another_negation(
    template, word, tail, phrase, alias, amount, punct, tag, separator, end
):
    held = template.format(w=word, t=tail, d=phrase, a=alias, m=amount, p=punct)
    text = f"{held}{separator}{tag}{end}"
    assert negation_words(text), text
    controller, chat = _session()
    response = chat.receive(text)
    assert not response["card"] and response["buttons"] == [], (text, response["status"])
    assert controller.gateway.bank.execute_count == 0


@settings(max_examples=300)
@given(
    text=st.text(
        alphabet=st.sampled_from(
            list("给转汇款打钱元块明天每月5号房东妈妈我和")
            + list("不别停取消算了免用要想先暂还急着好行可以吗吧呀，,。？! ")
        ),
        max_size=24,
    ),
    tag=st.sampled_from(TAGS),
    particle=st.sampled_from(PARTICLES),
    end=st.sampled_from(ENDS),
)
def test_a_final_polite_clause_is_removed_and_nothing_else(text, tag, particle, end):
    """Appending ",<tag>" to a text with no polite clause of its own removes exactly that
    clause, so the negation words it carries never change."""
    assume(without_polite_tags(text) == text)
    tail = f"，{tag}{particle}{end}"
    assert without_polite_tags(text + tail) == text
    assert negation_words(text + tail) == negation_words(text)


def test_the_five_tags_in_examples():
    released = [
        "给妈妈转500元，不用急",
        "给妈妈转500元不用急",
        "给妈妈转500元，不着急的",
        "给妈妈转500元，好不好？",
        "给妈妈转500元，行不行啊",
        "给妈妈转500元，可以吗",
        "给妈妈转500元可以吗？",
        "给房东转2000元，不用急，谢谢",
    ]
    for text in released:
        assert negation_words(text) == (), text
        assert recognize(text).intent == "transfer", text
        controller, chat = _session()
        assert chat.receive(text)["card"], text
    held = [
        "不用急，给妈妈转500元",
        "给妈妈转500元，不用急着转",
        "好不好给妈妈转500元",
        "给妈妈转500元，不行",
        "给妈妈转500元，不可以吗",
        "给房东转2000先不用急",
        "给房东转2000，先不用急",
        "给妈妈转500元，不着急了",
        "算了，不用急",
    ]
    for text in held:
        controller, chat = _session()
        assert not chat.receive(text)["card"], text
    # A genuine question about the transfer stays a question.
    assert recognize("我能给妈妈转500元吗").intent == "clarify"


def test_no_frozen_suite_line_changes():
    """Equivalence by construction: no frozen zh-suite or intent-eval line has a tag."""
    import json

    from tests.unit.test_s5_negation import DATA

    lines = [
        json.loads(line)["text"]
        for line in (DATA / "colloquial.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    for row in json.loads((DATA / "cases.json").read_text(encoding="utf-8")):
        lines.extend(turn["task"] for turn in [*row["request"]["context"], row["request"]])
    assert len(lines) > 150
    assert [text for text in lines if without_polite_tags(text) != text] == []
