"""B1: resolve a chat request to cancel one subscription authorization, closed and bounded.

Every candidate comes from trusted state: the customer's own active debit authorizations.
The user's words only narrow that list, by an identifier typed verbatim, the amount named
(每月扣49块), a synthetic merchant name from the display vocabulary, or the single item a
previous reply displayed. Nothing here signs, previews or executes: the subscription
form's own preparer builds the card and only the device gesture signs it.

A request that keeps a negation after its own closed cancel phrase (先别取消会员, 不要取消
订阅) is held like any other negated request (S5): no card.
"""

import re
import unicodedata
from fractions import Fraction

from corridor.agent.domestic_negation import negation_words
from corridor.agent.domestic_slots import money_candidate, money_span
from corridor.agent.domestic_wealth import plain_name

# The closed phrases that ask for the cancellation itself (取消订阅, 会员不要续了, 把自动扣费
# 关掉). Only what is left after removing them can negate the request.
_CANCEL = re.compile(
    r"取消|退订|退掉|退了|停掉|停了|关掉|关了|关闭|解约"
    r"|(?:不想|不要|不再|不用|别再|别|不)续(?:费|订|约)?"
)
_SPACE = re.compile(r"\s+")
_MONTHLY = re.compile(r"每个?月|月度|包月|按月")
_YEARLY = re.compile(r"每年|年度|包年|按年|年费")
# C1 (review of B1): a listed authorization is inherited only when the words point back at
# the listing (那个 / 这些 / 就它 / 第一个) right after it, and name no other object.
_DEICTIC = re.compile(
    r"那个|这个|那项|这项|那笔|这笔|那条|这条|那些|这些|就它|它|该|第[一二三四五六七八九十0-9]"
)
# Words a cancellation may carry besides an object clue: the service nouns, period words,
# deictics and closed filler. Whatever else is left names an object the chat cannot match
# (视频会员, 商户Y, 健身房): it is not a match and the list is shown again.
_SERVICE = re.compile(
    r"自动续费|自动扣费|自动扣款|续费|扣费|扣款|代扣|订阅|会员|服务|授权|业务|套餐|自动"
)
_FILLER = re.compile(
    r"帮我|帮忙|麻烦|请你|请|给我|替我|我的|我们|我|你|把|将|就|都|全部|所有|一下|掉|的|了|吧|啊|"
    r"呀|呢|嘛|那个|这个|那项|这项|那笔|这笔|那条|这条|那些|这些|它|该|个|那|这|项|笔|条|扣|"
    r"每个?月|月度|包月|按月|每年|年度|包年|按年|年费|元|块|钱|人民币|第|[0-9０-９一二三四五六七八九十]"
    r"|[\s，,。.!！~～…、；;：:]"
)


def cancel_negation(text: str) -> tuple[str, ...]:
    """Negation words left after removing the request's own cancel phrases."""
    compact = _SPACE.sub("", unicodedata.normalize("NFKC", text))
    return negation_words(_CANCEL.sub(" ", compact))


def _named(text: str, names: list[str]) -> bool:
    """A synthetic display name (without its synthetic-data suffix) appears verbatim."""
    folded = text.casefold()
    return any(plain_name(name).casefold() in folded for name in names if plain_name(name))


def _object_words(text: str, refs: list[str], names: dict[str, list[str]]) -> str:
    """C1: what is left after the known clues, cancel phrases, nouns and filler go."""
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
    span = money_span(rest)
    if span is not None:
        rest = rest[: span[0]] + " " + rest[span[1] :]
    rest = _CANCEL.sub(" ", rest)
    rest = _SERVICE.sub(" ", rest)
    return _FILLER.sub("", rest)


def cancel_candidates(
    text: str,
    active: list[dict],
    *,
    shown: list[str] | None = None,
    merchant_names: dict[str, list[str]] | None = None,
) -> dict:
    """Narrow the customer's active authorizations by the user's own words.

    ``active`` rows carry ``ref``, ``merchant_ref``, ``amount`` and ``period_months`` read
    from trusted state. The result's ``refs`` are candidates, never a selection: a card is
    prepared only when exactly one remains and a clue (or a displayed list) named it.
    """
    refs = [row["ref"] for row in active]
    if not refs:
        return {"status": "none", "refs": [], "clue": None}
    names = merchant_names or {}
    if _object_words(text, refs, names):
        # C1: an object the chat cannot match (视频会员, 商户Y的会员, 那个健身房的订阅) is
        # never resolved to a listed or single authorization: the list is shown again.
        return {"status": "no_match", "refs": refs, "clue": "object"}
    explicit = [
        ref
        for ref in refs
        if re.search(rf"(?<![A-Za-z0-9_]){re.escape(ref)}(?![A-Za-z0-9_])", text)
    ]
    amount = money_candidate(text)
    named = [row["ref"] for row in active if _named(text, names.get(row["merchant_ref"], []))]
    clues = []
    candidates = list(refs)
    if explicit:
        clues.append("identifier")
        candidates = [ref for ref in candidates if ref in explicit]
    if amount is not None:
        clues.append("amount")
        candidates = [
            row["ref"]
            for row in active
            if row["ref"] in candidates and Fraction(row["amount"]) == amount
        ]
    if named:
        clues.append("merchant")
        candidates = [ref for ref in candidates if ref in named]
    period = 1 if _MONTHLY.search(text) else 12 if _YEARLY.search(text) else None
    if period is not None:
        candidates = [
            row["ref"]
            for row in active
            if row["ref"] in candidates and row.get("period_months") == period
        ]
    if clues:
        status = (
            "resolved" if len(candidates) == 1 else "no_match" if not candidates else "ambiguous"
        )
        return {
            "status": status,
            "refs": candidates if candidates else refs,
            "clue": "+".join(clues),
        }
    # C1: only the reply right before this one counts (the caller passes it), and only
    # when the words point back at it.
    displayed = [ref for ref in shown or [] if ref in refs] if _DEICTIC.search(text) else []
    if displayed:
        return {
            "status": "resolved" if len(displayed) == 1 else "ambiguous",
            "refs": displayed,
            "clue": "displayed",
        }
    return {"status": "choose", "refs": refs, "clue": None}
