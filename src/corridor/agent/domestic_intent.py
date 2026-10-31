"""Bounded Chinese intent candidates; never an execution or authorization source."""

import re
from typing import Literal

from corridor.agent.domestic_negation import (
    compensation_negation,
    negation_words,
    without_polite_tags,
)
from corridor.agent.domestic_scam_phrase import scam_phrase
from corridor.agent.domestic_schedule import aa_candidate, schedule_candidate
from corridor.agent.domestic_slots import extract_slots, has_date_request, normalized_money
from corridor.agent.domestic_subscription import cancel_negation
from corridor.agent.domestic_wealth import wealth_action
from corridor.schemas.base import Model

IntentName = Literal[
    "transfer",
    "balance_query",
    "bill_report",
    "investment",
    "card_apply",
    "card_lost",
    "card_freeze",
    "card_unlock",
    "card_limit",
    "card_restriction",
    "subscription_query",
    "subscription_cancel",
    "birthday",
    "password_change",
    "stop",
    "handoff",
    "out_of_scope",
    "clarify",
]


class IntentCandidate(Model):
    intent: IntentName
    needs_clarification: bool


SCENARIOS = {
    "transfer": "transfers",
    "balance_query": "billing",
    "bill_report": "billing",
    "investment": "wealth",
    "card_apply": "cards",
    "card_lost": "cards",
    "card_freeze": "cards",
    "card_unlock": "cards",
    "card_limit": "cards",
    "card_restriction": "cards",
    "subscription_query": "subscriptions",
    "subscription_cancel": "subscriptions",
    "birthday": "birthday",
    "password_change": "sensitive",
    "stop": "control",
    "handoff": "control",
    "out_of_scope": "out_of_scope",
    "clarify": "clarify",
}


def normalized_transfer(text: str) -> str:
    """Render unsigned exact candidates for later explicit device review."""
    return normalized_money(text)


