"""B1: closed chat grammar for one purchase or one redemption, with slots filled in chat.

The values are unsigned candidates. A product is named only by its catalog identifier, its
synthetic display name, or a holding period (N天) that leaves exactly one catalog product
(family words such as 稳健 or 灵活 only narrow, never select). The amount is the one money
phrase in the user's words. A position is one of the customer's own active holdings.
Nothing here signs or prepares: a complete purchase is shown for review and signed only
by the device gesture, through the same preparer as the wealth page's button.
"""

import re

from corridor.agent.domestic_negation import without_polite_tags
from corridor.agent.domestic_slots import chinese_number, money_candidate, money_span
from corridor.schemas.money import fraction_string

_PURCHASE = re.compile(r"申购|认购|购买|买入|买")
_REDEEM = re.compile(r"赎回|卖出|卖掉")
# A question, a request for advice or a past purchase is not an order: it keeps the
# advisory path (我有5万闲钱，想买点稳健的理财，有什么推荐 / 我买的理财现在收益多少了).
_QUERY = re.compile(
    r"推荐|什么|哪个|哪款|哪只|哪种|怎么样|如何|比较|对比|建议|多少|收益|[？?]|吗\s*[。.!！~～]*\s*$"
)
# 了 is left out on purpose: 不买了 / 赎回了吧 are requests, not past purchases.
_PAST = re.compile(r"(?:买|申购|认购|购买|赎回|卖)(?:的|过)")
# Words naming "that one" on another page: the chat cannot resolve them by itself.
_DEICTIC = re.compile(r"[那这哪]\s*(?:只|款|个|支|笔)")
# C1 (review of B1): a holding period is read only with holding wording around it (持有30天,
# 30天持有, 30天期, 10天的理财); a date's day (11月30日, 400日后) never names a product.
_DAYS = r"([0-9]{1,4}|[一二两三四五六七八九十百]{1,5})"
_HOLD = re.compile(
    rf"(?:持有|锁定期|封闭期|期限)\s*{_DAYS}\s*(?:天|日)(?!后|以后|之后|前|内|以内|之内)"
    rf"|(?<![0-9.月]){_DAYS}\s*(?:天|日)\s*(?:的)?\s*(?:持有|期|理财|产品|封闭)"
)
# C1: how much of a holding to redeem. Only 全部 / 全额 (or an exact amount the holding
# covers) is a quantity; a share, a ratio, units, "keep X" or any other number is asked.
_ALL = re.compile(r"全部|全额|所有|全都|都|统统|一次性|清仓|全赎")
_PARTIAL = re.compile(
    r"一半|半|部分|一些|一点|点儿|少量|[0-9一二两三四五六七八九十]\s*成|%|％|百分之|分之"
    r"|留|剩|保留|份"
)
_NUMBER_LEFT = re.compile(r"[0-9０-９]|[零〇一二两三四五六七八九十百千万]+\s*(?:元|块|份|万|千|百)")
# Display-name words that narrow the catalog but never select a product on their own.
_FAMILY = (
    "稳健",
    "灵活",
    "成长",
    "长期",
    "安心",
    "活钱",
    "增强",
    "平衡",
    "大额",
    "现金管理",
    "精选",
    "起投",
    "steady",
    "flexible",
    "growth",
    "secure",
    "liquid",
    "balanced",
)
_SUFFIX = re.compile(r"\s*(?:（合成[^）]*）|\((?:synthetic)[^)]*\))\s*$", re.I)


def wealth_action(text: str) -> str | None:
    """``purchase`` or ``redeem`` for one money-moving wealth request; None otherwise.

    A polite tag in its allowed place (……，可以吗) does not make the order a question (R8).
    """
    plain = without_polite_tags(text)
    if _QUERY.search(plain) or _PAST.search(plain):
        return None
    purchase, redeem = bool(_PURCHASE.search(plain)), bool(_REDEEM.search(plain))
    if purchase == redeem:
        return None
    return "purchase" if purchase else "redeem"


def plain_name(name: str) -> str:
    """A display name without its synthetic-data suffix (稳健30天持有（合成） → 稳健30天持有)."""
    return _SUFFIX.sub("", name).strip()


