"""C1 property tests (review of the B batch): generated wording, never a card.

1. Any time phrase on the three B1 paths (purchase review, redemption card, subscription
   cancellation card) yields no review, no card and no signed input.
2. A time clause at the start or after a separator, followed by any of the inserted words
   the review found (记得, 麻烦, 你, 我, 马上, 就), or a 等…再 deferral, never yields an
   immediate transfer card.
3. A deferral or topic word in front of a polite tag (暂时 / 现在 / 目前 / 暂且 / 的事 /
   这事 …) keeps the negation; so do spaces and invisible characters after 先 / 暂 / 还.
4. After an unfinished purchase, a turn about anything else (a transfer, a bill, a
   question, a supposition, an incoming payment) never fills the purchase.
"""

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from corridor.agent.domestic_negation import negation_words
from corridor.agent.domestic_session import DomesticSession
from corridor.crypto.jcs import H
from corridor.ui.controller import DemoController, demo_state

SLOW = dict(deadline=None, suppress_health_check=[HealthCheck.too_slow], max_examples=60)
TIMES = (
    "明天",
    "下周一",
    "后天",
    "晚饭后",
    "睡前",
    "下个月",
    "下个月再",
    "月底",
    "到期以后",
    "到期再",
    "等工资到账以后",
    "11月30日",
    "下周五下午",
    "过几天",
    "回头",
    "改天",
    "发工资后",
    "开完会之后",
    "等她回来再",
)
PURCHASES = (
    "{t}帮我申购2000元稳健30天持有",
    "{t}申购2000元灵活10天理财",
    "帮我申购2000元稳健30天持有，{t}",
)
REDEMPTIONS = ("{t}把我持有的理财全部赎回", "{t}全部赎回我的理财", "全部赎回我的理财，{t}")
CANCELS = ("{t}取消会员服务A", "{t}把每月扣49块的会员取消掉", "取消会员服务A，{t}")


def _session(showcase=False):
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


def _nothing(response):
    return (
        not response["card"] and response["buttons"] == [] and "purchase" not in response["payload"]
    )


@settings(**SLOW)
@given(
    time=st.sampled_from(TIMES),
    kind=st.sampled_from(("purchase", "redeem", "cancel")),
    template=st.integers(min_value=0, max_value=2),
)
def test_a_time_phrase_never_reaches_a_card_on_the_new_paths(time, kind, template):
    texts = {"purchase": PURCHASES, "redeem": REDEMPTIONS, "cancel": CANCELS}[kind]
    text = texts[template].format(t=time)
    session = _session(showcase=kind == "redeem")
    before = _quiet(session)
    response = session.receive(text)
    assert _nothing(response), (text, response["status"], response["payload"])
    assert _quiet(session) == before, text


FRAGMENTS = ("晚饭后", "睡前", "开完会后", "收到货后", "下班回家后", "到家的时候")
INSERTED = ("", "记得", "麻烦", "你", "我", "马上", "就", "麻烦你")
DEFERRALS = ("等她到家了再", "等我确认一下再", "吃完饭再", "到家了再", "等工资到了再")
ALIASES = ("妈妈", "房东", "Chen", "新联系人")


@settings(**SLOW)
@given(
    fragment=st.sampled_from(FRAGMENTS),
    inserted=st.sampled_from(INSERTED),
    alias=st.sampled_from(ALIASES),
    amount=st.integers(min_value=1, max_value=9999),
    separator=st.sampled_from(("", "，", ",")),
)
def test_a_time_clause_never_yields_an_immediate_transfer(
    fragment, inserted, alias, amount, separator
):
    text = f"{fragment}{separator}{inserted}给{alias}转{amount}元"
    session = _session()
    response = session.receive(text)
    assert (response["card"] or {}).get("kind") != "transfer", text
    assert session.controller.gateway.bank.execute_count == 0


@settings(**SLOW)
@given(
    deferral=st.sampled_from(DEFERRALS),
    alias=st.sampled_from(ALIASES),
    amount=st.integers(min_value=1, max_value=9999),
    tail=st.booleans(),
)
def test_a_deferred_payment_never_yields_an_immediate_transfer(deferral, alias, amount, tail):
    # After the request only a clause opened by 等 defers it (给妈妈转500元，等她到家了); a
    # bare "到家了" after the request is not read as a deferral.
    text = (
        f"给{alias}转{amount}元，{deferral.removesuffix('再')}"
        if tail and deferral.startswith("等")
        else f"{deferral}给{alias}转{amount}元"
    )
    response = _session().receive(text)
    assert (response["card"] or {}).get("kind") != "transfer", text


PREFIXES = (
    "暂时",
    "暂且",
    "现在",
    "目前",
    "眼下",
    "一时",
    "今天",
    "这会儿",
    "的事",
    "这事",
    "这笔",
)
TAGS = ("不着急", "不用急")
INVISIBLE = (" ", "\u3000", "\u200b", "\u2060", "\ufeff")


@settings(**SLOW)
@given(
    prefix=st.sampled_from(PREFIXES),
    tag=st.sampled_from(TAGS),
    separator=st.sampled_from(("", "，", ",")),
    alias=st.sampled_from(ALIASES),
    amount=st.integers(min_value=1, max_value=9999),
)
def test_a_prefix_before_a_polite_tag_keeps_the_negation(prefix, tag, separator, alias, amount):
    text = f"给{alias}转{amount}元{separator}{prefix}{tag}"
    assert negation_words(text), text
    response = _session().receive(text)
    assert not response["card"], text


@settings(**SLOW)
@given(
    word=st.sampled_from(("先", "暂", "还")),
    gap=st.sampled_from(INVISIBLE),
    tag=st.sampled_from(TAGS),
    alias=st.sampled_from(ALIASES),
)
def test_spaces_or_invisible_characters_never_release_not_yet(word, gap, tag, alias):
    text = f"给{alias}转500元，{word}{gap}{tag}"
    assert negation_words(text), repr(text)
    assert not _session().receive(text)["card"], repr(text)


OTHER_TURNS = (
    "给{a}转{m}元吗",
    "如果给{a}转{m}呢",
    "给我转{m}",
    "{a}给我转了{m}",
    "上个月花了{m}元吗",
    "{m}元能有多少收益？",
    "给{a}转{m}元",
    "我和室友AA了{m}元",
    "上个月那笔{m}元是什么？",
)


@settings(**SLOW)
@given(
    opening=st.sampled_from(("我想买灵活10天理财", "我想申购稳健30天持有")),
    template=st.sampled_from(OTHER_TURNS),
    alias=st.sampled_from(("妈妈", "房东", "Chen")),
    amount=st.integers(min_value=100, max_value=9999),
)
def test_another_business_turn_never_fills_an_unfinished_purchase(opening, template, alias, amount):
    session = _session()
    first = session.receive(opening)
    product = first["payload"]["purchase_parse"]["product_ref"]
    response = session.receive(template.format(a=alias, m=amount))
    purchase = response["payload"].get("purchase") or {}
    assert purchase.get("product_ref") != product, (opening, template, purchase)
    assert session.controller.gateway.bank.execute_count == 0