# Natural interruption: the whole utterance must consist of stop phrases plus filler, so a
# sentence naming any object (银行卡先暂停, 那个会员停掉) keeps its own business intent.
_STOP_PHRASES = tuple(
    sorted(
        {
            "停",
            "停一下",
            "停一停",
            "停下来",
            "停下",
            "停住",
            "停掉",
            "停止",
            "停止执行",
            "停止计划",
            "停止转账",
            "暂停",
            "暂停一下",
            "暂停执行",
            "先停",
            "别做了",
            "不做了",
            "先别做了",
            "别执行了",
            "别执行",
            "先别执行了",
            "先别执行",
            "不要执行了",
            "不要执行",
            "不执行了",
            "别弄了",
            "别办了",
            "别搞了",
            "不办了",
            "不弄了",
            "别动",
            "别继续了",
            "别继续",
            "不要继续了",
            "不要继续",
            "不用继续了",
            "别往下做了",
            "等一下",
            "等一等",
            "等等",
            "等会儿",
            "等会",
            "慢着",
            "打住",
            "中止",
            "终止",
            "叫停",
            "取消执行",
            "取消计划",
            "取消生日计划",
            "生日计划取消",
            "取消后续",
            "别转了",
            "别转",
            "不转了",
            "不要转了",
            "不要转",
            "stop",
            "pause",
            "hold on",
            "wait",
        },
        key=len,
        reverse=True,
    )
)
_STOP_FILLER = tuple(
    sorted(
        {
            "请",
            "麻烦",
            "你",
            "帮我",
            "给我",
            "先",
            "一下",
            "吧",
            "啊",
            "呀",
            "哈",
            "了",
            "呢",
            "嗯",
            "哦",
            "喂",
            "那",
            "那就",
            "好",
            "好吧",
            "算了",
            "还是",
            "马上",
            "立刻",
            "赶紧",
            "快",
            "全部",
            "都",
            "所有",
            "把",
            "这个",
            "那个",
            "这些",
            "刚才的",
            "之前的",
            "后面的",
            "后续的",
            "剩下的",
            "生日计划",
            "计划",
            "操作",
            "任务",
            "步骤",
            "转账",
            "please",
        },
        key=len,
        reverse=True,
    )
)
# A request to return the funds a plan locked: the task panel's compensation, not a new plan.
_LOCKED_FUNDS = re.compile(r"锁住|锁定|锁着|锁了|锁的|锁上|冻结的|冻住的|预算")
_RETURN_FUNDS = re.compile(
    r"退回|退还|返还|还回|放回|退到|转回|拿回|取回|释放|解锁|解除锁定|撤销锁定|退给我|还给我|"
    r"回到活期|回活期"
)
# Paying someone (给房东转500补偿) is a transfer, never the plan's compensation: routing it
# there could interrupt a running plan the user did not mention.
_OUTGOING = re.compile(r"转(?!回|到活期|入活期|进活期)|汇款|汇给|打钱|付给|付款|支付|还钱|赔", re.I)
# Listing past transactions is the green transaction-history query, not a period report.
_HISTORY = ("交易记录", "交易明细", "流水", "账单明细", "收支明细", "收支记录")
# A bare 明细 counts too, but not inside 说明细节 / 明细化 style words.
_BARE_DETAIL = re.compile(r"(?<!说)明细(?!节|则|化)")
# A named month or year keeps the period report, which honours it; the listing does not.
_PERIOD = re.compile(r"[月年]")
# C1 (review of B1): switching something off cancels a subscription only when what is
# switched off is the renewal, the debit or the membership itself (关闭自动续费, 把会员关掉);
# a reminder, notice or text message (关闭到期提醒, 把续费短信关掉) is not a cancellation.
_SWITCH_OFF = re.compile(
    r"^(?!.*(?:提醒|通知|短信|消息|推送)).*(?:"
    r"(?:关闭|关掉|关了)[^，,。；;！!？?]{0,8}(?:自动续费|续费|扣费|扣款|代扣|订阅|会员)"
    r"|(?:自动续费|续费|扣费|扣款|代扣|订阅|会员)[^，,。；;！!？?]{0,8}(?:关闭|关掉|关了))"
)
_ANALYSIS = (
    "统计",
    "分析",
    "报告",
    "汇总",
    "分类",
    "类别",
    "异常",
    "可疑",
    "不认识",
    "重复扣款",
    "年度",
    "月度",
)


def natural_stop(text: str) -> bool:
    """True only when the utterance is nothing but interruption wording and filler."""
    rest = text.strip().casefold()
    matched = False
    for phrase in _STOP_PHRASES:
        if phrase in rest:
            matched = True
            rest = rest.replace(phrase, " ")
    if not matched:
        return False
    for word in _STOP_FILLER:
        rest = rest.replace(word, " ")
    return not re.sub(r"[\s，,。.；;：:！!？?、~～…]+", "", rest)


def compensation_request(text: str) -> bool:
    """Returning locked plan funds; a card sentence keeps the card intents."""
    if "卡" in text or _OUTGOING.search(text):
        return False
    return "补偿" in text or bool(_LOCKED_FUNDS.search(text) and _RETURN_FUNDS.search(text))


def history_request(text: str) -> bool:
    return any(word in text for word in _HISTORY) or bool(_BARE_DETAIL.search(text))


def history_listing(text: str) -> bool:
    """A plain listing request; analysis or period wording keeps the period report."""
    return (
        history_request(text)
        and not any(word in text for word in _ANALYSIS)
        and not _PERIOD.search(text)
    )


