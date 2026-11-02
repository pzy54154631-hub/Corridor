"""Only an exact third-party clause may be excluded from the negation scan."""

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from corridor.agent.domestic_intent import recognize
from corridor.agent.domestic_negation import negation_words
from corridor.agent.domestic_session import DomesticSession
from corridor.ui.controller import DemoController


@pytest.mark.parametrize(
    ("text", "status"),
    [
        ("房东说不用了，转 500 补偿", "clarify"),
        ("房东说不用了，给房东转500元补偿", "awaiting_input"),
    ],
)
def test_narrow_report_does_not_negate_the_later_transfer(text, status):
    assert negation_words(text) == ()
    controller = DemoController()
    controller.start_session()
    response = DomesticSession(controller).receive(text)
    assert response["intent"] == "transfer"
    assert response["status"] == status
    assert "negation" not in response["payload"]
    assert controller.gateway.bank.execute_count == 0


@pytest.mark.parametrize("text", ["别给妈妈转500", "不用给妈妈转了", "上次给妈妈转过500吗"])
def test_original_negative_requests_still_have_no_card(text):
    controller = DemoController()
    controller.start_session()
    response = DomesticSession(controller).receive(text)
    assert response["card"] == {} and response["buttons"] == []
    assert controller.gateway.bank.execute_count == 0


@settings(max_examples=200, deadline=None)
@given(
    word=st.sampled_from(["别", "不用", "不要", "先不", "取消", "停止", "不", "don't", "stop"]),
    gap=st.sampled_from(["", " ", "\u200b"]),
    template=st.sampled_from([
        "房东说不用了，{w}给妈妈转500",
        "房东说不用了，给妈妈{w}转500",
        "房东说不用了，给妈妈转500，{w}",
        "{w}房东说不用了，转500补偿",
        "房东说{w}转账，转500补偿",
        "房东说不用了，转500补偿，{w}",
    ]),
)
def test_mixed_report_and_negated_action_never_passes(word, gap, template):
    text = template.format(w=gap.join(word) if not word.isascii() else word)
    assert negation_words(text)
    assert recognize(text).intent in {"clarify", "stop"}
