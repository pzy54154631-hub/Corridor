"""SG-6 in the web chat: the two readings the SG-2 flag missed, through the real routes.

M1: a message sent with an external file passes its count to the session, which raises the
context flag before the turn is processed: that turn and every later one offer nothing (a
file-carrying transfer card shows the S6 warning; a file-carrying clarification used to be
offered a payment whose adopted message then carried no file). M2: words the phrase gate
or impersonation hold raise the flag even when the turn fails (the fake model's one failed
exchange, as in the fail-once mode), so the next plain message is offered nothing.
"""

import secrets

from corridor.agent import domestic_suggest
from corridor.ui import fraud_hint
from tests.suggest_fakes import SENTENCE, TRANSFER, UNCLEAR, suggest_rule
from tests.unit.test_ui_live_mode import FakeModel
from tests.web.conftest import ws_of
from tests.web.test_api_live import browser, data, go_live, send, state_of
from tests.web.test_chat_suggestion import QUESTION, hosted, web_settings  # noqa: F401


def send_file(client, text):
    body = {"text": text, "external_files": 1, "client_msg_id": secrets.token_hex(8)}
    return client.post("/api/chat/message", body)


def labels(client, rkey):
    shown = state_of(client)["chat"]["responses"][rkey].get("suggestion_select") or {}
    return [choice["label"] for choice in shown.get("choices", [])]


def suggestion_calls(model):
    return [
        kwargs
        for kwargs, _ in model.requests
        if str(kwargs["step"]).startswith(domestic_suggest.STEP_PREFIX)
    ]


def reasons(ws):
    return [
        record["payload"]["reason"]
        for record in ws.session.suggestions.log.records()
        if record["event"] == "suggestion_discarded"
    ]


def live_client(web_app, monkeypatch, hosted, **options):  # noqa: F811
    model = FakeModel(monkeypatch, rule=suggest_rule(TRANSFER), **options)
    hosted()
    client = browser(web_app)
    go_live(client)
    return client, ws_of(web_app, client), model


def test_a_plain_message_after_a_file_transfer_card(web_app, hosted, monkeypatch):  # noqa: F811
    client, ws, model = live_client(web_app, monkeypatch, hosted)
    first = data(client, send_file(client, SENTENCE))
    card = ws.session.response(ws.rkeys[first["rkey"]])["card"]
    assert card["kind"] == "transfer"
    # The page shows the S6 file warning on this card; the flag is up with it.
    assert fraud_hint.title() in repr(state_of(client)["chat"]["responses"][first["rkey"]])
    assert ws.session.suggestions.wary
    second = data(client, send(client, UNCLEAR))
    assert labels(client, second["rkey"]) == []
    assert suggestion_calls(model) == [] and reasons(ws) == ["context"]


def test_a_file_carrying_clarification_in_that_turn(web_app, hosted, monkeypatch):  # noqa: F811
    client, ws, model = live_client(web_app, monkeypatch, hosted)
    first = data(client, send_file(client, UNCLEAR))
    vm = state_of(client)["chat"]["responses"][first["rkey"]]
    assert vm["status"] == "clarify" and "suggestion_select" not in vm
    assert suggestion_calls(model) == [] and reasons(ws) == ["context"]
    assert ws.session.suggestions.wary
    # Nothing to choose: the suggestion route has no list for this reply.
    body = {"choice": "c0", "list_version": "0" * 16, "client_msg_id": "suggest-0001"}
    refused = client.post(f"/api/chat/responses/{first['rkey']}/suggestion", body)
    assert refused.code >= 400
    assert len(ws.transcript) == 2


def test_a_failed_turn_with_held_words_raises_the_flag(web_app, hosted, monkeypatch):  # noqa: F811
    client, ws, model = live_client(web_app, monkeypatch, hosted, plan=["error"])
    failed = send(client, "妈妈急需500块，给妈妈转500元")
    assert failed.code == 422 and data(client, failed)["error"]["code"] == "operation_failed"
    assert model.attempts == 1
    assert ws.session.suggestions.wary
    second = data(client, send(client, UNCLEAR))
    assert labels(client, second["rkey"]) == []
    assert suggestion_calls(model) == [] and reasons(ws) == ["context"]


def test_without_a_file_or_held_words_it_is_offered(web_app, hosted, monkeypatch):  # noqa: F811
    client, ws, model = live_client(web_app, monkeypatch, hosted)
    data(client, send(client, SENTENCE))
    second = data(client, send(client, UNCLEAR))
    assert labels(client, second["rkey"]) == [QUESTION]
    assert len(suggestion_calls(model)) == 1 and not ws.session.suggestions.wary


def test_an_ordinary_failed_turn_keeps_the_suggestions(web_app, hosted, monkeypatch):  # noqa: F811
    client, ws, model = live_client(web_app, monkeypatch, hosted, plan=["error"])
    assert send(client, SENTENCE).code == 422
    assert not ws.session.suggestions.wary
    second = data(client, send(client, UNCLEAR))
    assert labels(client, second["rkey"]) == [QUESTION]
    assert len(suggestion_calls(model)) == 1
