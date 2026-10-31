"""C1: whether a chat request that could move money orders it now, closed and bounded.

The B-batch paths that reach a card from chat (a purchase review, a redemption card, a
subscription cancellation card) read only "do it now" requests. A sentence that asks
(吗, ？, 能不能), supposes (如果, 要是, 的话), reports something past (已经, 昨天, 上周) or
names a time (明天, 晚饭后, 下个月再, 到期以后, 等工资到账) is answered in chat instead: no
review, no card, no signed input. These are fixed word lists: they miss some wording and
also hold some ordinary wording; holding is the safe side (the user restates the request).
"""

import re

from corridor.agent.domestic_negation import without_polite_tags
from corridor.agent.domestic_schedule import loose_time_fragment, residual_time
from corridor.agent.domestic_slots import has_date_request

_HYPOTHETICAL = re.compile(r"如果|要是|假如|假设|万一|倘若|若是|的话|会怎样|会怎么样|会不会|怎么办")
_QUESTION = re.compile(r"吗|[？?]|是否|能否|可否|能不能|可不可以|是不是|有没有|多少|什么|怎么")
_PAST = re.compile(
    r"已经|早就|早已|刚刚|刚才|昨天|昨晚|前天|上周|上星期|上礼拜|上个?月|上次|去年|前几天|之前"
)
_PAST_VERB = re.compile(r"(?:申购|认购|买入|购买|买|赎回|卖出|卖掉|取消|退订|关掉|关闭)过")
# 自动 names the nature of a debit (自动扣费, 自动续费), not when to act.
_AUTO = re.compile(r"自动")


def mood(text: str) -> str | None:
    """``hypothetical``, ``question`` or ``past`` when the request does not order anything
    now; None for a plain request. Polite tags in their place (……，可以吗) do not count."""
    plain = without_polite_tags(text)
    if _HYPOTHETICAL.search(plain):
        return "hypothetical"
    if _QUESTION.search(plain):
        return "question"
    if _PAST.search(plain) or _PAST_VERB.search(plain):
        return "past"
    return None


def names_a_time(text: str) -> bool:
    """True when the request names a time to act (a date, a time word, a clause ending in
    a time suffix, or a deferral such as 等…再 / 到期再), so it is not "now"."""
    plain = _AUTO.sub(" ", text)
    return bool(
        has_date_request(plain) or residual_time(plain) or loose_time_fragment(plain) is not None
    )
