"""session-intent-04: negated, incoming and interrogative transfer sentences become payment cards.

Stated rules: docs/DESIGN-DOMESTIC.md section 5 (ambiguous/implicit intent must be clarified;
E2 measures "追问恰当率"), and the session's own contract that the model may only make the
outcome more conservative (domestic_session._classify turns any model/lexical disagreement
into clarification).

domestic_intent.recognize routes any sentence containing "转" to `transfer`, ignoring
negation (不要/别), direction (老妈给我转) and questions (…吗？/如果…). The session then
previews an outgoing transfer. In addition, DomesticSession._receive_business never reads
IntentCandidate.needs_clarification, so even a model that correctly flags the sentence as
needing clarification cannot stop the executable card.
"""

from types import SimpleNamespace

import pytest

from corridor.agent.domestic_session import DomesticSession
from corridor.ui.controller import DemoController


def _session():
    controller = DemoController()
    controller.start_session()
    return DomesticSession(controller)


@pytest.mark.parametrize(
    "text",
    [
        "不要给老妈转800元",
        "先别给老妈转800元",
        "老妈给我转了800元",
        "妈妈昨天转给我800元，帮我查一下",
        "我上个月给老妈转了800元吗？",
        "如果给老妈转800元会超限吗",
    ],
)
def test_session_intent_04_non_instruction_never_yields_outgoing_transfer_card(text):
    session = _session()
    response = session.receive(text)
    assert not response["card"], (text, response["card"].get("amount"), response["tier"])
    assert response["buttons"] == []


@pytest.mark.parametrize("text", ["不转了", "别转了", "算了，不转了", "停止转账", "取消刚才的转账"])
def test_session_intent_04_cancel_phrase_retires_pending_card(text):
    # DESIGN section 5: "中断：用户随时「停」，已结算的保留，未执行的取消".
    session = _session()
    pending = session.receive("给房东转500元")
    assert pending["buttons"]
    reply = session.receive(text)
    after = session.confirm(pending["response_id"])
    assert after["status"] == "withdrawn", (text, reply["intent"], reply["status"])
    assert session.controller.gateway.bank.execute_count == 0


def test_session_intent_04_model_clarification_flag_is_honoured(monkeypatch):
    session = _session()
    original = session.client.complete

    def classify(**kwargs):
        # Only the session's intent classification step is answered by the stub model;
        # the controller's own extraction/planning steps keep their scripted behaviour.
        if kwargs["step"].startswith("intent:"):
            return SimpleNamespace(output={"intent": "transfer", "needs_clarification": True})
        return original(**kwargs)

    monkeypatch.setattr(session.client, "complete", classify)
    response = session.receive("不要给老妈转800元")
    # The (untrusted) model agrees on the intent but asks for clarification; the more
    # conservative signal is dropped and a confirmable payment card is offered.
    assert response["needs_clarification"], response["status"]
    assert not response["card"], response["card"].get("amount")
