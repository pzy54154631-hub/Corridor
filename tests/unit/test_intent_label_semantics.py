"""Label documentation must preserve the existing compensation authorization boundary."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from tests.unit.test_s1_chat_control import _session, _session_with_plan


class CandidateClient:
    mode = "live-stub"
    model = "stub"

    def __init__(self, intent):
        self.intent = intent
        self.requests = []

    def complete(self, **kwargs):
        self.requests.append(deepcopy(kwargs["prompt"]))
        return SimpleNamespace(
            output={
                "intent": self.intent,
                "needs_clarification": self.intent == "clarify",
            }
        )


def quiet(session):
    c = session.controller
    return deepcopy((c.gateway.bank.execute_count, c.state.user_inputs, c.snapshot()["balances"]))


def stopped():
    session = _session_with_plan()
    session.receive("停止")
    return session


def test_classifier_documents_nonliteral_label_without_exposing_account_state():
    session = stopped()
    session.client = CandidateClient("birthday")
    session.receive("退回锁定的预算")
    (prompt,) = session.client.requests
    definition = prompt["intent_definitions"]["birthday"]
    assert "locked" in definition and "plan" in definition
    assert "card" in definition and "merchant" in definition
    assert set(prompt["response_schema"]["properties"]) == {"intent", "needs_clarification"}
    assert set(prompt) == {"instruction", "intent_definitions", "user_text", "response_schema"}
    assert "A_CURRENT" not in str(prompt) and "A_LOCKED" not in str(prompt)


@pytest.mark.parametrize("intent", ["birthday", "clarify", "transfer"])
def test_model_agreement_only_prepares_compensation_and_disagreement_stays_closed(intent):
    session = stopped()
    session.client = CandidateClient(intent)
    before = quiet(session)
    response = session.receive("退回锁定的预算")
    assert quiet(session) == before
    if intent != "birthday":
        assert response["status"] == "clarify" and not response["card"]
        assert not response["buttons"]
        return
    assert response["card"]["kind"] == "unlock_funds"
    assert response["card"]["amount"] == "1000/1"
    assert response["tier"] == "yellow" and response["status"] == "awaiting_approval"
    assert session.confirm(response["response_id"])["status"] == "settled"
    assert session.controller.gateway.bank.execute_count == before[0] + 1
    assert session.controller.gateway.verify_log()


@pytest.mark.parametrize("has_plan", [False, True])
def test_a_model_label_never_creates_missing_plan_or_stops_running_plan(has_plan):
    session = _session_with_plan() if has_plan else _session()
    session.client = CandidateClient("birthday")
    before = quiet(session)
    plans_before = deepcopy(session.controller.snapshot()["plans"])
    response = session.receive("退回锁定的预算")
    assert response["status"] == ("clarify" if has_plan else "no_active_plan")
    assert not response["card"] and not response["buttons"]
    assert quiet(session) == before
    assert session.controller.snapshot()["plans"] == plans_before


@pytest.mark.parametrize("text", ["不要退回锁定的预算", "先别退回锁定的预算"])
def test_negated_compensation_never_asks_model_or_prepares_card(text):
    session = stopped()
    session.client = CandidateClient("birthday")
    before = quiet(session)
    response = session.receive(text)
    assert response["status"] == "clarify" and not response["card"]
    assert not session.client.requests and quiet(session) == before


def test_birthday_model_label_cannot_redirect_an_ordinary_transfer_to_compensation():
    session = stopped()
    session.client = CandidateClient("birthday")
    before = quiet(session)
    response = session.receive("给妈妈转500元")
    assert response["status"] == "clarify" and not response["card"]
    assert quiet(session) == before
