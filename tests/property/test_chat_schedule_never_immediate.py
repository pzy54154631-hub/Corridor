"""A dated chat request yields a future scheduled card or no card, never a transfer (G11)."""

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from corridor.agent.domestic_session import DomesticSession
from corridor.agent.domestic_slots import has_date_request
from corridor.ui.controller import DemoController

DATES = (
    "明天",
    "下周五",
    "下周一",
    "月底",
    "每月15号",
    "每个月二十日",
    "每月31号",
    "15号",
    "20号",
    "当月25号",
    "10月20号",
    "2026-10-20",
    "2025-10-20",
    "后天",
    "下星期五",
    "下礼拜三",
    "三天后",
    "今晚",
    "明天下午3点",
    "下下周五",
    "月初",
    "定时",
    "生日前两天",
    "每月15号持续一年",
    "明天和下周五",
)
ALIASES = ("妈妈", "Chen", "房东", "同学", "室友", "Lin")
TEMPLATES = (
    "{date}给{alias}转{amount}元",
    "给{alias}转{amount}元，{date}",
    "{alias}那边{date}转{amount}块",
    "{date}帮我给{alias}转{amount}",
)


@settings(deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    date=st.sampled_from(DATES),
    alias=st.sampled_from(ALIASES),
    amount=st.integers(min_value=1, max_value=5000),
    template=st.sampled_from(TEMPLATES),
)
def test_dated_chat_text_never_previews_an_immediate_transfer(date, alias, amount, template):
    text = template.format(date=date, alias=alias, amount=amount)
    if not has_date_request(text):
        return
    controller = DemoController()
    controller.start_session()
    response = DomesticSession(controller).receive(text)
    card = response["card"]
    assert not card or (
        card["kind"] == "schedule_transfer"
        and card["spa"]["start"] > controller.state.now
        and card["execution_window"]["start"] == card["spa"]["start"]
    ), (text, card.get("kind"))
    assert controller.gateway.bank.execute_count == 0
    assert not controller.state.user_inputs
