"""Closed chat grammars for scheduled transfers and named split bills.

The results are unsigned candidates for the same preparers the business forms call.
Every value comes from the user's words, the simulated clock or trusted contacts.
Anything outside the grammar is unresolved; it never becomes an immediate transfer.
"""

import calendar
import re
from datetime import date, timedelta
from fractions import Fraction

from corridor.agent.domestic_negation import compact, negation_words
from corridor.agent.domestic_slots import chinese_number, has_date_request, money_candidate
from corridor.schemas.money import fraction_string
from corridor.stores.bank import business_date

_DAY = r"(?P<day>[0-9]{1,2}|[一二三四五六七八九十]{1,3})"
_MONTH = r"(?P<month>1[0-2]|0?[1-9]|十[一二]?|[一二三四五六七八九])"
# A recognised phrase must end at a clause boundary or the transfer verb phrase, so
# qualifiers such as 前/后/之前/下午/楼 leave the phrase to the residual-time check.
# S4: the filler 自动 may sit between the phrase and the verb (每月5号自动给房东转), but
# only when 自动 itself ends at the same boundary or verb phrase: 自动停止/自动不给/自动取消
# or 18号自动售货机 leave the phrase unrecognised, exactly as before the filler existed.
_BOUNDARY = r"$|[\s，,。；;！!]|给|转|汇|打钱|还|向|往|把|帮|请|再"
_RIGHT = rf"(?=(?:自动\s*)?(?:{_BOUNDARY}))"
# S4: 自动 ("automatically") is consumed only when it touches the one recognised phrase;
# anywhere else it names no time, so it is a residual time word and the chat asks "when?".
_FILLER = "自动"
_NUMERALS = "零〇一二两三四五六七八九十"
_GRAMMAR = (
    ("birthday", re.compile(r"生日前(?:两|2)天")),
    ("monthly", re.compile(rf"每个?月(?:的)?{_DAY}[号日]{_RIGHT}")),
    ("iso", re.compile(rf"(?<!\d)(?P<iso>\d{{4}}-\d{{2}}-\d{{2}})(?!\d){_RIGHT}")),
    ("month_day", re.compile(rf"(?<![\d年{_NUMERALS}]){_MONTH}月{_DAY}[号日]{_RIGHT}")),
    ("current_month", re.compile(rf"当月{_DAY}[号日]{_RIGHT}")),
    ("tomorrow", re.compile(rf"明天{_RIGHT}")),
    ("next_week", re.compile(rf"(?<!下)下周(?P<weekday>[一二三四五六日天]){_RIGHT}")),
    ("month_end", re.compile(rf"(?<![下上个本这当每月])月底{_RIGHT}")),
    ("day", re.compile(rf"(?<![\d月第{_NUMERALS}]){_DAY}[号日]{_RIGHT}")),
)
# Recurrence outside "every month on day N" is unsupported, never a one-off transfer.
_RECURRENCE = re.compile(r"每(?:个)?月|每(?:个)?(?:周|星期|礼拜)|每天|每日|每年")
# Any payer wording (the customer paying out, or someone else having paid) is not a
# collection request: asking the actual payer to pay the customer would reverse the debt.
_PAYMENT_VERB = re.compile(r"转|汇|打钱|还钱|付|垫|买单|埋单|请客|结账|掏钱")
# A split that already happened is a historical statement, not a request.
_HISTORICAL_AA = re.compile(r"aa\s*[了过]", re.I)
# The vault tokenizer rewrites the numeral in 一起 as 1起.
_TOGETHER = r"(?:一起|1起)?"
# S4: the closed past words a narrative split bill may open with (上周五 -> 上周5).
_PAST_LEAD = r"(?:昨天|昨晚|前天|前晚|刚才|刚刚|上个?周末|上周[一二三四五六日天1-6])"
_PER_PERSON = re.compile(r"每人|人均|每个人|一人")
_SEPARATOR = r"\s*[、,，和跟与及]\s*"
_COUNT = re.compile(
    r"(?:我们|咱们|一共|共)\s*([0-9零〇一二两三四五六七八九十]+)\s*(?:个人|个|人|位)"
)
# Time words outside the frozen has_date_request lexicon. Text the grammar did not consume
# that still names a time is clarified ("when?"); it never yields an immediate card.
_RESIDUAL_WORDS = (
    "第二天",
    "第2天",
    "隔天",
    "隔日",
    "改天",
    "改日",
    "过几天",
    "过些天",
    "过些日子",
    "过段时间",
    "过一段时间",
    "过阵子",
    "过一阵",
    "晚点",
    "晚些",
    "晚一点",
    "迟点",
    "迟些",
    "早点",
    "回头",
    "等会",
    "等一会",
    "等一下",
    "等下",
    "待会儿",
    "下班后",
    "下班以后",
    "下班了",
    "放学后",
    "下课后",
    "发工资",
    "发薪",
    "工资到账",
    "月初",
    "年初",
    "年前",
    "年后",
    "节前",
    "节后",
    "过年",
    "新年",
    "跨年",
    "除夕",
    "元宵",
    "清明",
    "五一节",
    "五一假",
    "五一前",
    "五一后",
    "五一那",
    "劳动节",
    "七夕",
    "双十一",
    "双11",
    "双十二",
    "双12",
    "圣诞",
    "平安夜",
    "情人节",
    "母亲节",
    "父亲节",
    "儿童节",
    "教师节",
    "重阳",
    "腊八",
    "冬至",
    "前天",
    "大后天",
    "本周",
    "这周",
    "这个周",
    "有空",
    "方便的时候",
    "到时候",
    "那天",
    "当天",
    "哪天",
    "某天",
    "下回",
    "将来",
    "近期",
    "工作日",
    # Colloquial and mixed-language variants found in review.
    "明儿",
    "后儿",
    "赶明",
    "这两天",
    "这几天",
    "抽空",
    "发了工资",
    "工资发了",
    "发完工资",
    "tomorrow",
    "tonight",
    "next week",
    "next month",
    # S4: 自动 not attached to a recognised schedule phrase (see _FILLER).
    _FILLER,
)
# M/D, M.D or M-D written without a currency unit; with a unit it stays an amount.
_NUMERIC_DATE = re.compile(
    r"(?<![\d.,¥￥$])(?:1[0-2]|0?[1-9])\s*[./-]\s*(?:3[01]|[12]\d|0?[1-9])(?![\d.%])"
    r"(?!\s*(?:元|块|圆|万|千|百|毛|角|分|人民币|rmb|cny))",
    re.I,
)
# A bare "N号" is a date only next to a clause boundary, a date lead word or the transfer
# verb phrase; after a noun (房号18号, 门牌18号) or before one (18号楼) it is an identifier.
_DATE_LEAD = re.compile(r"(?:^|[\s，,。；;：:！!？?、]|在|于|到|就|再|先|改|定|等)$")
_DATE_FOLLOW = re.compile(r"^\s*(?:给|转|汇|打钱|还|向|往|把|帮|请|再)")
_NUMBER = r"(?:[0-9]+|[零〇一二两三四五六七八九十百]+)"
_IDENTIFIER_NOUN = (
    r"(?:房号|门牌号?|房间|房|室|楼|栋|幢|单元|层|座|床|车位|车牌|学号|工号|编号|尾号|号码|"
    r"地址|路|街|巷|弄|住在)\s*(?:是|为|[:：])?\s*"
)
# An identifier noun right before "N号" wins even when a transfer verb follows
# (房号18号给房东转500 names a room, not the 18th).
_IDENTIFIER_LEAD = re.compile(rf"{_IDENTIFIER_NOUN}$")
_IDENTIFIER_NUMBER = re.compile(
    rf"{_IDENTIFIER_NOUN}{_NUMBER}[号日]|"
    rf"{_NUMBER}[号日](?=楼|房|室|栋|幢|单元|门|院|床|位|线|口|桌|柜|窗|仓|库|车|机|座|台|馆|厅|街|路)"
)


