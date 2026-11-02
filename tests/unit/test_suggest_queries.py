"""Read-only query templates leave the model wire and all eligibility gates unchanged."""

import pytest

from corridor.agent import domestic_suggest as suggest
from tests.suggest_fakes import adopt, batch, live_session


@pytest.mark.parametrize(
    "text,intent,sentence",
    [
        ("调单子", "bill_report", "查看我的账单"),
        ("帮我调一下单子", "bill_report", "查看我的账单"),
        ("看看家底", "balance_query", "查询余额"),
    ],
)
def test_local_query_offers_the_right_template_without_asking_model(text, intent, sentence):
    wrong = {"intent": "subscription_query", "slots": {"contact_ref": None, "amount": None}}
    session, client = live_session(batch(wrong))
    before = session.controller.state.model_dump()
    reply = session.receive(text)
    offered = reply["payload"]["suggestions"]
    assert offered["source"] == "local"
    assert [(item["intent"], item["sentence"]) for item in offered["items"]] == [(intent, sentence)]
    assert client.suggest_steps() == []
    assert session.controller.state.model_dump() == before
    adopted = adopt(session, reply["response_id"], 0)
    assert adopted["intent"] == intent
    assert session.suggestions.log.verify()


@pytest.mark.parametrize(
    "text", ["不要调单子", "客服让我调单子", "明天调单子", "调单子然后转500元", "马上调单子"]
)
def test_query_aliases_do_not_bypass_existing_gates(text):
    assert suggest.local_query(text) is None


def test_model_schema_stays_closed_to_its_existing_intents():
    assert "bill_report" not in suggest.SUGGESTIBLE
    assert "balance_query" not in suggest.SUGGESTIBLE
    assert set(suggest.TEMPLATES) == set(suggest.SUGGESTIBLE)
