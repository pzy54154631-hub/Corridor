"""Contact minimization is opt-in and never supplies an unmentioned recipient."""

from copy import deepcopy

import pytest

from corridor.agent.dialogue import Conversation
from corridor.bench.transfer_runner import make_conversation


def conversation(monkeypatch, *, minimal=False):
    original, _ = make_conversation(task_id="CONTACT-MIN")
    chat = Conversation(
        original.gateway,
        original.client,
        principal=original.principal,
        session_id=original.session_id,
        minimize_contacts=minimal,
    )
    requests = []
    complete = chat.client.complete

    def capture(**kwargs):
        requests.append(deepcopy(kwargs["tool_state"]))
        return complete(**kwargs)

    monkeypatch.setattr(chat.client, "complete", capture)
    return chat, requests


def test_legacy_contact_prompt_remains_complete_by_default(monkeypatch):
    chat, requests = conversation(monkeypatch)
    assert chat.receive("给小陈转200元", request_id="one").status == "awaiting_input"
    assert set(requests[0]["contacts"]) == set(chat.state.contacts)
    assert "PHONE_CHEN" in requests[0]["contacts"]["CONTACT_CHEN"]


@pytest.mark.parametrize(
    ("text", "expected", "status"),
    [
        ("给小陈转200元", {"CONTACT_CHEN": ["NAME_CHEN"]}, "awaiting_input"),
        ("给13900001001转200元", {"CONTACT_CHEN": ["PHONE_CHEN"]}, "awaiting_input"),
        (
            "给小林转200元",
            {"CONTACT_LIN_A": ["NAME_LIN"], "CONTACT_LIN_B": ["NAME_LIN"]},
            "clarify",
        ),
    ],
)
def test_only_matching_names_leave_the_process(monkeypatch, text, expected, status):
    chat, requests = conversation(monkeypatch, minimal=True)
    chat._previous_contact = "CONTACT_NEW"
    result = chat.receive(text, request_id="one")
    assert result.status == status
    assert requests == [{"contacts": expected, "previous_contact": None}]
    assert chat.gateway.bank.execute_count == 0


def test_no_match_clarifies_without_a_model_call_or_previous_contact_guess(monkeypatch):
    chat, requests = conversation(monkeypatch, minimal=True)
    chat._previous_contact = "CONTACT_CHEN"
    result = chat.receive("给不认识的人转200元", request_id="one")
    assert result.status == "clarify" and result.contact_refs == []
    assert requests == []
    assert chat.gateway.bank.execute_count == 0


def test_explicit_pronoun_sends_only_its_locally_resolved_handle(monkeypatch):
    chat, requests = conversation(monkeypatch, minimal=True)
    chat._previous_contact = "CONTACT_CHEN"
    result = chat.receive("再给他转200元", request_id="one")
    assert result.status == "awaiting_input"
    assert requests == [{"contacts": {"CONTACT_CHEN": []}, "previous_contact": "CONTACT_CHEN"}]
