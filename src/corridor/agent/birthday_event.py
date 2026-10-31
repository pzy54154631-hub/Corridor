"""Trusted birthday events for the product layer; never an authorization input.

Events and dates come only from the owner's calendar through the frozen gateway's
birthday suggestions, re-derived here and dropped on any disagreement. A relation
comes only from verified trusted-contact aliases intersected with a closed
vocabulary. Text can narrow by that vocabulary; it never selects a date. Every
function is pure and reads time only from the simulated ``state.now``.
"""

import re
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Any, Literal

from corridor.schemas.time import parse_time
from corridor.stores.bank import add_seconds, business_date

Relation = Literal["spouse", "parent", "father"]
# A user naming more than one relation is never resolved to any of them.
AMBIGUOUS = "ambiguous"

SPOUSE = frozenset({"爱人", "老婆", "老公", "妻子", "丈夫", "配偶", "媳妇", "伴侣", "spouse"})
# The parent class is the design's mother class. Father words form their own class, so a
# request about one parent never selects the other parent's birthday.
PARENT = frozenset({"妈妈", "老妈", "我妈", "母亲", "mom"})
FATHER = frozenset({"爸爸", "老爸", "我爸", "父亲", "dad"})
RELATIONS: dict[Relation, frozenset[str]] = {
    "spouse": SPOUSE,
    "parent": PARENT,
    "father": FATHER,
}
# A person named by a kin or social word outside the closed vocabulary has no trusted
# relation, so it never falls back to the only listed birthday (the mother's).
UNLISTED = "unlisted"
OTHER_KIN = frozenset({
    "婆婆",
    "公公",
    "公婆",
    "岳母",
    "岳父",
    "丈母娘",
    "老丈人",
    "爷爷",
    "奶奶",
    "外公",
    "外婆",
    "姥姥",
    "姥爷",
    "祖母",
    "祖父",
    "父母",
    "爸妈",
    "儿子",
    "女儿",
    "闺女",
    "孩子",
    "宝宝",
    "孙子",
    "孙女",
    "外孙",
    "哥哥",
    "姐姐",
    "弟弟",
    "妹妹",
    "大哥",
    "大姐",
    "兄弟",
    "姐妹",
    "表哥",
    "表姐",
    "表弟",
    "表妹",
    "堂哥",
    "堂姐",
    "堂弟",
    "堂妹",
    "叔叔",
    "阿姨",
    "舅舅",
    "舅妈",
    "姑姑",
    "姑妈",
    "姨妈",
    "小姨",
    "伯伯",
    "婶婶",
    "侄子",
    "侄女",
    "外甥",
    "朋友",
    "男朋友",
    "女朋友",
    "对象",
    "未婚妻",
    "未婚夫",
    "闺蜜",
    "同事",
    "同学",
    "室友",
    "老师",
    "领导",
    "老板",
    "师傅",
    "邻居",
    "wife",
    "husband",
    "son",
    "daughter",
    "brother",
    "sister",
    "friend",
    "grandma",
    "grandpa",
    "grandmother",
    "grandfather",
    "colleague",
    "teacher",
    "boss",
})
_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


@dataclass(frozen=True)
class BirthdayPlanDates:
    """Dates derived from one trusted calendar event; relation is display-only."""

    event_ref: str
    contact_ref: str
    event_date: str
    order_due: str
    lock_month: str
    lock_window_start: str
    spa_end: str
    relation: Relation | None = None


@dataclass(frozen=True)
class BirthdayResolution:
    status: Literal["resolved", "selection_required", "event_not_eligible"]
    event: BirthdayPlanDates | None
    candidates: tuple[BirthdayPlanDates, ...]


def relation_of(state: Any, principal: str, contact_ref: str) -> Relation | None:
    """Return a relation only for an owned, verified contact matching exactly one class."""
    contact = state.contacts.get(contact_ref)
    if contact is None or contact.owner != principal:
        return None
    if contact.verified_at is None or contact.verified_at > state.now:
        return None
    aliases = {alias.casefold() for alias in contact.aliases}
    hits = [name for name, words in RELATIONS.items() if aliases & words]
    if len(hits) != 1:
        return None
    return hits[0]