# B2 (JUDGING-1 list item 19): spaces inside a date phrase are normalized first, so
# 每月5 号 / 10 月 20 日 read like 每月5号 / 10月20日. Only whitespace between a day or
# month number and 号 / 日 / 月 (or after 月 / 每) goes; full-width digits read as ASCII.
_WIDE_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")
_DAY_NUMBER = r"[0-9零〇一二两三四五六七八九十]"
_SPACED = re.compile(
    rf"(?<={_DAY_NUMBER})\s+(?=[号日月])|(?<=月)\s+(?={_DAY_NUMBER})|(?<=每)\s+(?=个?月)"
)
# B2: a short clause that ends in a time suffix (晚饭后给房东转, 睡前给妈妈转, 收到货后给他
# 转, 转500，到家后) names a time the closed grammar cannot read, so the chat asks "when?".
# C1 (review of B2): the clause opens the sentence or follows a separator and may be
# followed by the verb phrase, the end of the sentence, or the words the review found in
# between: a reminder or a polite word (记得, 麻烦), a pronoun (你, 我, 他), an adverb
# (马上, 就, 直接, 顺便) or a modal (要, 可以). A suffix that starts a noun instead (给前同事
# 转, 前任, 后妈) is not a time clause.
_TIME_SUFFIX = r"(?:以后|之后|以前|之前|的时候|前|后|时)"
_AFTER_FRAGMENT = (
    r"给|转|汇|打钱|打款|还|向|往|把|帮|请|再|就|先|自动|记得|别忘|麻烦|你|您|我|他|她|"
    r"马上|立刻|立即|赶紧|赶快|尽快|顺便|直接|一起|也|都|要|得|可以|能|会|"
    # The other money-moving chat services (C1): 晚饭后赎回, 睡前申购, 下班后取消.
    r"赎回|申购|认购|买|购买|卖|取消|退订|关掉|关闭|停"
)
_FRAGMENT = re.compile(
    r"(?:^|(?<=[\s，,。；;！!、]))"
    rf"(?P<fragment>[^\s，,。；;！!？?、]{{1,8}}?{_TIME_SUFFIX})"
    rf"(?=\s*[，,]?\s*(?:{_AFTER_FRAGMENT})|[\s。.!！~～]*$)"
)
# Sequence and manner words that end like a time clause but name no time; 定时 belongs to
# the frozen date lexicon (has_date_request), which already keeps it from being "now".
_NOT_FRAGMENT = frozenset({
    "定时",
    "然后",
    "随后",
    "最后",
    "先后",
    "前后",
    "背后",
    "同时",
    "及时",
    "随时",
    "准时",
    "按时",
    "实时",
    "暂时",
    "临时",
    "平时",
    "有时",
    # C1: "at present" and "ahead of time" open a request for now, not for later.
    "目前",
    "当前",
    "眼前",
    "提前",
})
# C1: a payment put off until something happens is not "now": a clause opened by 等 (等她到
# 家了再转, 转500，等她到家了), or a finished action followed by 再 and the payment verb (吃完
# 饭再给, 到家了再转, 确认一下再给). 再 alone ("again": 再给他100元) is not one.
_DEFERRAL = re.compile(
    r"(?:^|[\s，,。；;！!、])等(?!等|一下|下|会|一会)"
    r"|(?:了|完|好|过|后|一下)[^\s，,。；;！!？?、]{0,4}再(?:给|转|汇|打钱|打款|付)"
    r"|到期(?:再|以?后|之后|时|的时候)"
)


