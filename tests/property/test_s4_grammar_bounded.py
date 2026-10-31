"""S4: the 自动 filler and the narrative split bill keep the chat grammar closed.

Generated sentences either yield exactly the form's own card (schedule_transfer with a
future start, or aa_request with trusted participants) or no card; never an immediate
transfer, a dispatch or a signed input.
"""

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from corridor.agent.domestic_session import DomesticSession
from corridor.ui.controller import DemoController

PHRASES = ("每月5号", "每月15号", "明天", "下周五", "月底", "20号", "")
PLACEMENTS = (
    "{phrase}自动给{alias}转{amount}元",
    "自动{phrase}给{alias}转{amount}元",
    "{phrase}给{alias}自动转{amount}元",
    "给{alias}转{amount}元，{phrase}自动",
    "{phrase}自动自动给{alias}转{amount}元",
    # Verifier: words after 自动 that are not the verb phrase (stop, cancel, negation).
    "{phrase}自动停止给{alias}转{amount}元",
    "{phrase}自动取消给{alias}转{amount}元",
    "{phrase}自动不给{alias}转{amount}元",
)
ALIASES = ("妈妈", "房东", "同学", "室友")
LEADS = ("昨天", "前天", "上周五", "刚才", "", "明天", "下周", "上次", "今天下午", "昨天下午")
WHO = ("和室友、同学", "跟同学", "我和室友", "我们四个人", "和小王", "和Lin")
SPENT = ("一共花了", "总共花了", "花了", "一共花", "付了", "垫付了")
ASK = ("帮我发起AA收款", "帮我AA一下", "发个AA", "明天帮我发起AA", "AA了", "每人100，帮我AA")


def _session():
    controller = DemoController()
    controller.start_session()
    return controller, DomesticSession(controller)


@settings(deadline=None, max_examples=60, suppress_health_check=[HealthCheck.too_slow])
@given(
    phrase=st.sampled_from(PHRASES),
    placement=st.sampled_from(PLACEMENTS),
    alias=st.sampled_from(ALIASES),
    amount=st.integers(min_value=1, max_value=5000),
)
def test_auto_text_is_a_future_schedule_card_or_no_card(phrase, placement, alias, amount):
    text = placement.format(phrase=phrase, alias=alias, amount=amount)
    controller, chat = _session()
    response = chat.receive(text)
    card = response["card"]
    assert not card or (
        card["kind"] == "schedule_transfer"
        and card["spa"]["start"] > controller.state.now
        and card["execution_window"]["start"] == card["spa"]["start"]
    ), (text, card.get("kind"))
    if card:
        # Only 自动 touching the one recognised phrase is consumed.
        assert phrase and ("自动" + phrase in text or phrase + "自动" in text), text
        assert text.count("自动") == 1, text
        assert not any(word in text for word in ("停止", "取消", "不给")), text
    assert controller.gateway.bank.execute_count == 0
    assert not controller.state.user_inputs


@settings(deadline=None, max_examples=80, suppress_health_check=[HealthCheck.too_slow])
@given(
    lead=st.sampled_from(LEADS),
    who=st.sampled_from(WHO),
    spent=st.sampled_from(SPENT),
    amount=st.sampled_from((300, 460, 100, 61)),
    ask=st.sampled_from(ASK),
)
def test_narrative_split_bill_is_the_aa_card_or_no_card(lead, who, spent, amount, ask):
    text = f"{lead}{who}聚餐{spent}{amount}元，{ask}"
    controller, chat = _session()
    response = chat.receive(text)
    card = response["card"]
    roster = set(chat.aa_roster())
    assert not card or card["kind"] == "aa_request", (text, card.get("kind"))
    if card:
        assert set(card["objects"]["participant_refs"]) <= roster
        assert lead in {"昨天", "前天", "上周五", "刚才", ""}, text
        assert spent.endswith("了") and spent not in {"付了", "垫付了"}, text
        assert ask in {"帮我发起AA收款", "帮我AA一下", "发个AA"}, text
        assert who not in {"我们四个人", "和小王", "和Lin"}, text
    assert controller.gateway.bank.execute_count == 0
    assert not controller.state.user_inputs
