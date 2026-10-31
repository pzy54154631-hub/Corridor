"""B2 property tests (JUDGING-1 list item 19): spacing and unparsed time fragments.

1. Any whitespace between a day number and 号 (and after 月) reads exactly like the plain
   phrase: the same date, the same monthly day.
2. A short clause ending in a time suffix before the transfer verb phrase (or closing the
   sentence) never yields an immediate transfer card: the chat asks "when?".
3. Neither rule touches a frozen zh-suite or intent-eval line.
"""

import json
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from corridor.agent.domestic_schedule import normalized_spacing, schedule_candidate, time_fragment
from corridor.agent.domestic_session import DomesticSession
from corridor.ui.controller import DemoController

NOW = "2026-10-16T02:00:00Z"
DATA = Path(__file__).resolve().parents[2] / "data" / "scenarios" / "zh"
SPACES = st.text(alphabet=" 　\t", min_size=1, max_size=3)
ACTIVITIES = (
    "晚饭",
    "吃完饭",
    "睡",
    "睡觉",
    "收到货",
    "下单",
    "到家",
    "开完会",
    "领了工资",
    "我到公司",
    "孩子放学",
    "办完事",
)
SUFFIXES = ("前", "后", "以后", "之后", "以前", "之前", "的时候", "时")
ALIASES = ("妈妈", "房东", "同学", "Chen")
TEMPLATES = (
    "{fragment}给{alias}转{amount}元",
    "{fragment}，给{alias}转{amount}元",
    "{fragment}帮我给{alias}转{amount}元",
    "给{alias}转{amount}元，{fragment}",
)


@settings(deadline=None, max_examples=60)
@given(day=st.integers(min_value=1, max_value=28), left=SPACES, right=SPACES)
def test_spacing_inside_a_monthly_phrase_reads_like_the_plain_phrase(day, left, right):
    plain = schedule_candidate(f"每月{day}号给房东转2000元", {"now": NOW})
    spaced = schedule_candidate(f"每月{left}{day}{right}号给房东转2000元", {"now": NOW})
    assert plain["status"] == spaced["status"] == "complete"
    assert (plain["date_text"], plain["monthly_day"]) == (
        spaced["date_text"],
        spaced["monthly_day"],
    )


@settings(deadline=None, max_examples=40)
@given(month=st.integers(min_value=11, max_value=12), day=st.integers(1, 28), gap=SPACES)
def test_spacing_inside_a_month_day_phrase_reads_like_the_plain_phrase(month, day, gap):
    plain = schedule_candidate(f"{month}月{day}号给妈妈转500元", {"now": NOW})
    spaced = schedule_candidate(f"{month}{gap}月{gap}{day}{gap}号给妈妈转500元", {"now": NOW})
    assert plain["status"] == spaced["status"] == "complete"
    assert plain["date_text"] == spaced["date_text"]


@settings(deadline=None, suppress_health_check=[HealthCheck.too_slow], max_examples=60)
@given(
    activity=st.sampled_from(ACTIVITIES),
    suffix=st.sampled_from(SUFFIXES),
    alias=st.sampled_from(ALIASES),
    amount=st.integers(min_value=1, max_value=900),
    template=st.sampled_from(TEMPLATES),
)
def test_an_unparsed_time_clause_never_previews_an_immediate_transfer(
    activity, suffix, alias, amount, template
):
    text = template.format(fragment=activity + suffix, alias=alias, amount=amount)
    controller = DemoController()
    controller.start_session()
    assert schedule_candidate(text, {"now": NOW})["status"] == "unresolved", text
    response = DomesticSession(controller).receive(text)
    assert not response["card"], (text, response["card"].get("kind"))
    assert response["needs_clarification"] and not response["buttons"], text
    assert controller.gateway.bank.execute_count == 0
    assert not controller.state.user_inputs


def _frozen_lines():
    lines = [
        json.loads(line)["text"]
        for line in (DATA / "colloquial.jsonl").read_text("utf-8").splitlines()
        if line
    ]
    for case in json.loads((DATA / "cases.json").read_text(encoding="utf-8")):
        lines.append(case["request"]["task"])
        lines.extend(turn["task"] for turn in case["request"].get("context", []))
    return lines


def test_no_frozen_line_is_touched_by_either_rule():
    lines = _frozen_lines()
    assert len(lines) > 180
    assert [line for line in lines if time_fragment(line)] == []
    assert [line for line in lines if normalized_spacing(line) != line] == []