def normalized_spacing(text):
    """The text with spaces inside a date phrase removed (每月 5 号 → 每月5号)."""
    return _SPACED.sub("", text.translate(_WIDE_DIGITS))


def time_fragment(text):
    """The first unparsed time clause (晚饭后, 睡前, 收到货后), or None."""
    for match in _FRAGMENT.finditer(text):
        if match.group("fragment") not in _NOT_FRAGMENT:
            return match.group("fragment")
    return None


def deferral(text):
    """True when the request puts the payment off until something happens (C1)."""
    return bool(_DEFERRAL.search(text))


# C1: the purchase, redemption and cancellation paths read any clause that opens the
# sentence (or follows a separator) and ends in a time suffix as a time, whatever follows
# it (晚饭后全部赎回): holding there only asks "when", and their wording has no nouns such
# as 前同事 that a transfer's payee may carry.
_LOOSE_FRAGMENT = re.compile(
    r"(?:^|(?<=[\s，,。；;！!、]))"
    rf"(?P<fragment>[^\s，,。；;！!？?、]{{1,8}}?{_TIME_SUFFIX})"
)


def loose_time_fragment(text):
    """The first clause ending in a time suffix, whatever follows it (C1), or None."""
    for match in _LOOSE_FRAGMENT.finditer(text):
        if match.group("fragment") not in _NOT_FRAGMENT:
            return match.group("fragment")
    return None


