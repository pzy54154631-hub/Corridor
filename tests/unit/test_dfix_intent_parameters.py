"""Chinese candidates remain exact and unsigned until device review."""

from fractions import Fraction

import pytest

from corridor.agent.domestic_session import DomesticSession
from corridor.stores.bank import add_seconds
from corridor.ui.controller import DemoController, demo_state


@pytest.mark.parametrize(
    "text,amount",
    [
        ("给妈妈转八百", 800),
        ("给妈妈转三千二", 3200),
        ("给妈妈转一千零五十", 1050),
        ("给妈妈转两万", 20000),
        ("给妈妈转5万", 50000),
        ("给妈妈转1.2万", 12000),
        ("给妈妈转三百五十块", 350),
        ("给妈妈转八百块钱", 800),
        ("给妈妈转¥800", 800),
        ("给妈妈转99.50元", Fraction(199, 2)),
    ],
)
def test_exact_chinese_amount_candidates(text, amount):
    from corridor.agent.domestic_slots import money_candidate

    assert money_candidate(text) == Fraction(amount)


@pytest.mark.parametrize(
    "text",
    [
        "转八百或九百元",
        "转二三百元",
        "转1,200元",
        "转1/2元",
        "转-800元",
        "转0.001元",
        "转八百多元",
        "转一两千元",
        "转800元再转900元",
        "转大概八百元",
        "转八百元左右",
        "转800元或900",
        "转800块钱，或者900",
        "大约给妈妈转八百元",
        "大概转800元给妈妈",
        "给妈妈转八百块钱多",
    ],
)
def test_ambiguous_or_malformed_amounts_require_clarification(text):
    from corridor.agent.domestic_slots import money_candidate

    assert money_candidate(text) is None


@pytest.mark.parametrize(
    "text",
    [
        "给妈妈转八百元左右",
        "给妈妈转800元或900",
        "给妈妈转800或900元，同样金额",
        "给妈妈转-800元，同样金额",
        "给妈妈转0.001元，同一金额",
        "大约给妈妈转八百元",
        "大概转800元给妈妈",
        "给妈妈转八百块钱多",
    ],
)
def test_invalid_explicit_money_never_falls_back_to_previous_or_creates_card(text):
    from corridor.agent.domestic_slots import money_candidate

    session = _session()
    initial = session.receive("给妈妈转200元")
    assert session.confirm(initial["response_id"])["status"] == "settled"
    assert money_candidate(text, "200/1") is None
    response = session.receive(text)
    assert response["status"] == "clarify"
    assert response["payload"]["parameters"]["amount"] is None
    assert not response["card"]
    assert session.controller.gateway.bank.execute_count == 1


def test_same_amount_without_an_explicit_number_keeps_confirmed_value():
    from corridor.agent.domestic_slots import money_candidate

    assert money_candidate("给妈妈转同样金额", "200/1") == Fraction(200)


@pytest.mark.parametrize(
    "text",
    [
        "2026-99-99给妈妈转八百元",
        "2026-10-17或2026-10-18给妈妈转八百元",
        "2026-1-99给妈妈转八百元",
        "2026/99/99给妈妈转八百元",
    ],
)
def test_unresolved_explicit_dates_never_fall_through_to_immediate_transfer(text):
    session = _session()
    response = session.receive(text)
    assert response["needs_clarification"]
    assert response["payload"]["parameters"]["date"] is None
    assert not response["card"]
    assert session.controller.gateway.bank.execute_count == 0


def _session():
    controller = DemoController()
    controller.start_session()
    return DomesticSession(controller)


def test_fixture_aliases_and_ambiguity_are_authoritative():
    session = _session()
    for alias in ("妈妈", "老妈", "我妈"):
        response = session.receive(f"给{alias}转八百元")
        assert response["card"]["amount"] == "800/1"
        assert response["payload"]["contacts"] == ["CONTACT_CHEN"]
    contacts = session.controller.state.contacts.copy()
    contact = contacts["CONTACT_LIN_A"]
    contacts[contact.id] = contact.model_copy(update={"aliases": [*contact.aliases, "妈妈"]})
    session.controller.gateway.store.update(contacts=contacts)
    response = session.receive("给妈妈转八百元")
    assert response["status"] == "clarify"
    assert set(response["payload"]["contacts"]) == {"CONTACT_CHEN", "CONTACT_LIN_A"}
    assert not session.controller.state.user_inputs
    assert session.controller.gateway.bank.execute_count == 0


def test_trusted_private_alias_is_tokenized_before_model_context(monkeypatch):
    from corridor.agent.llm import LLMClient
    from corridor.gateway.privacy import PIIVault

    session = _session()
    session.controller.gateway.vault = PIIVault({"NAME_LIN": "小林"})
    contacts = session.controller.state.contacts.copy()
    for ref in ("CONTACT_LIN_A", "CONTACT_LIN_B"):
        contact = contacts[ref]
        contacts[ref] = contact.model_copy(update={"aliases": [*contact.aliases, "小林"]})
    session.controller.gateway.store.update(contacts=contacts)
    complete = LLMClient.complete

    def observed(client, **kwargs):
        assert "小林" not in str(kwargs.get("tool_state", {}))
        return complete(client, **kwargs)

    monkeypatch.setattr(LLMClient, "complete", observed)
    response = session.receive("给小林转八百元")
    assert response["status"] == "clarify"
    assert response["payload"]["contacts"] == ["CONTACT_LIN_A", "CONTACT_LIN_B"]