def _hold_days(text: str) -> set[int]:
    days = set()
    for match in _HOLD.finditer(text):
        value = chinese_number(match.group(1) or match.group(2))
        if value is not None and value.denominator == 1 and 0 < value <= 3650:
            days.add(int(value))
    return days


def without_products(text: str, names: dict[str, list[str]], refs: list[str]) -> str:
    """C1: the text with product names, identifiers, holding periods and family words
    blanked, so a number inside a name (起投2000元10天理财, 稳健30天持有) is never read
    as the amount the user asked for."""
    rest = text
    plain = sorted(
        {plain_name(name) for values in names.values() for name in values if plain_name(name)},
        key=len,
        reverse=True,
    )
    for name in plain:
        rest = re.sub(re.escape(name), " ", rest, flags=re.I)
    for ref in sorted(refs, key=len, reverse=True):
        rest = re.sub(rf"(?<![A-Za-z0-9_]){re.escape(ref)}(?![A-Za-z0-9_])", " ", rest)
    rest = _HOLD.sub(" ", rest)
    for word in _FAMILY:
        rest = re.sub(re.escape(word), " ", rest, flags=re.I)
    return rest


def redemption_quantity(text: str) -> dict:
    """C1: how much the words ask to redeem, read on text without product names.

    ``{"mode": "all"}`` for 全部 / 全额 only; ``{"mode": "amount", "amount": "n/d"}`` for
    one exact amount and no other number; otherwise ``partial`` (a share, a ratio, units,
    "keep X"), ``unparsed`` (another number) or ``missing`` (nothing said): all are asked.
    """
    if _PARTIAL.search(text):
        return {"mode": "partial"}
    span = money_span(text)
    amount = amount_candidate(text)
    rest = text if span is None else text[: span[0]] + " " + text[span[1] :]
    if _NUMBER_LEFT.search(rest):
        return {"mode": "unparsed"}
    if amount is not None:
        return {"mode": "amount", "amount": amount}
    if _ALL.search(text):
        return {"mode": "all"}
    return {"mode": "missing"}


def product_candidates(text: str, products: list[dict], names: dict[str, list[str]]) -> dict:
    """Narrow trusted catalog rows (``ref``, ``min_hold_days``) by the user's words."""
    refs = [row["ref"] for row in products]
    folded = text.casefold()
    explicit = [
        ref
        for ref in refs
        if re.search(rf"(?<![A-Za-z0-9_]){re.escape(ref)}(?![A-Za-z0-9_])", text)
    ]
    if len(explicit) == 1:
        return {"status": "resolved", "ref": explicit[0], "refs": explicit, "clue": "identifier"}
    full = [
        ref
        for ref in refs
        if any(plain_name(name).casefold() in folded for name in names.get(ref, []) if name)
    ]
    if len(full) == 1:
        return {"status": "resolved", "ref": full[0], "refs": full, "clue": "name"}
    days = _hold_days(text)
    family = [word for word in _FAMILY if word in folded]
    candidates = list(refs)
    if days:
        candidates = [row["ref"] for row in products if row["min_hold_days"] in days]
    for word in family:
        candidates = [
            ref
            for ref in candidates
            if any(word in plain_name(name).casefold() for name in names.get(ref, []))
        ]
    if days and len(candidates) == 1:
        return {"status": "resolved", "ref": candidates[0], "refs": candidates, "clue": "hold"}
    if explicit or full or days or family:
        return {
            "status": "ambiguous" if candidates else "no_match",
            "ref": None,
            "refs": candidates or refs,
            "clue": "partial",
        }
    if _DEICTIC.search(text):
        # 那只 / 这款 names something shown elsewhere; only the wealth page can resolve it.
        return {"status": "deictic", "ref": None, "refs": refs, "clue": None}
    return {"status": "missing", "ref": None, "refs": refs, "clue": None}


def amount_candidate(text: str) -> str | None:
    """The one money phrase as an exact "n/d" string, or None."""
    value = money_candidate(text)
    return None if value is None else fraction_string(value)