def residual_time(text):
    """A time word or numeric date the closed grammar and the frozen lexicon both miss."""
    folded = text.casefold()
    return (
        any(word in folded for word in _RESIDUAL_WORDS)
        or bool(_NUMERIC_DATE.search(text))
        or time_fragment(text) is not None
        or deferral(text)
    )


# B2: cancelling a standing transfer names it (每月 / 定时 / 自动) together with a cancel word.
_STANDING = re.compile(r"每个?月|每周|每星期|每礼拜|每天|每日|定时|自动")
_STANDING_CANCEL = re.compile(r"取消|撤销|撤回|终止|停止|停掉|关掉|关闭|不再|不要再|别再")
# Review fix: it must be a transfer (自动扣费 is a subscription debit, not a transfer).
# C1: 打款 and 汇钱 name a transfer too.
_STANDING_TRANSFER = re.compile(r"转|汇款|汇钱|打钱|打款")
# C1: a negation right before a cancel or stop verb negates the cancellation itself (不要取
# 消, 先别取消, 别给我取消, 别停): the user wants the authorization kept.
_CANCEL_NEGATED = re.compile(
    r"(?:不要|先别|千万别|可别|别|不想|先不|不用|不必|无需|无须|甭|勿)"
    r"(?:给我|帮我|替我|把它|再)?"
    r"(?:取消|撤销|撤回|终止|停止|停掉|停|关掉|关闭|删除|删掉|退订|撤)"
)


def cancel_negated(text):
    """True when a negation word negates a cancel or stop verb ("do not cancel")."""
    return bool(_CANCEL_NEGATED.search(compact(text)))


def standing_transfer(text):
    """True when the request names a standing (scheduled) transfer (每月 … 转, 定时 … 打款)."""
    folded = compact(text)
    return bool(_STANDING.search(folded) and _STANDING_TRANSFER.search(folded))


def standing_cancel(text):
    """True when the request asks to stop an existing standing (scheduled) transfer.

    C1: a cancel word, or a negation of the standing transfer itself (不想每月给房东转了,
    先不每月给房东转了), asks for it; a negated cancellation (不要取消, 别停) never does.
    """
    if not standing_transfer(text) or cancel_negated(text):
        return False
    return bool(_STANDING_CANCEL.search(compact(text)) or negation_words(text))


def _consume_filler(residue, match):
    """Blank one 自动 directly before or after the recognised phrase (spaces allowed)."""
    width = len(_FILLER)
    after = re.match(rf"\s*{_FILLER}", residue[match.end() :])
    if after:
        end = match.end() + after.end()
        return residue[: end - width] + " " * width + residue[end:]
    before = re.search(rf"{_FILLER}\s*$", residue[: match.start()])
    if before:
        start = before.start()
        return residue[:start] + " " * width + residue[start + width :]
    return residue


def _date_context(text, match):
    before = text[: match.start()]
    if _IDENTIFIER_LEAD.search(before):
        return False
    return bool(_DATE_LEAD.search(before) or _DATE_FOLLOW.search(text[match.end() :]))


def _number(text):
    value = chinese_number(text)
    return int(value) if value is not None and value.denominator == 1 else None


def _unresolved(reason, **extra):
    return {"status": "unresolved", "reason": reason, **extra}


def _date(match, kind, today):
    """Return (date, monthly_day) for one grammar match, or a reason string."""
    if kind == "monthly":
        day = _number(match.group("day"))
        if day is None or not 1 <= day <= 28:
            return "monthly_day_unsupported"
        # The first run is the next day N after today, so all three runs fit in 90 days.
        year, month = (
            (today.year, today.month)
            if today.day < day
            else ((today.year, today.month + 1) if today.month < 12 else (today.year + 1, 1))
        )
        return date(year, month, day), day
    if kind == "tomorrow":
        return today + timedelta(days=1), None
    if kind == "next_week":
        offset = "一二三四五六日".index(
            "日" if match.group("weekday") == "天" else match["weekday"]
        )
        return today + timedelta(days=7 - today.weekday() + offset), None
    if kind == "month_end":
        return today.replace(day=calendar.monthrange(today.year, today.month)[1]), None
    try:
        if kind == "iso":
            value = date.fromisoformat(match.group("iso"))
        elif kind == "month_day":
            month, day = _number(match.group("month")), _number(match.group("day"))
            if month is None or day is None:
                return "invalid_date"
            value = date(today.year, month, day)
        else:
            day = _number(match.group("day"))
            if day is None:
                return "invalid_date"
            value = date(today.year, today.month, day)
    except ValueError:
        return "day_passed" if kind in {"current_month", "day"} else "invalid_date"
    # A passed date is never rolled forward to another month or year.
    return value, None


