"""Adjudicated domestic language and presentation boundaries use synthetic sessions."""

from copy import deepcopy
from fractions import Fraction
from types import SimpleNamespace

import pytest

from corridor.agent.domestic_slots import money_candidate
from corridor.bench.domestic_runner import make_session
from corridor.channels.im import IMAdapter, render_message
from corridor.crypto.jcs import jcs_bytes


@pytest.mark.parametrize(
    "when", ["礼拜五", "下星期", "过两天", "下午", "三点", "19:30", "明年", "春节", "今晚"]
)
def test_temporal_words_cannot_create_immediate_transfer(when):
    session = make_session()
    response = session.receive(f"{when}给房东转800元")
    assert response["needs_clarification"]
    assert not response["card"]
    assert not response["buttons"]
    assert session.controller.gateway.bank.execute_count == 0


@pytest.mark.parametrize(
    "recipient", ["别人", "其他", "他们", "她们", "房东的朋友", "老妈妈", "新室友"]
)
def test_modified_or_plural_recipient_never_reuses_a_contact(recipient):
    session = make_session()
    response = session.receive("给老妈转100元")
    assert session.confirm(response["response_id"])["status"] == "settled"
    response = session.receive(f"给{recipient}转200元")
    assert response["needs_clarification"]
    assert not response["card"]
    assert session.controller.gateway.bank.execute_count == 1


@pytest.mark.parametrize("text", ["给(PHONE_CHEN转200元", "给(PHONE_CHEN)转200元"])
def test_tokenized_phone_formatting_preserves_a_whole_recipient(text):
    from corridor.agent.domestic_slots import recipient_candidates

    assert recipient_candidates(text, {"CONTACT_CHEN": ["PHONE_CHEN"]}) == (["CONTACT_CHEN"], False)


@pytest.mark.parametrize(
    "text", ["给新(PHONE_CHEN)转200元", "给(PHONE_CHEN)的朋友转200元", "给(PHONE_CHENg)转200元"]
)
def test_tokenized_phone_parentheses_do_not_hide_modifiers(text):
    from corridor.agent.domestic_slots import recipient_candidates

    assert recipient_candidates(text, {"CONTACT_CHEN": ["PHONE_CHEN"]}) == ([], False)


def test_standalone_pronoun_names_reused_recipient_in_card_text():
    adapter = IMAdapter(make_session())
    first = adapter.receive("给老妈转100元")
    assert adapter.click("confirm", first["response_id"])["status"] == "settled"
    next_message = adapter.receive("再给她200元")
    assert next_message["card"]["objects"]["contact_ref"] == "CONTACT_CHEN"
    assert "沿用上一位收款人" in next_message["text"]


@pytest.mark.parametrize("field", ["tier_reasons", "irreversible", "execution_window"])
def test_im_incomplete_card_has_no_confirm_button(field):
    session = make_session()
    response = session.receive("给房东转3200元")
    response["card"] = deepcopy(response["card"])
    response["card"].pop(field)
    rendered = render_message(response, controller=session.controller)
    assert not rendered["buttons"]


@pytest.mark.parametrize("field", ["contact_ref", "source_ref"])
def test_im_missing_transfer_object_has_no_confirm_button(field):
    session = make_session()
    response = session.receive("给房东转3200元")
    response["card"]["objects"].pop(field)
    rendered = render_message(response, controller=session.controller)
    assert not rendered["buttons"]


@pytest.mark.parametrize(
    "text,field", [("卡丢了", "card_ref"), ("取消S1订阅", "authorization_ref")]
)
def test_im_card_and_subscription_require_the_named_object(text, field):
    session = make_session("card")
    response = session.receive(text)
    complete = render_message(response, controller=session.controller)
    assert complete["buttons"] and response["card"]["objects"][field] in complete["text"]
    response["card"]["objects"].pop(field)
    assert not render_message(response, controller=session.controller)["buttons"]


def test_im_plan_renders_every_node_and_rejects_an_incomplete_node():
    from corridor.agent.domestic_session import DomesticSession
    from corridor.ui.controller import DemoController, demo_state

    # G06: the baseline fixture has no partner, so the old plan for Chen/mom was a mislabel.
    baseline = make_session()
    unresolved = baseline.receive("我爱人生日快到了，帮我安排一下")
    assert unresolved["status"] == "clarify" and not unresolved["payload"].get("nodes")
    assert not render_message(unresolved, controller=baseline.controller)["buttons"]
    controller = DemoController(initial_state=demo_state(showcase=True))
    controller.start_session()
    session = DomesticSession(controller)
    response = session.receive("我爱人生日快到了，帮我安排一下")
    complete = render_message(response, controller=session.controller)
    assert complete["buttons"]
    assert "FLOWERS" in complete["text"] or "鲜花" in complete["text"]
    assert "2026-10-21T16:00:00Z" in complete["text"]
    response["payload"]["nodes"][1]["card"].pop("execution_window")
    assert not render_message(response, controller=session.controller)["buttons"]


@pytest.mark.parametrize(
    "amount,expected",
    [("2万5", Fraction(25000)), ("1万2", Fraction(12000)), ("十块八毛", Fraction(54, 5))],
)
def test_colloquial_amounts_are_exact_in_preview(amount, expected):
    assert money_candidate(f"给房东转{amount}") == expected
    response = make_session().receive(f"给房东转{amount}")
    assert Fraction(response["card"]["amount"]) == expected


@pytest.mark.parametrize("amount", ["一笔钱", "两次钱", "三个人", "十块八毛多", "十块八毛或二十块"])
def test_quantities_and_qualified_money_require_clarification(amount):
    assert money_candidate(f"给老妈转{amount}") is None
    response = make_session().receive(f"给老妈转{amount}")
    assert response["needs_clarification"]
    assert not response["card"]


def test_intent_evaluation_uses_new_sessions_and_vault_guard(tmp_path):
    from corridor.bench.domestic_runner import evaluate_intents

    class Classifier:
        mode = "record"
        model = "synthetic-intent-classifier"

        def __init__(self):
            self.guards = []

        def complete(self, **request):
            assert request["step"] == "intent:1"
            assert request["tool_state"] == {"previous_intent": None}
            assert "Chen" not in request["prompt"]["user_text"]
            with pytest.raises(ValueError, match="PII"):
                self.context_guard("Chen")
            self.guards.append(self.context_guard)
            return SimpleNamespace(output={"intent": "transfer", "needs_clarification": False})

    client = Classifier()
    source = tmp_path / "intents.jsonl"
    rows = [
        {
            "id": f"ISOLATED-{index}",
            "text": text,
            "expected": {
                "intent": "transfer",
                "scenario": "transfers",
                "needs_clarification": index == 1,
                "parameters": {"recipient": None, "amount": None, "date": None, "product": None},
            },
        }
        for index, text in enumerate(("给Chen转200元", "再给他100元"))
    ]
    source.write_bytes(b"".join(jcs_bytes(row) + b"\n" for row in rows))
    report = evaluate_intents(client=client, source=source)
    assert report["clarification_correct"] == 2
    assert len(client.guards) == 2 and client.guards[0] != client.guards[1]
    assert not hasattr(client, "context_guard")
    assert report["results"][1]["parameters"]["recipient"] is None
