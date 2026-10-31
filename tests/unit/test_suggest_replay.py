"""What the journal's strict replay covers of a session with model suggestions.

The product's live client records every call (``JournalClient``, record mode), the
suggestion call included, and each recorded request replays strictly offline. A session
rebuilt over that journal with the strict client does not ask for suggestions (the desk
asks only a live client): its clarification comes back without them, and what the replay
covers is the adopted sentence, which the recording session received as a new message.
The materials say so (external review item 5); this test pins it.
"""

from corridor.agent.domestic_session import DomesticSession
from corridor.bench.recording import network_disabled, replay_client
from tests.suggest_fakes import SENTENCE, TRANSFER, UNCLEAR, adopt, controller, suggest_rule
from tests.unit.test_ui_live_mode import FakeModel, Owner, unit_session


def normalized(response):
    """A reply without the client mode it names (record or strict) and its suggestions."""
    payload = {key: value for key, value in response["payload"].items() if key != "suggestions"}
    return {**response, "planner_mode": None, "payload": payload}


def test_strict_replay_offers_no_suggestion_and_replays_the_adopted_sentence(tmp_path, monkeypatch):
    fake = FakeModel(monkeypatch, rule=suggest_rule(TRANSFER))
    live = unit_session(tmp_path, monkeypatch)
    client = live.client(Owner(), context_guard=None)
    recording = DomesticSession(controller(), client=client, suggest=True)
    first = recording.receive(UNCLEAR)
    assert [item["sentence"] for item in first["payload"]["suggestions"]["items"]] == [SENTENCE]
    adopted = adopt(recording, first["response_id"], 0)
    assert adopted["card"]["kind"] == "transfer"
    # A lexical clarification is never sent to the intent step: the suggestion is the one
    # call of the first turn; the adopted sentence's intent step is the next.
    steps = [kwargs["step"] for kwargs, _ in fake.requests]
    assert steps == ["suggest:1", "intent:2"]

    replay = replay_client(client)
    assert replay.mode == "strict"
    # Every recorded request, the suggestion's included, replays strictly offline.
    with network_disabled():
        for kwargs, output in fake.requests:
            again = replay.complete(**kwargs)
            assert again.source == "replay" and again.output == output
    # A session rebuilt over the journal: no suggestion, the same clarification, and the
    # adopted sentence replays as the message it was.
    replaying = DomesticSession(controller(), client=replay_client(client), suggest=True)
    with network_disabled():
        clarified = replaying.receive(UNCLEAR)
        typed = replaying.receive(SENTENCE)
    assert "suggestions" not in clarified["payload"]
    assert replaying.suggestions.log.records() == []
    assert normalized(clarified) == normalized(first)
    assert normalized(typed) == normalized(adopted)
    assert replaying.history == recording.history