def schedule_candidate(text, context):
    """Exactly one recognised time source and no residual time words, or unresolved."""
    text = normalized_spacing(text)
    working = text
    found = []
    for kind, pattern in _GRAMMAR:
        for match in pattern.finditer(working):
            found.append((kind, match))
        working = pattern.sub(lambda match: " " * len(match.group()), working)
    if any(kind == "birthday" for kind, _ in found):
        return _unresolved("birthday_template")
    identifiers = [
        match.group() for kind, match in found if kind == "day" and not _date_context(text, match)
    ]
    if identifiers or _IDENTIFIER_NUMBER.search(working):
        return _unresolved("number_not_date", phrases=identifiers)
    residue = working.replace("定时", "  ")
    if not found:
        if _RECURRENCE.search(residue):
            return _unresolved("recurrence_unsupported")
        if has_date_request(text):
            return _unresolved("unparsed_time")
        return _unresolved("residual_time") if residual_time(text) else {"status": "none"}
    if len(found) != 1:
        return _unresolved("conflict", phrases=[match.group() for _, match in found])
    residue = _consume_filler(residue, found[0][1])
    if _RECURRENCE.search(residue):
        return _unresolved("recurrence_unsupported")
    if has_date_request(residue):
        return _unresolved("unparsed_time")
    if residual_time(residue):
        return _unresolved("residual_time")
    if not context.get("now"):
        return _unresolved("clock_unavailable")
    kind, match = found[0]
    today = date.fromisoformat(business_date(context["now"]))
    parsed = _date(match, kind, today)
    if isinstance(parsed, str):
        return _unresolved(parsed, phrase=match.group())
    value, monthly_day = parsed
    if value <= today:
        return _unresolved("day_passed", phrase=match.group())
    # S5: a complete parse of a negated or cancelled request (别每月5号给房东转,
    # 每月5号，停止给房东转) is never a schedule. Checked last, so every earlier reason
    # for an incomplete parse stays exactly as it was.
    negated = negation_words(text)
    if negated:
        return _unresolved("negated", words=list(negated))
    return {
        "status": "complete",
        "kind": "monthly" if monthly_day else "once",
        "date_text": value.isoformat(),
        "monthly_day": monthly_day,
        "phrase": match.group(),
    }


def _labels(context):
    labels = {}
    for ref, names in context.get("contact_labels", {}).items():
        for name in names:
            if name:
                labels.setdefault(name, set()).add(ref)
    return labels


def _narrative_aa(names):
    """S4: one past-tense spending clause, then one explicit AA request, nothing else.

    昨天和室友、同学聚餐一共花了300元，帮我发起AA收款 — the only time word allowed is a
    closed past word at the very start; the request clause names no time, no payer and
    no other people. Read on the vault-tokenized text (一共 -> 1共, ， -> ,).
    """
    one = r"(?:1|一)"
    activity = (
        rf"(?:聚餐|聚会|聚了{one}?次|吃饭|吃了饭|吃了{one}?顿饭|吃火锅|吃了火锅|吃烧烤|"
        r"吃了烧烤|唱歌|唱了歌|唱K|唱k|看电影|看了电影|打车|打了车|买菜|买了菜)"
    )
    spent = rf"(?:{one}共|总共|共)?\s*(?:花了|花费了|消费了)"
    amount = r"[0-9零〇一二两三四五六七八九十百千万.]+\s*(?:块钱|元|块)?"
    ask = (
        rf"(?:帮我|请|麻烦你?|你)?\s*(?:发起|发个|发{one}下|发)?\s*(?:{one}个)?\s*(?i:aa)"
        rf"\s*(?:收款|{one}下)?\s*[。！!]?"
    )
    # Either named trusted contacts, or only a headcount (我们四个人) that B's P2 card then
    # completes from the verified roster in chat.
    who = (
        rf"(?:我?\s*(?:和|跟|与)\s*{names}|(?:我们|咱们)\s*"
        r"(?P<count>[0-9零〇一二两三四五六七八九十]+)\s*(?:个人|个|人|位))"
    )
    return re.compile(
        rf"^\s*{_PAST_LEAD}?\s*{who}\s*{_TOGETHER}\s*{activity}?\s*[,，]?"
        rf"\s*{spent}\s*{amount}\s*[,，。；;]\s*{ask}\s*$"
    )