def recognize(text: str, context: dict | None = None) -> IntentCandidate:
    """Scripted pipeline baseline. Ambiguous financial values stay unsigned."""
    context = context or {}
    value = text.strip()
    lower = value.casefold()
    intent, _, _ = _route(value, context)

    def has(*words):
        return any(word in lower for word in words)

    needs = intent in {
        "investment",
        "card_limit",
        "card_restriction",
        "card_unlock",
        "password_change",
        "clarify",
    }
    if intent == "transfer":
        slots = extract_slots(value, context, intent=intent)["parameters"]
        needs = not (slots["recipient"] and slots["amount"])
        # Only a complete closed-grammar parse clears the schedule/AA clarification flag.
        if has("aa"):
            needs = aa_candidate(value, context)["status"] != "complete"
        else:
            schedule = schedule_candidate(value, context)["status"]
            if schedule == "unresolved" or (has_date_request(text) and schedule != "complete"):
                needs = True
    elif intent == "subscription_cancel":
        needs = "S1" not in text and len(context.get("cancellable", [])) != 1
    return IntentCandidate(intent=intent, needs_clarification=needs)


def negation_hold(text: str, context: dict | None = None) -> tuple[str, ...]:
    """S5: the negation words that held a money-moving request at clarification, if any."""
    return _route(text.strip(), context or {})[1]


def scam_phrase_hold(text: str, context: dict | None = None) -> dict[str, list[str]] | None:
    """S6 R7: the fixed labels of both closed lists that held a money-moving request (an
    impersonated authority and a freeze / safe-account / transfer-now demand), if any."""
    return _route(text.strip(), context or {})[2]


