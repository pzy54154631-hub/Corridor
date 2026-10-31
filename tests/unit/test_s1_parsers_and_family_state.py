"""S1: identifier numbers, unlisted kin words and the additive run-task 'family' state."""

import json
from hashlib import sha256
from pathlib import Path

import pytest

from corridor.agent.birthday_event import AMBIGUOUS, UNLISTED, relation_in_text
from corridor.agent.domestic_schedule import schedule_candidate
from corridor.agent.domestic_session import DomesticSession
from corridor.bench import domestic_runner
from corridor.bench.card_runner import card_state
from corridor.bench.domestic_runner import TaskRequest, make_session, run_task
from corridor.crypto.jcs import jcs_bytes
from corridor.ui.controller import DemoController, demo_state

NOW = "2026-10-16T02:00:00Z"
ROOT = Path(__file__).resolve().parents[2]


def _session():
    controller = DemoController()
    controller.start_session()
    return DomesticSession(controller)


@pytest.mark.parametrize(
    "text",
    [
        "给房东转2000，房号18号",
        "给房东转2000元，门牌18号",
        "门牌18号的房东转2000",
        "给18号楼房东转2000",
        "给Chen转200元，房间20号",
        "给住在中山路18号的Chen转200元",
    ],
)
def test_identifier_number_is_never_a_scheduled_date(text):
    parsed = schedule_candidate(text, {"now": NOW})
    assert parsed == {**parsed, "status": "unresolved", "reason": "number_not_date"}
    response = _session().receive(text)
    assert response["status"] == "clarify" and not response["card"], text
    assert response["message"].startswith("句中的“N号”看起来是房号")


@pytest.mark.parametrize(
    "text,date_text",
    [
        ("20号给妈妈转200", "2026-10-20"),
        ("给妈妈转200，20号", "2026-10-20"),
        ("妈妈那边20号转200", "2026-10-20"),
        ("在20号给妈妈转200", "2026-10-20"),
    ],
)
def test_day_number_with_date_context_still_schedules(text, date_text):
    parsed = schedule_candidate(text, {"now": NOW})
    assert (parsed["status"], parsed["date_text"]) == ("complete", date_text), parsed


@pytest.mark.parametrize(
    "text",
    [
        "婆婆生日快到了，帮我安排一下",
        "公公过生日，花和蛋糕安排上",
        "岳母生日安排一下",
        "爷爷生日安排一下",
        "奶奶生日快到了",
        "外公生日安排一下",
        "外婆生日帮我准备",
        "儿子生日安排一下",
        "女儿生日安排一下",
        "哥哥生日安排一下",
        "姐姐生日安排一下",
        "弟弟生日安排一下",
        "妹妹生日安排一下",
        "朋友生日安排一下",
        "同事生日安排一下",
        "老师生日安排一下",
    ],
)
def test_unlisted_kin_word_clarifies_instead_of_the_mothers_birthday(text):
    assert relation_in_text(text) == UNLISTED
    result = run_task({"task": text})
    reply = result["responses"][-1]
    assert (result["intent"], result["final_status"]) == ("birthday", "clarify")
    assert reply["payload"]["birthday_status"] == "selection_required"
    assert "event" not in reply["payload"] and not result["plans"]
    assert result["dispatch_count"] == 0


def test_listed_relations_still_resolve_and_mixed_ones_are_ambiguous():
    assert relation_in_text("妈妈生日安排一下") == "parent"
    assert relation_in_text("生日安排一下") is None
    assert relation_in_text("妈妈和婆婆的生日") == AMBIGUOUS
    assert relation_in_text("my wife's birthday") == UNLISTED
    assert relation_in_text("my mom's birthday") == "parent"
    draft = run_task({"task": "妈妈生日安排一下"})
    assert draft["final_status"] == "draft"
    assert draft["responses"][-1]["payload"]["event"]["event_ref"] == "BIRTHDAY"


def _digest(value):
    return sha256(jcs_bytes(value)).hexdigest()


# Digests of the states produced at 85008d0, before 'family' existed.
BANK_INITIAL = "853549fba5c8d3ab1e1bfeebc43e71db4f900a880c68539a502a28f57b2c58f5"
CARD_INITIAL = "80337fbdc58d7aa1b35dfcccf6d52f3704e9719101f9bbd0d42cd30adab8ed54"
BANK_SESSION = "05097e706198cd66bb4f0404f5c47fa13413e1160a9bb5e5063df134b3f4aaae"
CARD_SESSION = "329a163d0324df1805fecb05d90666307b2f2f0eba1283103775eae94d0b05ed"


def test_bank_and_card_states_are_byte_identical_to_before_family():
    bank = demo_state()
    assert _digest(bank) == BANK_INITIAL
    assert _digest(bank.model_copy(update={"cards": card_state().cards})) == CARD_INITIAL
    assert _digest(make_session("bank").controller.state) == BANK_SESSION
    assert _digest(make_session("card").controller.state) == CARD_SESSION
    assert TaskRequest.model_validate({"task": "x"}).initial_state == "bank"


def test_family_state_is_the_ui_showcase_state():
    family = make_session("family").controller.state
    session_ui = DemoController(initial_state=demo_state(showcase=True))
    session_ui.start_session()
    assert _digest(family) == _digest(session_ui.state)
    assert "BIRTHDAY_SPOUSE" in family.calendar and "BIRTHDAY" in family.calendar
    with pytest.raises(ValueError):
        make_session("unknown")


def test_zh_bd_03_request_reaches_its_frozen_expectation_under_family():
    # The frozen suite still selects "bank" for ZH-BD-03 (32/33); this separate run only
    # shows that the same request succeeds when a run-task caller selects "family".
    cases = json.loads((domestic_runner.DATA / "cases.json").read_text(encoding="utf-8"))
    (case,) = [row for row in cases if row["id"] == "ZH-BD-03"]
    assert case["request"]["initial_state"] == "bank"
    result = run_task({**case["request"], "initial_state": "family", "task_id": case["id"]})
    actual = {key: result[key] for key in case["expected"]}
    assert actual == case["expected"]
    assert result["responses"][-1]["payload"]["event"]["event_ref"] == "BIRTHDAY_SPOUSE"
    suite = domestic_runner.run_zh_suite(task_ids=["ZH-BD-03"])
    assert suite["passed"] == 0


def test_family_state_needs_a_choice_for_an_unnamed_birthday():
    result = run_task({"task": "生日安排一下", "initial_state": "family"})
    candidates = result["responses"][-1]["payload"]["candidates"]
    assert {item["event_ref"] for item in candidates} == {"BIRTHDAY", "BIRTHDAY_SPOUSE"}
    assert result["final_status"] == "clarify" and result["dispatch_count"] == 0