def aa_candidate(text, context):
    """A named participant list anchored to the speaker and AA; the speaker shares too."""
    if not re.search(r"aa", text, re.I) or _PAYMENT_VERB.search(text):
        return {"status": "none"}
    # S5: a negated split bill (我和室友不用AA, 别跟同学AA) is never a collection, and
    # it is not a headcount for the roster completion card either.
    negated = negation_words(text)
    if negated:
        return _unresolved("negated", words=list(negated))
    labels = _labels(context)
    label = "(?:" + "|".join(re.escape(name) for name in sorted(labels, key=len, reverse=True))
    label += ")"
    names = rf"(?P<list>{label}(?:{_SEPARATOR}{label})*)"
    contact_text = context.get("contact_text", text)
    narrative = _narrative_aa(names).match(contact_text) if labels else None
    # Only a matched narrative may carry its one leading past word; any other time word
    # (or a past word outside the closed list) still means "not supported in chat".
    timed = re.sub(rf"^\s*{_PAST_LEAD}", "", text) if narrative else text
    if has_date_request(timed) or residual_time(timed):
        return _unresolved("time_not_supported")
    if _HISTORICAL_AA.search(text):
        return _unresolved("historical")
    if _PER_PERSON.search(text):
        return _unresolved("per_person_phrase")
    total = money_candidate(text)
    if total is None:
        return _unresolved("amount")
    if not labels or (narrative is not None and narrative.group("list") is None):
        # A headcount without names is left to the roster completion card (P2).
        return _unresolved("participants")
    grammars = (
        # 我和/跟/与 A、B (一起) AA: the list must be consumed up to AA, and 我 must not
        # follow a separator that may continue an earlier list (小王、我和… or 小王，我和…
        # would silently drop 小王 from the headcount), so those clarify instead.
        rf"(?:^|(?<=[\s，,。；;：:！!？?])|(?<=我)(?<![、,，和跟与及\s]我))(?:和|跟|与)\s*{names}"
        rf"\s*{_TOGETHER}\s*(?i:aa)",
        # A、B 和/跟/与 我 (一起) AA
        rf"(?:^|(?<=[\s。；;：:！!？?])){names}\s*(?:和|跟|与)\s*我\s*{_TOGETHER}\s*(?i:aa)",
    )
    matches = [match for grammar in grammars for match in re.finditer(grammar, contact_text)]
    if narrative is not None:
        matches.append(narrative)
    if len(matches) != 1:
        return _unresolved("participants")
    listed, refs = matches[0].group("list"), []
    while listed:
        item = re.match(rf"({label})(?:{_SEPARATOR}|\s*$)", listed)
        if item is None:
            return _unresolved("participants")
        owners = labels[item.group(1)]
        if len(owners) != 1:
            return _unresolved("ambiguous", candidates=sorted(owners))
        refs.append(next(iter(owners)))
        listed = listed[item.end() :]
    if len(set(refs)) != len(refs) or not 1 <= len(refs) <= 10:
        return _unresolved("participants")
    people = len(refs) + 1
    count = _COUNT.search(text)
    if count and _number(count.group(1)) != people:
        return _unresolved("count_mismatch", people=people)
    share = Fraction(total) / people
    if (share * 100).denominator != 1:
        return _unresolved("uneven", people=people)
    return {
        "status": "complete",
        "participants": refs,
        "people": people,
        "total": fraction_string(total),
        "per_person": fraction_string(share),
        "collect_total": fraction_string(share * len(refs)),
        "phrase": matches[0].group(),
    }
