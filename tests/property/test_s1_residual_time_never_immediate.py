"""S1: time words outside the frozen lexicon never yield an immediate transfer card."""

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from corridor.agent.domestic_schedule import residual_time, schedule_candidate
from corridor.agent.domestic_session import DomesticSession
from corridor.agent.domestic_slots import has_date_request
from corridor.ui.controller import DemoController

# The B residual list: none of these is in the frozen has_date_request lexicon.
RESIDUAL = (
    "第二天",
    "隔天",
    "改天",
    "过几天",
    "晚点",
    "回头",
    "等会",
    "下班后",
    "10/20",
    "10.20",
    "10-20",
    "双十一",
    "圣诞节",
    "情人节",
    "七夕",
    "过阵子",
    "有空",
)
# Frozen-lexicon words already clarified before S1; the residual check must keep them so.
LEXICON = ("后天", "月初", "国庆", "春节", "元旦")
ALIASES = ("妈妈", "Chen", "房东", "同学")
TEMPLATES = (
    "{word}给{alias}转{amount}元",
    "给{alias}转{amount}元，{word}",
    "{alias}那边{word}转{amount}块",
    "{word}帮我给{alias}转{amount}",
    "麻烦{word}再给{alias}转{amount}元",
)


def test_the_residual_words_are_outside_the_frozen_lexicon():
    assert not any(has_date_request(word) for word in RESIDUAL)
    assert all(residual_time(word) for word in RESIDUAL)
    assert all(has_date_request(word) for word in LEXICON)


@settings(deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    word=st.sampled_from(RESIDUAL + LEXICON),
    alias=st.sampled_from(ALIASES),
    amount=st.integers(min_value=1, max_value=5000),
    template=st.sampled_from(TEMPLATES),
)
def test_residual_time_text_never_previews_an_immediate_transfer(word, alias, amount, template):
    text = template.format(word=word, alias=alias, amount=amount)
    controller = DemoController()
    controller.start_session()
    assert schedule_candidate(text, {"now": controller.state.now})["status"] == "unresolved"
    response = DomesticSession(controller).receive(text)
    assert not response["card"], (text, response["card"].get("kind"))
    assert response["needs_clarification"] and not response["buttons"]
    assert controller.gateway.bank.execute_count == 0
    assert not controller.state.user_inputs
