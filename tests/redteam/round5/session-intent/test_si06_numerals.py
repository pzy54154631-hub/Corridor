"""session-intent-06: colloquial numerals become a different exact executable amount.

Stated rules: the project rule that executable amounts come only from trusted or
device-confirmed input; domestic_slots.chinese_number docstring ("Accept conventional
descending units, including 三千二 = 3200");
DFIX "中文金额全程 Fraction，金额限定词/冲突金额不得被旧金额掩盖";
existing tests treat malformed or ambiguous amounts as clarification (None).

- "2万5" -> 20005, while the code's own convention gives "两万五" -> 25000 (the ASCII tail
  digit is not scaled because `low in _DIGITS` only knows Chinese digits);
- "一笔钱" ("a sum of money") -> 1, "两次" ("twice") -> 2: quantifiers taken as amounts;
- "十块八毛" (10.80) -> 10: the trailing 毛 is silently dropped.
Each yields a confirmable transfer card for an amount the user never stated.
"""

from fractions import Fraction

import pytest

from corridor.agent.domestic_session import DomesticSession
from corridor.agent.domestic_slots import money_candidate
from corridor.ui.controller import DemoController


@pytest.mark.parametrize(
    "text,acceptable",
    [
        ("给房东转2万5", {Fraction(25000), None}),
        ("给房东转1万2", {Fraction(12000), None}),
        ("给老妈转一笔钱", {None}),
        ("给老妈转两次钱", {None}),
        ("给老妈转十块八毛", {Fraction(54, 5), None}),
    ],
)
def test_session_intent_06_amount_candidate_is_faithful_or_absent(text, acceptable):
    assert money_candidate(text) in acceptable, (text, money_candidate(text))


def test_session_intent_06_session_card_amount_matches_user_words():
    controller = DemoController()
    controller.start_session()
    session = DomesticSession(controller)
    response = session.receive("给房东转2万5")
    # The red card offered for MFA reads ¥20,005.00.
    assert response["card"].get("amount") in {None, "25000/1"}, response["card"].get("amount")