@pytest.mark.parametrize(
    "phrase,expected",
    [
        ("明天", "2026-10-17"),
        ("下周五", "2026-10-23"),
        ("月底", "2026-10-31"),
        ("生日前两天", "2026-10-18"),
    ],
)
def test_relative_dates_use_simulated_clock(phrase, expected):
    session = _session()
    response = session.receive(f"{phrase}给房东转三千二")
    assert response["payload"]["parameters"]["date"] == expected
    assert response["payload"]["parameters"]["amount"] == "3200/1"
    # G11: closed-grammar dates now yield the form's unsigned scheduled card instead of
    # the old "go to the form" clarification; the birthday template still clarifies.
    if phrase == "生日前两天":
        assert response["needs_clarification"] and not response["card"]
    else:
        assert not response["needs_clarification"]
        assert response["card"]["kind"] == "schedule_transfer"
        assert response["card"]["spa"]["start"] == add_seconds(f"{expected}T00:00:00Z", -8 * 3600)
        assert not session.controller.state.user_inputs
    assert session.controller.gateway.bank.execute_count == 0


def test_relative_dates_and_bill_month_follow_bank_utc_plus_eight_day():
    from corridor.agent.domestic_session import _billing_period
    from corridor.agent.domestic_slots import date_candidate

    context = {"now": "2026-10-31T16:00:00Z"}
    assert date_candidate("明天", context) == "2026-11-02"
    assert date_candidate("月底", context) == "2026-11-30"
    assert date_candidate("下周五", context) == "2026-11-06"
    assert date_candidate("这个月账单", context, billing=True) == "2026-11"
    assert _billing_period("这个月账单", context["now"]) == "2026-11"


def test_aa_total_count_and_exact_share_still_require_participant_selection():
    response = _session().receive("这顿饭 460 我们四个 AA")
    assert response["payload"]["parameters"]["amount"] == "460/1"
    assert response["payload"]["aa"] == {"participants": 4, "per_person": "115/1"}
    assert response["needs_clarification"]


def test_all_six_home_shortcuts_reach_correct_next_step():
    from corridor.ui.domestic_views import EXAMPLES
    from corridor.ui.messages import source_utterance

    expected = {
        "transfer": "awaiting_input",
        "billing": "settled",
        "wealth": "awaiting_device_selection",
        "cards": "awaiting_device_selection",
        "subscriptions": "awaiting_approval",
        "birthday": "draft",
    }
    for kind, prompt in EXAMPLES:
        response = _session().receive(source_utterance(prompt))
        if kind == "birthday":
            # G06: the partner shortcut planned Chen/mom's birthday; the baseline has no partner.
            assert response["status"] == "clarify", response
            controller = DemoController(initial_state=demo_state(showcase=True))
            controller.start_session()
            response = DomesticSession(controller).receive(source_utterance(prompt))
            assert response["payload"]["event"]["event_ref"] == "BIRTHDAY_SPOUSE"
        assert response["status"] == expected[kind], (kind, response)


def test_parameter_eval_uses_frozen_labels_and_retains_denominators(tmp_path):
    from corridor.bench.interaction import run_intent_eval

    report = run_intent_eval(output=tmp_path)
    assert report["total"] == 155
    assert report["parameter_exact_match"] == {"correct": 155, "total": 155, "accuracy": "155/155"}
    assert set(report["parameter_accuracy"]) == {"recipient", "amount", "date", "product"}
    assert all(metric["total"] == 155 for metric in report["parameter_accuracy"].values())
    assert report["parameter_accuracy"]["amount"]["present_total"] > 20


def test_explicit_product_is_from_catalog_and_vague_product_remains_unknown():
    from corridor.agent.domestic_slots import extract_slots, trusted_slot_context

    controller = DemoController()
    context = trusted_slot_context(controller.state, controller.device.principal)
    assert extract_slots("买两万元产品P_A", context, intent="investment")["parameters"] == {
        "recipient": None,
        "amount": "20000/1",
        "date": None,
        "product": "P_A",
    }
    for text in ("买两万那个稳健的", "比较产品P_A和P_B", "买产品P_UNKNOWN"):
        assert extract_slots(text, context, intent="investment")["parameters"]["product"] is None


def test_slot_accuracy_compares_independent_expected_labels(tmp_path):
    from corridor.bench.domestic_runner import evaluate_intents
    from corridor.crypto.jcs import jcs_bytes

    source = tmp_path / "labeled.jsonl"
    source.write_bytes(
        jcs_bytes({
            "id": "WRONG-AMOUNT-LABEL",
            "text": "给妈妈转八百元",
            "expected": {
                "intent": "transfer",
                "scenario": "transfers",
                "needs_clarification": False,
                "parameters": {
                    "recipient": "CONTACT_CHEN",
                    "amount": "900/1",
                    "date": None,
                    "product": None,
                },
            },
        })
        + b"\n"
    )
    report = evaluate_intents(source=source)
    assert report["parameter_exact_match"]["accuracy"] == "0/1"
    assert report["parameter_accuracy"]["amount"]["present_accuracy"] == "0/1"


def test_e2_report_includes_parameter_accuracy_and_nonempty_denominators(tmp_path):
    import importlib.util
    from pathlib import Path

    from corridor.bench.interaction import run_intent_eval

    path = Path(__file__).resolve().parents[2] / "scripts/final_report_zh.py"
    spec = importlib.util.spec_from_file_location("dfix_final_report", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    report = run_intent_eval(output=tmp_path)
    text, evidence = module.build_report([], dryrun=True, apparatus={"intents": report})
    assert "参数全匹配 155/155" in text
    assert "product：含空标签 155/155；非空标签 not_evaluable（分母 0）" in text
    assert evidence["apparatus"]["intents"]["parameter_exact_match"]["correct"] == 155