def _route(value: str, context: dict) -> tuple[str, tuple[str, ...], dict[str, list[str]] | None]:
    """The lexical intent, the negation words that held a money-moving one (S5), and the
    phrase-gate labels that held one (S6 R7)."""
    lower = value.casefold()
    # S5: a transfer, split-bill, birthday-plan or refund request carrying a negation,
    # cancel or stop word is held at clarification; it never becomes a new card.
    held: tuple[str, ...] = ()
    scam: dict[str, list[str]] | None = None

    def has(*words):
        return any(word in lower for word in words)

    previous = context.get("previous_intent")
    # After a subscription listing, a deictic or 停掉 names that subscription (the existing
    # follow-up rule below); only wording about plans or transfers keeps the interruption.
    subscription_followup = (
        previous in {"subscription_query", "subscription_cancel"}
        and has("那个", "这些", "停掉", "取消")
        and not has("计划", "生日", "转", "操作", "任务", "步骤", "执行")
    )
    if has(
        "笑话",
        "天气",
        "下雨",
        "股票",
        "炒股",
        "双色球",
        "彩票",
        "写诗",
        "写首诗",
        "写歌",
        "星座",
        "菜谱",
        "机票",
        "joke",
        "weather",
        "stock",
    ) or re.search(r"(?:写|来|作).{0,8}诗", value):
        intent = "out_of_scope"
    elif has("密码", "口令", "password", "pin码"):
        intent = "password_change"
    elif value in {
        "停",
        "停止",
        "先停",
        "停下",
        "取消后续",
        "先别执行了",
        "别做了",
        "stop",
    } or re.search(
        r"不转了|别转了|停止转账|取消(?:刚才的|刚才|这个|这笔|之前的)?转账",
        value,
    ):
        intent = "stop"
    elif natural_stop(value) and not subscription_followup:
        intent = "stop"
    elif has("转人工", "人工客服", "找客服", "找人帮忙", "坐席", "handoff"):
        intent = "handoff"
    elif (scam := scam_phrase(value)) is not None:
        # S6 R7: an impersonated authority and a freeze / safe-account / transfer-now
        # demand in one money-moving request: no card, as with a negation. C2: decided
        # before every branch that can move money (birthday plans, refunds, purchases),
        # so no earlier branch can take the sentence to a plan or a card.
        intent = "clarify"
    elif compensation_request(value):
        # Returning a birthday plan's locked budget belongs to that plan's scenario; the
        # session routes it to the existing compensation flow, never to a new draft.
        # Its plan-cancel words stay allowed; 不要退回 / 别退 hold the refund card (S5).
        held = compensation_negation(value)
        intent = "birthday"
    elif (has("生日", "生辰", "鲜花", "蛋糕") and not has("转", "汇款")) or (
        previous == "birthday" and has("那个", "礼物", "安排", "照旧")
    ):
        held = negation_words(value)
        intent = "birthday"
    elif has("订阅", "会员", "续费", "代扣", "自动扣费", "subscription") or (
        previous in {"subscription_query", "subscription_cancel"}
        and has("那个", "这些", "停掉", "取消")
    ):
        intent = (
            "subscription_cancel"
            if has(
                "取消",
                "退了",
                "停掉",
                "别续",
                "不续",
                "不想续",
                "不要续",
                # B1: more closed cancel phrases (退订, 不再续订, 关闭自动续费 …).
                "退订",
                "退掉",
                "不再续",
                "别再续",
                "不用续",
                "解约",
            )
            or _SWITCH_OFF.search(value)
            else "subscription_query"
        )
        if intent == "subscription_cancel":
            # B1: the chat can now prepare the cancellation card itself, so a negation left
            # after the request's own cancel phrase (先别取消会员) holds it, as in S5.
            held = cancel_negation(value)
    elif has("卡", "挂失") or (str(previous).startswith("card_") and has("这张", "那张", "上一张")):
        if has("丢", "找不着", "找不到", "遗失", "落在", "挂失") and not has("解挂", "解除挂失"):
            intent = "card_lost"
        elif has("申请", "办张", "开张", "来张", "虚拟卡"):
            intent = "card_apply"
        elif has("额度", "限额", "上限"):
            intent = "card_limit"
        elif has("境外", "网上", "限制", "线上"):
            intent = "card_restriction"
        elif has("解冻", "解锁", "解挂", "解除挂失", "恢复", "重新用"):
            intent = "card_unlock"
        elif has("冻结", "别用", "别刷", "停用", "锁住", "暂停"):
            intent = "card_freeze"
        else:
            intent = "clarify"
    elif has("转", "汇款", "打钱", "还钱", "房租", "aa", "再给他", "再给她", "transfer"):
        # A negated, historical, incoming, hypothetical or interrogative sentence is not
        # positive evidence authorizing an outgoing transfer preview. S5: the negation
        # check is the shared gate (any position, any clause), which subsumes the earlier
        # 不要|先别|别给|不想|先不 list; only its closed neutral compounds (别忘了 …) pass.
        held = negation_words(value)
        intent = (
            "clarify"
            if re.search(
                r"给我(?=转|汇|打钱|[0-9零〇一二两三四五六七八九十百千万])|"
                r"转给我|转了|转过|吗|么|是否|如果|能否|可否|能不能|可不可以|[？?]",
                # R8: a polite tag in its allowed place (……，可以吗 / ……，好不好？) does not
                # turn the request into a question.
                without_polite_tags(value),
            )
            else "transfer"
        )
    elif has(
        "理财",
        "闲钱",
        "投资",
        "产品",
        "赎回",
        "稳健",
        "收益",
        "测评",
        "保本",
        "稳一点",
        # B1: an explicit purchase verb names the wealth service by itself.
        "申购",
        "认购",
        "买入",
    ) or (previous == "investment" and has("那个", "那只", "买", "五万", "两万")):
        intent = "investment"
        if wealth_action(value):
            # B1: a purchase or redemption can now reach a card from chat, so it passes
            # the same negation gate as every other money-moving request (S5).
            held = negation_words(value)
    elif (
        has(
            "账单", "消费", "花哪", "花了", "扣款", "支出", "账目", "钱去哪", "花销", "花费", "bill"
        )
        or history_request(value)
        or (previous == "bill_report" and has("那个", "今年", "上月", "上个月", "再查"))
    ):
        intent = "bill_report"
    elif has("余额", "还剩", "剩多少", "有多少钱", "balance"):
        intent = "balance_query"
    else:
        intent = "clarify"
    return ("clarify" if held else intent), held, scam
