"""Birthday events, dates and relations come only from trusted state (G06/G07)."""

import pytest

from corridor.agent.birthday_event import (
    AMBIGUOUS,
    birthday_candidates,
    derive_dates,
    lock_window_open,
    relation_in_text,
    relation_of,
    resolve_birthday,
)
from corridor.crypto.jcs import H
from corridor.schemas.domain import CalendarEvent
from corridor.ui.controller import DemoController, demo_state


def _event(identifier, event_date, contact="CONTACT_CHEN", owner="C_DEMO"):
    return CalendarEvent(
        id=identifier,
        owner=owner,
        event_type="birthday",
        event_date=event_date,
        person_contact_ref=contact,
        source_ref="DEVICE_CAL",
    )


def _with(state, *, calendar=None, contacts=None):
    update = {}
    if calendar is not None:
        update["calendar"] = {**state.calendar, **{item.id: item for item in calendar}}
    if contacts is not None:
        update["contacts"] = {**state.contacts, **{item.id: item for item in contacts}}
    return state.model_copy(update=update)


def _gateway(state):
    return DemoController(initial_state=state).gateway


@pytest.mark.parametrize(
    "event_date,order_due,lock_month",
    [
        ("2026-10-20", "2026-10-17T16:00:00Z", "2026-10"),
        ("2026-10-24", "2026-10-21T16:00:00Z", "2026-10"),
        ("2026-11-01", "2026-10-29T16:00:00Z", "2026-10"),
        ("2027-01-01", "2026-12-29T16:00:00Z", "2026-12"),
    ],
)
def test_derived_dates_match_the_frozen_gateway_suggestion(event_date, order_due, lock_month):
    from corridor.sim.planning import BIRTHDAY_DUE

    state = _with(demo_state(), calendar=[_event("BIRTHDAY_PROBE", event_date)])
    dates = derive_dates(state.calendar["BIRTHDAY_PROBE"])
    assert (dates.order_due, dates.lock_month) == (order_due, lock_month)
    assert dates.spa_end > dates.order_due
    suggestions = {
        item["event_ref"]: item["due_at"] for item in _gateway(state).birthday_suggestions("C_DEMO")
    }
    assert suggestions["BIRTHDAY_PROBE"] == dates.order_due
    assert derive_dates(state.calendar["BIRTHDAY"]).order_due == BIRTHDAY_DUE


def test_lock_window_is_the_order_days_beijing_month_before_ordering():
    dates = derive_dates(_event("BIRTHDAY_PROBE", "2026-11-01"))
    assert dates.lock_window_start == "2026-09-30T16:00:00Z"
    assert not lock_window_open(dates, "2026-09-30T15:59:59Z")
    assert lock_window_open(dates, "2026-09-30T16:00:00Z")
    assert lock_window_open(dates, "2026-10-29T15:59:59Z")
    assert not lock_window_open(dates, "2026-10-29T16:00:00Z")


def test_relation_comes_from_owned_verified_aliases_in_one_closed_class():
    state = demo_state(showcase=True)
    assert relation_of(state, "C_DEMO", "CONTACT_SPOUSE") == "spouse"
    # Chen's aliases are roommate and mom: a parent, never a partner.
    assert relation_of(state, "C_DEMO", "CONTACT_CHEN") == "parent"
    assert relation_of(state, "C_DEMO", "CONTACT_LIN_A") is None
    spouse = state.contacts["CONTACT_SPOUSE"]
    unverified = spouse.model_copy(
        update={"id": "CONTACT_UNVERIFIED", "verified_at": None, "verification_ref": None}
    )
    foreign = spouse.model_copy(update={"id": "CONTACT_FOREIGN", "owner": "C_OTHER"})
    mixed = spouse.model_copy(update={"id": "CONTACT_MIXED", "aliases": ["爱人", "妈妈"]})
    state = _with(state, contacts=[unverified, foreign, mixed])
    for reference in ("CONTACT_UNVERIFIED", "CONTACT_FOREIGN", "CONTACT_MIXED", "CONTACT_GHOST"):
        assert relation_of(state, "C_DEMO", reference) is None
    ghost = _with(state, calendar=[_event("BIRTHDAY_GHOST", "2026-10-26", "CONTACT_GHOST")])
    candidates = {item.event_ref: item for item in birthday_candidates(_gateway(ghost), "C_DEMO")}
    assert candidates["BIRTHDAY_GHOST"].relation is None


@pytest.mark.parametrize(
    "text,relation",
    [
        ("我爱人生日快到了", "spouse"),
        ("老婆生日有什么安排", "spouse"),
        ("我妈生日快到了", "parent"),
        ("我妈和我爱人的生日", AMBIGUOUS),
        ("帮我安排生日", None),
        ("我爱人12月1日生日帮我安排", "spouse"),
        ("a moment for the birthday", None),
    ],
)
def test_text_only_narrows_by_the_closed_relation_vocabulary(text, relation):
    assert relation_in_text(text) == relation