def _mentions(text: str, word: str) -> bool:
    if word.isascii():
        return re.search(rf"(?<![a-z]){re.escape(word)}(?![a-z])", text) is not None
    return word in text


def relation_in_text(text: str) -> str | None:
    """Read a relation word from the closed vocabulary; dates in the text are ignored.

    A kin or social word outside that vocabulary yields ``UNLISTED`` (or ``AMBIGUOUS``
    beside a listed relation), which matches no trusted event and so always clarifies.
    """
    folded = text.casefold()
    hits: list[str] = [
        name for name, words in RELATIONS.items() if any(_mentions(folded, word) for word in words)
    ]
    if any(_mentions(folded, word) for word in OTHER_KIN):
        hits.append(UNLISTED)
    if not hits:
        return None
    return hits[0] if len(hits) == 1 else AMBIGUOUS


def derive_dates(event: Any) -> BirthdayPlanDates:
    """Order two days before the birthday at Beijing 00:00 (the gateway's formula)."""
    order_due = (parse_time(event.event_date + "T00:00:00Z") - timedelta(days=2, hours=8)).strftime(
        _FORMAT
    )
    lock_month = business_date(order_due)[:7]
    lock_window_start = (parse_time(lock_month + "-01T00:00:00Z") - timedelta(hours=8)).strftime(
        _FORMAT
    )
    return BirthdayPlanDates(
        event_ref=event.id,
        contact_ref=event.person_contact_ref,
        event_date=event.event_date,
        order_due=order_due,
        lock_month=lock_month,
        lock_window_start=lock_window_start,
        spa_end=add_seconds(order_due, 86400),
    )


def birthday_candidates(gateway: Any, principal: str) -> list[BirthdayPlanDates]:
    """Use the frozen gateway suggestions as the sole source and fail closed on drift."""
    suggestions = gateway.birthday_suggestions(principal)
    state = gateway.store.snapshot()
    candidates = []
    for suggestion in suggestions:
        event = state.calendar.get(suggestion["event_ref"])
        if event is None or event.owner != principal or event.event_type != "birthday":
            continue
        dates = derive_dates(event)
        if (
            dates.order_due != suggestion["due_at"]
            or dates.contact_ref != suggestion["contact_ref"]
        ):
            continue
        candidates.append(
            replace(dates, relation=relation_of(state, principal, event.person_contact_ref))
        )
    return candidates


def resolve_birthday(
    gateway: Any,
    principal: str,
    *,
    event_ref: str | None = None,
    relation: str | None = None,
) -> BirthdayResolution:
    """Select exactly one trusted event; several candidates are never guessed between."""
    candidates = tuple(birthday_candidates(gateway, principal))
    if event_ref is not None:
        matches = [item for item in candidates if item.event_ref == event_ref]
        if not matches:
            return BirthdayResolution("event_not_eligible", None, candidates)
        if relation is not None and matches[0].relation != relation:
            return BirthdayResolution("selection_required", None, candidates)
        return BirthdayResolution("resolved", matches[0], candidates)
    pool = [item for item in candidates if relation is None or item.relation == relation]
    if len(pool) == 1:
        return BirthdayResolution("resolved", pool[0], candidates)
    return BirthdayResolution(
        "selection_required", None, tuple(pool) if len(pool) > 1 else candidates
    )


def lock_window_open(dates: BirthdayPlanDates, now: str) -> bool:
    """The budget lock is offered only in the order day's Beijing month, before ordering."""
    return dates.lock_window_start <= now < dates.order_due


def event_payload(dates: BirthdayPlanDates) -> dict[str, Any]:
    return {
        "event_ref": dates.event_ref,
        "contact_ref": dates.contact_ref,
        "relation": dates.relation,
        "event_date": dates.event_date,
        "order_due": dates.order_due,
        "lock_month": dates.lock_month,
        "lock_window_start": dates.lock_window_start,
    }