def test_resolution_never_guesses_and_never_accepts_an_ineligible_event():
    state = _with(
        demo_state(showcase=True),
        calendar=[
            _event("BIRTHDAY_OTHER", "2026-10-26", owner="C_OTHER"),
            _event("BIRTHDAY_PAST", "2026-10-17"),
        ],
    )
    gateway = _gateway(state)
    before = H(gateway.store.snapshot())
    for reference in ("BIRTHDAY_UNKNOWN", "BIRTHDAY_OTHER", "BIRTHDAY_PAST"):
        result = resolve_birthday(gateway, "C_DEMO", event_ref=reference)
        assert result.status == "event_not_eligible" and result.event is None
    assert H(gateway.store.snapshot()) == before
    unresolved = resolve_birthday(gateway, "C_DEMO")
    assert unresolved.status == "selection_required" and unresolved.event is None
    assert [item.event_ref for item in unresolved.candidates] == ["BIRTHDAY", "BIRTHDAY_SPOUSE"]
    spouse = resolve_birthday(gateway, "C_DEMO", relation="spouse")
    assert spouse.status == "resolved" and spouse.event.event_ref == "BIRTHDAY_SPOUSE"
    parent = resolve_birthday(gateway, "C_DEMO", relation="parent")
    assert parent.status == "resolved" and parent.event.event_ref == "BIRTHDAY"
    assert resolve_birthday(gateway, "C_DEMO", relation=AMBIGUOUS).status == "selection_required"
    conflict = resolve_birthday(gateway, "C_DEMO", event_ref="BIRTHDAY", relation="spouse")
    assert conflict.status == "selection_required"
    assert H(gateway.store.snapshot()) == before


def test_father_words_never_select_the_mother_birthday():
    assert relation_in_text("我爸生日快到了") == "father"
    assert relation_in_text("我妈和我爸的生日") == AMBIGUOUS
    state = demo_state(showcase=True)
    chen = state.contacts["CONTACT_CHEN"]
    father = chen.model_copy(update={"id": "CONTACT_FATHER", "aliases": ["爸爸"]})
    both = chen.model_copy(update={"id": "CONTACT_BOTH", "aliases": ["妈妈", "爸爸"]})
    extended = _with(state, contacts=[father, both])
    assert relation_of(extended, "C_DEMO", "CONTACT_FATHER") == "father"
    assert relation_of(extended, "C_DEMO", "CONTACT_BOTH") is None
    for showcase, expected in ((True, ["BIRTHDAY", "BIRTHDAY_SPOUSE"]), (False, ["BIRTHDAY"])):
        result = resolve_birthday(
            _gateway(demo_state(showcase=showcase)), "C_DEMO", relation="father"
        )
        assert result.status == "selection_required" and result.event is None
        assert [item.event_ref for item in result.candidates] == expected


def test_baseline_partner_request_lists_the_only_birthday_instead_of_guessing():
    gateway = _gateway(demo_state())
    result = resolve_birthday(gateway, "C_DEMO", relation="spouse")
    assert result.status == "selection_required"
    assert [item.event_ref for item in result.candidates] == ["BIRTHDAY"]
    assert resolve_birthday(gateway, "C_DEMO").event.event_ref == "BIRTHDAY"


def test_builder_defaults_equal_the_derived_baseline_event():
    from corridor.agent.llm import LLMClient
    from corridor.bench.planning_runner import _case, prepare_birthday, run_j19

    default_case = _case("EQUIVALENCE", "birthday", None)
    derived_case = _case("EQUIVALENCE", "birthday", None)
    default = prepare_birthday(default_case)
    derived = prepare_birthday(
        derived_case,
        event_ref="BIRTHDAY",
        due_at=derive_dates(derived_case.state.calendar["BIRTHDAY"]).order_due,
    )
    assert H(default["plan"]) == H(derived["plan"])
    assert H(default["spas"]) == H(derived["spas"])
    assert H(default["creations"]) == H(derived["creations"])
    assert H(default_case.state) == H(derived_case.state)
    result = run_j19(LLMClient(mode="scripted", env_file=None))
    assert result["passed"] and all(result["checks"].values())


def test_operator_target_is_the_next_future_run_or_the_former_fallback():
    controller = DemoController(initial_state=demo_state(showcase=True))
    controller.start_session()
    assert controller.next_due_target() == "2026-10-18T02:00:00Z"
    draft = controller.preview_birthday(event_ref="BIRTHDAY_SPOUSE")
    controller.confirm_plan(draft["plan_id"], [node["id"] for node in draft["nodes"]])
    assert controller.next_due_target() == "2026-10-21T16:00:00Z"
    controller.advance_clock("2026-10-21T16:00:00Z")
    assert controller.next_due_target() is None


def test_synthetic_partner_vault_values_collide_with_no_frozen_text():
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    sources = [
        root / "data/scenarios/zh/cases.json",
        root / "data/scenarios/zh/colloquial.jsonl",
        *sorted((root / "src/corridor/ui").glob("messages*.json")),
    ]
    for source in sources:
        text = source.read_bytes().decode("utf-8")
        assert "Qiao" not in text and "13900001005" not in text, source
    vault = DemoController().gateway.vault
    assert vault.tokenize("Qiao 13900001005") == "NAME_SPOUSE PHONE_SPOUSE"
